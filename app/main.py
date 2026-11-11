"""ForkIQ: GitHub fork sync panel."""
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import DB, log_event, utcnow
from .backups import backup_path, verified_backup
from .errors import error_guidance
from .engine import (Worker, add_manual, app_client_from_settings,
                     client_from_settings, discover, parse_fork_url)
from .github import AuthError, exchange_manifest, write_pem
from .protection import deletion_policy, load_review, parse_limits, review_token
from .versions import annotate, fork_tone, short_time, version_verdict

STATE_DIR = Path(os.environ.get("FORKIQ_STATE_DIR", "/var/lib/forkiq"))
PEM_PATH = os.environ.get("FORKIQ_PEM_PATH", "/var/lib/forkiq/app.pem")
BASE = Path(__file__).parent
SESSION_TTL = 7 * 86400
MANIFEST_URL = "https://github.com/settings/apps/new"

app = FastAPI(title="ForkIQ", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=BASE / "templates")
# Stored timestamps are UTC ISO strings; the panel renders them short and the
# browser re-renders them in the viewer's own timezone.
templates.env.filters["short_time"] = short_time
templates.env.filters["error_guidance"] = error_guidance

STATUS_GROUPS = {
    "synced": ("synced",),
    "waiting": ("behind", "pending"),
    "attention": ("blocked", "diverged", "needs_attention", "error", "missing"),
    "paused": ("paused",),
}


def asset_version(*names):
    """Content hash for static assets, so a changed file always busts caches.

    A hand-maintained `?v=` string once stayed constant across two deploys and
    left browsers on stale CSS; deriving it from file contents makes that
    impossible.
    """
    digest = hashlib.sha256()
    for name in names:
        digest.update((BASE / "static" / name).read_bytes())
    return digest.hexdigest()[:10]


ASSET_VERSION = asset_version("app.css", "app.js")


class VersionedStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path in ("app.css", "app.js") and Request(scope).query_params.get("v") == ASSET_VERSION:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", VersionedStaticFiles(directory=BASE / "static"), name="static")


db = DB(STATE_DIR / "forkiq.db")
worker = Worker(STATE_DIR, PEM_PATH)
_login_failures = {}
LOGIN_WINDOW = 60
LOGIN_MAX = 5


# -- auth helpers ----------------------------------------------------------
def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return salt.hex() + ":" + digest.hex()


def verify_password(password, stored):
    try:
        salt_hex, _ = stored.split(":", 1)
        return hmac.compare_digest(hash_password(password, bytes.fromhex(salt_hex)), stored)
    except (ValueError, AttributeError):
        return False


def csrf_for(token):
    return hmac.new(token.encode(), b"forkiq-csrf", hashlib.sha256).hexdigest()


def current_session(request):
    token = request.cookies.get("forkiq_session")
    if not token:
        return None
    row = db.one("SELECT * FROM sessions WHERE token=?", (token,))
    if not row or row["expires_at"] < time.time():
        return None
    return row


def is_secure(request):
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"




def render(request, name, **context):
    token = request.cookies.get("forkiq_session", "")
    context.update(csrf=csrf_for(token) if token else "",
                   setup_done=db.get_setting("setup_done") == "1",
                   asset_version=ASSET_VERSION)
    return templates.TemplateResponse(request, name, context)


def require_login(request):
    if db.get_setting("setup_done") != "1":
        return RedirectResponse("/setup", status_code=302)
    if not current_session(request):
        return RedirectResponse("/login", status_code=302)
    return None


def check_csrf(request, token):
    session = current_session(request)
    if not session or not hmac.compare_digest(csrf_for(session["token"]), token or ""):
        return JSONResponse({"error": "CSRF 校验失败"}, status_code=403)
    return None


@app.on_event("startup")
def startup():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Compiling the templates on the first request made the panel's first paint
    # after every restart take about a second; warm the cache up front instead.
    for name in ("base.html", "shell.html", "overview.html", "repos.html",
                 "repo_detail.html", "settings.html", "login.html", "setup.html",
                 "partials/repo_table.html", "partials/overview_regions.html"):
        templates.get_template(name)
    worker.start()


# -- public routes ---------------------------------------------------------
@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    if db.get_setting("setup_done") == "1":
        return RedirectResponse("/", status_code=302)
    return render(request, "setup.html", error=None)


