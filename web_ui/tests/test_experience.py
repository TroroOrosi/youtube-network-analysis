"""The workspace stays usable through setup, collection, and report navigation."""

from html.parser import HTMLParser

from web_ui.tests.test_web import WebFixture


class PageStructure(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.headings = 0
        self.current = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "h1":
            self.headings += 1
        if tag == "a" and attributes.get("aria-current") == "page":
            self.current.append(attributes.get("href"))


class ExperienceTests(WebFixture):
    def test_switching_identity_clears_previous_workspace_selection(self):
        self.login("最初の担当")
        self.create_workspace()
        self.assertIn("yna_workspace", self.client.cookies)
        self.login("別の担当")
        self.assertNotIn("yna_workspace", self.client.cookies)
        self.assertIn("最初のワークスペースを作成", self.client.get("/").text)

    def test_setup_guides_the_owner_without_promising_real_data(self):
        self.login()
        self.create_workspace()
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("チャンネルを接続", page.text)
        self.assertIn('href="/audience-network?demo=true"', page.text)
        self.assertIn("接続中チャンネルの結果ではありません", page.text)
        structure = PageStructure(page.text)
        self.assertEqual(structure.headings, 1)
        self.assertIn("/", structure.current)

    def test_connected_channel_links_to_its_own_analysis(self):
        self.login()
        self.create_workspace()
        self.connect_channel()
        page = self.client.get("/")
        self.assertIn("動画とコメント", page.text)
        self.assertIn("登録者分析を見る", page.text)
        self.assertIn("視聴者ネットワークを見る", page.text)
        self.assertIn("接続の管理", page.text)
        self.assertIn("コメント収集後", page.text)

    def test_demo_report_keeps_source_and_navigation_visible(self):
        self.login()
        self.create_workspace()
        page = self.client.get("/audience-network?demo=true")
        self.assertEqual(page.status_code, 200)
        self.assertIn("この結果の読み方", page.text)
        self.assertIn("参考デモ", page.text)
        self.assertIn('href="#top-channels"', page.text)
        self.assertEqual(PageStructure(page.text).headings, 1)
        self.assertIn("/audience-network", PageStructure(page.text).current)

    def test_stylesheet_is_self_hosted_and_has_security_headers(self):
        page = self.client.get("/login")
        self.assertIn('href="/assets/workspace.css"', page.text)
        response = self.client.get("/assets/workspace.css")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/css", response.headers["content-type"])
        self.assertIn("nosniff", response.headers["x-content-type-options"])
        self.assertIn("@media", response.text)
