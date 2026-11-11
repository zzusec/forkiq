"""Web layer tests with FastAPI TestClient and a stub GitHub client."""
import json
import hashlib
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import engine
from app.github import RefNotFastForward

TEST_PASSWORD = "correct-horse-8"


class RenderedHTML(HTMLParser):
    """Inspect rendered form ownership and labels without browser dependencies."""
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                 "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.stack = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        element = {"tag": tag, "attrs": dict(attrs), "parents": tuple(self.stack), "text": ""}
        self.elements.append(element)
        if tag not in self.VOID_TAGS:
            self.stack.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        for element in self.stack:
            element["text"] += data

    def find(self, tag, **attrs):
        return [element for element in self.elements if element["tag"] == tag
                and all(element["attrs"].get(key) == value for key, value in attrs.items())]

    def inside(self, parent, tag, **attrs):
        return [element for element in self.find(tag, **attrs)
                if any(ancestor is parent for ancestor in element["parents"])]

    def labels_for(self, field):
        return [label for label in self.find("label") if label["text"].strip()
                and (any(ancestor is label for ancestor in field["parents"])
                     or (field["attrs"].get("id")
                         and label["attrs"].get("for") == field["attrs"]["id"]))]


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        import os
        os.environ["FORKIQ_STATE_DIR"] = cls.tmp.name
        os.environ["FORKIQ_PEM_PATH"] = str(Path(cls.tmp.name) / "app.pem")
        from app import main as main_module
        cls.main = main_module
        cls.main.STATE_DIR = Path(cls.tmp.name)
        cls.main.PEM_PATH = Path(cls.tmp.name) / "app.pem"
        cls.main.db = main_module.DB(Path(cls.tmp.name) / "forkiq.db")

    def setUp(self):
        public_url_env = mock.patch.dict("os.environ", {"FORKIQ_PUBLIC_URL": "https://forkiq.example"})
        public_url_env.start()
        self.addCleanup(public_url_env.stop)
        self.main.db.run("DELETE FROM sessions")
        self.main.db.run("DELETE FROM repositories")
        self.main.db.run("DELETE FROM repository_backups")
        self.main.db.run("DELETE FROM events")
        self.main.db.run("DELETE FROM settings")
        self.main._login_failures.clear()
        self.client = self._test_client()

    @classmethod
    def _test_client(cls):
        from fastapi.testclient import TestClient
        return TestClient(cls.main.app, follow_redirects=False)

    def setup_admin(self):
        self.client.post("/setup", data={"password": TEST_PASSWORD, "password2": TEST_PASSWORD})
        self.client.post("/login", data={"password": TEST_PASSWORD})
        session = self.main.db.one("SELECT token FROM sessions ORDER BY created_at DESC LIMIT 1")
        return session["token"]

    def csrf(self, token):
        return self.main.csrf_for(token)

    def rendered_page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return response, RenderedHTML(response.text)

    def assert_post_form(self, page, action, token=None):
        forms = page.find("form", action=action)
        self.assertEqual(len(forms), 1, action)
        form = forms[0]
        self.assertEqual(form["attrs"].get("method", "get").lower(), "post", action)
        if token is not None:
            csrf_fields = page.inside(form, "input", name="csrf")
            self.assertEqual(len(csrf_fields), 1, action)
            self.assertEqual(csrf_fields[0]["attrs"].get("type"), "hidden")
            self.assertEqual(csrf_fields[0]["attrs"].get("value"), self.csrf(token))
        return form

    def assert_page_accessibility(self, page):
        self.assertEqual(len(page.find("main", id="main-content")), 1)
        self.assertTrue(page.find("a", href="#main-content"))
        self.assertTrue(any(urlsplit(script["attrs"].get("src", "")).path == "/static/app.js"
                            for script in page.find("script")))

    def test_login_renders_password_only_with_accessible_label(self):
        self.client.post("/setup", data={"password": TEST_PASSWORD, "password2": TEST_PASSWORD})
        response, page = self.rendered_page("/login")
        self.assert_page_accessibility(page)
        form = self.assert_post_form(page, "/login")
        fields = page.inside(form, "input")
        self.assertEqual([field["attrs"].get("name") for field in fields], ["password"])
        password = fields[0]
        self.assertEqual(password["attrs"].get("type"), "password")
        self.assertEqual(password["attrs"].get("autocomplete"), "current-password")
        self.assertFalse(password["attrs"].get("value"))
        self.assertTrue(any("管理员密码" in label["text"] for label in page.labels_for(password)))
        self.assertNotIn(TEST_PASSWORD, response.text)

    def test_shell_pages_render_current_navigation_and_preserve_actions(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        self.main.db.set_setting("github_app_slug", "forkiq-test")
        routes = [("/", "/", ["/discover"]),
                  ("/repos", "/repos", ["/repos/add"]),
                  (f"/repos/{repo_id}", "/repos",
                   [f"/repos/{repo_id}/{action}" for action in
                    ("sync", "mode", "selfupdate", "pause", "actions", "remove")]),
                  ("/settings", "/settings",
                   ["/settings", "/settings/password", "/settings/disconnect"])]
        for path, active_path, actions in routes:
            with self.subTest(path=path):
                _, page = self.rendered_page(path)
                self.assert_page_accessibility(page)
                current = [link for link in page.find("a", **{"aria-current": "page"})
                           if any(parent["tag"] == "nav" for parent in link["parents"])]
                self.assertEqual([link["attrs"].get("href") for link in current], [active_path])
                self.assert_post_form(page, "/logout")
                for action in actions:
                    self.assert_post_form(page, action, token)

    def test_repository_list_labels_url_and_wraps_table(self):
        self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo",
                                    status="blocked")
        self.main.db.run("UPDATE repositories SET last_error=? WHERE id=?",
                         ("GitHub denied <unsafe>", repo_id))
        response, page = self.rendered_page("/repos?filter=blocked")
        form = self.assert_post_form(page, "/repos/add")
        fields = page.inside(form, "input", name="url")
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0]["attrs"].get("type"), "url")
        self.assertTrue(page.labels_for(fields[0]))
        self.assertTrue(page.find("a", href=f"/repos/{repo_id}"))
        tables = page.find("table")
        self.assertTrue(tables)
        for table in tables:
            self.assertTrue(any("table-wrap" in parent["attrs"].get("class", "").split()
                                for parent in table["parents"]))
        self.assertIn("GitHub denied &lt;unsafe&gt;", response.text)
        self.assertNotIn("<unsafe>", response.text)

    def test_repository_list_and_refreshed_rows_offer_csrf_protected_sync_controls(self):
        token = self.setup_admin()
        from app.db import insert_repository
        active_id = insert_repository(self.main.db, owner="me", name="active",
                                      full_name="me/active", upstream_full_name="up/active")
        paused_id = insert_repository(self.main.db, owner="me", name="paused",
                                      full_name="me/paused", upstream_full_name="up/paused",
                                      status="paused")
        disabled_id = insert_repository(self.main.db, owner="me", name="disabled",
                                        full_name="me/disabled", upstream_full_name="up/disabled")
        self.main.db.run("UPDATE repositories SET enabled=0 WHERE id IN (?,?)",
                         (paused_id, disabled_id))
        for path in ("/repos", "/partials/repos"):
            with self.subTest(path=path):
                _, page = self.rendered_page(path)
                self.assertTrue(page.find("th")[-1]["text"].strip())
                for repo_id, name, action in ((active_id, "active", "pause"),
                                              (paused_id, "paused", "resume"),
                                              (disabled_id, "disabled", "resume")):
                    form = self.assert_post_form(page, f"/repos/{repo_id}/{action}", token)
                    self.assertEqual(page.inside(form, "input", name="return_filter")[0]["attrs"]["value"], "all")
                    button = page.inside(form, "button")[0]
                    self.assertIn(f"me/{name}", button["attrs"].get("aria-label", ""))
                    self.assertIn("暂停" if action == "pause" else "恢复", button["text"])
                    self.assertTrue(any(parent["tag"] == "td" for parent in form["parents"]))
                    self.assertFalse(any("repo-pair" in parent["attrs"].get("class", "").split()
                                         for parent in form["parents"]))
                    self.assertFalse(page.find("form", action=f"/repos/{repo_id}/{'resume' if action == 'pause' else 'pause'}"))

    def test_full_and_refreshed_pause_resume_forms_submit_with_the_current_filter(self):
        self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo",
                                    status="blocked")
        for surface in ("/repos", "/partials/repos"):
            self.main.db.run("UPDATE repositories SET status='blocked', enabled=1 WHERE id=?", (repo_id,))
            for action, filter_name, expected_status, expected_enabled in (
                    ("pause", "attention", "paused", 0), ("resume", "paused", "pending", 1),
                    ("pause", "", "paused", 0), ("resume", "", "pending", 1)):
                with self.subTest(surface=surface, action=action, filter=filter_name):
                    _, page = self.rendered_page(f"{surface}?filter={filter_name}")
                    form = self.assert_post_form(page, f"/repos/{repo_id}/{action}")
                    fields = {field["attrs"]["name"]: field["attrs"]["value"]
                              for field in page.inside(form, "input")}
                    self.assertEqual(fields["return_filter"], filter_name or "all")
                    response = self.client.post(form["attrs"]["action"], data=fields)
                    self.assertEqual(response.status_code, 302)
                    target = urlsplit(response.headers["location"])
                    self.assertEqual(target.path, "/repos")
                    self.assertEqual(parse_qs(target.query, keep_blank_values=True)["filter"], [filter_name])
                    self.assertEqual(target.fragment, f"repo-{repo_id}")
                    row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
                    self.assertEqual((row["status"], row["enabled"]), (expected_status, expected_enabled))

    def test_repository_action_returns_to_every_supported_list_filter(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        for filter_name in ("all", "synced", "waiting", "attention", "paused", "diverged", "blocked"):
            for action in ("pause", "resume"):
                with self.subTest(filter=filter_name, action=action):
                    response = self.client.post(f"/repos/{repo_id}/{action}", data={
                        "csrf": self.csrf(token), "return_filter": filter_name})
                    self.assertEqual(response.status_code, 302)
                    target = urlsplit(response.headers["location"])
                    self.assertEqual((target.scheme, target.netloc, target.path), ("", "", "/repos"))
                    query = parse_qs(target.query, keep_blank_values=True)
                    self.assertEqual(query["filter"], ["" if filter_name == "all" else filter_name])
                    self.assertIn("me/demo", query["notice"][0])
                    self.assertIn("已暂停" if action == "pause" else "已恢复", query["notice"][0])
                    self.assertEqual(target.fragment, f"repo-{repo_id}")
                    result, page = self.rendered_page(response.headers["location"])
                    self.assertTrue(page.find("div", role="status"))
                    self.assertIn(query["notice"][0], result.text)

    def test_repository_action_rejects_untrusted_return_destinations(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        for value in ("https://evil.example", "//evil.example", "\\\\evil.example",
                      "/repos?filter=paused", "paused&notice=forged", "paused\r\nLocation: https://evil.example",
                      "unknown", "<script>alert(1)</script>"):
            with self.subTest(value=value):
                response = self.client.post(f"/repos/{repo_id}/pause", data={
                    "csrf": self.csrf(token), "return_filter": value,
                    "return_to": "https://evil.example"})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["location"], f"/repos/{repo_id}")
        for action in ("pause", "resume", "sync"):
            with self.subTest(detail_action=action):
                response = self.client.post(f"/repos/{repo_id}/{action}", data={"csrf": self.csrf(token)})
                self.assertEqual(response.headers["location"], f"/repos/{repo_id}")
        response = self.client.post(f"/repos/{repo_id}/sync", data={
            "csrf": self.csrf(token), "return_filter": "paused"})
        self.assertEqual(response.headers["location"], f"/repos/{repo_id}")

    def test_list_sync_controls_require_login_csrf_and_existing_repository(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        before = dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,)))
        for action in ("pause", "resume"):
            for csrf in (None, "invalid"):
                with self.subTest(action=action, csrf=csrf):
                    data = {"return_filter": "paused"}
                    if csrf is not None:
                        data["csrf"] = csrf
                    response = self.client.post(f"/repos/{repo_id}/{action}", data=data)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))), before)
            response = self.client.post(f"/repos/{repo_id + 9999}/{action}", data={
                "csrf": self.csrf(token), "return_filter": "paused"})
            self.assertEqual(response.status_code, 404)
        self.assertEqual(self.main.db.all("SELECT * FROM events WHERE repo_id=?", (repo_id,)), [])
        self.client.cookies.clear()
        response = self.client.post(f"/repos/{repo_id}/pause", data={
            "csrf": self.csrf(token), "return_filter": "paused"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/login")
        self.assertEqual(dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))), before)

    def test_overview_links_match_grouped_repository_lists(self):
        self.setup_admin()
        from app.db import insert_repository
        ids = {}
        for state in ("synced", "behind", "pending", "blocked", "diverged",
                      "needs_attention", "error", "paused", "missing"):
            ids[state] = insert_repository(self.main.db, owner="me", name=state,
                                          full_name=f"me/{state}", upstream_full_name=f"up/{state}",
                                          status=state)
        self.main.db.run("UPDATE repositories SET last_error='old error' WHERE status='paused'")
        _, page = self.rendered_page("/")
        stats = page.find("section", id="stats-region")[0]
        links = page.inside(stats, "a")
        expected = [("/repos", set(ids), "受管仓库"),
                    ("/repos?filter=synced", {"synced"}, "已同步"),
                    ("/repos?filter=waiting", {"behind", "pending"}, "待同步"),
                    ("/repos?filter=attention", {"blocked", "diverged", "needs_attention", "error", "missing"}, "需关注"),
                    ("/repos?filter=paused", {"paused"}, "已暂停")]
        self.assertEqual(len(links), len(expected))
        numbers = [int(page.inside(link, "span", **{"class": "num"})[0]["text"]) for link in links]
        self.assertEqual(numbers[0], sum(numbers[1:]))
        for link, (href, states, label) in zip(links, expected):
            with self.subTest(href=href):
                self.assertEqual(link["attrs"]["href"], href)
                self.assertIn(label, link["text"])
                self.assertEqual(page.inside(link, "span", **{"class": "num"})[0]["text"], str(len(states)))
                full, listing = self.rendered_page(href)
                rows = {int(urlsplit(a["attrs"]["href"]).path.rsplit("/", 1)[1])
                        for a in listing.find("a")
                        if a["attrs"].get("href", "").startswith("/repos/")
                        and not urlsplit(a["attrs"]["href"]).fragment}
                self.assertEqual(rows, {ids[state] for state in states})
                partial = self.client.get(href.replace("/repos", "/partials/repos", 1))
                self.assertEqual(partial.status_code, 200)
                for state in ids:
                    marker = f'href="/repos/{ids[state]}"'
                    self.assertEqual(marker in partial.text, state in states)
                self.assertIn('data-revision="', full.text)
        for state in ("diverged", "blocked", "error"):
            _, listing = self.rendered_page(f"/repos?filter={state}")
            self.assertTrue(listing.find("a", href=f"/repos/{ids[state]}"))
            self.assertEqual(len(listing.find("table")), 1)

    def test_empty_filter_keeps_a_revision_and_clear_filter_link(self):
        self.setup_admin()
        for query in ("attention", "waiting", "not-a-status", "' OR 1=1 --"):
            with self.subTest(query=query):
                response = self.client.get("/partials/repos", params={"filter": query})
                self.assertEqual(response.status_code, 200)
                page = RenderedHTML(response.text)
                self.assertIn("没有符合筛选条件的仓库", response.text)
                self.assertTrue(page.find("a", href="/repos"))
                revision = [node["attrs"]["data-revision"] for node in page.elements
                            if "data-revision" in node["attrs"]][0]
                self.assertEqual(self.client.get("/partials/repos", params={"filter": query, "v": revision}).status_code, 204)
        _, overview = self.rendered_page("/")
        self.assertEqual(len(overview.inside(overview.find("section", id="stats-region")[0], "a")), 5)
        self.assertIn("还没有受管仓库", self.client.get("/repos").text)

    def test_repository_errors_are_full_text_dialog_data_outside_the_refresh_region(self):
        self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="broken",
                                    full_name="me/broken", upstream_full_name="up/broken", status="diverged")
        message = '中文错误 <script>alert("unsafe")</script> & "quoted"\n' + "X" * 3000
        self.main.db.run("UPDATE repositories SET last_error=? WHERE id=?", (message, repo_id))
        response, page = self.rendered_page("/repos")
        trigger = page.find("a", href=f"/repos/{repo_id}#repo-error")[0]
        self.assertEqual(trigger["attrs"]["data-error-message"], message)
        self.assertEqual(trigger["attrs"]["data-error-repo"], "me/broken")
        self.assertEqual(trigger["attrs"]["data-error-status"], "存在分叉")
        self.assertEqual(trigger["text"], "查看详情")
        self.assertNotIn('<script>alert("unsafe")</script>', response.text)
        self.assertEqual(len(page.find("dialog", id="error-dialog")), 1)
        dialog = page.find("dialog", id="error-dialog")[0]
        self.assertFalse(any(parent["attrs"].get("id") == "repo-table-region" for parent in dialog["parents"]))
        self.assertFalse(page.find("div", **{"class": "err-hint"}))
        _, detail = self.rendered_page(f"/repos/{repo_id}")
        self.assertEqual(detail.find("dd", id="repo-error")[0]["text"], message)
        partial = self.client.get("/partials/repos")
        self.assertIn("data-error-message=", partial.text)
        self.assertNotIn("<dialog", partial.text)

    def test_known_errors_show_guidance_and_preserve_old_event_payloads(self):
        self.setup_admin()
        from app.db import insert_repository, log_event
        repo_id = insert_repository(self.main.db, owner="me", name="broken",
                                    full_name="me/broken", upstream_full_name="up/broken", status="error")
        message = "me/broken 强制对齐失败：{'message': 'Object does not exist', 'status': '422', 'extra': '<unsafe>'}"
        self.main.db.run("UPDATE repositories SET last_error=? WHERE id=?", (message, repo_id))
        log_event(self.main.db, repo_id, "error", message)
        for path in ("/", "/partials/overview", f"/repos/{repo_id}"):
            with self.subTest(path=path):
                response, page = self.rendered_page(path)
                summaries = page.find("p", **{"class": "error-summary"})
                self.assertTrue(summaries)
                self.assertTrue(all("GitHub" in item["text"] for item in summaries))
                self.assertTrue(page.find("details", **{"class": "technical-details"}))
                for details in page.find("details", **{"class": "technical-details"}):
                    self.assertNotIn("open", details["attrs"])
                    self.assertEqual(page.inside(details, "pre")[0]["text"], message)
                self.assertNotIn("<unsafe>", response.text)
                self.assertIn("10 分钟", response.text)
        _, listing = self.rendered_page("/repos")
        link = listing.find("a", href=f"/repos/{repo_id}#repo-error")[0]
        self.assertEqual(link["attrs"]["data-error-message"], message)
        self.assertIn("GitHub", link["attrs"]["data-error-summary"])
        self.assertIn("10 分钟", link["attrs"]["data-error-action"])
        self.assertEqual(self.main.db.one("SELECT message FROM events WHERE repo_id=?", (repo_id,))["message"], message)

    def test_manual_import_explains_automatic_discovery_and_github_authorization(self):
        self.setup_admin()
        for enabled in ("0", "1"):
            self.main.db.set_setting("auto_discover", enabled)
            response, page = self.rendered_page("/repos")
            help_text = page.find("p", id="fork-add-help")[0]["text"]
            self.assertIn("不会在 GitHub 创建仓库", help_text)
            self.assertIn("加入后安排同步", help_text)
            self.assertIn("已获 App 授权", help_text)
            self.assertIn("自动发现已开启" if enabled == "1" else "自动发现已关闭", response.text)
            if enabled == "1":
                self.assertIn("60 分钟", response.text)
            self.assertTrue(page.find("form", action="/discover"))
            self.assertTrue(page.find("input", **{"aria-describedby": "fork-add-help"}))

    def test_timestamps_render_short_and_keep_the_machine_readable_value(self):
        self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        self.main.db.run("UPDATE repositories SET last_check_at=?, last_success_at=? WHERE id=?",
                         ("2026-10-15T03:55:19+00:00", "2026-10-15T03:55:19+00:00", repo_id))
        self.main.db.run("INSERT INTO events(repo_id, at, kind, message) VALUES(?,?,?,?)",
                         (repo_id, "2026-10-15T03:55:19+00:00", "synced", "me/demo 快进到 abc123"))
        for path in ("/", "/repos", f"/repos/{repo_id}"):
            with self.subTest(path=path):
                response, page = self.rendered_page(path)
                stamps = [element for element in page.find("time")
                          if "ts" in element["attrs"].get("class", "").split()]
                self.assertTrue(stamps, path)
                self.assertTrue(any(element["attrs"].get("datetime") == "2026-10-15T03:55:19+00:00"
                                    for element in stamps), path)
                self.assertTrue(any(element["text"] == "10-15 03:55" for element in stamps), path)
                # The raw ISO string must never be the visible text.
                self.assertNotIn(">2026-10-15T03:55:19+00:00<", response.text)

    def test_repository_and_upstream_names_link_to_github(self):
        self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        self.main.db.run("INSERT INTO events(repo_id, at, kind, message) VALUES(?,?,?,?)",
                         (repo_id, "2026-01-01T00:00:00+00:00", "synced", "me/demo 已与上游一致"))
        # The upstream name is a GitHub link everywhere it is shown.
        for path in ("/repos", f"/repos/{repo_id}"):
            with self.subTest(path=path, target="up/demo"):
                _, page = self.rendered_page(path)
                links = page.find("a", href="https://github.com/up/demo")
                self.assertEqual(len(links), 1, path)
                self.assertEqual(links[0]["attrs"].get("target"), "_blank")
                self.assertIn("noopener", links[0]["attrs"].get("rel", ""))
        # On the overview event log the repository column also links to GitHub.
        _, page = self.rendered_page("/")
        repo_links = page.find("a", href="https://github.com/me/demo")
        self.assertEqual(len(repo_links), 1)
        self.assertEqual(repo_links[0]["attrs"].get("target"), "_blank")
        # The repository list keeps its internal detail link and adds GitHub links.
        _, page = self.rendered_page("/repos")
        self.assertTrue(page.find("a", href=f"/repos/{repo_id}"))
        fork_links = page.find("a", href="https://github.com/me/demo")
        self.assertEqual(len(fork_links), 1)
        self.assertEqual(fork_links[0]["attrs"].get("target"), "_blank")
        # The detail page exposes both names.
        _, page = self.rendered_page(f"/repos/{repo_id}")
        self.assertTrue(page.find("a", href="https://github.com/me/demo"))
        self.assertTrue(page.find("a", href="https://github.com/up/demo"))

    def test_github_links_omit_missing_names(self):
        self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name=None)
        for path in ("/repos", f"/repos/{repo_id}"):
            with self.subTest(path=path):
                response, page = self.rendered_page(path)
                # The fork itself still links; only the missing upstream is a dash.
                self.assertEqual(len(page.find("a", href="https://github.com/me/demo")), 1)
                self.assertFalse([link for link in page.find("a")
                                  if link["attrs"].get("href") == "https://github.com/None"])
                self.assertIn("—", response.text)

    def test_repository_versions_show_tag_short_sha_and_date(self):
        self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        self.main.db.run(
            "UPDATE repositories SET fork_sha=?, upstream_sha=?, fork_tag=?, upstream_tag=?, "
            "fork_commit_at=?, upstream_commit_at=? WHERE id=?",
            ("a" * 40, "b" * 40, "v1.2.3", "v9.9.9",
             "2026-01-02T03:04:05Z", "2026-02-03T04:05:06Z", repo_id))
        for path in ("/repos", f"/repos/{repo_id}"):
            with self.subTest(path=path):
                response, page = self.rendered_page(path)
                # Both sides expose tag, a readable 12-char SHA and commit date.
                for tag, sha, date in (("v1.2.3", "a" * 12, "2026-01-02"),
                                       ("v9.9.9", "b" * 12, "2026-02-03")):
                    self.assertIn(tag, response.text)
                    self.assertIn(sha, response.text)
                    self.assertIn(date, response.text)
                # The full 40-char SHA stays out of the rendered text; it is only
                # offered in a tooltip so it can be pasted into GitHub.
                self.assertFalse([element for element in page.elements
                                  if "a" * 40 in element["text"]])

    def test_repository_list_pairs_local_and_upstream_versions_with_a_verdict(self):
        self.setup_admin()
        from app.db import insert_repository
        same = insert_repository(self.main.db, owner="me", name="same",
                                 full_name="me/same", upstream_full_name="up/same")
        behind = insert_repository(self.main.db, owner="me", name="behind",
                                   full_name="me/behind", upstream_full_name="up/behind",
                                   status="behind")
        ahead = insert_repository(self.main.db, owner="me", name="ahead",
                                  full_name="me/ahead", upstream_full_name="up/ahead")
        self.main.db.run("UPDATE repositories SET fork_sha=?, upstream_sha=? WHERE id=?",
                         ("c" * 40, "c" * 40, same))
        self.main.db.run("UPDATE repositories SET fork_sha=?, upstream_sha=? WHERE id=?",
                         ("d" * 40, "e" * 40, behind))
        # A manual fork that already contains upstream plus its own commits: the
        # label names the reason instead of leaving "不一致" unexplained.
        self.main.db.run("UPDATE repositories SET fork_sha=?, upstream_sha=?, "
                         "last_merged_upstream_sha=?, upstream_ahead=0, fork_ahead=15 "
                         "WHERE id=?",
                         ("f" * 40, "b" * 40, "b" * 40, ahead))
        response, page = self.rendered_page("/repos")
        # Each version line names its side so the two short SHAs can be matched.
        sides = [element["text"].strip() for element in page.find("span", **{"class": "version-side"})]
        self.assertEqual(sides.count("本地"), 3)
        self.assertEqual(sides.count("上游"), 3)
        # A shared commit reads as one version; a differing pair names the upstream SHA.
        self.assertIn("同一版本", response.text)
        self.assertIn("c" * 12, response.text)
        self.assertIn("落后上游", response.text)
        self.assertIn("e" * 12, response.text)
        self.assertIn("本地领先 15 个提交", response.text)
        own_commits = page.find("span", **{"class": "verdict v-contains"})
        self.assertEqual(len(own_commits), 1)
        self.assertIn("无需更新", own_commits[0]["attrs"].get("title", ""))
        # The verdict is rendered once per row and carries the full SHA in its tooltip.
        verdicts = page.find("span", **{"class": "verdict v-same"})
        self.assertEqual(len(verdicts), 1)
        self.assertIn("c" * 40, verdicts[0]["attrs"].get("title", ""))

    def test_repository_row_shares_one_grid_across_local_and_upstream(self):
        """两侧版本行同层：名称/版本两行由同一个列优先网格共用。"""
        self.setup_admin()
        from app.db import insert_repository
        insert_repository(self.main.db, owner="me", name="demo",
                          full_name="me/demo", upstream_full_name="up/demo", discovered=1)
        self.main.db.run("UPDATE repositories SET mode='manual', self_update_interval=1")
        _, page = self.rendered_page("/repos")
        # 手工 / 自更新 / 自动发现这些模式标记只属于仓库详情页，列表里不再重复。
        self.assertEqual(page.find("span", **{"class": "tag"}), [])
        # 前两列合并成一个单元格，网格子项必须按「本地两项 + 上游两项」排列。
        cell = page.find("td", colspan="2")
        self.assertEqual(len(cell), 1)
        pairs = page.inside(cell[0], "div", **{"class": "repo-pair"})
        self.assertEqual(len(pairs), 1)
        children = [element for element in page.elements
                    if element["parents"] and element["parents"][-1] is pairs[0]]
        self.assertEqual([[part for part in child["attrs"].get("class", "").split()]
                          for child in children],
                         [["pair-name", "pair-local"], ["version-line", "pair-local"],
                          ["pair-name", "pair-upstream", "muted"], ["version-line", "pair-upstream"]])
        css = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.css").read_text(encoding="utf-8")
        rule = css.split(".repo-pair {", 1)[1].split("}", 1)[0]
        self.assertIn("grid-auto-flow: column", rule)
        self.assertIn("grid-template-rows: auto auto", rule)

    def test_version_lines_keep_the_tag_slot_when_a_side_has_no_tag(self):
        """某侧没有可达标签时保留槽位，两侧版本行保持同一形状。"""
        self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        self.main.db.run("UPDATE repositories SET fork_sha=?, upstream_sha=?, upstream_tag=?, "
                         "fork_commit_at=?, upstream_commit_at=? WHERE id=?",
                         ("a" * 40, "b" * 40, "v9.9.9",
                          "2026-01-02T03:04:05Z", "2026-02-03T04:05:06Z", repo_id))
        response, page = self.rendered_page("/repos")
        missing = page.find("span", **{"class": "tag tag-version tag-missing"})
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["text"].strip(), "—")
        self.assertIn("没有可达的版本标签", missing[0]["attrs"].get("title", ""))
        # 占位不替代 SHA 与日期：两侧仍可逐项比对。
        for text in ("v9.9.9", "a" * 12, "b" * 12, "2026-01-02", "2026-02-03"):
            self.assertIn(text, response.text)

    def test_partials_render_rows_without_the_shell(self):
        self.setup_admin()
        from app.db import insert_repository
        insert_repository(self.main.db, owner="me", name="demo",
                          full_name="me/demo", upstream_full_name="up/demo")
        for path, marker in (("/partials/repos", "<table"), ("/partials/overview", "受管仓库")):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(marker, response.text)
                # A fragment never re-sends the shell, CSS or JS.
                self.assertNotIn("<aside", response.text)
                self.assertNotIn("app.css", response.text)
        filtered = self.client.get("/partials/repos?filter=blocked")
        self.assertEqual(filtered.status_code, 200)
        self.assertIn("没有符合筛选条件的仓库", filtered.text)

    def test_full_page_stamps_the_revision_the_refresh_sends_back(self):
        self.setup_admin()
        from app.db import insert_repository
        insert_repository(self.main.db, owner="me", name="demo",
                          full_name="me/demo", upstream_full_name="up/demo")
        # The full page and the fragment must agree, otherwise every tick would
        # re-fetch the whole row set even when nothing changed.
        _, page = self.rendered_page("/repos")
        stamped = [element["attrs"]["data-revision"] for element in page.elements
                   if element["attrs"].get("data-revision")]
        self.assertEqual(len(stamped), 1)
        self.assertEqual(self.client.get(f"/partials/repos?v={stamped[0]}").status_code, 204)
        _, overview = self.rendered_page("/")
        overview_stamp = [element["attrs"]["data-revision"] for element in overview.elements
                          if element["attrs"].get("data-revision")]
        self.assertEqual(len(overview_stamp), 1)
        self.assertEqual(self.client.get(f"/partials/overview?v={overview_stamp[0]}").status_code, 204)

    def test_partials_require_a_session(self):
        self.setup_admin()
        self.client.cookies.clear()
        for path in ("/partials/repos", "/partials/overview"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)

    def test_unchanged_fragment_is_skipped_via_its_revision_token(self):
        self.setup_admin()
        from app.db import insert_repository
        insert_repository(self.main.db, owner="me", name="demo",
                          full_name="me/demo", upstream_full_name="up/demo")
        first = self.client.get("/partials/repos")
        # The revision is stamped into the markup the browser will send back.
        import re
        match = re.search(r'data-revision="([^"]+)"', first.text)
        self.assertTrue(match, first.text[:200])
        revision = match.group(1)
        again = self.client.get(f"/partials/repos?v={revision}")
        self.assertEqual(again.status_code, 204)
        self.assertEqual(again.content, b"")
        # A changed row must invalidate the stored fragment.
        self.main.db.run("UPDATE repositories SET status='blocked', last_error='boom'")
        changed = self.client.get(f"/partials/repos?v={revision}")
        self.assertEqual(changed.status_code, 200)
        self.assertIn("boom", changed.text)
        self.assertNotIn(f'data-revision="{revision}"', changed.text)

    def test_unchanged_fragments_skip_context_construction(self):
        self.setup_admin()
        for path, revision, builders in (
            ("/partials/repos", self.main._repos_revision(""), ("_repo_rows", "annotate")),
            ("/partials/overview", self.main._overview_revision(), ("_overview_context",)),
        ):
            with self.subTest(path=path):
                with mock.patch.object(self.main, builders[0], side_effect=AssertionError("built unchanged context")):
                    if len(builders) > 1:
                        with mock.patch.object(self.main, builders[1], side_effect=AssertionError("annotated unchanged rows")):
                            response = self.client.get(path, params={"v": revision})
                    else:
                        response = self.client.get(path, params={"v": revision})
                self.assertEqual(response.status_code, 204)
                self.assertEqual(response.headers["cache-control"], "no-store")
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/partials/repos", params={"v": self.main._repos_revision("")}).status_code, 401)

    def test_revisions_track_nonmax_row_errors_and_overview_settings(self):
        self.setup_admin()
        from app.db import insert_repository
        ids = [insert_repository(self.main.db, owner="me", name=f"repo-{i}",
                                 full_name=f"me/repo-{i}", upstream_full_name=f"up/repo-{i}", status="error")
               for i in range(2)]
        self.main.db.run("UPDATE repositories SET updated_at='2026-10-15T00:00:00+00:00'")
        revision = self.main._repos_revision("attention")
        overview_revision = self.main._overview_revision()
        self.main.db.run("UPDATE repositories SET last_error='new diagnosis' WHERE id=?", (ids[0],))
        self.assertNotEqual(revision, self.main._repos_revision("attention"))
        self.assertEqual(self.client.get("/partials/repos", params={"filter": "attention", "v": revision}).status_code, 200)
        self.assertNotEqual(overview_revision, self.main._overview_revision())
        for key, value in (("auth_error", "installation expired"), ("github_installation_id", "123")):
            before = self.main._overview_revision()
            self.main.db.set_setting(key, value)
            self.assertNotEqual(before, self.main._overview_revision())
            self.assertEqual(self.client.get("/partials/overview", params={"v": before}).status_code, 200)
        self.assertNotEqual(self.main._repos_revision("waiting"), self.main._repos_revision("attention"))

    def test_enabled_change_invalidates_the_repository_controls_fragment(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        first = self.client.get("/partials/repos")
        revision = first.headers["x-forkiq-revision"]
        self.main.db.run("UPDATE repositories SET enabled=0 WHERE id=?", (repo_id,))
        response = self.client.get("/partials/repos", params={"v": revision})
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.headers["x-forkiq-revision"], revision)
        page = RenderedHTML(response.text)
        self.assert_post_form(page, f"/repos/{repo_id}/resume", token)
        self.assertFalse(page.find("form", action=f"/repos/{repo_id}/pause"))

    def test_static_asset_cache_requires_the_current_content_hash(self):
        self.setup_admin()
        for name in ("app.css", "app.js"):
            path = f"/static/{name}"
            response = self.client.get(path, params={"v": self.main.ASSET_VERSION})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "public, max-age=31536000, immutable")
            for query in (None, "old-version"):
                response = self.client.get(path, params={"v": query} if query else {})
                self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertEqual(self.client.get("/static/icons.svg", params={"v": self.main.ASSET_VERSION}).headers["cache-control"], "no-cache")
        self.assertNotIn("immutable", self.client.get("/repos").headers.get("cache-control", ""))

    def test_static_assets_use_content_hash_cache_buster(self):
        self.setup_admin()
        response, page = self.rendered_page("/repos")
        expected = self.main.ASSET_VERSION
        self.assertEqual(len(expected), 10)
        for element, attr in (("link", "href"), ("script", "src")):
            urls = [item["attrs"].get(attr, "") for item in page.find(element)]
            asset = [url for url in urls if "/static/app." in url]
            self.assertTrue(asset, element)
            self.assertTrue(any(url.endswith(f"?v={expected}") for url in asset), asset)

    def test_repository_detail_preserves_paused_actions_and_hidden_enabled(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo",
                                    status="paused")
        self.main.db.run("UPDATE repositories SET enabled=0, actions_disabled=1, "
                         "mode='manual', self_update_interval=1 WHERE id=?", (repo_id,))
        _, page = self.rendered_page(f"/repos/{repo_id}")
        self.assert_post_form(page, f"/repos/{repo_id}/resume", token)
        self.assertFalse(page.find("form", action=f"/repos/{repo_id}/pause"))
        actions_form = self.assert_post_form(page, f"/repos/{repo_id}/actions", token)
        self.assertEqual(page.inside(actions_form, "input", name="enabled")[0]["attrs"]["value"], "1")
        self_update_form = self.assert_post_form(page, f"/repos/{repo_id}/selfupdate", token)
        self.assertIn("关闭自更新", self_update_form["text"])
        remove_form = self.assert_post_form(page, f"/repos/{repo_id}/remove", token)
        self.assertTrue(remove_form["attrs"].get("data-confirm"))

    def test_repository_detail_prioritizes_sync_and_groups_advanced_actions(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        for mode in ("manual", "mirror"):
            self.main.db.run("UPDATE repositories SET mode=? WHERE id=?", (mode, repo_id))
            _, page = self.rendered_page(f"/repos/{repo_id}")
            toolbar = page.find("section", **{"aria-labelledby": "repo-sync-title"})[0]
            advanced = page.find("details", **{"class": "advanced-actions"})[0]
            self.assertNotIn("open", advanced["attrs"])
            for action in ("sync", "pause"):
                self.assertTrue(page.inside(toolbar, "form", action=f"/repos/{repo_id}/{action}"))
            for action in ("mode", "selfupdate", "actions", "remove"):
                self.assertTrue(page.inside(advanced, "form", action=f"/repos/{repo_id}/{action}"))
            mode_form = self.assert_post_form(page, f"/repos/{repo_id}/mode", token)
            if mode == "manual":
                self.assertIn("不再保留 fork 自有提交", mode_form["attrs"].get("data-confirm", ""))
            else:
                self.assertNotIn("data-confirm", mode_form["attrs"])
            remove_form = self.assert_post_form(page, f"/repos/{repo_id}/remove", token)
            self.assertIn("自动发现", remove_form["attrs"]["data-confirm"])

    def test_paused_detail_requires_resume_before_immediate_sync(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="paused",
                                    full_name="me/paused", upstream_full_name="up/paused",
                                    status="paused")
        self.main.db.run("UPDATE repositories SET enabled=0 WHERE id=?", (repo_id,))
        _, page = self.rendered_page(f"/repos/{repo_id}")
        toolbar = page.find("section", **{"aria-labelledby": "repo-sync-title"})[0]
        self.assertIn("GitHub Actions 不受影响", toolbar["text"])
        resume = self.assert_post_form(page, f"/repos/{repo_id}/resume", token)
        self.assertNotIn("disabled", page.inside(resume, "button")[0]["attrs"])
        sync = self.assert_post_form(page, f"/repos/{repo_id}/sync", token)
        self.assertIn("disabled", page.inside(sync, "button")[0]["attrs"])

    def test_settings_fields_keep_labels_values_and_notices(self):
        token = self.setup_admin()
        self.main.db.set_setting("check_interval", "90")
        self.main.db.set_setting("auto_discover", "0")
        self.main.db.set_setting("auth_error", "GitHub credentials expired")
        response, page = self.rendered_page("/settings?notice=Saved")
        self.assertIn("Saved", response.text)
        self.assertIn("GitHub credentials expired", response.text)
        form = self.assert_post_form(page, "/settings", token)
        interval = page.inside(form, "input", name="interval_minutes")[0]
        self.assertEqual(interval["attrs"].get("value"), "1.5")
        self.assertEqual(interval["attrs"].get("min"), "0.25")
        self.assertNotIn("max", interval["attrs"])
        self.assertTrue(page.find("p", id=interval["attrs"].get("aria-describedby")))
        self.assertIn("已受管且未暂停", response.text)
        self.assertIn("每 60 分钟", response.text)
        explanations = page.find("details", **{"class": "settings-disclosure"})
        self.assertTrue(any("同一轮" in detail["text"] and "仅选定仓库" in detail["text"]
                            and "不是完成时限" in detail["text"] for detail in explanations))
        auto_discover = page.inside(form, "input", name="auto_discover")[0]
        self.assertNotIn("checked", auto_discover["attrs"])
        password_form = self.assert_post_form(page, "/settings/password", token)
        fields = [interval, auto_discover] + page.inside(password_form, "input", type="password")
        self.assertEqual(len(fields), 5)
        for field in fields:
            self.assertTrue(page.labels_for(field), field["attrs"].get("name"))
        self.assertNotIn(TEST_PASSWORD, response.text)

    def test_settings_groups_connection_sync_and_security_with_a_real_workflow(self):
        token = self.setup_admin()
        self.main.db.set_setting("github_app_slug", "forkiq-test")
        self.main.db.set_setting("github_installation_id", "999")
        _, page = self.rendered_page("/settings")
        sections = [page.find("section", id=name)[0] for name in ("connection", "sync", "security")]
        self.assertEqual([section["attrs"]["id"] for section in page.find("section")
                          if section in sections], ["connection", "sync", "security"])
        workflow = page.find("section", **{"aria-labelledby": "flow-title"})[0]
        steps = page.inside(workflow, "li")
        self.assertEqual(len(steps), 3)
        self.assertEqual([page.inside(step, "a")[0]["attrs"]["href"] for step in steps],
                         ["#connection", "/repos", "#sync"])
        self.assertTrue(page.inside(workflow, "a", href="/repos"))
        self.assertIn("暂停", workflow["text"])
        sync_form = self.assert_post_form(page, "/settings", token)
        password_form = self.assert_post_form(page, "/settings/password", token)
        disconnect_form = self.assert_post_form(page, "/settings/disconnect", token)
        for form, section in ((sync_form, sections[1]), (password_form, sections[2]),
                              (disconnect_form, sections[0])):
            self.assertTrue(any(parent is section for parent in form["parents"]))
        self.assertTrue(any(parent["tag"] == "details" and "open" not in parent["attrs"]
                            for parent in password_form["parents"]))
        self.assertTrue(any(parent["tag"] == "details" and "open" not in parent["attrs"]
                            for parent in disconnect_form["parents"]))
        self.assertTrue(disconnect_form["attrs"].get("data-confirm"))
        self.assertIn("重新登录", sections[2]["text"])
        self.assertIn("不会卸载", sections[0]["text"])
        self.assertTrue(page.inside(sections[0], "a", href="https://github.com/settings/installations/999"))
        self.assert_post_form(page, "/github/detect", token)

    def test_settings_connection_state_and_notices_remain_actionable(self):
        token = self.setup_admin()
        for slug, installation_id, label in (("", "", "未连接"),
                                              ("forkiq-test", "", "待安装授权"),
                                              ("forkiq-test", "999", "已连接")):
            with self.subTest(slug=slug, installation_id=installation_id):
                self.main.db.set_setting("github_app_slug", slug)
                self.main.db.set_setting("github_installation_id", installation_id)
                _, page = self.rendered_page("/settings")
                section = page.find("section", id="connection")[0]
                self.assertIn(label, section["text"])
                if not installation_id:
                    self.assertTrue(page.inside(section, "a", href="/github/connect"))
                if slug and not installation_id:
                    self.assertTrue(page.inside(section, "a", href="https://github.com/apps/forkiq-test/installations/new"))
                    self.assert_post_form(page, "/github/detect", token)
                if not slug:
                    self.assertFalse(page.find("form", action="/settings/disconnect"))
                    self.assertFalse(page.find("form", action="/github/detect"))
        self.main.db.set_setting("auth_error", "Credentials <expired>")
        response, page = self.rendered_page("/settings?notice=Saved")
        self.assertIn("Saved", page.find("div", role="status")[0]["text"])
        alert = page.find("div", role="alert")[0]
        self.assertIn("Credentials <expired>", alert["text"])
        self.assertTrue(page.inside(alert, "a", href="/github/connect"))
        self.assertIn("Credentials &lt;expired&gt;", response.text)
        self.assertNotIn("Credentials <expired>", response.text)

    def test_connection_and_installation_keep_manifest_and_detect_forms(self):
        token = self.setup_admin()
        _, page = self.rendered_page("/github/connect")
        self.assert_page_accessibility(page)
        manifest_forms = page.find("form", id="manifest-form")
        self.assertEqual(len(manifest_forms), 1)
        form = manifest_forms[0]
        state = self.main.db.get_setting("github_manifest_state")
        self.assertEqual(form["attrs"]["action"], f"{self.main.MANIFEST_URL}?state={state}")
        self.assertEqual(form["attrs"].get("method"), "post")
        import json
        payload = json.loads(page.inside(form, "input", name="manifest")[0]["attrs"]["value"])
        self.assertEqual(payload["redirect_url"], "https://forkiq.example/api/github/callback")
        self.assertEqual(payload["default_permissions"]["workflows"], "write")
        app_payload = {"id": 123, "slug": "forkiq-test", "pem": "test-pem"}
        with mock.patch.object(self.main, "exchange_manifest", return_value=app_payload), \
             mock.patch.object(self.main, "write_pem"):
            response = self.client.get(f"/api/github/callback?code=test-code&state={state}")
        self.assertEqual(response.status_code, 200)
        installation_page = RenderedHTML(response.text)
        self.assert_page_accessibility(installation_page)
        self.assertTrue(installation_page.find("a", href="https://github.com/apps/forkiq-test/installations/new"))
        self.assert_post_form(installation_page, "/github/detect", token)
        _, awaiting_installation = self.rendered_page("/github/connect")
        self.assertTrue(awaiting_installation.find("a", href="https://github.com/apps/forkiq-test/installations/new"))
        self.assert_post_form(awaiting_installation, "/github/detect", token)
        self.assertFalse(awaiting_installation.find("form", id="manifest-form"))

    def test_manifest_uses_configured_origin_not_host_or_forwarded_headers(self):
        self.setup_admin()
        for origin in ("https://selfhost.example:8443/", "http://localhost:8792", "http://[::1]:8792/"):
            with self.subTest(origin=origin), mock.patch.dict("os.environ", {"FORKIQ_PUBLIC_URL": origin}):
                response = self.client.get("/github/connect", headers={
                    "host": "attacker.example", "x-forwarded-host": "another.example",
                    "x-forwarded-proto": "http"})
                self.assertEqual(response.status_code, 200)
                page = RenderedHTML(response.text)
                form = page.find("form", id="manifest-form")[0]
                payload = json.loads(page.inside(form, "input", name="manifest")[0]["attrs"]["value"])
                base = origin.rstrip("/")
                self.assertEqual(payload["url"], base)
                self.assertEqual(payload["hook_attributes"], {"url": base + "/healthz", "active": False})
                self.assertEqual(payload["redirect_url"], base + "/api/github/callback")
                self.assertEqual(payload["callback_urls"], [base + "/api/github/callback"])
                self.assertNotIn("fork.llkan.com", response.text)
                self.assertNotIn("attacker.example", response.text)
                self.assertNotIn("another.example", response.text)

    def test_missing_public_url_blocks_app_creation_and_can_be_corrected(self):
        self.setup_admin()
        with mock.patch.dict("os.environ", {"FORKIQ_PUBLIC_URL": ""}):
            response, page = self.rendered_page("/github/connect")
        self.assertTrue(page.find("div", role="alert"))
        self.assertIn("FORKIQ_PUBLIC_URL", response.text)
        self.assertFalse(page.find("form", id="manifest-form"))
        self.assertNotIn("fork.llkan.com", response.text)
        self.assertIsNone(self.main.db.get_setting("github_manifest_state"))
        _, corrected = self.rendered_page("/github/connect")
        self.assertTrue(corrected.find("form", id="manifest-form"))
        self.assertTrue(self.main.db.get_setting("github_manifest_state"))

    def test_invalid_public_origins_never_create_a_manifest_or_replace_state(self):
        self.setup_admin()
        self.main.db.set_setting("github_manifest_state", "previous-state")
        invalid = ("fork.example", "//fork.example", "ftp://fork.example", "https://",
                   "https://user:password@fork.example", "https://@fork.example",
                   "https://fork.example/subpath", "https://fork.example//",
                   "https://fork.example?query=1", "https://fork.example?",
                   "https://fork.example#fragment", "https://fork.example#",
                   "https://fork.example:bad", "https://fork.example:65536",
                   "https://fork.example:0", "https://fork.example:",
                   " https://fork.example", "https://fork.example\n", "https://fork. example",
                   "https://fork.example\\@other.example", "https://fork%2eexample",
                   "https://-fork.example", "https://fork..example", "https://[not-ipv6]",
                   "https://[::1]other.example", "https://[::1]other.example:8792")
        for origin in invalid:
            with self.subTest(origin=origin), mock.patch.dict("os.environ", {"FORKIQ_PUBLIC_URL": origin}):
                response, page = self.rendered_page("/github/connect")
                self.assertIn("FORKIQ_PUBLIC_URL", response.text)
                self.assertTrue(page.find("div", role="alert"))
                self.assertFalse(page.find("form", id="manifest-form"))
                self.assertEqual(self.main.db.get_setting("github_manifest_state"), "previous-state")

    def test_existing_app_can_finish_installation_without_public_url_configuration(self):
        token = self.setup_admin()
        self.main.db.set_setting("github_app_slug", "forkiq-test")
        with mock.patch.dict("os.environ", {"FORKIQ_PUBLIC_URL": ""}):
            for path in ("/github/connect", "/settings"):
                with self.subTest(path=path):
                    response, page = self.rendered_page(path)
                    self.assertNotIn("FORKIQ_PUBLIC_URL", response.text)
                    self.assertTrue(page.find("a", href="https://github.com/apps/forkiq-test/installations/new"))
                    self.assert_post_form(page, "/github/detect", token)
                    self.assertFalse(page.find("form", id="manifest-form"))

    def test_detection_before_installation_can_be_retried_after_leaving_the_page(self):
        token = self.setup_admin()
        self.main.db.set_setting("github_app_slug", "forkiq-test")
        stub = mock.Mock()
        stub.app_installations.side_effect = [[], [{"id": 999, "account": {"login": "owner"}}]]
        with mock.patch.object(self.main, "app_client_from_settings", return_value=stub):
            response = self.client.post("/github/detect", data={"csrf": self.csrf(token)})
            self.assertEqual(response.status_code, 200)
            page = RenderedHTML(response.text)
            self.assertIn("尚未安装", response.text)
            self.assertTrue(page.find("a", href="https://github.com/apps/forkiq-test/installations/new"))
            self.assert_post_form(page, "/github/detect", token)
            self.assertFalse(self.main.db.get_setting("github_installation_id"))
            self.rendered_page("/")
            _, settings = self.rendered_page("/settings")
            form = self.assert_post_form(settings, "/github/detect", token)
            csrf = settings.inside(form, "input", name="csrf")[0]["attrs"]["value"]
            response = self.client.post("/github/detect", data={"csrf": csrf})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.main.db.get_setting("github_installation_id"), "999")
        self.assertEqual(stub.app_installations.call_count, 2)

    def test_failed_detection_keeps_installation_and_retry_controls(self):
        token = self.setup_admin()
        self.main.db.set_setting("github_app_slug", "forkiq-test")
        stub = mock.Mock()
        stub.app_installations.side_effect = self.main.AuthError("credentials <expired>")
        for installation_id in ("", "999"):
            with self.subTest(installation_id=installation_id):
                self.main.db.set_setting("github_installation_id", installation_id)
                with mock.patch.object(self.main, "app_client_from_settings", return_value=stub):
                    response = self.client.post("/github/detect", data={"csrf": self.csrf(token)})
                page = RenderedHTML(response.text)
                self.assertIn("检测失败", response.text)
                self.assertIn("credentials &lt;expired&gt;", response.text)
                install_url = ("https://github.com/settings/installations/999" if installation_id
                               else "https://github.com/apps/forkiq-test/installations/new")
                self.assertTrue(page.find("a", href=install_url))
                self.assert_post_form(page, "/github/detect", token)
                self.assertFalse(page.find("form", id="manifest-form"))
                self.assertEqual(self.main.db.get_setting("github_installation_id"), installation_id)

    def test_detection_still_requires_login_and_csrf(self):
        token = self.setup_admin()
        with mock.patch.object(self.main, "app_client_from_settings") as client:
            for data in ({}, {"csrf": "wrong"}):
                self.assertEqual(self.client.post("/github/detect", data=data).status_code, 403)
            self.client.cookies.clear()
            response = self.client.post("/github/detect", data={"csrf": self.csrf(token)})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["location"], "/login")
            self.assertEqual(self.client.get("/github/connect").headers["location"], "/login")
            client.assert_not_called()

    def test_existing_app_connection_does_not_recreate_or_reset_manifest_state(self):
        token = self.setup_admin()
        self.main.db.set_setting("github_app_slug", "forkiq-test")
        self.main.db.set_setting("github_manifest_state", "existing-state")
        for installation_id in ("", "999"):
            with self.subTest(installation_id=installation_id):
                self.main.db.set_setting("github_installation_id", installation_id)
                with mock.patch.object(self.main, "manifest_payload", side_effect=AssertionError("recreated App")):
                    response, page = self.rendered_page("/github/connect")
                self.assertEqual(self.main.db.get_setting("github_manifest_state"), "existing-state")
                self.assertFalse(page.find("form", id="manifest-form"))
                if installation_id:
                    self.assertIn("已连接", response.text)
                    self.assertTrue(page.find("a", href="https://github.com/settings/installations/999"))
                    self.assertFalse(page.find("form", action="/github/detect"))
                    self.assertFalse(page.find("a", href="https://github.com/apps/forkiq-test/installations/new"))
                else:
                    self.assertNotIn("已连接", response.text)
                    self.assertTrue(page.find("a", href="https://github.com/apps/forkiq-test/installations/new"))
                    self.assert_post_form(page, "/github/detect", token)

    def test_disconnect_keeps_existing_app_and_repository_configuration_reconnectable(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        before = dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,)))
        for key, value in (("github_app_id", "123"), ("github_app_slug", "forkiq-test"),
                           ("github_pem_path", "existing-app.pem"), ("github_installation_id", "999"),
                           ("auth_error", "expired")):
            self.main.db.set_setting(key, value)
        response = self.client.post("/settings/disconnect", data={"csrf": self.csrf(token)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.main.db.get_setting("github_installation_id"), "")
        self.assertEqual(self.main.db.get_setting("auth_error"), "")
        self.assertEqual(self.main.db.get_setting("github_app_id"), "123")
        self.assertEqual(self.main.db.get_setting("github_pem_path"), "existing-app.pem")
        self.assertEqual(dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))), before)
        _, page = self.rendered_page("/github/connect")
        self.assertTrue(page.find("a", href="https://github.com/apps/forkiq-test/installations/new"))
        self.assert_post_form(page, "/github/detect", token)
        self.assertFalse(page.find("form", id="manifest-form"))

    def test_empty_and_error_pages_remain_actionable(self):
        self.setup_admin()
        self.main.db.run("DELETE FROM events")
        response, _ = self.rendered_page("/")
        self.assertIn("还没有事件", response.text)
        self.assertIn('/github/connect', response.text)
        response, page = self.rendered_page("/repos")
        self.assertIn("还没有受管仓库", response.text)
        self.assert_post_form(page, "/repos/add")
        response, page = self.rendered_page("/repos/99999")
        self.assertIn("未找到", response.text)
        self.assertIn("仓库不在管理列表中", response.text)
        self.assert_page_accessibility(page)
        self.assertTrue(page.find("a", href="/"))

    def test_connected_app_with_auth_error_offers_recovery_instead_of_success(self):
        token = self.setup_admin()
        self.main.db.set_setting("github_app_slug", "forkiq-test")
        self.main.db.set_setting("github_installation_id", "999")
        self.main.db.set_setting("auth_error", "GitHub credentials expired <unsafe>")
        response, page = self.rendered_page("/github/connect")
        self.assertIn("连接异常", response.text)
        self.assertNotIn("已连接：", response.text)
        self.assertIn("GitHub credentials expired &lt;unsafe&gt;", response.text)
        self.assertTrue(page.find("div", role="alert"))
        self.assert_post_form(page, "/github/detect", token)
        self.assertTrue(page.find("a", href="https://github.com/settings/installations/999"))
        self.assertFalse(page.find("form", id="manifest-form"))

    def test_setup_requires_matching_passwords_and_min_length(self):
        response = self.client.post("/setup", data={"password": "short", "password2": "short"})
        self.assertIn("至少 8 位", response.text)
        response = self.client.post("/setup", data={"password": TEST_PASSWORD, "password2": "other-1234"})
        self.assertIn("不一致", response.text)
        response = self.client.post("/setup", data={"password": TEST_PASSWORD, "password2": TEST_PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.main.db.get_setting("setup_done"), "1")

    def test_requires_login_after_setup(self):
        self.setup_admin()
        self.client.cookies.clear()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/login")

    def test_login_failure_and_rate_limit(self):
        self.client.post("/setup", data={"password": TEST_PASSWORD, "password2": TEST_PASSWORD})
        for _ in range(5):
            response = self.client.post("/login", data={"password": "wrong-pass"})
            self.assertIn("不正确", response.text)
        response = self.client.post("/login", data={"password": "wrong-pass"})
        self.assertIn("尝试过多", response.text)
        response = self.client.post("/login", data={"password": TEST_PASSWORD})
        self.assertIn("尝试过多", response.text)

    def test_csrf_missing_is_rejected(self):
        token = self.setup_admin()
        response = self.client.post("/repos/add", data={"url": "https://github.com/a/b"})
        self.assertEqual(response.status_code, 403)

    def test_add_fork_url_validation(self):
        token = self.setup_admin()
        stub = mock.Mock()
        with mock.patch.object(self.main, "client_from_settings", return_value=stub), \
             mock.patch.object(self.main, "add_manual", side_effect=ValueError("不是 fork，无法自动同步")):
            response = self.client.post("/repos/add", data={
                "url": "https://github.com/a/b", "csrf": self.csrf(token)})
        self.assertIn("不是 fork", response.text)

    def test_pause_resume_and_remove(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        response = self.client.post(f"/repos/{repo_id}/pause", data={"csrf": self.csrf(token)})
        self.assertEqual(response.status_code, 302)
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual(row["status"], "paused")
        self.client.post(f"/repos/{repo_id}/resume", data={"csrf": self.csrf(token)})
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual(row["status"], "pending")
        self.client.post(f"/repos/{repo_id}/sync", data={"csrf": self.csrf(token)})
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual(row["sync_requested"], 1)
        self.client.post(f"/repos/{repo_id}/remove", data={"csrf": self.csrf(token)})
        self.assertIsNone(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,)))

    def test_mode_toggle_switches_and_resets_state(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        self.main.db.run("UPDATE repositories SET status='diverged', diverged_at='x', "
                         "diverged_upstream_sha='y' WHERE id=?", (repo_id,))
        response = self.client.post(f"/repos/{repo_id}/mode", data={"csrf": self.csrf(token)})
        self.assertEqual(response.status_code, 302)
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual(row["mode"], "manual")
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["diverged_at"])
        self.assertEqual(row["sync_requested"], 1)
        # toggling back returns to mirror mode
        self.client.post(f"/repos/{repo_id}/mode", data={"csrf": self.csrf(token)})
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual(row["mode"], "mirror")

    def test_selfupdate_toggle_enables_actions_and_permissions(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        stub = mock.Mock()
        stub.actions_enabled.return_value = False
        stub.workflow_permissions.return_value = {"default_workflow_permissions": "read"}
        stub.list_workflows.return_value = [
            {"id": 5, "name": "Update", "state": "disabled_fork",
             "path": ".github/workflows/update.yml"}]
        with mock.patch.object(self.main, "client_from_settings", return_value=stub):
            response = self.client.post(f"/repos/{repo_id}/selfupdate",
                                        data={"csrf": self.csrf(token)})
        self.assertEqual(response.status_code, 302)
        stub.set_actions_enabled.assert_called_once_with("me/demo", True)
        stub.set_workflow_permissions.assert_called_once_with("me/demo", "write")
        stub.enable_workflow.assert_called_once_with("me/demo", 5)
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual(row["self_update_interval"], 1)
        self.assertEqual(row["sync_requested"], 1)
        # toggle off
        with mock.patch.object(self.main, "client_from_settings", return_value=stub):
            self.client.post(f"/repos/{repo_id}/selfupdate", data={"csrf": self.csrf(token)})
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertIsNone(row["self_update_interval"])

    def test_selfupdate_reports_permission_failure(self):
        token = self.setup_admin()
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo",
                                    full_name="me/demo", upstream_full_name="up/demo")
        stub = mock.Mock()
        stub.actions_enabled.return_value = True
        stub.workflow_permissions.return_value = {"default_workflow_permissions": "write"}
        stub.list_workflows.return_value = [
            {"id": 5, "name": "Update", "state": "disabled_fork",
             "path": ".github/workflows/update.yml"}]
        stub.enable_workflow.side_effect = RuntimeError("Resource not accessible by integration")
        with mock.patch.object(self.main, "client_from_settings", return_value=stub):
            response = self.client.post(f"/repos/{repo_id}/selfupdate",
                                        data={"csrf": self.csrf(token)})
        self.assertIn("Actions", response.text)
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertIsNone(row["self_update_interval"])
        self.assertIn("开启自更新部分失败", row["self_update"])

    def test_manifest_callback_rejects_bad_state_and_accepts_good_code(self):
        token = self.setup_admin()
        self.main.db.set_setting("github_manifest_state", "state-1")
        # missing code
        response = self.client.get("/api/github/callback")
        self.assertIn("连接失败", response.text)
        # wrong state
        response = self.client.get("/api/github/callback?code=abc&state=wrong")
        self.assertIn("连接失败", response.text)
        # valid exchange writes pem and stores app id
        payload = {"id": 123, "slug": "forkiq-test", "pem": "-----BEGIN RSA PRIVATE KEY-----\nxx\n-----END RSA PRIVATE KEY-----\n"}
        with mock.patch.object(self.main, "exchange_manifest", return_value=payload):
            response = self.client.get("/api/github/callback?code=abc&state=state-1")
        self.assertIn("安装", response.text)
        self.assertEqual(self.main.db.get_setting("github_app_id"), "123")
        pem = Path(self.main.PEM_PATH)
        self.assertTrue(pem.exists())
        self.assertEqual(oct(pem.stat().st_mode)[-3:], "600")

    def test_healthz_is_public(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_detect_installation_stores_id(self):
        token = self.setup_admin()
        self.main.db.set_setting("github_app_id", "1")
        self.main.db.set_setting("github_pem_path", str(self.main.PEM_PATH))
        stub = mock.Mock()
        stub.app_installations.return_value = [
            {"id": 999, "account": {"login": "zzusec"}}]
        with mock.patch.object(self.main, "app_client_from_settings", return_value=stub):
            response = self.client.post("/github/detect", data={"csrf": self.csrf(token)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.main.db.get_setting("github_installation_id"), "999")

    def test_detect_before_installation_id_exists(self):
        # The detect step runs before an installation id is stored; it must not
        # depend on client_from_settings (which requires one).
        token = self.setup_admin()
        self.main.db.set_setting("github_app_id", "1")
        self.main.db.set_setting("github_pem_path", str(self.main.PEM_PATH))
        stub = mock.Mock()
        stub.app_installations.return_value = [
            {"id": 1000, "account": {"login": "zzusec"}}]
        with mock.patch.object(self.main, "app_client_from_settings", return_value=stub):
            response = self.client.post("/github/detect", data={"csrf": self.csrf(token)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.main.db.get_setting("github_installation_id"), "1000")

    def test_settings_save_keeps_minimum_but_allows_long_intervals(self):
        token = self.setup_admin()
        self.client.post("/settings", data={"csrf": self.csrf(token), "interval_minutes": "0.01",
                                            "auto_discover": "1"})
        self.assertEqual(self.main.db.get_setting("check_interval"), "15")
        self.client.post("/settings", data={"csrf": self.csrf(token), "interval_minutes": "9999",
                                            "auto_discover": "0"})
        self.assertEqual(self.main.db.get_setting("check_interval"), "599940")
        self.assertEqual(self.main.db.get_setting("auto_discover"), "0")

    def test_minutes_round_trip_preserves_stored_seconds_and_wakes_worker(self):
        token = self.setup_admin()
        for minutes, seconds in (("0.25", "15"), ("0.5", "30"), ("1", "60"), ("1.5", "90"), ("5", "300"), ("30", "1800"), ("60", "3600"), ("1440", "86400")):
            with self.subTest(minutes=minutes):
                with mock.patch.object(self.main.worker, "request_sync") as wake:
                    response = self.client.post("/settings", data={
                        "csrf": self.csrf(token), "interval_minutes": minutes, "auto_discover": "1"})
                self.assertEqual(response.status_code, 302)
                wake.assert_called_once()
                self.assertEqual(self.main.db.get_setting("check_interval"), seconds)
                _, page = self.rendered_page("/settings")
                self.assertEqual(page.find("input", name="interval_minutes")[0]["attrs"]["value"], minutes)

    def test_predeploy_seconds_form_still_saves_during_rolling_deploy(self):
        token = self.setup_admin()
        with mock.patch.object(self.main.worker, "request_sync") as wake:
            response = self.client.post("/settings", data={
                "csrf": self.csrf(token), "interval": "90", "auto_discover": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.main.db.get_setting("check_interval"), "90")
        wake.assert_called_once()

    def test_invalid_minutes_leave_settings_unchanged(self):
        token = self.setup_admin()
        self.main.db.set_setting("check_interval", "90")
        self.main.db.set_setting("auto_discover", "1")
        for invalid in ("nan", "inf", "-inf", "invalid", "", "0", "-1", "1e308", "1e30"):
            with self.subTest(invalid=invalid), mock.patch.object(self.main.worker, "request_sync") as wake:
                response = self.client.post("/settings", data={
                    "csrf": self.csrf(token), "interval_minutes": invalid, "auto_discover": "0"})
                self.assertIn(response.status_code, (302, 422))
                if response.status_code == 302:
                    self.assertIn("notice=", response.headers["location"])
                self.assertEqual(self.main.db.get_setting("check_interval"), "90")
                self.assertEqual(self.main.db.get_setting("auto_discover"), "1")
                wake.assert_not_called()

    def dangerous_deletion_review(self, tree_error=None):
        """Run the real engine against immutable four-file/one-file fixtures."""
        from app.db import insert_repository
        from test_engine import StubGitHub, node
        self.main.db.run("DELETE FROM repositories")
        self.main.db.run("DELETE FROM repository_backups")
        self.main.db.run("DELETE FROM events")
        repo_id = insert_repository(self.main.db, owner="me", name="demo", full_name="me/demo",
                                    upstream_full_name="up/demo", node_id="N1")
        stub = StubGitHub()
        stub.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "b" * 40)
        stub.refs["me/demo"] = "a" * 40
        stub.file_trees["me/demo", "a" * 40] = {"README.md", "one.py", "two.py", "three.py"}
        stub.file_trees["up/demo", "b" * 40] = {"README.md"}
        if tree_error:
            stub.tree_errors["up/demo", "b" * 40] = tree_error
        backup = self.backup_record(repo_id, fork_sha="a" * 40)
        verified = mock.patch.object(self.main, "verified_backup", return_value=True)
        verified.start()
        self.addCleanup(verified.stop)
        backups = mock.patch.object(engine, "ensure_backup", return_value=backup)
        backups.start()
        self.addCleanup(backups.stop)
        self.main.db.set_setting("auto_discover", "0")
        engine.run_cycle(self.main.db, stub)
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual(row["status"], "needs_attention")
        self.assertEqual(stub.refs["me/demo"], "a" * 40)
        self.assertFalse(any(call[0] in ("update_ref", "merge_commit") for call in stub.calls))
        return repo_id, stub, row

    def backup_record(self, repo_id, full_name="me/demo", fork_sha=None, status="ready",
                      contents=b"isolated backup fixture\n", error=None):
        """Publish an intact local file without invoking Git or GitHub."""
        fork_sha = fork_sha or "a" * 40
        now = self.main.utcnow()
        cursor = self.main.db.run(
            "INSERT INTO repository_backups(repo_id,full_name,fork_sha,status,created_at,updated_at,"
            "sha256,size_bytes,last_error) VALUES(?,?,?,?,?,?,?,?,?)",
            (repo_id, full_name, fork_sha, status, now, now,
             hashlib.sha256(contents).hexdigest() if status == "ready" else None,
             len(contents) if status == "ready" else None, error))
        record = self.main.db.one("SELECT * FROM repository_backups WHERE id=?", (cursor.lastrowid,))
        if status == "ready":
            from app.backups import backup_path
            root = Path(self.main.db.path).parent / "backups"
            root.mkdir(mode=0o700, exist_ok=True)
            backup_path(self.main.db, record).write_bytes(contents)
        return dict(record)

    def repository_with_backup(self):
        from app.db import insert_repository
        repo_id = insert_repository(self.main.db, owner="me", name="demo", full_name="me/demo",
                                    upstream_full_name="up/demo", node_id="N1")
        return repo_id, self.backup_record(repo_id)

    def test_deletion_protection_defaults_and_inputs_have_accessible_labels(self):
        token = self.setup_admin()
        from app.db import insert_repository
        from app.protection import deletion_policy
        repo_id = insert_repository(self.main.db, owner="me", name="demo", full_name="me/demo",
                                    upstream_full_name="up/demo")
        expected = {"enabled": True, "max_files": 100, "max_percent": 50}
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual(deletion_policy(self.main.db), expected)
        self.assertEqual(deletion_policy(self.main.db, row), expected)
        for path, action in (("/settings", "/settings/deletion-protection"),
                             (f"/repos/{repo_id}", f"/repos/{repo_id}/protection")):
            with self.subTest(path=path):
                response, page = self.rendered_page(path)
                form = self.assert_post_form(page, action, token)
                fields = [field for field in page.inside(form, "input")
                          if field["attrs"].get("type") != "hidden"] + page.inside(form, "select")
                self.assertEqual(len(fields), 3)
                for field in fields:
                    self.assertTrue(page.labels_for(field), field["attrs"].get("name"))
                self.assertNotIn(TEST_PASSWORD, response.text)
                if path == "/settings":
                    self.assertIn("checked", page.inside(form, "input", name="enabled")[0]["attrs"])
                    self.assertEqual(page.inside(form, "input", name="max_files")[0]["attrs"]["value"], "100")
                    self.assertEqual(float(page.inside(form, "input", name="max_percent")[0]["attrs"]["value"]), 50)
                else:
                    selected = [option["attrs"]["value"] for option in page.inside(form, "option")
                                if "selected" in option["attrs"]]
                    self.assertEqual(selected, ["inherit"])
                    for name in ("max_files", "max_percent"):
                        self.assertEqual(page.inside(form, "input", name=name)[0]["attrs"]["value"], "")

    def test_system_deletion_settings_save_and_invalid_values_do_not_write(self):
        token = self.setup_admin()
        repo_id, _, _ = self.dangerous_deletion_review()
        self.main.db.run("UPDATE repositories SET deletion_approval='old-approval' WHERE id=?", (repo_id,))
        payload = {"csrf": self.csrf(token), "enabled": "0", "max_files": "27", "max_percent": "12.5"}
        with mock.patch.object(self.main.worker, "request_sync") as wake:
            response = self.client.post("/settings/deletion-protection", data=payload)
        self.assertEqual(response.status_code, 302)
        wake.assert_called_once()
        self.assertEqual(self.main.db.get_setting("deletion_protection"), "0")
        self.assertEqual(self.main.db.get_setting("deletion_max_files"), "27")
        self.assertEqual(self.main.db.get_setting("deletion_max_percent"), "12.5")
        self.assertIsNone(self.main.db.one("SELECT deletion_approval FROM repositories WHERE id=?", (repo_id,))[0])
        for field, invalid in (("enabled", "yes"), ("max_files", "-1"), ("max_files", "1.5"),
                               ("max_files", "1000001"), ("max_files", ""),
                               ("max_percent", "-1"), ("max_percent", "100.1"),
                               ("max_percent", "nan"), ("max_percent", "inf"),
                               ("max_percent", "")):
            with self.subTest(field=field, invalid=invalid):
                before = [tuple(row) for row in self.main.db.all("SELECT * FROM settings ORDER BY key")]
                before_repo = dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,)))
                with mock.patch.object(self.main.worker, "request_sync") as wake:
                    response = self.client.post("/settings/deletion-protection", data={**payload, field: invalid})
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.json()["error"])
                self.assertEqual([tuple(row) for row in self.main.db.all("SELECT * FROM settings ORDER BY key")], before)
                self.assertEqual(dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))), before_repo)
                wake.assert_not_called()

    def test_repository_deletion_policy_can_override_partially_and_clear_to_inherit(self):
        token = self.setup_admin()
        repo_id, _, _ = self.dangerous_deletion_review()
        from app.protection import deletion_policy
        self.main.db.set_setting("deletion_protection", "0")
        self.main.db.set_setting("deletion_max_files", "80")
        self.main.db.set_setting("deletion_max_percent", "40")
        cases = [({"protection_mode": "on", "max_files": "5", "max_percent": "2.5"},
                  (1, 5, 2.5), {"enabled": True, "max_files": 5, "max_percent": 2.5}),
                 ({"protection_mode": "off", "max_files": "", "max_percent": ""},
                  (0, None, None), {"enabled": False, "max_files": 80, "max_percent": 40}),
                 ({"protection_mode": "inherit", "max_files": "3", "max_percent": ""},
                  (None, 3, None), {"enabled": False, "max_files": 3, "max_percent": 40}),
                 ({"protection_mode": "inherit", "max_files": "", "max_percent": ""},
                  (None, None, None), {"enabled": False, "max_files": 80, "max_percent": 40})]
        for payload, stored, expected in cases:
            with self.subTest(payload=payload):
                self.main.db.run("UPDATE repositories SET deletion_approval='old-approval' WHERE id=?", (repo_id,))
                with mock.patch.object(self.main.worker, "request_sync") as wake:
                    response = self.client.post(f"/repos/{repo_id}/protection", data={"csrf": self.csrf(token), **payload})
                self.assertEqual(response.status_code, 302)
                wake.assert_called_once()
                row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
                self.assertEqual(tuple(row[key] for key in ("deletion_protection", "deletion_max_files", "deletion_max_percent")), stored)
                self.assertEqual(deletion_policy(self.main.db, row), expected)
                self.assertIsNone(row["deletion_approval"])
        for payload in ({"protection_mode": "invalid"}, {"max_files": "0.5"},
                        {"max_percent": "101"}, {"max_percent": "nan"}):
            before = dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,)))
            with self.subTest(invalid=payload), mock.patch.object(self.main.worker, "request_sync") as wake:
                response = self.client.post(f"/repos/{repo_id}/protection", data={"csrf": self.csrf(token), **payload})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))), before)
                wake.assert_not_called()

    def test_deletion_protection_mutations_require_login_and_csrf(self):
        token = self.setup_admin()
        repo_id, _, _ = self.dangerous_deletion_review()
        routes = [("/settings/deletion-protection", {"enabled": "1", "max_files": "3", "max_percent": "1"}),
                  (f"/repos/{repo_id}/protection", {"protection_mode": "off"}),
                  (f"/repos/{repo_id}/confirm-deletion", {"review": "old"})]
        before_settings = [tuple(row) for row in self.main.db.all("SELECT * FROM settings ORDER BY key")]
        before_repo = dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,)))
        with mock.patch.object(self.main.worker, "request_sync") as wake:
            for route, payload in routes:
                for csrf in (None, "wrong"):
                    with self.subTest(route=route, csrf=csrf):
                        response = self.client.post(route, data={**payload, **({"csrf": csrf} if csrf else {})})
                        self.assertEqual(response.status_code, 403)
            self.client.cookies.clear()
            for route, payload in routes:
                with self.subTest(unauthenticated=route):
                    response = self.client.post(route, data={**payload, "csrf": self.csrf(token)})
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.headers["location"], "/login")
            wake.assert_not_called()
        self.assertEqual([tuple(row) for row in self.main.db.all("SELECT * FROM settings ORDER BY key")], before_settings)
        self.assertEqual(dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))), before_repo)

    def test_dangerous_deletion_details_show_review_counts_target_and_confirmation(self):
        token = self.setup_admin()
        repo_id, _, row = self.dangerous_deletion_review()
        from app.protection import load_review, review_token
        review = load_review(row)
        self.assertEqual((review["deleted_files"], review["current_files"], review["deleted_percent"]), (3, 4, 75))
        response, page = self.rendered_page(f"/repos/{repo_id}")
        section = page.find("section", **{"aria-labelledby": "deletion-review-title"})[0]
        self.assertIn("已保留 fork 当前内容", section["text"])
        self.assertIn("3 / 4（75.0%）", section["text"])
        target = page.inside(section, "a", href="https://github.com/up/demo/commit/" + "b" * 40)[0]
        self.assertEqual(target["text"], "b" * 40)
        form = self.assert_post_form(page, f"/repos/{repo_id}/confirm-deletion", token)
        self.assertEqual(page.inside(form, "input", name="review")[0]["attrs"]["value"], review_token(review))
        self.assertTrue(form["attrs"].get("data-confirm"))
        self.assertIn("确认并同步此次变更", form["text"])
        self.assertNotIn(TEST_PASSWORD, response.text)

    def test_confirm_deletion_only_queues_work_and_worker_consumes_exact_target_once(self):
        token = self.setup_admin()
        repo_id, stub, row = self.dangerous_deletion_review()
        from app.protection import load_review, review_token
        from test_engine import node
        approval = review_token(load_review(row))
        before_calls = list(stub.calls)
        with mock.patch.object(self.main.worker, "request_sync") as wake, \
             mock.patch.object(self.main, "client_from_settings", side_effect=AssertionError("HTTP handler wrote GitHub")), \
             mock.patch.object(self.main, "app_client_from_settings", side_effect=AssertionError("HTTP handler wrote GitHub")):
            response = self.client.post(f"/repos/{repo_id}/confirm-deletion", data={"csrf": self.csrf(token), "review": approval})
        self.assertEqual(response.status_code, 302)
        wake.assert_called_once()
        queued = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual((queued["sync_requested"], queued["deletion_approval"]), (1, approval))
        self.assertEqual(stub.calls, before_calls)
        self.assertEqual(stub.refs["me/demo"], "a" * 40)
        with mock.patch.object(engine, "client_from_settings", return_value=stub):
            self.main.worker._tick(self.main.db)
        synced = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual((synced["status"], synced["fork_sha"], synced["sync_requested"]), ("synced", "b" * 40, 0))
        self.assertIsNone(synced["deletion_approval"])
        self.assertEqual([call for call in stub.calls if call[0] == "update_ref"], [("update_ref", "me/demo", "main", "b" * 40)])
        response = self.client.post(f"/repos/{repo_id}/confirm-deletion", data={"csrf": self.csrf(token), "review": approval})
        self.assertEqual(response.status_code, 409)
        stub.nodes["N1"] = node("N1", "me/demo", "up/demo", "b" * 40, "c" * 40)
        stub.file_trees["me/demo", "b" * 40] = {"README.md"}
        stub.file_trees["up/demo", "c" * 40] = set()
        with mock.patch.object(engine, "client_from_settings", return_value=stub):
            self.main.worker._tick(self.main.db)
        blocked = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual(blocked["status"], "needs_attention")
        self.assertEqual(stub.refs["me/demo"], "b" * 40)
        self.assertEqual(load_review(blocked)["identity"]["target_sha"], "c" * 40)
        self.assertEqual([call for call in stub.calls if call[0] == "update_ref"], [("update_ref", "me/demo", "main", "b" * 40)])

    def test_upstream_change_after_confirmation_requires_a_fresh_review(self):
        token = self.setup_admin()
        repo_id, stub, row = self.dangerous_deletion_review()
        from app.protection import load_review, review_token
        from test_engine import node
        approval = review_token(load_review(row))
        self.assertEqual(self.client.post(f"/repos/{repo_id}/confirm-deletion", data={"csrf": self.csrf(token), "review": approval}).status_code, 302)
        stub.nodes["N1"] = node("N1", "me/demo", "up/demo", "a" * 40, "c" * 40)
        stub.file_trees["up/demo", "c" * 40] = set()
        with mock.patch.object(engine, "client_from_settings", return_value=stub):
            self.main.worker._tick(self.main.db)
        row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual(row["status"], "needs_attention")
        self.assertIsNone(row["deletion_approval"])
        self.assertEqual(load_review(row)["identity"]["target_sha"], "c" * 40)
        self.assertEqual(stub.refs["me/demo"], "a" * 40)
        self.assertFalse(any(call[0] in ("update_ref", "merge_commit") for call in stub.calls))
        self.assertEqual(self.client.post(f"/repos/{repo_id}/confirm-deletion", data={"csrf": self.csrf(token), "review": approval}).status_code, 409)

    def test_stale_deletion_confirmation_is_rejected_after_identity_or_policy_changes(self):
        token = self.setup_admin()
        from app.protection import load_review, review_token
        changes = [("upstream_sha", "c" * 40), ("fork_sha", "d" * 40),
                   ("mode", "manual"), ("branch", "other"), ("upstream_full_name", "other/demo"),
                   ("deletion_max_files", 5), ("system_policy", "12")]
        for field, value in changes:
            with self.subTest(field=field):
                self.main.db.run("DELETE FROM settings WHERE key LIKE 'deletion_%'")
                repo_id, _, row = self.dangerous_deletion_review()
                approval = review_token(load_review(row))
                if field == "system_policy":
                    self.main.db.set_setting("deletion_max_percent", value)
                else:
                    self.main.db.run(f"UPDATE repositories SET {field}=? WHERE id=?", (value, repo_id))
                with mock.patch.object(self.main.worker, "request_sync") as wake:
                    response = self.client.post(f"/repos/{repo_id}/confirm-deletion", data={"csrf": self.csrf(token), "review": approval})
                self.assertEqual(response.status_code, 409)
                wake.assert_not_called()
                row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
                self.assertEqual(row["sync_requested"], 0)
                self.assertIsNone(row["deletion_approval"])
                _, page = self.rendered_page(f"/repos/{repo_id}")
                self.assertFalse(page.find("form", action=f"/repos/{repo_id}/confirm-deletion"))

    def test_wrong_token_paused_repo_and_incomplete_detection_cannot_be_confirmed(self):
        token = self.setup_admin()
        from app.protection import load_review, review_token
        for condition in ("wrong_token", "non_ascii_token", "paused", "disabled", "incomplete"):
            with self.subTest(condition=condition):
                repo_id, _, row = self.dangerous_deletion_review(
                    RuntimeError("tree unavailable <retry>") if condition == "incomplete" else None)
                approval = review_token(load_review(row))
                if condition == "wrong_token":
                    approval = "wrong-token"
                elif condition == "non_ascii_token":
                    approval = "无效确认"
                elif condition == "paused":
                    self.main.db.run("UPDATE repositories SET status='paused' WHERE id=?", (repo_id,))
                elif condition == "disabled":
                    self.main.db.run("UPDATE repositories SET enabled=0 WHERE id=?", (repo_id,))
                with mock.patch.object(self.main.worker, "request_sync") as wake:
                    response = self.client.post(f"/repos/{repo_id}/confirm-deletion", data={"csrf": self.csrf(token), "review": approval})
                self.assertEqual(response.status_code, 409)
                wake.assert_not_called()
                row = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
                self.assertEqual(row["sync_requested"], 0)
                self.assertIsNone(row["deletion_approval"])
                response, page = self.rendered_page(f"/repos/{repo_id}")
                self.assertNotIn(TEST_PASSWORD, response.text)
                if condition not in ("wrong_token", "non_ascii_token"):
                    self.assertFalse(page.find("form", action=f"/repos/{repo_id}/confirm-deletion"))
                if condition == "incomplete":
                    self.assertIn("检测未完成", response.text)
                    self.assertIn("tree unavailable &lt;retry&gt;", response.text)


    def test_backup_download_requires_login_and_serves_a_verified_attachment_without_caching(self):
        self.setup_admin()
        repo_id, record = self.repository_with_backup()
        from app.backups import backup_path, verified_backup
        self.assertTrue(verified_backup(self.main.db, record))
        with mock.patch.object(self.main, "client_from_settings", side_effect=AssertionError("download used GitHub")):
            response = self.client.get(f"/backups/{record['id']}/download")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, backup_path(self.main.db, record).read_bytes())
        self.assertIn("no-store", response.headers.get("cache-control", ""))
        disposition = response.headers.get("content-disposition", "")
        self.assertTrue(disposition.startswith("attachment;"), disposition)
        self.assertIn(".bundle", disposition)
        self.client.cookies.clear()
        response = self.client.get(f"/backups/{record['id']}/download")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/login")

    def test_unavailable_or_corrupt_backup_cannot_be_downloaded_or_restored(self):
        token = self.setup_admin()
        repo_id, record = self.repository_with_backup()
        from app.backups import backup_path
        path = backup_path(self.main.db, record)
        original = path.read_bytes()
        preview_url = f"/repos/{repo_id}/backups/{record['id']}/restore"
        before = dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,)))
        for condition in ("missing", "corrupt", "error"):
            with self.subTest(condition=condition):
                path.write_bytes(original)
                self.main.db.run("UPDATE repository_backups SET status='ready' WHERE id=?", (record["id"],))
                if condition == "missing":
                    path.unlink()
                elif condition == "corrupt":
                    path.write_bytes(b"x" * len(original))
                else:
                    self.main.db.run("UPDATE repository_backups SET status='error' WHERE id=?", (record["id"],))
                with mock.patch.object(self.main.worker, "request_sync") as wake, \
                     mock.patch.object(self.main, "client_from_settings", side_effect=AssertionError("bad backup used GitHub")):
                    self.assertEqual(self.client.get(f"/backups/{record['id']}/download").status_code, 409)
                    self.assertEqual(self.client.get(preview_url).status_code, 409)
                    response = self.client.post(preview_url, data={"csrf": self.csrf(token), "expected_sha": "c" * 40})
                    self.assertEqual(response.status_code, 409)
                    wake.assert_not_called()
                self.assertEqual(dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))), before)

    def test_backup_restore_rejects_missing_records_and_cross_repository_snapshots(self):
        token = self.setup_admin()
        repo_id, _ = self.repository_with_backup()
        foreign = self.backup_record(repo_id, full_name="other/demo", fork_sha="d" * 40)
        before = dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,)))
        with mock.patch.object(self.main.worker, "request_sync") as wake, \
             mock.patch.object(self.main, "client_from_settings", side_effect=AssertionError("invalid restore used GitHub")):
            self.assertEqual(self.client.get("/backups/999999/download").status_code, 404)
            for path in (f"/repos/{repo_id}/backups/999999/restore",
                         f"/repos/{repo_id}/backups/{foreign['id']}/restore",
                         f"/repos/999999/backups/{foreign['id']}/restore"):
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 404)
                    self.assertEqual(self.client.post(path, data={"csrf": self.csrf(token), "expected_sha": "c" * 40}).status_code, 404)
            wake.assert_not_called()
        self.assertEqual(dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))), before)

    def test_restore_preview_reads_current_head_and_renders_csrf_confirmation_without_mutation(self):
        token = self.setup_admin()
        repo_id, record = self.repository_with_backup()
        path = f"/repos/{repo_id}/backups/{record['id']}/restore"
        stub = mock.Mock()
        stub.get_ref.return_value = "c" * 40
        before = dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,)))
        before_events = [tuple(row) for row in self.main.db.all("SELECT * FROM events ORDER BY id")]
        with mock.patch.object(self.main.worker, "request_sync") as wake, \
             mock.patch.object(self.main, "client_from_settings", return_value=stub):
            response, page = self.rendered_page(path)
        stub.get_ref.assert_called_once_with("me/demo", "main")
        self.assertEqual(stub.mock_calls, [mock.call.get_ref("me/demo", "main")])
        wake.assert_not_called()
        self.assert_page_accessibility(page)
        form = self.assert_post_form(page, path, token)
        self.assertEqual(page.inside(form, "input", name="expected_sha")[0]["attrs"]["value"], "c" * 40)
        self.assertIn(record["fork_sha"], response.text)
        self.assertIn("me/demo", response.text)
        self.assertIn("暂停", response.text)
        self.assertNotIn(TEST_PASSWORD, response.text)
        self.assertEqual(dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))), before)
        self.assertEqual([tuple(row) for row in self.main.db.all("SELECT * FROM events ORDER BY id")], before_events)

    def test_restore_preview_and_queue_require_login_and_post_requires_csrf(self):
        token = self.setup_admin()
        repo_id, record = self.repository_with_backup()
        path = f"/repos/{repo_id}/backups/{record['id']}/restore"
        before = dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,)))
        with mock.patch.object(self.main.worker, "request_sync") as wake, \
             mock.patch.object(self.main, "client_from_settings", side_effect=AssertionError("unauthorized restore used GitHub")):
            for csrf in (None, "wrong"):
                response = self.client.post(path, data={"expected_sha": "c" * 40, **({"csrf": csrf} if csrf else {})})
                self.assertEqual(response.status_code, 403)
            self.client.cookies.clear()
            for response in (self.client.get(path), self.client.post(path, data={"expected_sha": "c" * 40, "csrf": self.csrf(token)})):
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["location"], "/login")
            wake.assert_not_called()
        self.assertEqual(dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))), before)

    def test_restore_queue_validates_expected_sha_and_only_schedules_paused_work(self):
        token = self.setup_admin()
        repo_id, record = self.repository_with_backup()
        path = f"/repos/{repo_id}/backups/{record['id']}/restore"
        self.main.db.run("UPDATE repositories SET sync_requested=1,deletion_approval='old' WHERE id=?", (repo_id,))
        before = dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,)))
        for sha in ("", "c" * 39, "c" * 41, "z" * 40, "无效提交"):
            with self.subTest(sha=sha), mock.patch.object(self.main.worker, "request_sync") as wake, \
                 mock.patch.object(self.main, "client_from_settings", side_effect=AssertionError("HTTP restore wrote GitHub")):
                response = self.client.post(path, data={"csrf": self.csrf(token), "expected_sha": sha})
                self.assertEqual(response.status_code, 400)
                wake.assert_not_called()
                self.assertEqual(dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))), before)
        idle_client = mock.Mock(spec=[])
        with mock.patch.object(self.main.worker, "request_sync") as wake, \
             mock.patch.object(self.main, "client_from_settings", return_value=idle_client):
            response = self.client.post(path, data={"csrf": self.csrf(token), "expected_sha": "c" * 40})
        self.assertEqual(idle_client.mock_calls, [])
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["location"].startswith(f"/repos/{repo_id}"))
        wake.assert_called_once()
        queued = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        self.assertEqual((queued["status"], queued["enabled"], queued["sync_requested"]), ("paused", 0, 0))
        self.assertIsNone(queued["deletion_approval"])
        self.assertEqual((queued["restore_backup_id"], queued["restore_expected_sha"]), (record["id"], "c" * 40))
        self.assertEqual(queued["fork_sha"], before["fork_sha"])

    def test_pending_restore_rejects_duplicate_queue_and_normal_repository_actions(self):
        token = self.setup_admin()
        repo_id, record = self.repository_with_backup()
        path = f"/repos/{repo_id}/backups/{record['id']}/restore"
        self.main.db.run("UPDATE repositories SET status='paused',enabled=0,restore_backup_id=?,restore_expected_sha=? WHERE id=?",
                         (record["id"], "c" * 40, repo_id))
        before = dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,)))
        with mock.patch.object(self.main.worker, "request_sync") as wake, \
             mock.patch.object(self.main, "client_from_settings", side_effect=AssertionError("pending restore mutated GitHub")):
            response = self.client.post(path, data={"csrf": self.csrf(token), "expected_sha": "d" * 40})
            self.assertEqual(response.status_code, 409)
            for action in ("pause", "resume", "sync", "mode", "selfupdate", "remove", "actions"):
                with self.subTest(action=action):
                    response = self.client.post(f"/repos/{repo_id}/{action}", data={"csrf": self.csrf(token), "enabled": "1"})
                    self.assertEqual(response.status_code, 409)
            wake.assert_not_called()
        self.assertEqual(dict(self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))), before)

    def test_restore_belongs_to_repository_name_not_a_stale_row_identifier(self):
        token = self.setup_admin()
        repo_id, record = self.repository_with_backup()
        self.main.db.run("UPDATE repository_backups SET repo_id=? WHERE id=?", (repo_id + 12345, record["id"]))
        path = f"/repos/{repo_id}/backups/{record['id']}/restore"
        stub = mock.Mock()
        stub.get_ref.return_value = "c" * 40
        with mock.patch.object(self.main, "client_from_settings", return_value=stub):
            self.assertEqual(self.client.get(path).status_code, 200)
        idle_client = mock.Mock(spec=[])
        with mock.patch.object(self.main.worker, "request_sync") as wake, \
             mock.patch.object(self.main, "client_from_settings", return_value=idle_client):
            response = self.client.post(path, data={"csrf": self.csrf(token), "expected_sha": "c" * 40})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(idle_client.mock_calls, [])
        wake.assert_called_once()

    def test_dangerous_change_needs_an_intact_backup_of_its_exact_baseline_before_confirmation(self):
        token = self.setup_admin()
        repo_id, _, row = self.dangerous_deletion_review()
        from app.protection import load_review, review_token
        approval = review_token(load_review(row))
        for condition in ("missing", "wrong_baseline", "corrupt", "error"):
            with self.subTest(condition=condition):
                self.main.db.run("DELETE FROM repository_backups")
                if condition != "missing":
                    record = self.backup_record(repo_id, fork_sha="d" * 40 if condition == "wrong_baseline" else "a" * 40,
                                                status="error" if condition == "error" else "ready")
                with mock.patch.object(self.main, "verified_backup", return_value=condition != "corrupt"), \
                     mock.patch.object(self.main.worker, "request_sync") as wake:
                    _, page = self.rendered_page(f"/repos/{repo_id}")
                    self.assertFalse(page.find("form", action=f"/repos/{repo_id}/confirm-deletion"))
                    response = self.client.post(f"/repos/{repo_id}/confirm-deletion", data={"csrf": self.csrf(token), "review": approval})
                self.assertEqual(response.status_code, 409)
                wake.assert_not_called()
                current = self.main.db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
                self.assertEqual(current["sync_requested"], 0)
                self.assertIsNone(current["deletion_approval"])

    def test_repository_details_offer_only_three_newest_ready_backups_and_latest_error(self):
        self.setup_admin()
        repo_id, first = self.repository_with_backup()
        records = [first]
        for digit in "bcde":
            records.append(self.backup_record(repo_id, fork_sha=digit * 40))
        for index, record in enumerate(records):
            self.main.db.run("UPDATE repository_backups SET created_at=?,updated_at=? WHERE id=?",
                             (f"2026-10-28T0{index}:00:00+00:00", f"2026-10-28T0{index}:00:00+00:00", record["id"]))
        older_error = self.backup_record(repo_id, fork_sha="f" * 40, status="error", error="Old backup error")
        latest_error = self.backup_record(repo_id, fork_sha="1" * 40, status="error", error="Backup <disk full>")
        self.main.db.run("UPDATE repository_backups SET updated_at=? WHERE id=?", ("2026-10-28T05:00:00+00:00", older_error["id"]))
        self.main.db.run("UPDATE repository_backups SET updated_at=? WHERE id=?", ("2026-10-28T06:00:00+00:00", latest_error["id"]))
        response, page = self.rendered_page(f"/repos/{repo_id}")
        for record in records[-3:]:
            self.assertTrue(page.find("a", href=f"/backups/{record['id']}/download"))
            self.assertTrue(page.find("a", href=f"/repos/{repo_id}/backups/{record['id']}/restore"))
        for record in records[:-3]:
            self.assertFalse(page.find("a", href=f"/backups/{record['id']}/download"))
        self.assertIn("Backup &lt;disk full&gt;", response.text)
        self.assertNotIn("Backup <disk full>", response.text)
        self.assertNotIn("Old backup error", response.text)
        self.assertNotIn(TEST_PASSWORD, response.text)


if __name__ == "__main__":
    unittest.main()