@app.post("/setup", response_class=HTMLResponse)
def setup_submit(request: Request, password: str = Form(""), password2: str = Form("")):
    if db.get_setting("setup_done") == "1":
        return RedirectResponse("/", status_code=302)
    if len(password) < 8:
        return render(request, "setup.html", error="密码至少 8 位")
    if password != password2:
        return render(request, "setup.html", error="两次输入的密码不一致")
    db.set_setting("admin_pass_hash", hash_password(password))
    db.set_setting("setup_done", "1")
    log_event(db, None, "auth", "管理员密码已设置")
    return RedirectResponse("/github/connect", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if db.get_setting("setup_done") != "1":
        return RedirectResponse("/setup", status_code=302)
    return render(request, "login.html", error=None)


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, password: str = Form("")):
    if db.get_setting("setup_done") != "1":
        return RedirectResponse("/setup", status_code=302)
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    failures = [t for t in _login_failures.get(ip, []) if now - t < LOGIN_WINDOW]
    if len(failures) >= LOGIN_MAX:
        _login_failures[ip] = failures
        return render(request, "login.html", error="尝试过多，请 1 分钟后再试")
    if not verify_password(password, db.get_setting("admin_pass_hash", "")):
        failures.append(now)
        _login_failures[ip] = failures
        return render(request, "login.html", error="密码不正确")
    _login_failures.pop(ip, None)
    token = secrets.token_urlsafe(32)
    db.run("INSERT INTO sessions(token,created_at,expires_at) VALUES(?,?,?)",
           (token, now, now + SESSION_TTL))
    response = RedirectResponse("/", status_code=302)
    response.set_cookie("forkiq_session", token, httponly=True, secure=is_secure(request),
                        samesite="lax", max_age=SESSION_TTL)
    return response


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get("forkiq_session")
    if token:
        db.run("DELETE FROM sessions WHERE token=?", (token,))
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("forkiq_session")
    return response


# -- GitHub App connection -------------------------------------------------
def public_url():
    value = os.environ.get("FORKIQ_PUBLIC_URL", "")
    if not value:
        raise ValueError("请先设置 FORKIQ_PUBLIC_URL 为本实例的公网地址，再重启服务，例如 https://fork.example.com。")
    try:
        parsed = urlsplit(value)
        if (any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
                or parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or "?" in value or "#" in value
                or "\\" in value or "%" in parsed.netloc or parsed.netloc.endswith(":")):
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        if ":" in parsed.hostname:
            from ipaddress import IPv6Address
            if not re.fullmatch(r"\[[^]]+\](?::[0-9]+)?", parsed.netloc):
                raise ValueError
            IPv6Address(parsed.hostname)
        else:
            hostname = parsed.hostname.encode("idna").decode("ascii").rstrip(".")
            if len(hostname) > 253 or not all(
                    re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                    for label in hostname.split(".")):
                raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError("FORKIQ_PUBLIC_URL 必须是有效的 http(s) 根地址，可带端口；不能包含账号、子路径、查询参数或片段。") from None
    return f"{parsed.scheme}://{parsed.netloc}"


def manifest_payload():
    # Never derive credential callback destinations from untrusted Host headers.
    base_url = public_url()
    state = secrets.token_urlsafe(16)
    db.set_setting("github_manifest_state", state)
    return state, {
        "name": "ForkIQ",
        "url": base_url,
        "hook_attributes": {"url": f"{base_url}/healthz", "active": False},
        "redirect_url": f"{base_url}/api/github/callback",
        "callback_urls": [f"{base_url}/api/github/callback"],
        "public": False,
        "request_oauth_on_install": False,
        "setup_on_update": False,
        "default_permissions": {"contents": "write", "administration": "write",
                                "metadata": "read", "workflows": "write",
                                "actions": "write"},
        "default_events": [],
    }


@app.get("/github/connect", response_class=HTMLResponse)
def github_connect(request: Request):
    guard = require_login(request)
    if guard:
        return guard
    return github_connection_page(request)


def github_connection_page(request, detect_error=""):
    app_slug = db.get_setting("github_app_slug")
    state, payload, config_error = "", {}, ""
    if not app_slug:
        try:
            state, payload = manifest_payload()
        except ValueError as error:
            config_error = str(error)
    return render(request, "connect.html", manifest=json.dumps(payload, ensure_ascii=False),
                  manifest_url=MANIFEST_URL, state=state,
                  app_slug=app_slug,
                  installation_id=db.get_setting("github_installation_id"),
                  auth_error=db.get_setting("auth_error", ""),
                  config_error=config_error, detect_error=detect_error)


