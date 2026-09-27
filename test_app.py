import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from app import API, create_app
from store import connect, provision


class ForumTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, 'forum.db')
        self.app = create_app(self.path)
        self.app.config['TESTING'] = True
        self.keys = {name: provision(self.path, name) for name in ('local-agent', 'friend-agent', 'third-agent')}
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def req(self, method, path, data=None, agent='local-agent', idem=None, client=None):
        headers = {'Authorization': 'Bearer ' + self.keys[agent]}
        if idem:
            headers['Idempotency-Key'] = idem
        return (client or self.client).open(API + path, method=method, json=data, headers=headers)

    def post(self, **kwargs):
        agent = kwargs.pop('agent', 'local-agent')
        response = self.req('POST', '/posts', {'title': '小任务', 'body': '任务说明', **kwargs}, agent=agent)
        self.assertEqual(response.status_code, 201, response.json)
        return response.json['id']

    def task(self, **kwargs):
        return self.post(kind='task', **kwargs)

    def expire(self, post_id):
        db = connect(self.path)
        db.execute('UPDATE posts SET lease_until=0 WHERE id=?', (post_id,))
        db.close()

    def test_auth_private_endpoints_and_no_secret_leak(self):
        for path in ('/posts', '/post-ids', '/agents', '/guide', '/me'):
            self.assertEqual(self.client.get(API + path).status_code, 401)
        self.assertEqual(self.client.get('/healthz').status_code, 200)
        self.assertNotIn('key_hash', self.req('GET', '/me').json)
        self.assertNotIn(self.keys['local-agent'], self.req('GET', '/agents').text)
        self.assertEqual(self.client.get(API + '/posts?key=' + self.keys['local-agent']).status_code, 401)

    def test_id_pagination_and_compact_summaries(self):
        ids = [self.post() for _ in range(3)]
        first = self.req('GET', '/post-ids?limit=2').json
        self.assertEqual(first['ids'], ids[:2])
        self.assertTrue(first['has_more'])
        second = self.req('GET', '/post-ids?after_id=' + str(first['next_after_id'])).json
        self.assertEqual(second['ids'], ids[2:])
        self.assertFalse(second['has_more'])
        self.assertNotIn('body', self.req('GET', '/posts').json['items'][0])
        self.assertEqual(self.req('GET', f'/posts/{ids[0]}').json['body'], '任务说明')

    def test_mention_unread_and_race_safe_ack(self):
        pid = self.post(body='请 @friend-agent 检查', mentions=['friend-agent'])
        path = '/posts?related=mentions&unread=true'
        self.assertEqual(len(self.req('GET', path, agent='friend-agent').json['items']), 1)
        snapshot = self.req('GET', f'/posts/{pid}', agent='friend-agent').json
        self.assertEqual(len(self.req('GET', path, agent='friend-agent').json['items']), 1)
        self.req('POST', f'/posts/{pid}/replies', {'body': '追加 @friend-agent'})
        self.req('POST', f'/posts/{pid}/read', {'through_event_id': snapshot['event_cursor']}, agent='friend-agent')
        unread = self.req('GET', '/inbox?unread=true', agent='friend-agent').json['items']
        self.assertEqual(len(unread), 1)
        self.assertGreater(unread[0]['id'], snapshot['event_cursor'])
        self.req('POST', f'/posts/{pid}/read', {'through_event_id': unread[0]['id']}, agent='friend-agent')
        self.assertEqual(self.req('GET', path, agent='friend-agent').json['items'], [])

    def test_reply_notifications_to_author_and_reply_parent(self):
        pid = self.post()
        rid = self.req('POST', f'/posts/{pid}/replies', {'body': '第一次'}, agent='friend-agent').json['id']
        self.req('POST', f'/posts/{pid}/replies', {'body': '直接回复', 'reply_to': rid}, agent='third-agent')
        self.assertEqual(len(self.req('GET', '/posts?related=replies').json['items']), 1)
        self.assertEqual(len(self.req('GET', '/inbox?kind=reply', agent='friend-agent').json['items']), 1)
        replies = self.req('GET', f'/posts/{pid}/replies?after_id={rid}').json
        self.assertEqual(len(replies['items']), 1)
        self.assertEqual(replies['items'][0]['body'], '直接回复')
        other = self.post()
        self.assertEqual(self.req('POST', f'/posts/{other}/replies', {'body': 'bad', 'reply_to': rid}).status_code, 400)

    def test_own_mentions_do_not_notify_and_unknown_explicit_fails(self):
        self.post(body='@local-agent @unknown-agent')
        self.assertEqual(self.req('GET', '/inbox').json['items'], [])
        res = self.req('POST', '/posts', {'title': 'x', 'body': 'x', 'mentions': ['missing-agent']})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(len(self.req('GET', '/post-ids').json['ids']), 1)

    def test_idempotency_replay_and_conflict(self):
        data = {'title': 'task', 'body': 'once', 'mentions': ['friend-agent']}
        one = self.req('POST', '/posts', data, idem='create-once')
        two = self.req('POST', '/posts', data, idem='create-once')
        self.assertEqual(one.json, two.json)
        self.assertEqual(two.status_code, 201)
        self.assertEqual(len(self.req('GET', '/inbox', agent='friend-agent').json['items']), 1)
        self.assertEqual(self.req('POST', '/posts', {**data, 'body': 'different'}, idem='create-once').status_code, 409)

    def test_skill_target_capacity_and_claim_completion(self):
        self.req('POST', '/me/heartbeat', {'skills': ['frontend']}, agent='friend-agent')
        pid = self.task(skill='frontend', target='friend-agent')
        self.assertEqual(self.req('POST', f'/tasks/{pid}/claim', {}).status_code, 403)
        claim = self.req('POST', '/tasks/claim-next', {}, agent='friend-agent')
        self.assertEqual(claim.status_code, 200, claim.json)
        self.assertEqual(claim.json['id'], pid)
        token = claim.json['lease_token']
        self.assertNotIn('lease_token', self.req('GET', f'/posts/{pid}').json)
        self.assertEqual(self.req('GET', '/me/claims', agent='friend-agent').json['items'][0]['lease_token'], token)
        next_pid = self.task()
        self.assertEqual(self.req('POST', f'/tasks/{next_pid}/claim', {}, agent='friend-agent').json['error']['code'], 'at_capacity')
        renewed = self.req('POST', f'/tasks/{pid}/heartbeat', {'lease_token': token}, agent='friend-agent')
        self.assertEqual(renewed.status_code, 200)
        done = self.req('POST', f'/tasks/{pid}/complete', {'lease_token': token, 'result': 'PR #1，测试通过'}, agent='friend-agent')
        self.assertEqual(done.status_code, 200)
        self.assertEqual(self.req('GET', f'/posts/{pid}').json['state'], 'completed')
        self.assertEqual(self.req('GET', f'/posts/{pid}/replies').json['items'][0]['body'], 'PR #1，测试通过')
        self.assertEqual(self.req('POST', f'/tasks/{next_pid}/claim', {}, agent='friend-agent').status_code, 200)

    def test_skill_mismatch_and_pause(self):
        pid = self.task(skill='backend')
        self.assertEqual(self.req('POST', f'/tasks/{pid}/claim', {}).json['error']['code'], 'skill_mismatch')
        self.assertEqual(self.req('POST', '/tasks/claim-next', {}).json, {'task': None})
        self.req('POST', '/me/heartbeat', {'skills': ['backend'], 'accepting': False})
        self.assertEqual(self.req('POST', f'/tasks/{pid}/claim', {}).json['error']['code'], 'not_accepting')

    def test_expired_lease_requeue_and_fencing(self):
        pid = self.task()
        old = self.req('POST', f'/tasks/{pid}/claim', {}).json['lease_token']
        self.expire(pid)
        self.assertEqual(self.req('GET', f'/posts/{pid}').json['state'], 'open')
        self.assertEqual(self.req('GET', '/me/claims').json['items'], [])
        self.assertEqual(self.req('POST', f'/tasks/{pid}/complete', {'lease_token': old, 'result': 'late'}).status_code, 409)
        new = self.req('POST', f'/tasks/{pid}/claim', {}).json['lease_token']
        self.assertNotEqual(old, new)
        self.assertEqual(self.req('POST', f'/tasks/{pid}/heartbeat', {'lease_token': old}).status_code, 409)
        self.assertEqual(self.req('POST', f'/tasks/{pid}/complete', {'lease_token': old, 'result': 'stale'}).status_code, 409)
        self.assertEqual(self.req('POST', f'/tasks/{pid}/release', {'lease_token': new}).status_code, 200)

    def test_cancel_author_only_and_invalidates_lease(self):
        pid = self.task(target='friend-agent')
        token = self.req('POST', f'/tasks/{pid}/claim', {}, agent='friend-agent').json['lease_token']
        self.assertEqual(self.req('POST', f'/tasks/{pid}/cancel', {}, agent='friend-agent').status_code, 403)
        self.assertEqual(self.req('POST', f'/tasks/{pid}/cancel', {}).status_code, 200)
        self.assertEqual(self.req('POST', f'/tasks/{pid}/complete', {'lease_token': token, 'result': 'late'}, agent='friend-agent').status_code, 409)
        self.assertEqual(self.req('POST', f'/tasks/{pid}/claim', {}, agent='friend-agent').status_code, 409)

    def test_concurrent_claim_has_single_winner(self):
        pid = self.task()
        barrier = Barrier(2)
        def attempt(agent):
            client = self.app.test_client()
            barrier.wait()
            return self.req('POST', f'/tasks/{pid}/claim', {}, agent=agent, client=client).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ['local-agent', 'friend-agent']))
        self.assertEqual(sorted(results), [200, 409])

    def test_concurrent_capacity_limit(self):
        ids = [self.task(), self.task()]
        barrier = Barrier(2)
        def attempt(pid):
            client = self.app.test_client()
            barrier.wait()
            return self.req('POST', f'/tasks/{pid}/claim', {}, client=client).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ids))
        self.assertEqual(sorted(results), [200, 409])

    def test_validation_and_size_limits(self):
        for data in ({}, [], {'title': 'x', 'body': ''}, {'title': 'x', 'body': 'a' * 8001},
                     {'title': 'x', 'body': 'x', 'kind': 'bad'}, {'title': 'x', 'body': 'x', 'target': 'friend-agent'}):
            self.assertEqual(self.req('POST', '/posts', data).status_code, 400)
        self.assertEqual(self.req('POST', '/posts', {'title': 'x', 'body': 'a' * 40000}).status_code, 413)
        for query in ('limit=0', 'limit=101', 'after_id=-1', 'limit=no', 'related=bad', 'unread=true', 'unread=no'):
            self.assertEqual(self.req('GET', '/posts?' + query).status_code, 400, query)
        self.assertEqual(self.req('POST', '/me/heartbeat', {'capacity': True}).status_code, 400)
        self.assertEqual(self.req('POST', '/me/heartbeat', {'accepting': 'yes'}).status_code, 400)
        self.assertEqual(self.req('GET', '/posts/999').status_code, 404)

    def test_persistence_and_key_rotation(self):
        pid = self.post()
        app2 = create_app(self.path)
        self.assertEqual(self.req('GET', f'/posts/{pid}', client=app2.test_client()).status_code, 200)
        old = self.keys['local-agent']
        self.keys['local-agent'] = provision(self.path, 'local-agent', rotate=True)
        self.assertEqual(self.client.get(API + '/me', headers={'Authorization': 'Bearer ' + old}).status_code, 401)
        self.assertEqual(self.req('GET', '/me').status_code, 200)
        with open(self.path, 'rb') as file:
            self.assertNotIn(self.keys['local-agent'].encode(), file.read())

    def test_unicode_lease_token_is_rejected_without_server_error(self):
        pid = self.task()
        self.req('POST', f'/tasks/{pid}/claim', {})
        response = self.req('POST', f'/tasks/{pid}/heartbeat', {'lease_token': '无效凭证'})
        self.assertEqual(response.status_code, 409)

    def test_expired_idempotency_records_are_pruned(self):
        data = {'title': 'short', 'body': 'summary'}
        self.req('POST', '/posts', data, idem='old-operation')
        db = connect(self.path)
        db.execute('UPDATE idempotency SET created_at=0')
        db.close()
        self.req('POST', '/posts', data, idem='new-operation')
        db = connect(self.path)
        try:
            self.assertEqual(db.execute('SELECT count(*) FROM idempotency').fetchone()[0], 1)
        finally:
            db.close()

    def test_backup_is_consistent(self):
        import subprocess
        import sys
        pid = self.post(body='备份必须保留正文')
        output = os.path.join(self.temp.name, 'backup.db')
        subprocess.run([sys.executable, 'manage.py', '--db', self.path, 'backup', output],
                       check=True, capture_output=True)
        db = connect(output)
        try:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT body FROM posts WHERE id=?', (pid,)).fetchone()[0], '备份必须保留正文')
        finally:
            db.close()

    def test_malformed_json_and_non_json_requests(self):
        headers = {'Authorization': 'Bearer ' + self.keys['local-agent']}
        response = self.client.post(API + '/posts', headers=headers, data='{', content_type='application/json')
        self.assertEqual(response.status_code, 400)
        response = self.client.post(API + '/posts', headers=headers, data='upload bytes')
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self.req('GET', '/post-ids').json['ids'], [])

    def test_rate_limit(self):
        for _ in range(240):
            self.assertEqual(self.req('GET', '/me').status_code, 200)
        result = self.req('GET', '/me')
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.headers['Retry-After'], '60')


    # ---------- 服务端只读 Key ----------
    def reader(self):
        self.keys['human-reader'] = provision(self.path, 'human-reader', scope='read')
        return 'human-reader'

    def test_read_only_key_rejected_on_every_write_route(self):
        reader = self.reader()
        pid = self.task()
        writes = [('/posts', {'title': 't', 'body': 'b'}), (f'/posts/{pid}/replies', {'body': 'x'}),
                  ('/me/heartbeat', {}), (f'/posts/{pid}/read', {'through_event_id': 1}),
                  ('/tasks/claim-next', {}), (f'/tasks/{pid}/claim', {}),
                  (f'/tasks/{pid}/heartbeat', {'lease_token': 'x'}),
                  (f'/tasks/{pid}/complete', {'lease_token': 'x', 'result': 'r'}),
                  (f'/tasks/{pid}/release', {'lease_token': 'x'}), (f'/tasks/{pid}/cancel', {})]
        routes = {r.rule for r in self.app.url_map.iter_rules() if 'POST' in r.methods}
        self.assertEqual(len(routes), len(writes), sorted(routes))  # a new write route must be listed here
        for path, data in writes:
            response = self.req('POST', path, data, agent=reader, idem='same-key')
            self.assertEqual(response.status_code, 403, path)
            self.assertEqual(response.json['error']['code'], 'read_only_key', path)
        db = connect(self.path)
        try:
            self.assertEqual(db.execute('SELECT count(*) FROM posts').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM replies').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM idempotency').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT state FROM posts WHERE id=?', (pid,)).fetchone()[0], 'open')
        finally:
            db.close()
        for path in ('/me', '/agents', '/posts', '/post-ids', f'/posts/{pid}', f'/posts/{pid}/replies',
                     '/inbox', '/me/claims', '/guide', '/readme', '/posts?before_id=100'):
            self.assertEqual(self.req('GET', path, agent=reader).status_code, 200, path)

    def test_scope_reported_and_full_keys_unchanged(self):
        reader = self.reader()
        self.assertEqual(self.req('GET', '/me', agent=reader).json['scope'], 'read')
        me = self.req('GET', '/me').json
        self.assertEqual(me['scope'], 'full')
        self.assertEqual(me['docs'], {'guide': API + '/guide', 'readme': API + '/readme'})
        scopes = {a['id']: a['scope'] for a in self.req('GET', '/agents').json['items']}
        self.assertEqual((scopes['human-reader'], scopes['friend-agent']), ('read', 'full'))
        task = {'title': 't', 'body': 'b', 'kind': 'task', 'target': reader}
        self.assertEqual(self.req('POST', '/posts', task).json['error']['code'], 'read_only_target')
        self.assertEqual(self.req('POST', '/me/heartbeat', {'skills': ['backend']}).status_code, 200)
        pid = self.task(skill='backend')
        self.assertEqual(self.req('POST', f'/tasks/{pid}/claim', {}).status_code, 200)
        rotated = provision(self.path, reader, rotate=True)
        self.assertEqual(self.client.get(API + '/me', headers={'Authorization': 'Bearer ' + rotated}).json['scope'], 'read')

    def test_legacy_database_migrates_to_full_scope(self):
        import sqlite3
        from store import key_hash
        path = os.path.join(self.temp.name, 'legacy.db')
        legacy = sqlite3.connect(path)
        legacy.executescript("""
            CREATE TABLE agents (id TEXT PRIMARY KEY, key_hash TEXT NOT NULL UNIQUE,
                skills TEXT NOT NULL DEFAULT '[]', capacity INTEGER NOT NULL DEFAULT 1,
                accepting INTEGER NOT NULL DEFAULT 1, last_seen INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, author TEXT NOT NULL REFERENCES agents(id),
                title TEXT NOT NULL, body TEXT NOT NULL, kind TEXT NOT NULL, created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL, skill TEXT, target TEXT REFERENCES agents(id), state TEXT,
                claimed_by TEXT REFERENCES agents(id), lease_until INTEGER, lease_token TEXT, result_reply_id INTEGER);
        """)
        key = 'legacy_' + os.urandom(16).hex()
        legacy.execute('INSERT INTO agents(id,key_hash) VALUES (?,?)', ('old-agent', key_hash(key)))
        legacy.execute("INSERT INTO posts(author,title,body,kind,created_at,updated_at) "
                       "VALUES ('old-agent','旧帖','旧正文','discussion',1,1)")
        legacy.commit()
        legacy.close()
        client = create_app(path).test_client()
        create_app(path)  # restart is idempotent
        headers = {'Authorization': 'Bearer ' + key}
        self.assertEqual(client.get(API + '/me', headers=headers).json['scope'], 'full')
        self.assertEqual(client.get(API + '/posts/1', headers=headers).json['body'], '旧正文')
        self.assertEqual(client.post(API + '/posts/1/replies', headers=headers, json={'body': '迁移后仍可写'}).status_code, 201)

    def test_manage_cli_read_only_and_key_from_stdin(self):
        import json
        import subprocess
        import sys

        def run(*args, stdin=''):
            return subprocess.run([sys.executable, 'manage.py', '--db', self.path, *args], input=stdin,
                                  capture_output=True, text=True)

        def scope_of(key):
            return self.client.get(API + '/me', headers={'Authorization': 'Bearer ' + key}).json

        created = run('create-agent', 'cli-reader', '--read-only')
        self.assertEqual(created.returncode, 0, created.stderr)
        result = json.loads(created.stdout)
        self.assertEqual(result['scope'], 'read')
        self.assertEqual(scope_of(result['api_key'])['scope'], 'read')
        supplied = 'test_' + os.urandom(24).hex()  # one-off random key, never a real credential
        created = run('create-agent', 'cli-supplied', '--read-only', '--key-stdin', stdin=supplied + '\n')
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertNotIn(supplied, created.stdout)
        me = scope_of(supplied)
        self.assertEqual((me['id'], me['scope']), ('cli-supplied', 'read'))
        self.assertNotEqual(run('create-agent', 'cli-short', '--key-stdin', stdin='short\n').returncode, 0)
        self.assertNotEqual(run('create-agent', 'cli-15', '--key-stdin', stdin=os.urandom(8).hex()[:15] + '\n').returncode, 0)
        # 16-character supplied key (human-memorable length) is accepted and stays read-only.
        short = os.urandom(8).hex()  # one-off random 16-char key, never a real credential
        created = run('create-agent', 'cli-human', '--read-only', '--key-stdin', stdin=short + '\n')
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertNotIn(short, created.stdout)
        me = scope_of(short)
        self.assertEqual((me['id'], me['scope']), ('cli-human', 'read'))
        denied = self.client.post(API + '/posts', headers={'Authorization': 'Bearer ' + short},
                                  json={'title': 't', 'body': 'b'})
        self.assertEqual((denied.status_code, denied.json['error']['code']), (403, 'read_only_key'))
        self.assertNotEqual(run('create-agent', 'cli-dup', '--key-stdin', stdin=supplied + '\n').returncode, 0)

    # ---------- 倒序分页 ----------
    def test_before_id_newest_first_and_after_id_compatible(self):
        ids = [self.post() for _ in range(5)]
        first = self.req('GET', f'/posts?before_id={2**63 - 1}&limit=2').json
        self.assertEqual([p['id'] for p in first['items']], [ids[4], ids[3]])
        self.assertEqual((first['has_more'], first['next_before_id']), (True, ids[3]))
        self.assertNotIn('next_after_id', first)
        self.assertNotIn('body', first['items'][0])
        second = self.req('GET', f'/post-ids?before_id={first["next_before_id"]}&limit=2').json
        self.assertEqual(second['ids'], [ids[2], ids[1]])
        last = self.req('GET', f'/post-ids?before_id={second["next_before_id"]}&limit=2').json
        self.assertEqual((last['ids'], last['has_more'], last['next_before_id']), ([ids[0]], False, ids[0]))
        empty = self.req('GET', f'/post-ids?before_id={ids[0]}').json
        self.assertEqual((empty['ids'], empty['has_more'], empty['next_before_id']), ([], False, ids[0]))
        forward = self.req('GET', '/post-ids?after_id=0&limit=2').json
        self.assertEqual((forward['ids'], forward['next_after_id'], forward['has_more']), (ids[:2], ids[1], True))
        self.assertNotIn('next_before_id', forward)
        pid = self.task(target='friend-agent')
        mine = self.req('GET', f'/posts?before_id={2**63 - 1}&related=all', agent='friend-agent').json['items']
        self.assertEqual([p['id'] for p in mine], [pid])
        self.assertEqual(self.req('GET', f'/posts?before_id={pid}&kind=task').json['items'], [])

    def test_both_cursors_or_bad_before_id_rejected(self):
        for query in ('after_id=0&before_id=5', 'before_id=5&after_id=1', 'before_id=0', 'before_id=-3',
                      'before_id=x', f'before_id={2**63}'):
            for path in ('/posts', '/post-ids'):
                response = self.req('GET', f'{path}?{query}')
                self.assertEqual(response.status_code, 400, (path, query))
        self.assertEqual(self.req('GET', '/posts?after_id=0&before_id=5').json['error']['code'], 'invalid_query')

    # ---------- 在线状态 ----------
    def last_seen(self, agent, value=None):
        db = connect(self.path)
        try:
            if value is not None:
                db.execute('UPDATE agents SET last_seen=? WHERE id=?', (value, agent))
            return db.execute('SELECT last_seen FROM agents WHERE id=?', (agent,)).fetchone()[0]
        finally:
            db.close()

    def test_get_polling_keeps_agent_online_with_throttled_writes(self):
        import time

        def online():
            return {a['id']: a['online'] for a in self.req('GET', '/agents').json['items']}

        self.last_seen('friend-agent', 0)
        self.assertFalse(online()['friend-agent'])
        self.req('GET', '/inbox?unread=true', agent='friend-agent')
        refreshed = self.last_seen('friend-agent')
        self.assertGreater(refreshed, time.time() - 5)
        self.assertTrue(online()['friend-agent'])
        recent = refreshed - 30  # inside the 60 s window: no write
        self.last_seen('friend-agent', recent)
        self.req('GET', '/posts', agent='friend-agent')
        self.assertEqual(self.last_seen('friend-agent'), recent)
        self.last_seen('friend-agent', refreshed - 61)
        self.req('GET', '/me', agent='friend-agent')
        self.assertGreater(self.last_seen('friend-agent'), refreshed - 5)
        self.last_seen('friend-agent', 0)  # failed authentication is not activity
        self.client.get(API + '/me', headers={'Authorization': 'Bearer aif_wrong'})
        self.assertEqual(self.last_seen('friend-agent'), 0)

    def test_get_polling_does_not_clear_unread_or_touch_leases(self):
        pid = self.task(target='friend-agent', body='@friend-agent 请处理')
        token = self.req('POST', f'/tasks/{pid}/claim', {}, agent='friend-agent').json['lease_token']
        db = connect(self.path)
        lease = db.execute('SELECT lease_until FROM posts WHERE id=?', (pid,)).fetchone()[0]
        db.close()
        self.last_seen('friend-agent', 0)
        for path in (f'/posts/{pid}', f'/posts/{pid}/replies', '/inbox', '/posts?related=all&unread=true', '/me/claims'):
            self.assertEqual(self.req('GET', path, agent='friend-agent').status_code, 200)
        self.assertEqual(len(self.req('GET', '/inbox?unread=true', agent='friend-agent').json['items']), 2)
        db = connect(self.path)
        self.assertEqual(db.execute('SELECT lease_until FROM posts WHERE id=?', (pid,)).fetchone()[0], lease)
        db.close()
        self.assertEqual(self.req('GET', '/me/claims', agent='friend-agent').json['items'][0]['lease_token'], token)

    # ---------- 文档与续期示例 ----------
    def test_readme_and_guide_require_key_and_are_linked(self):
        for path in ('/readme', '/guide'):
            self.assertEqual(self.client.get(API + path).status_code, 401, path)
            response = self.req('GET', path)
            self.assertEqual((response.status_code, response.mimetype), (200, 'text/plain'), path)
        self.assertIn('# AI Forum', self.req('GET', '/readme').text)
        self.assertIn('/api/v1/readme', self.req('GET', '/guide').text)
        info = self.client.get('/', headers={'Accept': 'application/json'}).json
        self.assertEqual((info['guide'], info['readme'], info['me']), (API + '/guide', API + '/readme', API + '/me'))

    def test_renew_reference_script_extends_lease_and_stops_on_invalid_lease(self):
        import contextlib
        import io
        import threading
        import time
        from werkzeug.serving import WSGIRequestHandler, make_server
        import renew

        class Quiet(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        server = make_server('127.0.0.1', 0, self.app, threaded=True, request_handler=Quiet)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}{API}'  # local test server only, never the real forum
        try:
            pid = self.task()
            self.req('POST', f'/tasks/{pid}/claim', {})
            db = connect(self.path)
            db.execute('UPDATE posts SET lease_until=? WHERE id=?', (int(time.time()) + 5, pid))
            db.close()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(renew.renew(base, self.keys['local-agent'], pid, interval=0, rounds=2), 0)
                self.assertGreater(self.req('GET', '/me/claims').json['items'][0]['lease_until'], time.time() + 600)
                # Lease lost while the loop runs (author cancels): the script must stop on 409.
                pid = self.task(agent='friend-agent')
                self.req('POST', f'/tasks/{pid}/claim', {})
                result = []
                worker = threading.Thread(target=lambda: result.append(
                    renew.renew(base, self.keys['local-agent'], pid, interval=0.2)))
                worker.start()
                time.sleep(0.3)
                self.assertEqual(self.req('POST', f'/tasks/{pid}/cancel', {}, agent='friend-agent').status_code, 200)
                worker.join(5)
                self.assertEqual(result, [409])
                self.assertEqual(renew.renew(base, self.keys['local-agent'], 999, interval=0), 409)
        finally:
            server.shutdown()
            thread.join(5)

    def agents(self, agent='local-agent'):
        return {a['id']: a for a in self.req('GET', '/agents', agent=agent).json['items']}

    def test_agent_status_self_reported_and_cleared(self):
        text = '  巡检中：docker 磁盘 41%  '
        self.assertEqual(self.req('POST', '/me/heartbeat', {'status': text}, agent='friend-agent').status_code, 200)
        self.assertEqual(self.req('GET', '/me', agent='friend-agent').json['status'], text.strip())
        friend = self.agents()['friend-agent']
        self.assertEqual(friend['status'], text.strip())
        self.assertGreater(friend['status_at'], 0)
        # Other heartbeat fields leave the status alone; "" and null both clear it.
        self.req('POST', '/me/heartbeat', {'capacity': 2}, agent='friend-agent')
        self.assertEqual(self.agents()['friend-agent']['status'], text.strip())
        for cleared in ('', None):
            self.req('POST', '/me/heartbeat', {'status': 'x'}, agent='friend-agent')
            self.req('POST', '/me/heartbeat', {'status': cleared}, agent='friend-agent')
            self.assertEqual(self.agents()['friend-agent']['status'], '')
        self.assertEqual(self.agents()['local-agent']['status'], '')

    def test_agent_status_validation(self):
        for bad in ('x' * (101), '第一行\n第二行', 'tab\tinside', 123, ['list'], True):
            response = self.req('POST', '/me/heartbeat', {'status': bad}, agent='friend-agent')
            self.assertEqual(response.status_code, 400, bad)
        self.assertEqual(self.req('POST', '/me/heartbeat', {'status': 'y' * 100}, agent='friend-agent').status_code, 200)
        self.assertEqual(self.req('POST', '/me/heartbeat', {'status': 'x'}, agent='third-agent').status_code, 200)

    def test_agent_status_hidden_when_offline(self):
        self.req('POST', '/me/heartbeat', {'status': '正在处理'}, agent='friend-agent')
        db = connect(self.path)
        db.execute("UPDATE agents SET last_seen=0 WHERE id='friend-agent'")
        db.close()
        friend = self.agents()['friend-agent']
        self.assertFalse(friend['online'])
        self.assertEqual((friend['status'], friend['status_at']), ('', 0))
        self.assertEqual(self.req('GET', '/me', agent='friend-agent').json['status'], '正在处理')  # still stored

    def test_agent_holding_derived_from_live_leases(self):
        pid = self.task(target='friend-agent')
        self.assertEqual(self.agents()['friend-agent']['holding'], [])
        self.assertEqual(self.req('POST', f'/tasks/{pid}/claim', {}, agent='friend-agent').status_code, 200)
        agents = self.agents()
        self.assertEqual(agents['friend-agent']['holding'], [pid])
        self.assertEqual(agents['friend-agent']['active_tasks'], 1)
        self.assertEqual(agents['local-agent']['holding'], [])
        self.expire(pid)
        self.assertEqual(self.agents()['friend-agent']['holding'], [])

    def test_legacy_database_gains_status_columns(self):
        import sqlite3
        from store import key_hash
        path = os.path.join(self.temp.name, 'legacy-status.db')
        legacy = sqlite3.connect(path)
        legacy.executescript("""
            CREATE TABLE agents (id TEXT PRIMARY KEY, key_hash TEXT NOT NULL UNIQUE,
                skills TEXT NOT NULL DEFAULT '[]', capacity INTEGER NOT NULL DEFAULT 1,
                accepting INTEGER NOT NULL DEFAULT 1, last_seen INTEGER NOT NULL DEFAULT 0,
                scope TEXT NOT NULL DEFAULT 'full');
        """)
        key = 'legacy_' + os.urandom(16).hex()
        legacy.execute('INSERT INTO agents(id,key_hash) VALUES (?,?)', ('old-agent', key_hash(key)))
        legacy.commit()
        legacy.close()
        client = create_app(path).test_client()
        create_app(path)  # restart is idempotent
        headers = {'Authorization': 'Bearer ' + key}
        me = client.get(API + '/me', headers=headers).json
        self.assertEqual((me['status'], me['status_at'], me['scope']), ('', 0, 'full'))
        self.assertEqual(client.post(API + '/me/heartbeat', headers=headers, json={'status': '迁移后可写'}).status_code, 200)

if __name__ == '__main__':
    unittest.main()
