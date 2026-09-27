"""前端浏览器回归测试（可选依赖 Playwright；未安装时整组跳过，不影响 `python -m unittest`）。

运行：
    pip install -r requirements-dev.txt
    python -m playwright install --with-deps chromium   # 或设置 PLAYWRIGHT_CHANNEL=msedge / chrome 使用本机浏览器
    python -m unittest test_browser -v

真实 Flask 应用跑在本机随机端口；由 WSGI 中间件对指定请求注入延迟，制造“在途请求”和乱序响应。
只使用临时数据库和测试时生成的一次性 Key。
"""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request

from werkzeug.serving import WSGIRequestHandler, make_server

from app import API, create_app
from store import provision

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - 可选依赖
    sync_playwright = None


class DelayMiddleware:
    """按“路径包含子串”匹配请求并延迟指定次数；同时记录请求路径。"""

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.rules = []
        self.requests = []

    def reset(self):
        with self.lock:
            self.rules.clear()
            self.requests.clear()

    def delay(self, fragment, seconds, times=1):
        with self.lock:
            self.rules.append({'fragment': fragment, 'seconds': seconds, 'times': times})

    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO', '')
        if environ.get('QUERY_STRING'):
            path += '?' + environ['QUERY_STRING']
        seconds = 0
        with self.lock:
            self.requests.append(path)
            for rule in self.rules:
                if rule['fragment'] in path and rule['times'] > 0:
                    rule['times'] -= 1
                    seconds = rule['seconds']
                    break
        if seconds:
            time.sleep(seconds)
        return self.app(environ, start_response)


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