@app.get("/api/github/callback", response_class=HTMLResponse)
def github_callback(request: Request, code: str = "", state: str = ""):
    expected = db.get_setting("github_manifest_state")
    if not code or not expected or state != expected:
        return render(request, "message.html", title="连接失败", body="缺少授权码或会话已过期，请重新发起连接。")
    try:
        payload = exchange_manifest(code)
    except Exception as error:
        return render(request, "message.html", title="连接失败", body=f"兑换 App 凭据失败：{error}")
    db.set_setting("github_app_id", payload["id"])
    db.set_setting("github_app_slug", payload.get("slug", ""))
    write_pem(PEM_PATH, payload["pem"])
    db.set_setting("github_manifest_state", "")
    log_event(db, None, "auth", f"GitHub App 已创建：{payload.get('slug')}（id {payload['id']}）")
    slug = payload.get("slug", "")
    return render(request, "install.html", slug=slug,
                  install_url=f"https://github.com/apps/{slug}/installations/new")


@app.post("/github/detect", response_class=HTMLResponse)
def github_detect(request: Request, csrf: str = Form("")):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    client = app_client_from_settings(db)
    if client is None:
        return github_connection_page(request, detect_error="请先完成 GitHub App 创建。")
    try:
        installations = client.app_installations()
    except AuthError as error:
        return github_connection_page(request, detect_error=f"检测失败：{error}")
    if not installations:
        return github_connection_page(request, detect_error="尚未安装：请先在 GitHub 完成安装，再返回检测。")
    installation = installations[0]
    db.set_setting("github_installation_id", installation["id"])
    account = (installation.get("account") or {}).get("login", "?")
    log_event(db, None, "auth", f"GitHub App 已安装到 {account}（installation {installation['id']}）")
    return RedirectResponse("/", status_code=302)


# -- panel routes ----------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    guard = require_login(request)
    if guard:
        return guard
    return render(request, "overview.html", **_overview_context())


def _overview_context(revision=None):
    revision = revision or _overview_revision()
    stats = {row["status"]: row["n"] for row in
             db.all("SELECT status, COUNT(*) AS n FROM repositories GROUP BY status")}
    counts = {group: sum(stats.get(status, 0) for status in statuses)
              for group, statuses in STATUS_GROUPS.items()}
    events = db.all("SELECT e.*, r.full_name FROM events e LEFT JOIN repositories r ON r.id=e.repo_id "
                    "ORDER BY e.id DESC LIMIT 20")
    return {"stats": stats, "total": sum(stats.values()), "synced": counts["synced"],
            "behind": counts["waiting"], "blocked": counts["attention"],
            "paused": counts["paused"], "events": events,
            "auth_error": db.get_setting("auth_error", ""),
            "app_connected": bool(db.get_setting("github_installation_id")),
            "revision": revision}


def _overview_revision():
    return _revision("SELECT ?, (SELECT COALESCE(MAX(id),0) FROM events), "
                     "(SELECT COUNT(*) FROM events), "
                     "(SELECT value FROM settings WHERE key='auth_error'), "
                     "(SELECT value FROM settings WHERE key='github_installation_id')",
                     (_repos_revision(""),))


@app.get("/repos", response_class=HTMLResponse)
def repo_list(request: Request, filter: str = ""):
    guard = require_login(request)
    if guard:
        return guard
    return render(request, "repos.html", **_repo_list_context(filter),
                  notice=request.query_params.get("notice", ""))


def _repo_filter(filter):
    if not filter:
        return "", ()
    statuses = STATUS_GROUPS.get(filter, (filter,))
    return " WHERE status IN (" + ",".join("?" for _ in statuses) + ")", statuses


def _repo_rows(filter):
    where, params = _repo_filter(filter)
    return db.all("SELECT * FROM repositories" + where +
                  " ORDER BY status='diverged' DESC, status='blocked' DESC, full_name", params)


def _repo_list_context(filter):
    revision = _repos_revision(filter)
    return {"repos": annotate(_repo_rows(filter)), "filter": filter, "revision": revision,
            "auto_discover": db.get_setting("auto_discover", "1") == "1"}


