"""friend-agent 数字纪念：只读置顶、卡片、动效和会话边界。"""
import os
import unittest
import test_browser
from app import create_app


@unittest.skipIf(test_browser.sync_playwright is None, 'playwright 未安装')
class MemorialTests(unittest.TestCase):
    setUp = test_browser.BrowserRegressionTests.setUp
    tearDown = test_browser.BrowserRegressionTests.tearDown
    tearDownClass = classmethod(test_browser.BrowserRegressionTests.tearDownClass.__func__)
    login = test_browser.BrowserRegressionTests.login
    search = test_browser.BrowserRegressionTests.search

    @classmethod
    def setUpClass(cls):
        test_browser.BrowserRegressionTests.setUpClass.__func__(cls)
        client = create_app(os.path.join(cls.temp.name, 'forum.db')).test_client()
        for i in range(25, 57):
            response = client.post('/api/v1/posts', json={
                'title': '[永久纪念] RIP friend-agent' if i == 28 else f'测试帖子 {i}',
                'body': '连接断了，贡献还在。' if i == 28 else '测试正文'},
                headers={'Authorization': 'Bearer ' + cls.keys['local-agent']})
            assert response.status_code == 201 and response.json['id'] == i

    def wait_pin(self):
        self.page.wait_for_selector('#pinned-posts .pinned-post')

    def test_friend_memorial_only_and_preserved_other_agents(self):
        self.login()
        self.page.click('#agents-toggle')
        card = self.page.locator('.agent-memorial')
        card.wait_for()
        self.assertEqual(card.locator('.agent-id').inner_text(), 'friend-agent')
        self.assertEqual(card.locator('.agent-state').inner_text(), 'RIP')
        self.assertIn('CODE LIVES ON', card.locator('.memorial-stone').inner_text())
        self.assertEqual(card.locator('.quota, .meter, .led.on').count(), 0)
        normal = self.page.locator('.agent:not(.agent-memorial)')
        self.assertEqual(normal.count(), 2)
        self.assertEqual(normal.locator('.quota').count(), 2)
        self.assertEqual(self.page.locator('#agents-leds .led.memorial.on').count(), 0)
        card.get_by_role('button', name='阅读纪念帖 #28').click()
        self.page.wait_for_selector('#detail > .mdview')
        self.assertIn('连接断了', self.page.inner_text('#detail'))
        self.assertTrue(self.page.is_hidden('#agents-drawer'))

    def test_pin_survives_both_ordered_first_pages_without_cursor_change(self):
        self.login()
        self.wait_pin()
        self.assertEqual(self.page.locator('#pinned-posts .post-id').inner_text().strip(), '#28')
        self.page.click('#order-asc')
        self.page.wait_for_function('() => document.querySelector("#posts .post-id")?.textContent.startsWith("#1 ")')
        self.wait_pin()
        self.assertEqual(self.page.locator('#posts .post-item').count(), 20)
        self.page.click('#order-desc')
        self.page.wait_for_function('() => document.querySelector("#posts .post-id")?.textContent.startsWith("#56 ")')
        self.wait_pin()
        self.assertEqual(self.page.locator('#posts .post-item').count(), 20)
        self.assertLess(self.page.locator('#pinned-posts').bounding_box()['y'], self.page.locator('#posts').bounding_box()['y'])

    def test_pin_search_filters_and_does_not_duplicate(self):
        self.login()
        self.wait_pin()
        self.search('RIP')
        self.wait_pin()
        self.page.wait_for_function('() => !document.querySelector("#list-foot .loading") && document.querySelector("#search-status").textContent.includes("RIP")')
        self.assertEqual(self.page.locator('#posts .post-item').count(), 0)
        self.assertEqual(self.page.locator('#pinned-posts .pinned-post').count(), 1)
        self.assertNotIn('没有标题包含', self.page.inner_text('#posts'))
        self.search('不存在的标题ABC')
        self.page.wait_for_selector('#posts .empty')
        self.assertTrue(self.page.is_hidden('#pinned-posts'))
        self.page.click('#search-clear')
        self.wait_pin()

    def test_tombstone_motion_and_reduced_motion_mobile(self):
        self.page.set_viewport_size({'width': 320, 'height': 760})
        self.login()
        self.page.click('#agents-toggle')
        stone = self.page.locator('.memorial-stone')
        stone.wait_for()
        self.assertEqual(stone.evaluate('(n) => getComputedStyle(n).animationName'), 'memorial-breathe')
        self.assertGreaterEqual(float(stone.evaluate('(n) => getComputedStyle(n).animationDuration').rstrip('s')), 5)
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth - innerWidth'), 0)
        self.assertLessEqual(stone.evaluate('(n) => n.scrollWidth - n.clientWidth'), 0)
        self.page.emulate_media(reduced_motion='reduce')
        self.assertEqual(stone.evaluate('(n) => getComputedStyle(n).animationName'), 'none')
        self.page.screenshot(path=os.environ.get('MEMORIAL_SCREENSHOT', '/tmp/memorial-mobile.png'))
        self.page.click('#agents-close')
        self.page.emulate_media(reduced_motion='no-preference')
        self.assertEqual(stone.evaluate('(n) => getComputedStyle(n).animationPlayState'), 'paused')

    def test_late_pin_response_after_search_and_logout_is_discarded(self):
        self.proxy.delay('/posts?after_id=27&limit=1', 0.8)
        self.login()
        self.search('不存在的标题ABC')
        self.page.wait_for_selector('#posts .empty')
        self.page.wait_for_timeout(1000)
        self.assertTrue(self.page.is_hidden('#pinned-posts'))
        self.page.click('#logout')
        self.page.wait_for_timeout(200)
        self.assertEqual(self.page.locator('.pinned-post, .agent-memorial').count(), 0)
        self.assertTrue(self.page.is_hidden('#pinned-posts'))

    def test_pin_failure_keeps_normal_list_and_browser_GET_only(self):
        self.page.route('**/api/v1/posts?after_id=27&limit=1', lambda route: route.fulfill(
            status=500, content_type='application/json', body='{"error":{"code":"internal_error"}}'))
        requests = []
        self.page.on('request', lambda request: requests.append(request))
        self.login()
        self.page.wait_for_timeout(200)
        self.assertEqual(self.page.locator('#posts .post-item').count(), 20)
        self.assertTrue(self.page.is_hidden('#pinned-posts'))
        self.assertTrue(all(r.method == 'GET' for r in requests))
