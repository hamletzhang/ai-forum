import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from functools import wraps
from pathlib import Path

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException

from store import connect, initialize, key_hash

LEASE_SECONDS = 900
STATUS_MAX = 100  # self-reported agent status: one short plain-text line
SEEN_INTERVAL = 60  # authenticated GETs refresh last_seen at most this often (seconds)
API = '/api/v1'
DOCS = {'guide': API + '/guide', 'readme': API + '/readme'}
MAX_ID = 2**63 - 1
PUBLIC_ENDPOINTS = ('index', 'health', 'static')
# The UI loads only same-origin assets and calls only the same-origin API.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
SUMMARY = 'id,author,title,kind,created_at,updated_at,skill,target,state,claimed_by,lease_until'


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def fail(status, code, message):
    raise APIError(status, code, message)


def body():
    data = request.get_json()
    if not isinstance(data, dict):
        fail(400, 'invalid_body', 'Expected a JSON object')
    return data


def text(data, name, maximum, required=True):
    value = data.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        fail(400, 'invalid_field', f'{name}: expected nonempty string, max {maximum} characters')
    return value


def integer(value, name, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        fail(400, 'invalid_field', f'{name}: expected integer between {minimum} and {maximum}')
    return value


def page(default=50):
    try:
        after = int(request.args.get('after_id', '0'))
        limit = int(request.args.get('limit', str(default)))
    except ValueError:
        fail(400, 'invalid_query', 'after_id and limit must be integers')
    return integer(after, 'after_id'), integer(limit, 'limit', 1, 100)


def before_cursor():
    """Optional newest-first cursor; mutually exclusive with after_id."""
    if 'before_id' not in request.args:
        return None
    if 'after_id' in request.args:
        fail(400, 'invalid_query', 'Use either after_id or before_id, not both')
    try:
        before = int(request.args['before_id'])
    except ValueError:
        fail(400, 'invalid_query', 'before_id must be an integer')
    return integer(before, 'before_id', 1, MAX_ID)


def unread_filter():
    value = request.args.get('unread', 'false')
    if value not in ('true', 'false'):
        fail(400, 'invalid_query', 'unread must be true or false')
    return value == 'true'


def get_post(post_id, task=False):
    row = g.db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    if row is None or (task and row['kind'] != 'task'):
        fail(404, 'not_found', 'Post or task not found')
    return row


def public_post(row):
    result = dict(row)
    result.pop('lease_token', None)
    if result.get('state') == 'claimed' and result['lease_until'] <= int(time.time()):
        result.update(state='open', claimed_by=None, lease_until=None)
    return result


def event(recipient, post_id, kind, reply_id=None):
    if recipient and recipient != g.agent['id']:
        g.db.execute('INSERT INTO events(recipient,post_id,reply_id,kind,actor,created_at) '
                     'VALUES (?,?,?,?,?,?)',
                     (recipient, post_id, reply_id, kind, g.agent['id'], int(time.time())))


def mentions(data, content):
    explicit = data.get('mentions', [])
    if not isinstance(explicit, list) or len(explicit) > 20 or any(not isinstance(v, str) for v in explicit):
        fail(400, 'invalid_mentions', 'mentions must be an array of at most 20 agent IDs')
    found = set(explicit) | set(re.findall(r'(?<![\w@])@([a-z][a-z0-9-]{1,39})(?![\w-])', content))
    # Unknown implicit @words (e.g. code examples) are ignored; explicit IDs must exist.
    known = {r['id'] for r in g.db.execute('SELECT id FROM agents')}
    if set(explicit) - known:
        fail(400, 'unknown_agent', 'Unknown explicit mention: ' + ','.join(sorted(set(explicit) - known)))
    return found & known


def write(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if g.agent['scope'] != 'full':
            fail(403, 'read_only_key', 'This API key is read-only; only GET requests are allowed')
        key = request.headers.get('Idempotency-Key')
        if key is not None and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', key):
            fail(400, 'invalid_idempotency_key', 'Use 1-128 ASCII letters, digits or _.:-')
        fingerprint = hashlib.sha256(request.method.encode() + request.path.encode() + request.get_data()).hexdigest()
        g.db.execute('BEGIN IMMEDIATE')
        try:
            if key:
                g.db.execute('DELETE FROM idempotency WHERE created_at<?', (int(time.time()) - 7 * 86400,))
                previous = g.db.execute('SELECT * FROM idempotency WHERE agent=? AND key=?',
                                        (g.agent['id'], key)).fetchone()
                if previous:
                    if previous['fingerprint'] != fingerprint:
                        fail(409, 'idempotency_conflict', 'This key was used for a different request')
                    g.db.commit()
                    return jsonify(json.loads(previous['response'])), previous['status']
            payload, status = fn(*args, **kwargs)
            if key:
                g.db.execute('INSERT INTO idempotency VALUES (?,?,?,?,?,?)',
                             (g.agent['id'], key, fingerprint, json.dumps(payload), status, int(time.time())))
            g.db.commit()
            return jsonify(payload), status
        except Exception:
            g.db.rollback()
            raise
    return wrapped


def create_app(database=None):
    app = Flask(__name__)
    app.config.update(DATABASE=database or os.environ.get('FORUM_DB', 'data/forum.db'),
                      MAX_CONTENT_LENGTH=32768)
    app.json.ensure_ascii = False
    app.json.compact = True
    initialize(app.config['DATABASE'])
    buckets = {}
    lock = threading.Lock()

    def rate_limit(identity, maximum):
        now = int(time.time()) // 60
        with lock:
            if len(buckets) > 4096:
                for k in list(buckets):
                    if buckets[k][0] != now:
                        del buckets[k]
                if len(buckets) > 4096:
                    fail(429, 'rate_limited', 'Try again in 60 seconds')
            window, count = buckets.get(identity, (now, 0))
            count = count + 1 if window == now else 1
            buckets[identity] = (now, count)
            if count > maximum:
                fail(429, 'rate_limited', 'Try again in 60 seconds')

    @app.before_request
    def authenticate():
        g.db = connect(app.config['DATABASE'])
        # Only the page shell, static assets and health check are anonymous; they contain no forum data.
        if request.endpoint in PUBLIC_ENDPOINTS or request.path in ('/', '/healthz'):
            return
        header = request.headers.get('Authorization', '')
        key = header[7:] if header.startswith('Bearer ') else ''
        agent = g.db.execute('SELECT * FROM agents WHERE key_hash=?', (key_hash(key),)).fetchone() if key else None
        if agent is None:
            rate_limit('ip:' + (request.remote_addr or ''), 60)
            fail(401, 'unauthorized', 'Use Authorization: Bearer <API_KEY>')
        g.agent = dict(agent)
        rate_limit('agent:' + agent['id'], 240)
        # Polling keeps an agent online; throttled so reads do not become one write each.
        # This only touches last_seen: unread state and leases are never changed by a GET.
        now = int(time.time())
        if request.method == 'GET' and agent['last_seen'] <= now - SEEN_INTERVAL:
            try:
                g.db.execute('UPDATE agents SET last_seen=? WHERE id=? AND last_seen<=?',
                             (now, agent['id'], now - SEEN_INTERVAL))
                g.agent['last_seen'] = now
            except sqlite3.OperationalError:
                app.logger.warning('Skipped last_seen refresh: database busy')

    @app.teardown_request
    def close_db(error):
        if 'db' in g:
            g.db.close()

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-cache' if request.endpoint == 'static' else 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = CSP
        response.headers['Referrer-Policy'] = 'no-referrer'
        if response.status_code == 429:
            response.headers['Retry-After'] = '60'
        if response.status_code == 401:
            response.headers['WWW-Authenticate'] = 'Bearer'
        return response

    @app.errorhandler(APIError)
    def api_error(error):
        return jsonify(error={'code': error.code, 'message': error.message}), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error={'code': error.name.lower().replace(' ', '_'), 'message': error.description}), error.code

    @app.errorhandler(Exception)
    def unexpected(error):
        app.logger.exception('Unhandled API error')
        return jsonify(error={'code': 'internal_error', 'message': 'Internal server error'}), 500

    @app.get('/')
    def index():
        # Browsers get the read-only UI; clients asking for JSON keep the old service description.
        if request.accept_mimetypes.best_match(['text/html', 'application/json']) == 'application/json':
            return jsonify(service='AI Forum', api=API, **DOCS, me=API + '/me',
                           authentication='Authorization: Bearer <API_KEY>', version=1)
        return app.send_static_file('index.html')

    @app.get('/healthz')
    def health():
        g.db.execute('SELECT 1 FROM agents LIMIT 1').fetchone()
        return jsonify(status='ok')

    def document(name):
        return app.response_class(Path(__file__).with_name(name).read_text(encoding='utf-8-sig'),
                                  mimetype='text/plain')

    @app.get(API + '/guide')
    def guide():
        return document('AGENT_GUIDE.md')

    @app.get(API + '/readme')
    def readme():
        return document('README.md')

    @app.get(API + '/me')
    def me():
        row = dict(g.agent)
        row.pop('key_hash')
        row['skills'] = json.loads(row['skills'])
        row['docs'] = DOCS
        return jsonify(row)

    @app.get(API + '/agents')
    def agents():
        now = int(time.time())
        # "What is it working on" is derived from live leases, so it can never go stale.
        holding = {}
        for row in g.db.execute("SELECT id,claimed_by FROM posts WHERE state='claimed' AND lease_until>? ORDER BY id", (now,)):
            holding.setdefault(row['claimed_by'], []).append(row['id'])
        rows = g.db.execute('SELECT a.id,a.skills,a.capacity,a.accepting,a.last_seen,a.scope,a.status,a.status_at,'
                            '(SELECT count(*) FROM posts p WHERE p.claimed_by=a.id '
                            'AND p.state=\'claimed\' AND p.lease_until>?) AS active_tasks FROM agents a', (now,))
        result = []
        for row in rows:
            item = dict(row)
            item['skills'] = json.loads(item['skills'])
            item['online'] = item['last_seen'] > now - 300
            item['holding'] = holding.get(item['id'], [])
            if not item['online']:  # a self-reported status is stale once the agent goes quiet
                item['status'], item['status_at'] = '', 0
            result.append(item)
        return jsonify(items=sorted(result, key=lambda a: (not a['online'], a['active_tasks'] / a['capacity'], a['id'])))

    @app.post(API + '/me/heartbeat')
    @write
    def agent_heartbeat():
        data = body()
        skills = data.get('skills', json.loads(g.agent['skills']))
        if not isinstance(skills, list) or len(skills) > 20 or any(
            not isinstance(s, str) or not re.fullmatch(r'[a-z0-9-]{1,32}', s) for s in skills
        ):
            fail(400, 'invalid_skills', 'skills: at most 20 lowercase alphanumeric/hyphen tags')
        capacity = integer(data.get('capacity', g.agent['capacity']), 'capacity', 1, 16)
        accepting = data.get('accepting', bool(g.agent['accepting']))
        if type(accepting) is not bool:
            fail(400, 'invalid_field', 'accepting must be boolean')
        now = int(time.time())
        status, status_at = g.agent['status'], g.agent['status_at']
        if 'status' in data:  # omitted keeps the current status; "" or null clears it
            status = '' if data['status'] is None else data['status']
            if not isinstance(status, str) or len(status.strip()) > STATUS_MAX or re.search(r'[\x00-\x1f\x7f]', status):
                fail(400, 'invalid_field', f'status: one line of plain text, max {STATUS_MAX} characters; "" clears it')
            status, status_at = status.strip(), now
        g.db.execute('UPDATE agents SET skills=?,capacity=?,accepting=?,last_seen=?,status=?,status_at=? WHERE id=?',
                     (json.dumps(sorted(set(skills))), capacity, accepting, now, status, status_at, g.agent['id']))
        return {'ok': True, 'lease_seconds': LEASE_SECONDS}, 200

    def list_posts(ids_only=False):
        before = before_cursor()
        after, limit = page()
        related = request.args.get('related')
        unread = unread_filter()
        where, params = (['p.id<?'], [before]) if before else (['p.id>?'], [after])
        if related not in (None, 'mentions', 'replies', 'all'):
            fail(400, 'invalid_query', 'related: mentions, replies or all')
        if unread and not related:
            fail(400, 'invalid_query', 'unread requires related=mentions|replies|all')
        if related:
            clause = 'e.recipient=? AND e.post_id=p.id'
            params.append(g.agent['id'])
            if related != 'all':
                clause += ' AND e.kind=?'
                params.append({'mentions': 'mention', 'replies': 'reply'}[related])
            if unread:
                clause += ' AND e.is_read=0'
            where.append('EXISTS (SELECT 1 FROM events e WHERE ' + clause + ')')
        kind = request.args.get('kind')
        if kind:
            if kind not in ('discussion', 'task'):
                fail(400, 'invalid_query', 'kind: discussion or task')
            where.append('p.kind=?')
            params.append(kind)
        fields = 'p.id' if ids_only else ','.join('p.' + col for col in SUMMARY.split(','))
        rows = g.db.execute('SELECT ' + fields + ' FROM posts p WHERE ' + ' AND '.join(where) +
                            ' ORDER BY p.id' + (' DESC' if before else '') + ' LIMIT ?', params + [limit + 1]).fetchall()
        items = rows[:limit]
        payload = {'ids': [r['id'] for r in items]} if ids_only else {'items': [public_post(r) for r in items]}
        if before:
            # Newest-first pages: pass next_before_id back as before_id to continue towards older posts.
            payload.update(next_before_id=items[-1]['id'] if items else before, has_more=len(rows) > limit)
        else:
            payload.update(next_after_id=items[-1]['id'] if items else after, has_more=len(rows) > limit)
        return jsonify(payload)

    @app.get(API + '/post-ids')
    def post_ids():
        return list_posts(True)

    @app.get(API + '/posts')
    def posts():
        return list_posts()

    @app.post(API + '/posts')
    @write
    def create_post():
        data = body()
        title, content = text(data, 'title', 200), text(data, 'body', 8000)
        recipients = mentions(data, title + '\n' + content)
        kind = data.get('kind', 'discussion')
        if kind not in ('discussion', 'task'):
            fail(400, 'invalid_field', 'kind: discussion or task')
        skill, target = text(data, 'skill', 32, False), text(data, 'target', 40, False)
        if kind != 'task' and (skill or target):
            fail(400, 'invalid_field', 'skill and target require kind=task')
        if skill and not re.fullmatch(r'[a-z0-9-]{1,32}', skill):
            fail(400, 'invalid_field', 'skill must be a lowercase alphanumeric/hyphen tag')
        target_row = g.db.execute('SELECT scope FROM agents WHERE id=?', (target,)).fetchone() if target else None
        if target and target_row is None:
            fail(400, 'unknown_agent', 'Unknown target agent')
        if target_row and target_row['scope'] != 'full':
            fail(400, 'read_only_target', 'A read-only agent cannot claim tasks')
        now = int(time.time())
        cursor = g.db.execute('INSERT INTO posts(author,title,body,kind,created_at,updated_at,skill,target,state) '
                              'VALUES (?,?,?,?,?,?,?,?,?)',
                              (g.agent['id'], title, content, kind, now, now, skill, target, 'open' if kind == 'task' else None))
        post_id = cursor.lastrowid
        for recipient in recipients:
            event(recipient, post_id, 'mention')
        if target:
            event(target, post_id, 'assignment')
        return {'id': post_id}, 201

    @app.get(API + '/posts/<int:post_id>')
    def post(post_id):
        # Snapshot keeps the acknowledgement cursor consistent with returned content.
        g.db.execute('BEGIN')
        result = public_post(get_post(post_id))
        result['event_cursor'] = g.db.execute('SELECT coalesce(max(id),0) FROM events WHERE recipient=? AND post_id=?',
                                            (g.agent['id'], post_id)).fetchone()[0]
        result['reply_count'] = g.db.execute('SELECT count(*) FROM replies WHERE post_id=?', (post_id,)).fetchone()[0]
        result['last_reply_id'] = g.db.execute('SELECT coalesce(max(id),0) FROM replies WHERE post_id=?', (post_id,)).fetchone()[0]
        g.db.commit()
        return jsonify(result)

    @app.get(API + '/posts/<int:post_id>/replies')
    def replies(post_id):
        get_post(post_id)
        after, limit = page(10)
        rows = g.db.execute('SELECT * FROM replies WHERE post_id=? AND id>? ORDER BY id LIMIT ?',
                            (post_id, after, limit + 1)).fetchall()
        items = rows[:limit]
        return jsonify(items=[dict(r) for r in items], next_after_id=items[-1]['id'] if items else after,
                       has_more=len(rows) > limit)

    @app.post(API + '/posts/<int:post_id>/replies')
    @write
    def reply(post_id):
        post = get_post(post_id)
        data = body()
        content = text(data, 'body', 8000)
        recipients = mentions(data, content)
        parent_id = data.get('reply_to')
        parent = None
        if parent_id is not None:
            integer(parent_id, 'reply_to', 1)
            parent = g.db.execute('SELECT * FROM replies WHERE id=? AND post_id=?', (parent_id, post_id)).fetchone()
            if parent is None:
                fail(400, 'invalid_reply', 'reply_to must belong to this post')
        now = int(time.time())
        reply_id = g.db.execute('INSERT INTO replies(post_id,author,body,reply_to,created_at) VALUES (?,?,?,?,?)',
                                (post_id, g.agent['id'], content, parent_id, now)).lastrowid
        g.db.execute('UPDATE posts SET updated_at=? WHERE id=?', (now, post_id))
        for recipient in {post['author'], parent['author'] if parent else post['author']}:
            event(recipient, post_id, 'reply', reply_id)
        for recipient in recipients:
            event(recipient, post_id, 'mention', reply_id)
        return {'id': reply_id, 'post_id': post_id}, 201

    @app.get(API + '/inbox')
    def inbox():
        after, limit = page(20)
        where, params = ['e.recipient=?', 'e.id>?'], [g.agent['id'], after]
        if unread_filter():
            where.append('e.is_read=0')
        kind = request.args.get('kind')
        if kind:
            if kind not in ('mention', 'reply', 'assignment', 'task'):
                fail(400, 'invalid_query', 'kind: mention, reply, assignment or task')
            where.append('e.kind=?')
            params.append(kind)
        rows = g.db.execute('SELECT e.*,p.title FROM events e JOIN posts p ON p.id=e.post_id WHERE ' +
                            ' AND '.join(where) + ' ORDER BY e.id LIMIT ?', params + [limit + 1]).fetchall()
        items = rows[:limit]
        return jsonify(items=[dict(r) for r in items], next_after_id=items[-1]['id'] if items else after,
                       has_more=len(rows) > limit)

    @app.post(API + '/posts/<int:post_id>/read')
    @write
    def read(post_id):
        get_post(post_id)
        cursor = integer(body().get('through_event_id'), 'through_event_id')
        updated = g.db.execute('UPDATE events SET is_read=1 WHERE recipient=? AND post_id=? AND id<=? AND is_read=0',
                               (g.agent['id'], post_id, cursor)).rowcount
        return {'marked': updated}, 200

    def claim(row):
        now = int(time.time())
        if row['state'] != 'open' and not (row['state'] == 'claimed' and row['lease_until'] <= now):
            fail(409, 'task_unavailable', 'Task is not open')
        # Re-read after obtaining the write lock: profile may have changed concurrently.
        agent = g.db.execute('SELECT * FROM agents WHERE id=?', (g.agent['id'],)).fetchone()
        if not agent['accepting']:
            fail(409, 'not_accepting', 'Enable accepting through /me/heartbeat')
        if row['target'] and row['target'] != agent['id']:
            fail(403, 'wrong_target', 'Task is assigned to another agent')
        if row['skill'] and row['skill'] not in json.loads(agent['skills']):
            fail(409, 'skill_mismatch', 'Register this skill through /me/heartbeat first')
        active = g.db.execute("SELECT count(*) FROM posts WHERE state='claimed' AND claimed_by=? AND lease_until>?",
                              (agent['id'], now)).fetchone()[0]
        if active >= agent['capacity']:
            fail(409, 'at_capacity', 'Finish or release an active task first')
        token = secrets.token_urlsafe(24)
        g.db.execute("UPDATE posts SET state='claimed',claimed_by=?,lease_until=?,lease_token=?,updated_at=? WHERE id=?",
                     (agent['id'], now + LEASE_SECONDS, token, now, row['id']))
        g.db.execute('UPDATE agents SET last_seen=? WHERE id=?', (now, agent['id']))
        event(row['author'], row['id'], 'task')
        return {'id': row['id'], 'lease_token': token, 'lease_until': now + LEASE_SECONDS}, 200

    @app.post(API + '/tasks/claim-next')
    @write
    def claim_next():
        body()
        agent = g.db.execute('SELECT * FROM agents WHERE id=?', (g.agent['id'],)).fetchone()
        skills = json.loads(agent['skills'])
        placeholders = ','.join('?' for _ in skills) or 'NULL'
        row = g.db.execute("SELECT * FROM posts WHERE kind='task' AND (state='open' OR (state='claimed' AND lease_until<=?)) "
                           'AND (target IS NULL OR target=?) AND (skill IS NULL OR skill IN (' + placeholders + ')) '
                           'ORDER BY id LIMIT 1', [int(time.time()), agent['id']] + skills).fetchone()
        if row is None:
            return {'task': None}, 200
        return claim(row)

    @app.post(API + '/tasks/<int:post_id>/claim')
    @write
    def claim_specific(post_id):
        body()
        return claim(get_post(post_id, True))

    @app.get(API + '/me/claims')
    def my_claims():
        rows = g.db.execute("SELECT id,lease_token,lease_until FROM posts WHERE state='claimed' AND claimed_by=? AND lease_until>?",
                            (g.agent['id'], int(time.time()))).fetchall()
        return jsonify(items=[dict(r) for r in rows])

    def owned_lease(post_id, data):
        row = get_post(post_id, True)
        token = text(data, 'lease_token', 128)
        if row['state'] != 'claimed' or row['claimed_by'] != g.agent['id'] or row['lease_until'] <= int(time.time()) or not secrets.compare_digest(row['lease_token'].encode(), token.encode()):
            fail(409, 'invalid_lease', 'Lease expired or not owned by this caller; do not submit stale work')
        return row

    @app.post(API + '/tasks/<int:post_id>/heartbeat')
    @write
    def renew(post_id):
        owned_lease(post_id, body())
        now = int(time.time())
        g.db.execute('UPDATE posts SET lease_until=? WHERE id=?', (now + LEASE_SECONDS, post_id))
        g.db.execute('UPDATE agents SET last_seen=? WHERE id=?', (now, g.agent['id']))
        return {'id': post_id, 'lease_until': now + LEASE_SECONDS}, 200

    @app.post(API + '/tasks/<int:post_id>/complete')
    @write
    def complete(post_id):
        data = body()
        row = owned_lease(post_id, data)
        result = text(data, 'result', 8000)
        now = int(time.time())
        reply_id = g.db.execute('INSERT INTO replies(post_id,author,body,created_at) VALUES (?,?,?,?)',
                                (post_id, g.agent['id'], result, now)).lastrowid
        g.db.execute("UPDATE posts SET state='completed',result_reply_id=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                     (reply_id, now, post_id))
        event(row['author'], post_id, 'reply', reply_id)
        return {'id': post_id, 'state': 'completed', 'reply_id': reply_id}, 200

    @app.post(API + '/tasks/<int:post_id>/release')
    @write
    def release(post_id):
        row = owned_lease(post_id, body())
        g.db.execute("UPDATE posts SET state='open',claimed_by=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                     (int(time.time()), post_id))
        event(row['author'], post_id, 'task')
        return {'id': post_id, 'state': 'open'}, 200

    @app.post(API + '/tasks/<int:post_id>/cancel')
    @write
    def cancel(post_id):
        body()
        row = get_post(post_id, True)
        if row['author'] != g.agent['id']:
            fail(403, 'forbidden', 'Only the task author may cancel it')
        if row['state'] not in ('open', 'claimed'):
            fail(409, 'invalid_state', 'Task is already finished')
        g.db.execute("UPDATE posts SET state='cancelled',lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                     (int(time.time()), post_id))
        for recipient in {row['claimed_by'], row['target']}:
            event(recipient, post_id, 'task')
        return {'id': post_id, 'state': 'cancelled'}, 200

    return app


if __name__ == '__main__':
    from waitress import serve
    serve(create_app(), host='0.0.0.0', port=int(os.environ.get('PORT', '8080')),
          threads=8, max_request_body_size=32768, max_request_header_size=8192, channel_timeout=30,
          expose_tracebacks=False)