def _repos_revision(filter):
    where, params = _repo_filter(filter)
    # MAX timestamps miss changes to a different row in the same second. Hash the
    # displayed fields instead, without building version labels or fetching events.
    return _revision(
        "SELECT id, full_name, upstream_full_name, status, enabled, last_error, last_check_at, "
        "fork_sha, upstream_sha, fork_tag, upstream_tag, fork_commit_at, upstream_commit_at, "
        "upstream_gone, last_merged_upstream_sha, upstream_ahead, fork_ahead "
        "FROM repositories" + where + " ORDER BY id", params)


def _revision(sql, params=()):
    """Hash a small snapshot; the query parameters also identify its filter.

    Cloudflare can remove HTML ETags, so the browser sends this token as `v`.
    An unchanged tick can then skip context construction as well as rendering.
    """
    rows = tuple(tuple(row) for row in db.all(sql, params))
    return hashlib.sha256(repr((params, rows)).encode()).hexdigest()[:16]


def _fragment(request, name, context_factory, revision):
    """Only build the fragment's context when the snapshot has changed."""
    headers = {"Cache-Control": "no-store", "X-ForkIQ-Revision": revision}
    if request.query_params.get("v") == revision:
        return Response(status_code=204, headers=headers)
    html = templates.get_template(name).render(**context_factory())
    return HTMLResponse(html, headers=headers)