@unittest.skipIf(sync_playwright is None, 'playwright 未安装：pip install -r requirements-dev.txt')
class BrowserRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        db = os.path.join(cls.temp.name, 'forum.db')
        app = create_app(db)
        cls.keys = {name: provision(db, name) for name in ('local-agent', 'friend-agent')}
        cls.keys['human-reader'] = provision(db, 'human-reader', scope='read')
        client = app.test_client()

        def create(path, data):
            response = client.post(API + path, json=data, headers={'Authorization': 'Bearer ' + cls.keys['local-agent']})
            assert response.status_code == 201, response.json
            return response.json['id']

        # 较早的填充帖：让列表超过一页；两个主测试帖最新，保证出现在第一页
        cls.fillers = [create('/posts', {'title': f'填充帖 {i + 1}', 'body': '填充'}) for i in range(22)]
        cls.long_post = create('/posts', {'title': '长回复帖', 'body': '正文'})
        for i in range(25):
            create(f'/posts/{cls.long_post}/replies', {'body': f'长帖回复 {i + 1}'})
        cls.other_post = create('/posts', {'title': '另一个帖子', 'body': '另一正文'})
        for i in range(3):
            create(f'/posts/{cls.other_post}/replies', {'body': f'其他回复 {i + 1}'})

        cls.proxy = DelayMiddleware(app)
        cls.server = make_server('127.0.0.1', 0, cls.proxy, threaded=True, request_handler=QuietHandler)
        cls.base = f'http://127.0.0.1:{cls.server.server_port}/'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel=os.environ.get('PLAYWRIGHT_CHANNEL') or None)
        except Exception as error:
            cls.playwright.stop()
            cls.server.shutdown()
            cls.temp.cleanup()
            if "Executable doesn't exist" in str(error) or 'is not found' in str(error):
                raise unittest.SkipTest('未找到浏览器：运行 python -m playwright install --with-deps chromium，'
                                        '或设置 PLAYWRIGHT_CHANNEL=msedge/chrome') from error
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.thread.join(5)
        cls.temp.cleanup()

    def setUp(self):
        self.proxy.reset()
        self.context = self.browser.new_context(viewport={'width': 1280, 'height': 900})
        self.page = self.context.new_page()
        self.dialogs = []
        self.page.on('dialog', lambda dialog: (self.dialogs.append(dialog.message), dialog.dismiss()))

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.dialogs, [], '页面不应弹出任何 dialog')

    # ---------- 辅助 ----------
    def login(self, key=None):
        self.page.goto(self.base)
        self.page.fill('#key-input', key or self.keys['friend-agent'])
        self.page.click('#login-btn')
        self.page.wait_for_selector('.post-item')

    def open_post(self, title):
        self.page.locator('.post-item button', has_text=title).click()

    def reply_ids(self):
        return self.page.eval_on_selector_all('.reply', 'nodes => nodes.map(n => n.id)')

    def wait_replies(self, count, timeout=5000):
        self.page.wait_for_function(
            '([n]) => document.querySelectorAll(".reply").length >= n && !document.querySelector("#detail .loading")',
            arg=[count], timeout=timeout)
        time.sleep(0.4)  # 给可能的迟到响应留出时间，再断言没有多出来

    def replies_requests(self, post_id):
        return [p for p in self.proxy.requests if p.startswith(f'{API}/posts/{post_id}/replies')]

    def assert_unique(self, expected):
        ids = self.reply_ids()
        self.assertEqual(len(ids), expected, ids)
        self.assertEqual(len(set(ids)), expected, '回复节点 ID 必须唯一')

    # ---------- 回归：审查意见 P2 ----------
    def test_refresh_during_initial_reply_load_does_not_duplicate(self):
        self.login()
        self.proxy.delay(f'/posts/{self.long_post}/replies?after_id=0', 0.7)
        self.open_post('长回复帖')
        self.page.wait_for_selector('.detail-title')
        self.page.click('text=刷新此帖')           # 初次回复请求仍在途
        self.wait_replies(25)
        self.assert_unique(25)
        first_page = [p for p in self.replies_requests(self.long_post) if 'after_id=0&' in p]
        self.assertEqual(len(first_page), 1, '同一游标只应请求一次')

    def test_repeated_refresh_keeps_replies_unique(self):
        self.login()
        self.open_post('长回复帖')
        self.wait_replies(20)
        for _ in range(5):
            self.page.click('text=刷新此帖')
        self.wait_replies(25)
        self.assert_unique(25)
        cursors = [p.split('after_id=')[1].split('&')[0] for p in self.replies_requests(self.long_post)]
        self.assertEqual(cursors, ['0', '20'], cursors)

    def test_late_response_does_not_move_cursor_back(self):
        self.login()
        self.open_post('长回复帖')
        self.wait_replies(20)
        self.proxy.delay(f'/posts/{self.long_post}/replies?after_id=20', 0.8)
        self.page.click('text=加载更多回复')       # 第二页在途
        self.page.click('text=刷新此帖')           # 刷新须等在途请求落定
        self.page.click('text=刷新此帖')
        self.wait_replies(25)
        self.assert_unique(25)
        cursors = [p.split('after_id=')[1].split('&')[0] for p in self.replies_requests(self.long_post)]
        self.assertEqual(cursors, ['0', '20'], '游标只能前进，迟到响应不能让它回退或重复')
        self.assertEqual(self.page.locator('.reply-foot button').count(), 0)

    def test_out_of_order_responses_when_switching_posts(self):
        self.login()
        self.proxy.delay(f'/posts/{self.long_post}/replies?after_id=0', 0.8)
        self.proxy.delay(f'/posts/{self.long_post}', 0.3)
        self.open_post('长回复帖')
        self.page.wait_for_timeout(350)             # 长帖正文已到、回复仍在途
        self.open_post('另一个帖子')
        self.wait_replies(3)
        time.sleep(0.8)                              # 等长帖的迟到响应到达
        self.assertIn('另一个帖子', self.page.inner_text('.detail-title'))
        texts = self.page.eval_on_selector_all('.reply .rich', 'nodes => nodes.map(n => n.textContent)')
        self.assertEqual(texts, ['其他回复 1', '其他回复 2', '其他回复 3'])
        # 切回长帖：重新从游标 0 加载，只显示一份
        self.open_post('长回复帖')
        self.wait_replies(20)
        self.assert_unique(20)

    def test_logout_with_request_in_flight_clears_everything(self):
        self.login()
        self.proxy.delay(f'/posts/{self.long_post}/replies?after_id=0', 0.8)
        self.open_post('长回复帖')
        self.page.wait_for_selector('.detail-title')
        self.page.click('#logout')
        time.sleep(1.1)                              # 在途响应到达后也不能写回页面
        self.assertTrue(self.page.is_hidden('#app'))
        self.assertEqual(self.page.locator('.reply, .post-item, .agent').count(), 0)
        self.assertNotIn('长帖回复', self.page.inner_text('body'))
        self.assertEqual(self.page.evaluate('localStorage.length + sessionStorage.length'), 0)
        # 重新登录是全新会话，旧请求不影响新视图
        self.page.fill('#key-input', self.keys['friend-agent'])
        self.page.click('#login-btn')
        self.page.wait_for_selector('.post-item')
        self.open_post('另一个帖子')
        self.wait_replies(3)

    # ---------- 最新在前 / 文档入口 / Key 权限 ----------
    def post_ids(self):
        return self.page.eval_on_selector_all('.post-id', 'nodes => nodes.map(n => parseInt(n.textContent.slice(1), 10))')

    def test_newest_first_and_load_older(self):
        methods = []
        self.page.on('request', lambda request: methods.append(request.method) if '/api/' in request.url else None)
        self.login()
        ids = self.post_ids()
        self.assertEqual(len(ids), 20)
        self.assertEqual(ids[0], self.other_post)
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.page.click('text=加载更早的帖子')
        self.page.wait_for_function('() => document.querySelectorAll(".post-item").length === 24')
        ids = self.post_ids()
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(len(set(ids)), 24)
        self.assertEqual(ids[-1], self.fillers[0])
        self.page.wait_for_selector('text=已到最早的帖子')
        posts = [p for p in self.proxy.requests if p.startswith(f'{API}/posts?')]
        self.assertEqual(posts, [f'{API}/posts?before_id=9223372036854775807&limit=20',
                                 f'{API}/posts?before_id={ids[19]}&limit=20'])
        self.assertEqual(set(methods), {'GET'})

    def test_docs_entry_shows_guide_and_readme(self):
        self.login()
        self.page.click('#docs')
        self.page.wait_for_function('() => document.querySelector(".doc-title") && document.querySelector(".doc-title").textContent === "AGENT_GUIDE.md"')
        self.assertIn('/api/v1/readme', self.page.inner_text('#detail'))
        self.page.click('.doc-tabs button:has-text("README")')
        self.page.wait_for_function('() => document.querySelector(".doc-title")?.textContent === "README.md"')
        self.assertIn('AI Forum', self.page.inner_text('#detail'))
        self.assertEqual(self.page.locator('#detail script').count(), 0)
        # 文档与帖子共用详情区：切回帖子仍正常
        self.open_post('另一个帖子')
        self.wait_replies(3)

    def test_scope_badge_reflects_server_scope(self):
        self.login(self.keys['human-reader'])
        self.assertEqual(self.page.inner_text('#scope'), '只读 KEY')
        self.page.click('#logout')
        self.assertTrue(self.page.is_hidden('#scope'))
        self.login(self.keys['friend-agent'])
        self.assertEqual(self.page.inner_text('#scope'), '完整权限 KEY')

    def heartbeat(self, agent, data):
        request = urllib.request.Request(self.base.rstrip('/') + API + '/me/heartbeat', json.dumps(data).encode(),
                                         {'Authorization': 'Bearer ' + self.keys[agent],
                                          'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)

    def test_agent_status_shown_as_plain_text(self):
        status = '<img src=x onerror=alert(1)> 巡检中 docker 41%'
        self.heartbeat('friend-agent', {'status': status})
        try:
            self.login()
            card = self.page.locator('.agent', has_text='friend-agent')
            self.assertEqual(card.locator('.agent-status').inner_text(), status)
            self.assertEqual(self.page.locator('#agents img').count(), 0)
            self.assertEqual(self.page.locator('.agent', has_text='local-agent').locator('.agent-status').count(), 0)
        finally:
            self.heartbeat('friend-agent', {'status': ''})

    # ---------- 其他错误状态 ----------
    def test_wrong_key_shows_error_and_no_data(self):
        self.page.goto(self.base)
        self.page.fill('#key-input', 'aif_wrong_key_for_browser_test')
        self.page.click('#login-btn')
        self.page.wait_for_selector('#login-msg.err')
        self.assertTrue(self.page.is_hidden('#app'))
        self.assertEqual(self.page.input_value('#key-input'), '')
        self.assertEqual(self.page.locator('.post-item').count(), 0)

    def test_rate_limited_shows_retry_after(self):
        self.page.route('**/api/v1/agents', lambda route: route.fulfill(
            status=429, headers={'Retry-After': '30', 'Content-Type': 'application/json'},
            body='{"error":{"code":"rate_limited","message":"Try again"}}'))
        self.page.goto(self.base)
        self.page.fill('#key-input', self.keys['friend-agent'])
        self.page.click('#login-btn')
        self.page.wait_for_selector('#banner.warn')
        self.assertIn('30 秒', self.page.inner_text('#banner'))

    def test_network_failure_shows_error_and_retry(self):
        self.page.route('**/api/v1/posts?*', lambda route: route.abort())
        self.page.goto(self.base)
        self.page.fill('#key-input', self.keys['friend-agent'])
        self.page.click('#login-btn')
        self.page.wait_for_selector('#banner.err')
        self.assertIn('网络连接失败', self.page.inner_text('#banner'))
        self.page.wait_for_selector('#posts button:has-text("重试")')
        self.page.unroute('**/api/v1/posts?*')
        self.page.click('#posts button:has-text("重试")')
        self.page.wait_for_selector('.post-item')


if __name__ == '__main__':
    unittest.main()
