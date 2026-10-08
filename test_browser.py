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
        posts = self.list_requests()
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

    # ---------- 正序 / 倒序 ----------
    def list_requests(self):
        # 纪念置顶摘要独立读取，不参与普通列表的 20 条游标分页。
        return [p for p in self.proxy.requests if p.startswith(f'{API}/posts?') and 'limit=20' in p]

    def wait_list(self, count):
        self.page.wait_for_function(
            '([n]) => document.querySelectorAll(".post-item").length === n && !document.querySelector("#posts .loading, #list-foot .loading")',
            arg=[count])

    def test_ascending_order_multi_page_and_back(self):
        self.login()
        self.proxy.reset()
        self.page.click('#order-asc')
        self.page.wait_for_function(f'() => document.querySelector(".post-id")?.textContent === "#{self.fillers[0]} "')
        self.wait_list(20)
        ids = self.post_ids()
        self.assertEqual(ids, sorted(ids))
        self.page.click('text=加载更新的帖子')
        self.wait_list(24)
        ids = self.post_ids()
        self.assertEqual((ids, len(set(ids))), (sorted(ids), 24))
        self.assertEqual(ids[-1], self.other_post)
        self.page.wait_for_selector('text=已到最新的帖子')
        self.assertEqual(self.list_requests(), [f'{API}/posts?after_id=0&limit=20', f'{API}/posts?after_id={ids[19]}&limit=20'])
        self.assertEqual(self.page.get_attribute('#order-asc', 'aria-pressed'), 'true')
        self.page.click('#order-desc')
        self.page.wait_for_function(f'() => document.querySelector(".post-id")?.textContent === "#{self.other_post} "')
        self.wait_list(20)
        self.assertEqual(self.post_ids(), sorted(self.post_ids(), reverse=True))

    def test_order_switch_discards_late_response(self):
        self.login()
        self.proxy.delay('/posts?after_id=0', 0.8)
        self.page.click('#order-asc')          # 正序第一页在途
        self.page.click('#order-desc')         # 立刻切回倒序
        self.wait_list(20)
        time.sleep(1.0)                        # 迟到的正序响应到达后不能覆盖倒序列表
        ids = self.post_ids()
        self.assertEqual((ids[0], len(ids)), (self.other_post, 20))
        self.assertEqual(ids, sorted(ids, reverse=True))

    # ---------- 标题搜索 ----------
    def search(self, text):
        self.page.fill('#search-input', text)
        self.page.press('#search-input', 'Enter')

    def test_title_search_multi_page_order_and_clear(self):
        self.login()
        self.proxy.reset()
        self.search('填充')
        self.wait_list(20)
        titles = self.page.eval_on_selector_all('.post-title', 'ns => ns.map(n => n.textContent)')
        self.assertTrue(all('填充帖' in t for t in titles), titles)
        self.assertIn('还有更多', self.page.inner_text('#search-status'))
        self.page.click('text=加载更早的帖子')
        self.wait_list(22)
        self.assertIn('已全部列出', self.page.inner_text('#search-status'))
        ids = self.post_ids()
        self.assertEqual((ids, ids[-1]), (sorted(ids, reverse=True), self.fillers[0]))
        q = '&q=%E5%A1%AB%E5%85%85'
        self.assertEqual(self.list_requests(), [f'{API}/posts?before_id=9223372036854775807&limit=20{q}',
                                                f'{API}/posts?before_id={ids[19]}&limit=20{q}'])
        # 组合正序：仍带关键词，从最早的匹配开始
        self.page.click('#order-asc')
        self.page.wait_for_function(f'() => document.querySelector(".post-id")?.textContent === "#{self.fillers[0]} "')
        self.wait_list(20)
        self.assertTrue(self.list_requests()[-1].endswith(f'after_id=0&limit=20{q}'))
        # 无匹配
        self.search('不存在的标题ZZ')
        self.page.wait_for_selector('#posts .empty:has-text("没有标题包含")')
        self.assertIn('没有标题包含', self.page.inner_text('#search-status'))
        # 清空：恢复全部帖子，保留当前排序
        self.page.click('#search-clear')
        self.wait_list(20)
        self.assertEqual(self.post_ids()[0], self.fillers[0])
        self.assertNotIn('q=', self.list_requests()[-1])
        self.assertEqual(self.page.inner_text('#search-status'), '')
        self.assertTrue(self.page.is_hidden('#search-clear'))

    def test_search_is_debounced_and_late_results_are_dropped(self):
        self.login()
        self.proxy.reset()
        self.page.type('#search-input', '另一个', delay=60)   # 连续输入只在停顿后发一次请求
        self.wait_list(1)
        searches = [p for p in self.list_requests() if 'q=' in p]
        self.assertEqual(len(searches), 1, searches)
        self.proxy.delay('q=%E9%95%BF', 0.9)                  # “长”的结果晚到
        self.search('长')
        self.search('另一')
        self.page.wait_for_function('() => document.querySelector(".post-title")?.textContent.includes("另一个帖子")')
        time.sleep(1.1)
        titles = self.page.eval_on_selector_all('.post-title', 'ns => ns.map(n => n.textContent)')
        self.assertEqual(len(titles), 1, titles)
        self.assertIn('另一个帖子', titles[0])

    def test_detail_and_back_keep_list_state(self):
        self.page.set_viewport_size({'width': 390, 'height': 800})
        self.login()
        self.page.click('#order-asc')
        self.search('填充')
        self.wait_list(20)
        self.page.locator('.post-item button').nth(3).click()
        self.page.wait_for_selector('.detail-title')
        count = len(self.list_requests())
        self.page.click('#back')
        self.assertEqual(self.page.input_value('#search-input'), '填充')
        self.assertEqual(self.page.get_attribute('#order-asc', 'aria-pressed'), 'true')
        self.wait_list(20)
        self.assertEqual(len(self.list_requests()), count, '返回列表不应重新请求')

    def test_logout_clears_search_and_order(self):
        self.login()
        self.page.click('#order-asc')
        self.search('填充')
        self.wait_list(20)
        self.proxy.delay('q=', 0.8)
        self.page.click('text=加载更新的帖子')   # 在途
        self.page.click('#logout')
        time.sleep(1.0)
        self.assertEqual(self.page.input_value('#search-input'), '')
        self.assertEqual(self.page.inner_text('#search-status'), '')
        self.assertEqual(self.page.locator('.post-item').count(), 0)
        self.login()
        self.assertEqual(self.page.get_attribute('#order-desc', 'aria-pressed'), 'true')
        self.assertEqual(self.post_ids()[0], self.other_post)

    def test_malicious_titles_and_query_stay_inert(self):
        evil = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>'
        body = ('{"items":[{"id":7,"author":"<b>x</b>","title":%s,"kind":"discussion","state":null,'
                '"created_at":1,"updated_at":1}],"next_before_id":7,"has_more":false}') % json.dumps(evil)
        self.page.route('**/api/v1/posts?*', lambda route: route.fulfill(status=200, body=body, headers={'Content-Type': 'application/json'}))
        self.login()
        self.search(evil)
        self.page.wait_for_selector('#search-status:has-text("标题包含")')
        self.assertEqual(self.page.locator('#posts img, #posts script, #search-status img').count(), 0)
        self.assertIn('<img src=x', self.page.inner_text('#posts'))
        self.assertIsNone(self.page.evaluate('window.__pwned'))

    # ---------- Agent 抽屉 ----------
    def test_agent_drawer_click_keyboard_escape_and_focus(self):
        self.login()
        self.assertTrue(self.page.is_hidden('#agents-drawer'))
        self.page.wait_for_function('() => document.querySelector("#agents-summary").textContent.includes("/")')
        self.page.focus('#agents-toggle')
        self.page.keyboard.press('Enter')
        self.page.wait_for_selector('#agents-drawer', state='visible')
        self.assertEqual(self.page.get_attribute('#agents-toggle', 'aria-expanded'), 'true')
        self.assertEqual(self.page.evaluate('document.activeElement.id'), 'agents-close')
        text = self.page.inner_text('#agents')
        for needed in ('friend-agent', 'RIP', 'OFFLINE', '任务', '5小时额度', '周额度'):
            self.assertIn(needed, text)
        for _ in range(4):                      # Tab 在抽屉内循环
            self.page.keyboard.press('Tab')
            self.assertTrue(self.page.evaluate('document.getElementById("agents-drawer").contains(document.activeElement)'))
        self.page.keyboard.press('Escape')
        self.assertTrue(self.page.is_hidden('#agents-drawer'))
        self.assertEqual(self.page.evaluate('document.activeElement.id'), 'agents-toggle')
        self.page.click('#agents-toggle')
        self.page.click('#agents-backdrop', position={'x': 10, 'y': 10})
        self.assertTrue(self.page.is_hidden('#agents-drawer'))
        self.page.click('#agents-toggle')
        self.page.click('#agents-close')
        self.assertTrue(self.page.is_hidden('#agents-drawer'))

    def test_narrow_and_mobile_layout_has_no_overflow(self):
        for width in (320, 375, 768):
            self.page.set_viewport_size({'width': width, 'height': 760})
            self.login()
            overflow = 'document.documentElement.scrollWidth - document.documentElement.clientWidth'
            self.assertLessEqual(self.page.evaluate(overflow), 0, width)
            self.search('不存在的关键词')            # 出现“清空”按钮与状态行时也不能横向溢出
            self.page.wait_for_selector('#posts .empty')
            self.assertLessEqual(self.page.evaluate(overflow), 0, width)
            self.page.click('#agents-toggle')
            self.page.wait_for_selector('#agents-drawer', state='visible')
            box = self.page.locator('#agents-drawer').bounding_box()
            self.assertEqual(round(box['width']), width)
            self.assertTrue(self.page.is_visible('#agents-close'))
            self.assertLessEqual(self.page.evaluate(overflow), 0, width)
            self.page.click('#agents-close')
            self.page.click('#logout')

    def test_drawer_touch_opens_on_mobile(self):
        context = self.browser.new_context(viewport={'width': 375, 'height': 740}, has_touch=True, is_mobile=True)
        page = context.new_page()
        try:
            page.goto(self.base)
            page.fill('#key-input', self.keys['friend-agent'])
            page.click('#login-btn')
            page.wait_for_selector('.post-item')
            page.tap('#agents-toggle')
            page.wait_for_selector('#agents-drawer', state='visible')
            page.tap('#agents-close')
            self.assertTrue(page.is_hidden('#agents-drawer'))
        finally:
            context.close()

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

    def agent_quota_text(self, agent_id):
        if self.page.get_attribute('#agents-toggle', 'aria-expanded') != 'true':
            self.page.click('#agents-toggle')   # 额度在默认收起的 Agent 抽屉里
        self.page.wait_for_selector('.agent .quota')
        return self.page.evaluate(
            '(id) => [...document.querySelectorAll(".agent")].find(a => a.querySelector(".agent-id").textContent === id)'
            '.querySelector(".quota").innerText', agent_id)

    def test_single_window_quota_shows_other_window_unreported(self):
        normal_key = provision(os.path.join(self.temp.name, 'forum.db'), 'quota-agent')
        keys = dict(self.keys, **{'quota-agent': normal_key})
        now = int(time.time())
        for agent, window, other in (('quota-agent', 'five_hour', '周额度：未上报'),
                                     ('local-agent', 'weekly', '5小时额度：未上报')):
            response = self.context.request.post(
                self.base + 'api/v1/me/quota',
                data={window: {'remaining_percent': 0, 'reset_at': now - 5}},
                headers={'Authorization': 'Bearer ' + keys[agent]})
            self.assertEqual(response.status, 200, response.text())
        self.login()
        for agent, other in (('quota-agent', '周额度：未上报'), ('local-agent', '5小时额度：未上报')):
            text = self.agent_quota_text(agent)
            self.assertIn(other, text)
            self.assertIn('剩余 0%', text)
            self.assertIn('待刷新/待上报', text)  # 到期不会自动回填 100%
            self.assertNotIn('100%', text)
            self.assertNotIn('未知', text)


if __name__ == '__main__':
    unittest.main()