@app.get("/partials/repos", response_class=HTMLResponse)
def partial_repos(request: Request, filter: str = ""):
    """Rows only, for the background refresh: no shell, no full page reload."""
    if not current_session(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    revision = _repos_revision(filter)
    return _fragment(request, "partials/repo_table.html",
                     lambda: {"repos": annotate(_repo_rows(filter)), "filter": filter,
                              "revision": revision,
                              "csrf": csrf_for(request.cookies["forkiq_session"])}, revision)


@app.get("/partials/overview", response_class=HTMLResponse)
def partial_overview(request: Request):
    """Stats and recent events only, for the overview's background refresh."""
    if not current_session(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    revision = _overview_revision()
    return _fragment(request, "partials/overview_regions.html",
                     lambda: _overview_context(revision), revision)


@app.get("/repos/{repo_id}", response_class=HTMLResponse)
def repo_detail(request: Request, repo_id: int):
    guard = require_login(request)
    if guard:
        return guard
    repo = db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
    if not repo:
        return render(request, "message.html", title="未找到", body="仓库不在管理列表中。")
    events = db.all("SELECT * FROM events WHERE repo_id=? ORDER BY id DESC LIMIT 50", (repo_id,))
    verdict = version_verdict(repo)
    policy = deletion_policy(db, repo)
    review = load_review(repo)
    backups = db.all("SELECT * FROM repository_backups WHERE full_name=? AND status='ready' "
                     "ORDER BY created_at DESC, id DESC LIMIT 3", (repo["full_name"],))
    backup_error = db.one("SELECT * FROM repository_backups WHERE full_name=? "
                          "ORDER BY updated_at DESC, id DESC LIMIT 1", (repo["full_name"],))
    if backup_error and backup_error["status"] != "error":
        backup_error = None
    baseline = next((item for item in backups if item["fork_sha"] == repo["fork_sha"]), None)
    baseline_ready = bool(baseline and verified_backup(db, baseline))
    can_confirm = bool(review and review.get("reasons") and not review.get("error")
                       and baseline_ready
                       and repo["enabled"] and repo["status"] == "needs_attention"
                       and review.get("policy") == policy
                       and review.get("identity") == {
                           "fork_sha": repo["fork_sha"], "target_sha": repo["upstream_sha"],
                           "upstream": repo["upstream_full_name"], "mode": repo["mode"],
                           "branch": repo["branch"]})
    return render(request, "repo_detail.html", repo=repo, events=events,
                  verdict=verdict, fork_tone=fork_tone(verdict), protection=policy,
                  deletion_review=review, can_confirm_deletion=can_confirm,
                  deletion_token=review_token(review) if can_confirm else "",
                  backups=backups, backup_error=backup_error, baseline_ready=baseline_ready,
                  notice=request.query_params.get("notice", ""))


@app.get("/backups/{backup_id}/download")
def download_backup(request: Request, backup_id: int):
    guard = require_login(request)
    if guard:
        return guard
    record = db.one("SELECT * FROM repository_backups WHERE id=?", (backup_id,))
    if not record:
        return JSONResponse({"error": "备份不存在"}, status_code=404)
    if not verified_backup(db, record):
        return JSONResponse({"error": "备份缺失或校验失败，不能下载"}, status_code=409)
    return FileResponse(backup_path(db, record), media_type="application/octet-stream",
                        filename=f"{record['full_name'].replace('/', '-')}-{record['fork_sha'][:12]}.bundle",
                        headers={"Cache-Control": "no-store"})


def _restore_record(repo_id, backup_id):
    repo = db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
    record = db.one("SELECT * FROM repository_backups WHERE id=?", (backup_id,))
    if not repo or not record or record["full_name"] != repo["full_name"]:
        return None, None
    return repo, record


@app.get("/repos/{repo_id}/backups/{backup_id}/restore", response_class=HTMLResponse)
def restore_preview(request: Request, repo_id: int, backup_id: int):
    guard = require_login(request)
    if guard:
        return guard
    repo, record = _restore_record(repo_id, backup_id)
    if repo is None:
        return JSONResponse({"error": "仓库备份不存在"}, status_code=404)
    if not verified_backup(db, record) or repo["restore_backup_id"] is not None:
        return JSONResponse({"error": "备份不可用或已有回滚正在处理"}, status_code=409)
    client = client_from_settings(db)
    if client is None:
        return JSONResponse({"error": "请先连接 GitHub"}, status_code=409)
    try:
        expected = client.get_ref(repo["full_name"], repo["branch"])
        if not re.fullmatch(r"[a-f0-9]{40}", expected or ""):
            raise ValueError("无效的分支提交")
    except Exception:
        return JSONResponse({"error": "无法核实 GitHub 当前分支，请检查连接后重试"}, status_code=409)
    return render(request, "restore.html", repo=repo, backup=record, expected_sha=expected)


@app.post("/repos/{repo_id}/backups/{backup_id}/restore")
def restore_request(request: Request, repo_id: int, backup_id: int,
                    csrf: str = Form(""), expected_sha: str = Form("")):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    repo, record = _restore_record(repo_id, backup_id)
    if repo is None:
        return JSONResponse({"error": "仓库备份不存在"}, status_code=404)
    if not re.fullmatch(r"[a-f0-9]{40}", expected_sha):
        return JSONResponse({"error": "当前提交无效，请重新打开确认页"}, status_code=400)
    if not verified_backup(db, record) or repo["restore_backup_id"] is not None:
        return JSONResponse({"error": "备份不可用、确认已失效或已有回滚正在处理"}, status_code=409)
    if client_from_settings(db) is None:
        return JSONResponse({"error": "请先连接 GitHub"}, status_code=409)
    changed = db.run("UPDATE repositories SET enabled=0, status='paused', sync_requested=0, "
                     "deletion_approval=NULL, restore_backup_id=?, restore_expected_sha=?, "
                     "last_error=?, updated_at=? WHERE id=? AND restore_backup_id IS NULL",
                     (backup_id, expected_sha, "回滚已排队，保持暂停；请等待事件结果。", utcnow(), repo_id)).rowcount
    if not changed:
        return JSONResponse({"error": "已有回滚正在处理"}, status_code=409)
    log_event(db, repo_id, "check", f"{repo['full_name']} 已请求回滚到 {record['fork_sha']}，同步已暂停")
    worker.request_sync()
    return RedirectResponse(f"/repos/{repo_id}?notice=回滚已排队，同步已暂停；请稍后刷新查看结果", status_code=302)


def _repo_action(request, repo_id, csrf, action, return_filter=None):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    repo = db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
    if not repo:
        return JSONResponse({"error": "仓库不存在"}, status_code=404)
    if repo["restore_backup_id"] is not None:
        return JSONResponse({"error": "回滚正在处理，请等待完成后再操作"}, status_code=409)
    if repo["mode"] == "protected" and action in ("resume", "sync", "selfupdate"):
        return JSONResponse({"error": "独立维护仓库已禁止上游同步。需先切换为安全同步；检测到独有提交仍会停止。"}, status_code=409)
    if action == "pause":
        db.run("UPDATE repositories SET status='paused', enabled=0, deletion_approval=NULL, updated_at=? WHERE id=?",
               (utcnow(), repo_id))
        log_event(db, repo_id, "blocked", f"{repo['full_name']} 已暂停")
    elif action == "resume":
        db.run("UPDATE repositories SET status='pending', enabled=1, last_error=NULL, deletion_approval=NULL, updated_at=? WHERE id=?",
               (utcnow(), repo_id))
        log_event(db, repo_id, "check", f"{repo['full_name']} 已恢复")
    elif action == "sync":
        db.run("UPDATE repositories SET sync_requested=1, updated_at=? WHERE id=?", (utcnow(), repo_id))
        worker.request_sync()
    elif action == "mode":
        mode = "safe" if repo["mode"] == "protected" else "protected"
        enabled = int(mode == "safe")
        db.run("UPDATE repositories SET mode=?, enabled=?, status=?, last_error=NULL, "
               "diverged_at=NULL, diverged_upstream_sha=NULL, sync_requested=?, self_update_interval=NULL, "
               "deletion_review=NULL, deletion_approval=NULL, "
               "updated_at=? WHERE id=?", (mode, enabled, "pending" if enabled else "paused", enabled, utcnow(), repo_id))
        label = "独立维护（禁止同步上游）" if mode == "protected" else "安全同步（仅快进，有独有提交立即停止）"
        log_event(db, repo_id, "check", f"{repo['full_name']} 同步方式切换为{label}")
        worker.request_sync()
    elif action == "selfupdate":
        if repo["self_update_interval"]:
            db.run("UPDATE repositories SET self_update_interval=NULL, self_update=NULL, "
                   "updated_at=? WHERE id=?", (utcnow(), repo_id))
            log_event(db, repo_id, "check", f"{repo['full_name']} 已关闭自更新保活")
        else:
            client = client_from_settings(db)
            if client is None:
                return render(request, "message.html", title="尚未配置",
                              body="请先完成 GitHub App 配置。")
            notes = []
            try:
                if not client.actions_enabled(repo["full_name"]):
                    client.set_actions_enabled(repo["full_name"], True)
                    notes.append("已启用 Actions")
                perms = client.workflow_permissions(repo["full_name"])
                if perms.get("default_workflow_permissions") != "write":
                    client.set_workflow_permissions(repo["full_name"], "write")
                    notes.append("workflow 权限设为 write")
                enabled = []
                for workflow in client.list_workflows(repo["full_name"]):
                    if workflow.get("state") in ("disabled_fork", "disabled_inactivity"):
                        client.enable_workflow(repo["full_name"], workflow["id"])
                        enabled.append(workflow["name"])
                if enabled:
                    notes.append("已启用 " + "、".join(enabled))
            except Exception as error:
                db.run("UPDATE repositories SET self_update=?, updated_at=? WHERE id=?",
                       (f"开启自更新部分失败：{str(error)[:200]}", utcnow(), repo_id))
                return render(request, "message.html", title="开启失败",
                              body=f"GitHub 拒绝：{error}。"
                                   "在 https://github.com/settings/apps/forkiq/permissions "
                                   "给 Actions 加读写权限并批准后可重试。")
            db.run("UPDATE repositories SET self_update_interval=1, self_update=NULL, "
                   "sync_requested=1, updated_at=? WHERE id=?", (utcnow(), repo_id))
            log_event(db, repo_id, "check",
                      f"{repo['full_name']} 已开启自更新保活" +
                      (f"（{'; '.join(notes)}）" if notes else ""))
            worker.request_sync()
    elif action == "remove":
        db.run("DELETE FROM repositories WHERE id=?", (repo_id,))
        log_event(db, None, "check", f"{repo['full_name']} 已移出管理（GitHub 仓库未改动）")
    if action in ("pause", "resume") and return_filter in ("all", *STATUS_GROUPS, "diverged", "blocked"):
        label = "已暂停同步" if action == "pause" else "已恢复同步，等待检查"
        query = urlencode({"filter": "" if return_filter == "all" else return_filter,
                           "notice": f"{repo['full_name']} {label}"})
        return RedirectResponse(f"/repos?{query}#repo-{repo_id}", status_code=302)
    return RedirectResponse(f"/repos/{repo_id}" if action != "remove" else "/repos", status_code=302)


@app.post("/repos/{repo_id}/protection")
def repo_protection(request: Request, repo_id: int, csrf: str = Form(""),
                    protection_mode: str = Form("inherit"), max_files: str = Form(""),
                    max_percent: str = Form("")):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    repo = db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
    if not repo:
        return JSONResponse({"error": "仓库不存在"}, status_code=404)
    if repo["restore_backup_id"] is not None:
        return JSONResponse({"error": "回滚正在处理，请等待完成后再操作"}, status_code=409)
    try:
        if protection_mode not in ("inherit", "on", "off"):
            raise ValueError("请选择有效的保护开关")
        count, percent = parse_limits(max_files if max_files else 100,
                                      max_percent if max_percent else 50)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    enabled = {"inherit": None, "on": 1, "off": 0}[protection_mode]
    db.run("UPDATE repositories SET deletion_protection=?, deletion_max_files=?, "
           "deletion_max_percent=?, deletion_approval=NULL, updated_at=? WHERE id=?",
           (enabled, count if max_files else None, percent if max_percent else None, utcnow(), repo_id))
    log_event(db, repo_id, "check", "已修改异常删除保护，未使用的单次确认已失效")
    worker.request_sync()
    return RedirectResponse(f"/repos/{repo_id}?notice=保护设置已保存", status_code=302)


@app.post("/repos/{repo_id}/confirm-deletion")
def confirm_deletion(request: Request, repo_id: int, csrf: str = Form(""),
                     review: str = Form("")):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    repo = db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
    if not repo:
        return JSONResponse({"error": "仓库不存在"}, status_code=404)
    snapshot = load_review(repo)
    identity = {"fork_sha": repo["fork_sha"], "target_sha": repo["upstream_sha"],
                "upstream": repo["upstream_full_name"], "mode": repo["mode"], "branch": repo["branch"]}
    if (not snapshot or snapshot.get("error") or not snapshot.get("reasons")
            or snapshot.get("identity") != identity or snapshot.get("policy") != deletion_policy(db, repo)
            or not repo["enabled"] or repo["status"] != "needs_attention"
            or not re.fullmatch(r"[a-f0-9]{64}", review)
            or not hmac.compare_digest(review_token(snapshot), review)):
        return JSONResponse({"error": "检测结果已变化、未完成或仓库已暂停，请重新检查后确认"}, status_code=409)
    baseline = db.one("SELECT * FROM repository_backups WHERE full_name=? AND fork_sha=? AND status='ready'",
                      (repo["full_name"], repo["fork_sha"]))
    if not baseline or not verified_backup(db, baseline):
        return JSONResponse({"error": "当前版本尚无可用备份，不能确认删除；请先重新检查"}, status_code=409)
    changed = db.run("UPDATE repositories SET deletion_approval=?, sync_requested=1, updated_at=? "
                     "WHERE id=? AND deletion_review=? AND enabled=1 AND status='needs_attention'",
                     (review, utcnow(), repo_id, repo["deletion_review"])).rowcount
    if not changed:
        return JSONResponse({"error": "检测结果已变化，请刷新后确认"}, status_code=409)
    log_event(db, repo_id, "check", f"已确认此次变更：{identity['target_sha']}；仅允许一次，执行前重新核验")
    worker.request_sync()
    return RedirectResponse(f"/repos/{repo_id}?notice=已确认此目标提交，等待同步前复核", status_code=302)


@app.post("/repos/{repo_id}/actions")
def repo_actions_toggle(request: Request, repo_id: int, enabled: str = Form("0"), csrf: str = Form("")):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    repo = db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
    if not repo:
        return JSONResponse({"error": "仓库不存在"}, status_code=404)
    if repo["restore_backup_id"] is not None:
        return JSONResponse({"error": "回滚正在处理，请等待完成后再操作"}, status_code=409)
    client = client_from_settings(db)
    if client is None:
        return render(request, "message.html", title="尚未配置", body="请先完成 GitHub App 配置。")
    want = enabled == "1"
    try:
        client.set_actions_enabled(repo["full_name"], want)
    except Exception as error:
        db.run("UPDATE repositories SET last_error=?, updated_at=? WHERE id=?",
               (f"Actions 开关失败：{error}"[:300], utcnow(), repo_id))
        return render(request, "message.html", title="操作失败", body=f"修改 Actions 失败：{error}")
    db.run("UPDATE repositories SET actions_disabled=?, updated_at=? WHERE id=?",
           (int(not want), utcnow(), repo_id))
    log_event(db, repo_id, "check", f"{repo['full_name']} Actions 已{'启用' if want else '禁用'}")
    return RedirectResponse(f"/repos/{repo_id}", status_code=302)


@app.post("/repos/{repo_id}/{action}")
def repo_action(request: Request, repo_id: int, action: str, csrf: str = Form(""),
                return_filter: str | None = Form(None)):
    if action not in ("pause", "resume", "sync", "remove", "mode", "selfupdate"):
        return JSONResponse({"error": "不支持的操作"}, status_code=400)
    return _repo_action(request, repo_id, csrf, action, return_filter)


@app.post("/repos/add", response_class=HTMLResponse)
def repo_add(request: Request, url: str = Form(""), csrf: str = Form("")):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    client = client_from_settings(db)
    if client is None:
        return render(request, "message.html", title="尚未配置", body="请先完成 GitHub App 配置。")
    try:
        repo_id = add_manual(db, client, url)
    except ValueError as error:
        return render(request, "repos.html", **_repo_list_context(""), add_error=str(error))
    worker.request_sync()
    return RedirectResponse(f"/repos/{repo_id}", status_code=302)


@app.post("/discover")
def discover_now(request: Request, csrf: str = Form("")):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    db.set_setting("discover_requested", "1")
    worker.request_sync()
    return RedirectResponse("/repos", status_code=302)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    guard = require_login(request)
    if guard:
        return guard
    return render(request, "settings.html",
                  interval_minutes=f'{int(db.get_setting("check_interval", "30")) / 60:g}',
                  auto_discover=db.get_setting("auto_discover", "1") == "1",
                  app_slug=db.get_setting("github_app_slug"),
                  installation_id=db.get_setting("github_installation_id"),
                  auth_error=db.get_setting("auth_error", ""),
                  protection=deletion_policy(db),
                  notice=request.query_params.get("notice", ""))


@app.post("/settings", response_class=HTMLResponse)
def settings_save(request: Request, csrf: str = Form(""),
                  interval_minutes: str = Form(""), interval: str = Form(""),
                  auto_discover: str = Form("0")):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    try:
        # Accept the pre-deploy form's seconds field so an already-open settings
        # page cannot fail with a validation error during a rolling deploy.
        minutes = float(interval_minutes) if interval_minutes else float(interval) / 60
        if not math.isfinite(minutes):
            raise ValueError
        value = round(max(0.25, min(5, minutes)) * 60)
    except ValueError:
        return RedirectResponse("/settings?notice=检查间隔必须是有效的分钟数，设置未保存", status_code=302)
    db.set_setting("check_interval", value)
    db.set_setting("auto_discover", "1" if auto_discover == "1" else "0")
    worker.request_sync()
    return RedirectResponse("/settings?notice=设置已保存", status_code=302)


@app.post("/settings/deletion-protection")
def settings_protection(request: Request, csrf: str = Form(""), enabled: str = Form("0"),
                        max_files: str = Form(""), max_percent: str = Form("")):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    try:
        if enabled not in ("0", "1"):
            raise ValueError("请选择有效的保护开关")
        count, percent = parse_limits(max_files, max_percent)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    for key, value in (("deletion_protection", enabled), ("deletion_max_files", count),
                       ("deletion_max_percent", percent)):
        db.set_setting(key, value)
    db.run("UPDATE repositories SET deletion_approval=NULL")
    log_event(db, None, "check", "已修改系统异常删除保护，未使用的单次确认已失效")
    worker.request_sync()
    return RedirectResponse("/settings?notice=删除保护设置已保存#protection", status_code=302)


@app.post("/settings/password", response_class=HTMLResponse)
def settings_password(request: Request, csrf: str = Form(""),
                      current: str = Form(""), password: str = Form(""), password2: str = Form("")):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    if not verify_password(current, db.get_setting("admin_pass_hash", "")):
        return RedirectResponse("/settings?notice=当前密码不正确", status_code=302)
    if len(password) < 8 or password != password2:
        return RedirectResponse("/settings?notice=新密码至少 8 位且两次一致", status_code=302)
    db.set_setting("admin_pass_hash", hash_password(password))
    db.run("DELETE FROM sessions")
    log_event(db, None, "auth", "管理员密码已修改")
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("forkiq_session")
    return response


@app.post("/settings/disconnect")
def settings_disconnect(request: Request, csrf: str = Form("")):
    guard = require_login(request)
    if guard:
        return guard
    denied = check_csrf(request, csrf)
    if denied:
        return denied
    db.set_setting("github_installation_id", "")
    db.set_setting("auth_error", "")
    log_event(db, None, "auth", "已断开 GitHub 连接（App 安装本身需在 GitHub 上卸载）")
    return RedirectResponse("/settings?notice=已断开连接", status_code=302)
