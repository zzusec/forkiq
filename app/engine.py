"""Sync engine: poll upstream refs and fast-forward forks via the GitHub API."""
import fcntl
import re
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .db import DB, log_event, insert_repository, utcnow
from .backups import ensure_backup
from .github import (AuthError, DispatchRejected, Forbidden, GitHub, MergeConflict,
                     RateLimited, RefNotFastForward, RefNotFound, RefObjectNotFound)
from .protection import (assess_change, change_identity, deletion_policy, load_review,
                         review_json, review_token, valid_comparison)

BATCH = 50
ERROR_RETRY_SECONDS = 600
# A repo whose comparison counts are unknown is probed at most this often, so a
# probe that keeps failing cannot turn into one extra API call per cycle.
COUNTS_PROBE_SECONDS = 6 * 3600
NODES_QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Repository {
      id nameWithOwner isFork isArchived
      defaultBranchRef { name target { ... on Commit { oid committedDate } } }
      refs(refPrefix: "refs/tags/", first: 1, orderBy: {field: TAG_COMMIT_DATE, direction: DESC}) {
        nodes { name target { oid } }
      }
      parent {
        nameWithOwner
        defaultBranchRef { name target { ... on Commit { oid committedDate } } }
        refs(refPrefix: "refs/tags/", first: 1, orderBy: {field: TAG_COMMIT_DATE, direction: DESC}) {
          nodes { name target { oid } }
        }
      }
    }
  }
}
"""
FORK_URL = re.compile(
    r"^https?://github\.com/(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?"
    r"(?:/(?:tree|blob|commits|releases|issues|pull)/.*)?/?$")


def parse_fork_url(url):
    match = FORK_URL.match((url or "").strip())
    if not match:
        raise ValueError("请输入形如 https://github.com/owner/repo 的仓库链接")
    return match.group("owner"), match.group("name")


def _throttled(db, key, minutes=5):
    last = db.get_setting(f"throttle:{key}")
    if last:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
            if age.total_seconds() < minutes * 60:
                return True
        except ValueError:
            pass
    db.set_setting(f"throttle:{key}", utcnow())
    return False


def client_from_settings(db):
    app_id = db.get_setting("github_app_id")
    pem = db.get_setting("github_pem_path")
    installation_id = db.get_setting("github_installation_id")
    if not (app_id and pem and installation_id):
        return None
    return GitHub(app_id=app_id, pem_path=pem, installation_id=installation_id)


def app_client_from_settings(db):
    """App-level client (App JWT only) for installation listing, pre-install."""
    app_id = db.get_setting("github_app_id")
    pem = db.get_setting("github_pem_path")
    if not (app_id and pem):
        return None
    return GitHub(app_id=app_id, pem_path=pem)


def _set_repo(db, repo_id, **fields):
    fields["updated_at"] = utcnow()
    columns = ", ".join(f"{key}=?" for key in fields)
    db.run(f"UPDATE repositories SET {columns} WHERE id=?", (*fields.values(), repo_id))


def _latest_tag(ref):
    """Newest tag name reachable from a ref, or None when the ref has no tags.

    The batched GraphQL query asks each side's default branch for its single
    most recent tag (ordered by commit date), so a fork that never retagged
    still reports the upstream tag it inherited.
    """
    nodes = ((ref or {}).get("refs") or {}).get("nodes") or []
    return nodes[0]["name"] if nodes else None


def _version_fields(node, parent):
    """Human-readable version columns for one repository (tags + commit dates).

    `refs` sits on the repository (all tags, newest first); the commit dates
    come from each side's default-branch head.
    """
    fork_target = ((node.get("defaultBranchRef") or {}).get("target")) or {}
    parent_target = ((parent.get("defaultBranchRef") or {}).get("target")) or {}
    return {
        "fork_tag": _latest_tag(node),
        "upstream_tag": _latest_tag(parent),
        "fork_commit_at": fork_target.get("committedDate"),
        "upstream_commit_at": parent_target.get("committedDate"),
    }


def _count_fields(payload):
    """Commit counts behind the list's comparison label.

    Kept next to the two SHAs so the panel can name *why* they differ without a
    probe on every page load. compare(base=fork, head=upstream) reports both
    counts from the upstream branch's point of view: ahead_by is how many
    upstream commits the fork lacks (the local side is behind by that many),
    behind_by is how many commits are unique to the fork (it is ahead by that
    many). An unprobeable comparison returns no counts, leaving whatever was
    stored before in place rather than claiming zero.
    """
    if not isinstance(payload, dict):
        return {}
    return {"upstream_ahead": int(payload.get("ahead_by") or 0),
            "fork_ahead": int(payload.get("behind_by") or 0)}


def _counts_probe_due(db, repo_id):
    """Whether a repo with unknown counts may be probed again."""
    stamp = db.get_setting(f"counts_probe:{repo_id}")
    if not stamp:
        return True
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
    except ValueError:
        return True
    return age.total_seconds() >= COUNTS_PROBE_SECONDS


def _counts_missing(row):
    """True while a row has never had a comparison count recorded."""
    return row["upstream_ahead"] is None or row["fork_ahead"] is None


def _probe_counts(db, client, row, fork_branch):
    """One compare probe that fills counts for a repo whose SHAs never move."""
    repo_id = row["id"]
    db.set_setting(f"counts_probe:{repo_id}", utcnow())
    head = f"{row['upstream_full_name'].split('/')[0]}:{fork_branch}"
    try:
        payload = client.compare(row["full_name"], fork_branch, head)
    except (RateLimited, AuthError):
        raise
    except Exception:
        return {}
    return _count_fields(payload)


def _parents_by_node(client, node_ids):
    """Batch-fetch parent full names for fork node IDs via GraphQL.

    The REST endpoint /installation/repositories does not include `parent`,
    so fork→upstream mapping has to come from GraphQL nodes(ids:).
    """
    parents = {}
    for start in range(0, len(node_ids), BATCH):
        chunk = node_ids[start:start + BATCH]
        data = client.graphql(NODES_QUERY, {"ids": chunk})
        for node in data.get("nodes") or []:
            if node and (node.get("parent") or {}).get("nameWithOwner"):
                parents[node["id"]] = node["parent"]["nameWithOwner"]
    return parents


def discover(db, client, *, manual=False):
    """Import forks visible to the installation; mark vanished ones missing."""
    account = None
    repos = client.installation_repos()
    present_by_name = {repo["full_name"]: repo for repo in repos}
    forks = [repo for repo in repos if repo.get("fork") and repo.get("node_id")]
    parents = _parents_by_node(client, [repo["node_id"] for repo in forks])
    known = {row["full_name"]: row for row in db.all("SELECT * FROM repositories")}
    added = 0
    for repo in repos:
        if not repo.get("fork"):
            continue
        parent_full = parents.get(repo.get("node_id"))
        if not parent_full:
            continue
        if account is None:
            account = repo["owner"]["login"]
        if repo["owner"]["login"] != account:
            continue
        full = repo["full_name"]
        if full in known:
            row = known.pop(full)
            fields = {"node_id": repo["node_id"]}
            if row["status"] == "missing":
                fields.update(status="pending", last_error=None)
            if repo["archived"] != row["is_archived"]:
                fields["is_archived"] = int(bool(repo["archived"]))
            _set_repo(db, row["id"], **fields)
            continue
        repo_id = insert_repository(
            db, owner=repo["owner"]["login"], name=repo["name"], full_name=full,
            upstream_full_name=parent_full, branch=repo["default_branch"],
            node_id=repo["node_id"], status="pending", discovered=1,
            is_archived=int(bool(repo["archived"])))
        log_event(db, repo_id, "discover", f"发现新 fork {full}（上游 {parent_full}）")
        added += 1
    for full, row in known.items():
        present = present_by_name.get(full)
        if present is None:
            # The installation cannot see it; deletion and revoked access are
            # indistinguishable here, so do not claim the repository was deleted.
            if row["status"] != "missing":
                message = "GitHub App 无法访问此仓库（可能已删除、转移或移出授权范围），请检查仓库和安装授权"
                _set_repo(db, row["id"], status="missing", last_error=message)
                log_event(db, row["id"], "blocked", f"{full} {message}")
            continue
        if not present.get("fork"):
            # The repository still exists but is no longer a fork: its upstream
            # was deleted and GitHub promoted the fork to a standalone repo.
            # Keep managing it; never report it as gone or drop it. sync_one
            # owns the status so it stays consistent within a cycle.
            if not row["upstream_gone"]:
                _set_repo(db, row["id"], upstream_gone=1)
    db.set_setting("last_discover_at", utcnow())
    if added or manual:
        log_event(db, None, "discover", f"自动发现完成：新增 {added} 个 fork")
    return added


def add_manual(db, client, url):
    owner, name = parse_fork_url(url)
    full = f"{owner}/{name}"
    existing = db.one("SELECT id FROM repositories WHERE full_name=?", (full,))
    if existing:
        raise ValueError(f"{full} 已在管理列表中")
    repo = client.get_repo(full)
    if not repo.get("fork") or not (repo.get("parent") or {}).get("full_name"):
        raise ValueError(f"{full} 不是 fork，无法自动同步")
    repo_id = insert_repository(
        db, owner=owner, name=name, full_name=full,
        upstream_full_name=repo["parent"]["full_name"],
        branch=repo["default_branch"], node_id=repo["node_id"], status="pending",
        is_archived=int(bool(repo["archived"])))
    log_event(db, repo_id, "discover", f"手动添加 {full}（上游 {repo['parent']['full_name']}）")
    return repo_id


def _mark_diverged(db, row, upstream_sha, message, version=None):
    _set_repo(db, row["id"], status="diverged", diverged_at=utcnow(),
              diverged_upstream_sha=upstream_sha, last_error=message, **(version or {}))
    log_event(db, row["id"], "blocked", f"{row['full_name']} {message}")


def _merge_retry_due(row):
    """needs_attention after a merge conflict: retry at most every ERROR_RETRY_SECONDS."""
    if not row["last_check_at"]:
        return True
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["last_check_at"])
        return age.total_seconds() >= ERROR_RETRY_SECONDS
    except ValueError:
        return True


SELF_UPDATE_STALE_SECONDS = 24 * 3600
SELF_UPDATE_MIN_INTERVAL = 3600      # at most one keep-alive dispatch per hour per repo
SELF_UPDATE_CHECK_INTERVAL = 600     # staleness checks at most every 10 minutes per repo
UPSTREAM_ACTIVE_GRACE_SECONDS = 24 * 3600   # fork takes over after 24h of upstream silence
SELF_WORKFLOW_NAME_HINTS = ("sync", "update", "checkin", "cron", "refresh", "data",
                            "mirror", "更新", "同步", "签到")
# Never auto-dispatch these: heavy build pipelines (not "data") and anything
# that mass-sends messages or attacks (SMS bombing, spam, flood tooling).
SELF_EXCLUDE_WORDS = ("编译", "打包", "固件", "内核", "镜像", "build", "compile", "package",
                      "firmware", "kernel", "docker", "image", "release", "deploy", "publish",
                      "sms", "bomb", "spam", "flood", "ddos", "attack", "phish")


def _self_workflow_score(name, path, text):
    """Score a workflow as a self-update candidate; None when ineligible.

    Only dispatchable (workflow_dispatch) non-excluded workflows qualify; a
    schedule trigger or a data-producing name adds preference.
    """
    head = text.split("jobs:")[0]
    if "workflow_dispatch:" not in head:
        return None
    haystack = f"{name} {path}".lower()
    if any(word in haystack for word in SELF_EXCLUDE_WORDS):
        return None
    score = 0
    if "schedule:" in head:
        score += 3
    if any(hint in (name or "").lower() for hint in SELF_WORKFLOW_NAME_HINTS):
        score += 2
    if score == 0:
        return None
    return score


def _upstream_is_active(client, row):
    """True while the upstream repo received a push within the grace window.

    The fork only takes over when upstream stops producing commits (dead
    Actions, abandoned repo); while upstream is alive the fork stays on
    standby to avoid two independent data producers colliding on merge.
    """
    try:
        upstream = client.get_repo(row["upstream_full_name"])
    except (RateLimited, AuthError):
        raise
    except Exception:
        return False    # upstream gone/unreadable: the fork should take over
    pushed_at = (upstream or {}).get("pushed_at")
    if not pushed_at:
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(
            pushed_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return age.total_seconds() < UPSTREAM_ACTIVE_GRACE_SECONDS


def _pick_self_workflow(db, client, row, candidates):
    """Choose which workflow to keep alive (cached per repo).

    Candidates are scored by trigger and name (see _self_workflow_score); the
    pick is cached, and workflows whose dispatch was rejected are skipped.
    Returns None when nothing qualifies.
    """
    repo_id = row["id"]
    bad = {part for part in db.get_setting(f"self_wf_bad:{repo_id}", "").split(",") if part}
    usable = [w for w in candidates if str(w["id"]) not in bad]
    cached = db.get_setting(f"self_wf:{repo_id}")
    if cached:
        for workflow in usable:
            if str(workflow["id"]) == cached:
                haystack = f"{workflow.get('name') or ''} {workflow['path']}".lower()
                if not any(word in haystack for word in SELF_EXCLUDE_WORDS):
                    return workflow
    scored = []
    for workflow in usable[:8]:
        try:
            text = client.get_file_text(row["full_name"], workflow["path"])
        except (RateLimited, AuthError):
            raise
        except Exception:
            text = ""
        score = _self_workflow_score(workflow.get("name") or "", workflow["path"], text)
        if score is not None:
            scored.append((score, workflow))
    if not scored:
        return None
    scored.sort(key=lambda pair: -pair[0])
    chosen = scored[0][1]
    db.set_setting(f"self_wf:{repo_id}", str(chosen["id"]))
    return chosen


def _self_update(db, client, row, now, forced):
    """Keep a fork's own workflow alive so it updates itself (manual mode).

    The fork's workflow produces the fork's own commits (preserved by manual
    mode). GitHub disables fork workflows by default and its scheduler is
    unreliable, so the chosen workflow is enabled once and re-dispatched when
    it has not run for SELF_UPDATE_STALE_SECONDS (both need the App's Actions
    write permission). Best effort only: never changes sync status; permission
    problems surface as a status note on the repository.
    """
    repo_id = row["id"]
    last_check = db.get_setting(f"self_check:{repo_id}")
    if last_check and not forced:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last_check)
            if age.total_seconds() < SELF_UPDATE_CHECK_INTERVAL:
                return
        except ValueError:
            pass
    db.set_setting(f"self_check:{repo_id}", utcnow())
    if _upstream_is_active(client, row):
        message = "待命：上游仍在更新（连续 24 小时无提交后 fork 自动接管）"
        if row["self_update"] != message:
            _set_repo(db, repo_id, self_update=message)
        return
    try:
        if not client.actions_enabled(row["full_name"]):
            client.set_actions_enabled(row["full_name"], True)
            log_event(db, repo_id, "check",
                      f"{row['full_name']} Actions 已启用（fork 自更新需要）")
    except (RateLimited, AuthError):
        raise
    except Exception as error:
        _set_repo(db, repo_id, self_update=f"启用 Actions 失败：{str(error)[:120]}")
        return
    try:
        workflows = client.list_workflows(row["full_name"])
    except (RateLimited, AuthError):
        raise
    except Exception as error:
        _set_repo(db, repo_id, self_update=f"读取 workflow 失败：{str(error)[:120]}")
        return
    if not workflows:
        return
    active = [w for w in workflows if w.get("state") == "active"]
    if active:
        target = _pick_self_workflow(db, client, row, active)
        if target is None:
            message = "没有可保活的数据型 workflow（跳过编译/发布类），自更新待命"
            if row["self_update"] != message:
                _set_repo(db, repo_id, self_update=message)
            return
    else:
        disabled = [w for w in workflows
                    if w.get("state") in ("disabled_fork", "disabled_inactivity")]
        if not disabled:
            return
        target = _pick_self_workflow(db, client, row, disabled)
        if target is None:
            message = "没有可保活的数据型 workflow（跳过编译/发布类），自更新待命"
            if row["self_update"] != message:
                _set_repo(db, repo_id, self_update=message)
            return
        try:
            client.enable_workflow(row["full_name"], target["id"])
        except Forbidden:
            message = ("fork 的 workflow 被 GitHub 停用，且 App 缺 Actions 读写权限；"
                       "在 App 设置授予后自动恢复")
            if row["self_update"] != message:
                _set_repo(db, repo_id, self_update=message)
            return
        except (RateLimited, AuthError):
            raise
        except DispatchRejected as error:
            _set_repo(db, repo_id, self_update=f"启用 workflow 被拒：{str(error)[:150]}")
            return
        except Exception as error:
            _set_repo(db, repo_id, self_update=f"启用 workflow 失败：{str(error)[:150]}")
            return
        _set_repo(db, repo_id, self_update=f"已启用「{target['name']}」，等待首次运行")
        log_event(db, repo_id, "check",
                  f"{row['full_name']} 自身 workflow「{target['name']}」已被 GitHub 停用，已重新启用")
    try:
        latest = client.latest_workflow_run(row["full_name"], target["id"])
    except (RateLimited, AuthError):
        raise
    except Exception:
        latest = None
    if latest and latest.get("created_at"):
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(
                latest["created_at"].replace("Z", "+00:00"))
            if age.total_seconds() < SELF_UPDATE_STALE_SECONDS:
                if row["self_update"] != "ok":
                    _set_repo(db, repo_id, self_update="ok")
                return
        except ValueError:
            pass
    last_dispatch = db.get_setting(f"self_dispatch:{repo_id}")
    if last_dispatch and not forced:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last_dispatch)
            if age.total_seconds() < SELF_UPDATE_MIN_INTERVAL:
                return
        except ValueError:
            pass
    try:
        client.dispatch_workflow(row["full_name"], target["id"], row["branch"])
    except Forbidden:
        message = "缺少 actions:write 权限，无法触发 fork 自身 workflow（在 App 设置授予后自动恢复）"
        if row["self_update"] != message:
            _set_repo(db, repo_id, self_update=message)
        return
    except DispatchRejected as error:
        bad = db.get_setting(f"self_wf_bad:{repo_id}", "")
        bad = ",".join(part for part in (bad.split(",") + [str(target["id"])]) if part)
        db.set_setting(f"self_wf_bad:{repo_id}", bad)
        db.set_setting(f"self_wf:{repo_id}", "")
        _set_repo(db, repo_id, self_update=f"触发 workflow 被拒：{str(error)[:150]}")
        return
    except (RateLimited, AuthError):
        raise
    except Exception as error:
        _set_repo(db, repo_id, self_update=f"触发失败：{str(error)[:150]}")
        return
    db.set_setting(f"self_dispatch:{repo_id}", utcnow())
    _set_repo(db, repo_id, self_update=f"已触发「{target['name']}」")
    log_event(db, repo_id, "check",
              f"{row['full_name']} 自身 workflow「{target['name']}」超过 24 小时未运行，已触发自更新")


def _deletion_guard(db, client, row, fork_sha, target_sha, comparison=None):
    """No write is allowed without a complete review or an exact one-use approval."""
    live = db.one("SELECT * FROM repositories WHERE id=?", (row["id"],))
    if not live or not live["enabled"] or live["status"] == "paused":
        return False
    fatal = None
    policy = None
    try:
        policy = deletion_policy(db, live)
        if any(live[key] != row[key] for key in ("mode", "branch", "upstream_full_name")):
            raise RuntimeError("同步配置已变化，请重新检查后再同步")
        if client.get_ref(row["full_name"], row["branch"]) != fork_sha:
            raise RuntimeError("fork HEAD 已变化，请重新检查后再同步")
        review = (assess_change(client, row, fork_sha, target_sha, policy,
                                previous=load_review(live), comparison=comparison)
                  if policy["enabled"] else
                  {"identity": change_identity(row, fork_sha, target_sha),
                   "error": None, "reasons": []})
        if client.get_ref(row["full_name"], row["branch"]) != fork_sha:
            raise RuntimeError("检测期间 fork HEAD 已变化，请重新检查后再同步")
        latest = db.one("SELECT * FROM repositories WHERE id=?", (row["id"],))
        if not latest or not latest["enabled"] or latest["status"] == "paused":
            return False
        if (deletion_policy(db, latest) != policy or
                any(latest[key] != row[key] for key in ("mode", "branch", "upstream_full_name"))):
            raise RuntimeError("检测期间保护或同步配置已变化，请重新检查")
    except Exception as error:
        review = {"identity": change_identity(row, fork_sha, target_sha),
                  "error": str(error)[:500], "reasons": []}
        if isinstance(error, (RateLimited, AuthError)):
            fatal = error
    # A snapshot is mandatory even when deletion protection is disabled or the
    # change is blocked. Approval never bypasses a failed backup.
    if not _backup_guard(db, client, row, fork_sha, target_sha):
        _set_repo(db, row["id"], deletion_review=review_json(review), deletion_approval=None)
        if fatal:
            raise fatal
        return False
    latest = db.one("SELECT * FROM repositories WHERE id=?", (row["id"],))
    if not latest or not latest["enabled"] or latest["status"] == "paused":
        return False
    try:
        if not review["error"] and (client.get_ref(row["full_name"], row["branch"]) != fork_sha or
                deletion_policy(db, latest) != policy or
                any(latest[key] != row[key] for key in ("mode", "branch", "upstream_full_name"))):
            raise RuntimeError("备份期间 fork HEAD 或同步配置已变化，请重新检查")
    except Exception as error:
        review = {"identity": change_identity(row, fork_sha, target_sha),
                  "error": str(error)[:500], "reasons": []}
        if isinstance(error, (RateLimited, AuthError)):
            fatal = error
    serialized = review_json(review)
    if not review["error"] and not review["reasons"]:
        _set_repo(db, row["id"], deletion_review=serialized, deletion_approval=None)
        return True
    if not review["error"]:
        token = review_token(review)
        consumed = db.run("UPDATE repositories SET deletion_approval=NULL WHERE id=? "
                          "AND deletion_approval=? AND deletion_review=? AND enabled=1 "
                          "AND status<>'paused'", (row["id"], token, serialized)).rowcount
        if consumed:
            log_event(db, row["id"], "check",
                      f"{row['full_name']} 已使用一次性确认：目标 {target_sha}，"
                      f"删除 {review['deleted_files']} 个文件（{review['deleted_percent']:.1f}%）")
            return True
    reason = (f"检测未完成：{review['error']}" if review["error"] else
              "；".join(review["reasons"]) +
              f"；删除 {review['deleted_files']} / {review['current_files']} 个文件"
              f"（{review['deleted_percent']:.1f}%）")
    message = f"异常删除保护：{reason}。已保留 fork，目标 {target_sha}；请在仓库详情检查。"
    if live["last_error"] != message or live["status"] != "needs_attention":
        log_event(db, row["id"], "blocked", f"{row['full_name']} {message}")
    _set_repo(db, row["id"], status="needs_attention", fork_sha=fork_sha,
              upstream_sha=target_sha, last_check_at=utcnow(), last_error=message,
              deletion_review=serialized, deletion_approval=None)
    if fatal:
        raise fatal
    return False


def _backup_guard(db, client, row, fork_sha, target_sha=None):
    result = ensure_backup(db, client, row, fork_sha)
    if result["status"] == "ready":
        return True
    live = db.one("SELECT * FROM repositories WHERE id=?", (row["id"],))
    if not live or not live["enabled"] or live["status"] == "paused":
        return False
    message = f"同步前备份失败，未同步：{result.get('last_error') or '备份不可用'}。fork 当前内容已保留。"
    if live["last_error"] != message or live["status"] != "needs_attention":
        log_event(db, row["id"], "blocked", f"{row['full_name']} {message}")
    _set_repo(db, row["id"], status="needs_attention", fork_sha=fork_sha,
              upstream_sha=target_sha or live["upstream_sha"], last_check_at=utcnow(),
              last_error=message, deletion_approval=None)
    return False


def _sync_manual(db, client, row, fork_branch, fork_sha, upstream_sha, now, forced,
                 version=None):
    """Compatibility path for legacy rows: only fast-forward after ancestry checks.

    Persistent legacy manual rows migrate to protected on startup. This path
    still fails closed for callers holding an older row in memory.
    """
    repo_id = row["id"]
    version = version or {}
    if not _fast_forward_guard(db, client, row, fork_sha, upstream_sha):
        return "paused"
    if fork_sha == upstream_sha:
        # Same commit: both sides agree, so both counts are zero.
        version = {**version, "upstream_ahead": 0, "fork_ahead": 0}
    if not forced:
        if fork_sha == upstream_sha:
            if row["status"] != "synced":
                log_event(db, repo_id, "synced",
                          f"{row['full_name']} 已与上游一致（保留自有提交）")
            _set_repo(db, repo_id, status="synced", fork_sha=fork_sha, upstream_sha=upstream_sha,
                      last_merged_upstream_sha=upstream_sha, last_check_at=now,
                      last_success_at=now, last_error=None,
                      deletion_review=None, deletion_approval=None, **version)
            return "synced"
        if (row["status"] == "synced" and fork_sha == row["fork_sha"]
                and upstream_sha == row["last_merged_upstream_sha"]):
            # Neither side moved since the last observation: nothing to do.
            if _counts_missing(row) and _counts_probe_due(db, repo_id):
                # Stable since before the counts were recorded: one probe fills
                # them in for the list's comparison label.
                version = {**version, **_probe_counts(db, client, row, fork_branch)}
            _set_repo(db, repo_id, last_check_at=now, **version)
            return "synced"
    if (row["status"] == "needs_attention" and not row["deletion_review"]
            and not forced and not _merge_retry_due(row)):
        return "needs_attention"
    if (row["status"] == "diverged" and not forced
            and row["diverged_upstream_sha"] == upstream_sha):
        # Conflict is sticky until upstream advances or the user intervenes.
        _set_repo(db, repo_id, last_check_at=now, fork_sha=fork_sha, **version)
        return "diverged"
    try:
        payload = client.compare(row["full_name"], fork_sha, upstream_sha)
    except (RateLimited, AuthError):
        raise
    except Exception:
        payload = "probe-failed"
    counts = _count_fields(payload) if valid_comparison(payload) else {}
    version = {**version, **counts}
    if valid_comparison(payload) and payload["ahead_by"] == 0:
        # Fork already contains the upstream head (its own commits sit on top,
        # or a merge raced ahead of us); record the marker without another merge.
        if row["status"] != "synced":
            log_event(db, repo_id, "synced",
                      f"{row['full_name']} 已包含上游 {upstream_sha[:12]}（保留自有提交）")
        _set_repo(db, repo_id, status="synced", fork_sha=fork_sha, upstream_sha=upstream_sha,
                  last_merged_upstream_sha=upstream_sha, last_check_at=utcnow(),
                  last_success_at=utcnow(), last_error=None,
                  deletion_review=None, deletion_approval=None, **version)
        return "synced"
    if not _deletion_guard(db, client, row, fork_sha, upstream_sha,
                           comparison=payload if isinstance(payload, dict) else "unavailable"):
        return "needs_attention"
    try:
        client.update_ref(row["full_name"], fork_branch, upstream_sha, force=False)
        result = {"sha": upstream_sha, "merge_type": "fast-forward"}
        confirmed = client.get_ref(row["full_name"], fork_branch)
        merge_type = (result or {}).get("merge_type")
        if merge_type != "none" and confirmed != (result or {}).get("sha"):
            raise RuntimeError("合并后 fork HEAD 与 GitHub 返回的目标合并提交不一致")
        if merge_type == "none":
            label = "已包含"
        elif merge_type == "fast-forward":
            label = "快进"
        else:
            label = "合并"
        if counts:
            # Upstream is inside the fork now; a real merge also adds the merge
            # commit itself to the fork's unique commits.
            version = {**version, "upstream_ahead": 0,
                       "fork_ahead": counts["fork_ahead"] + (1 if merge_type == "merge" else 0)}
        _set_repo(db, repo_id, status="synced", fork_sha=confirmed, upstream_sha=upstream_sha,
                  last_merged_upstream_sha=upstream_sha, last_check_at=utcnow(),
                  last_success_at=utcnow(), last_error=None,
                  deletion_review=None, deletion_approval=None, **version)
        log_event(db, repo_id, "synced",
                  f"{row['full_name']} {label}上游 {upstream_sha[:12]}（保留 fork 自有提交）")
        return "synced"
    except RefNotFastForward:
        return _protect_repository(db, row, "远端拒绝非快进更新，已禁止同步；不会强制覆盖或自动合并。")
    except MergeConflict as error:
        # A conflict is a stable state, not a failure: the fork keeps its own
        # commits and its own Actions keep updating it; the upstream merge is
        # retried when upstream advances (or when the user resolves on GitHub).
        message = (f"上游有无法自动合并的改动（双方修改了同一内容或历史被重写）：{str(error)[:100]}。"
                   "fork 自有提交已保留、自身 Actions 更新不受影响；"
                   "如需并入上游，可在 GitHub fork 页解决冲突后点「立即同步」")
        if row["status"] != "diverged" or row["last_error"] != message:
            log_event(db, repo_id, "blocked", f"{row['full_name']} {message}")
        _set_repo(db, repo_id, status="diverged", diverged_at=utcnow(),
                  diverged_upstream_sha=upstream_sha, fork_sha=fork_sha,
                  last_check_at=utcnow(), last_error=message, **version)
        return "diverged"
    except Forbidden as error:
        text = str(error)
        if "workflow" in text.lower():
            message = ("待合并提交含 .github/workflows/ 变更，App 缺 Workflows 权限："
                       "在 GitHub App 设置授予 Workflows 读写并批准后自动恢复")
        else:
            message = f"合并被 GitHub 拒绝（403）：{text[:200]}"
        if row["status"] != "error" or row["last_error"] != message:
            log_event(db, repo_id, "error", f"{row['full_name']} 同步失败：{message}")
        _set_repo(db, repo_id, status="error", last_check_at=utcnow(), last_error=message,
                  **version)
        return "error"
    except (RateLimited, AuthError):
        raise
    except Exception as error:
        message = str(error)[:300]
        if row["status"] != "error" or row["last_error"] != message:
            log_event(db, repo_id, "error", f"{row['full_name']} 同步失败：{message}")
        _set_repo(db, repo_id, status="error", last_check_at=utcnow(), last_error=message,
                  **version)
        return "error"


def _classify_forbidden(db, client, row, upstream_sha, error, version=None):
    """403 on a ref update: diverged fork vs missing workflows permission vs other.

    GitHub returns 403 (not 422) for App ref updates it refuses; compare tells
    apart the real cause. Only the workflows-permission case can auto-recover.
    """
    try:
        payload = client.compare(row["full_name"], row["branch"],
                                 f"{row['upstream_full_name'].split('/')[0]}:{row['branch']}")
    except Exception:
        payload = "probe-failed"
    version = {**(version or {}), **_count_fields(payload)}
    if payload is None:
        _mark_diverged(db, row, upstream_sha, "与上游无共同祖先（历史被重写），不会覆盖", version)
        return "diverged"
    if isinstance(payload, dict):
        unique = payload.get("behind_by", 0)
        if unique > 0:
            _mark_diverged(db, row, upstream_sha, f"有 {unique} 个独有提交，不会覆盖", version)
            return "diverged"
        files = payload.get("files") or []
        if any(str(f.get("filename", "")).startswith(".github/workflows/") for f in files):
            message = ("待同步提交含 .github/workflows/ 变更，App 缺 Workflows 权限被拒："
                       "在 GitHub App 设置授予 Workflows 读写并批准后自动恢复")
            if row["status"] != "error" or row["last_error"] != message:
                log_event(db, row["id"], "error", f"{row['full_name']} 同步失败：{message}")
            _set_repo(db, row["id"], status="error", last_check_at=utcnow(),
                      last_error=message, **version)
            return "error"
    message = f"推送被 GitHub 拒绝（403）：{str(error)[:200]}"
    if row["status"] != "error" or row["last_error"] != message:
        log_event(db, row["id"], "error", f"{row['full_name']} 同步失败：{message}")
    _set_repo(db, row["id"], status="error", last_check_at=utcnow(), last_error=message,
              **version)
    return "error"


def _update_fast_forward(client, row, upstream_sha, fork_sha):
    """The server must enforce fast-forward even if the observed HEAD is stale."""
    client.update_ref(row["full_name"], row["branch"], upstream_sha, force=False)



def _protect_repository(db, row, message, fork_sha=None, upstream_sha=None, counts=None):
    fields = dict(enabled=0, mode="protected", status="paused", sync_requested=0,
                  self_update_interval=None, deletion_approval=None,
                  last_check_at=utcnow(), last_error=message)
    if fork_sha:
        fields["fork_sha"] = fork_sha
    if upstream_sha:
        fields["upstream_sha"] = upstream_sha
    fields.update(counts or {})
    _set_repo(db, row["id"], **fields)
    if row["mode"] != "protected" or row["last_error"] != message:
        log_event(db, row["id"], "blocked", f"{row['full_name']} {message}")
    return "paused"


def _fast_forward_guard(db, client, row, fork_sha, upstream_sha):
    """Unique commits, unrelated history and uncertain comparisons all fail closed."""
    if fork_sha == upstream_sha:
        return True
    try:
        comparison = client.compare(row["full_name"], fork_sha, upstream_sha)
    except (AuthError, RateLimited):
        raise
    except Exception:
        comparison = None
    if not valid_comparison(comparison):
        _protect_repository(db, row, "无法确认提交包含关系，已禁止自动同步；请人工检查历史。",
                            fork_sha, upstream_sha)
        return False
    if comparison["behind_by"]:
        _protect_repository(db, row,
                            f"检测到 {comparison['behind_by']} 个上游没有的提交，已禁止自动同步，保留全部改动。",
                            fork_sha, upstream_sha, _count_fields(comparison))
        return False
    return True


def sync_one(db, client, row, node, forced=False):
    """Apply one repository's observed state. Returns final status."""
    repo_id = row["id"]
    if row["mode"] == "protected":
        return _protect_repository(db, row, "独立维护仓库：禁止自动同步上游。")
    if node is None:
        if row["status"] != "missing":
            log_event(db, repo_id, "blocked", f"{row['full_name']} 无法访问，标记 missing")
        _set_repo(db, repo_id, status="missing", last_check_at=utcnow(),
                  last_error="仓库不可访问（可能已删除或权限变化）")
        return "missing"
    if not node.get("isFork"):
        # The repository is still in the installation but is no longer a fork:
        # its upstream was deleted and GitHub promoted the fork to a standalone
        # repository (or it was detached). Never treat this as "gone" or remove
        # it — the fork and its history are intact and keep updating themselves.
        if row["upstream_gone"] and row["status"] == "needs_attention":
            _set_repo(db, repo_id, last_check_at=utcnow())
            return "needs_attention"
        message = (f"上游仓库已删除，{row['full_name']} 已成为独立仓库；"
                   "fork 及其历史保留，不会被移除或改动")
        if row["status"] != "needs_attention" or row["last_error"] != message:
            log_event(db, repo_id, "blocked", f"{row['full_name']} {message}")
        _set_repo(db, repo_id, status="needs_attention", upstream_gone=1,
                  last_check_at=utcnow(), last_error=message)
        return "needs_attention"
    parent = node.get("parent") or {}
    fork_ref = node.get("defaultBranchRef") or {}
    parent_ref = parent.get("defaultBranchRef") or {}
    if not parent.get("nameWithOwner"):
        # GitHub still reports the repository as a fork but the parent is gone
        # (deleted upstream). Same guarantee as above: keep the fork managed.
        message = (f"上游仓库不可见（可能已删除），{row['full_name']} 保持管理；"
                   "fork 及其历史不会被移除或改动")
        if row["status"] != "needs_attention" or row["last_error"] != message:
            log_event(db, repo_id, "blocked", f"{row['full_name']} {message}")
        _set_repo(db, repo_id, status="needs_attention", upstream_gone=1,
                  last_check_at=utcnow(), last_error=message)
        return "needs_attention"
    if not fork_ref.get("target") or not parent_ref.get("target"):
        _set_repo(db, repo_id, status="blocked", last_check_at=utcnow(),
                  last_error="默认分支不存在")
        return "blocked"
    version = _version_fields(node, parent)
    if row["upstream_gone"]:
        # The upstream is back (re-forked or restored): resume normal tracking.
        _set_repo(db, repo_id, upstream_gone=0)
    if parent["nameWithOwner"] != row["upstream_full_name"]:
        if row["status"] != "needs_attention":
            log_event(db, repo_id, "blocked",
                      f"{row['full_name']} 的上游变为 {parent['nameWithOwner']}，已暂停等待确认")
        _set_repo(db, repo_id, upstream_full_name=parent["nameWithOwner"],
                  status="needs_attention", last_check_at=utcnow(),
                  last_error=f"上游变更为 {parent['nameWithOwner']}，请确认后恢复", **version)
        return "needs_attention"
    fork_branch, parent_branch = fork_ref["name"], parent_ref["name"]
    if row["mode"] == "manual":
        # merge-upstream sources the upstream branch with the same name as the
        # fork's default branch; keep the row's branch in step with the fork.
        if fork_branch != row["branch"]:
            _set_repo(db, repo_id, branch=fork_branch)
            row = db.one("SELECT * FROM repositories WHERE id=?", (repo_id,))
        upstream_sha = parent_ref["target"]["oid"]
        if fork_branch != parent_branch:
            # Track the same-named upstream ref (that is what merge-upstream
            # folds in). Its absence is diagnosed here because the merge call
            # would only report a generic 409.
            try:
                upstream_sha = client.get_ref(row["upstream_full_name"], fork_branch)
            except (RateLimited, AuthError):
                raise
            except RefNotFound:
                message = (f"上游没有与 fork 默认分支同名的分支 {fork_branch}"
                           f"（上游默认分支为 {parent_branch}），无法自动合并；"
                           "fork 可保持独立更新，如需与上游对齐请在 GitHub 手动处理")
                if row["status"] != "needs_attention" or row["last_error"] != message:
                    log_event(db, repo_id, "blocked", f"{row['full_name']} {message}")
                _set_repo(db, repo_id, status="needs_attention", last_check_at=utcnow(),
                          last_error=message)
                return "needs_attention"
            except Exception as error:
                # Transient API failure (network, 5xx): report it as an error
                # instead of falsely claiming the upstream branch is missing.
                message = f"读取上游分支 {fork_branch} 失败：{str(error)[:200]}"
                if row["status"] != "error" or row["last_error"] != message:
                    log_event(db, repo_id, "error", f"{row['full_name']} 检查失败：{message}")
                _set_repo(db, repo_id, status="error", last_check_at=utcnow(),
                          last_error=message, **version)
                return "error"
        status = _sync_manual(db, client, row, fork_branch,
                              fork_ref["target"]["oid"], upstream_sha, utcnow(), forced,
                              version=version)
        if row["self_update_interval"] and status == "synced":
            _self_update(db, client, row, utcnow(), forced)
        return status
    if fork_branch != row["branch"] or parent_branch != row["branch"]:
        fields = {"status": "needs_attention", "last_check_at": utcnow()}
        if fork_branch == parent_branch:
            # Both sides renamed the default branch to the same name; record it
            # so that "resume" continues syncing on the new branch.
            fields["branch"] = fork_branch
            fields["last_error"] = f"默认分支已变为 {fork_branch}，点恢复后将在该分支继续同步"
        else:
            fields["last_error"] = (f"默认分支不一致（fork {fork_branch} vs 上游 {parent_branch}）；"
                                    "请人工核对分支；不会自动合并或覆盖")
        if row["status"] != "needs_attention":
            log_event(db, repo_id, "blocked",
                      f"{row['full_name']} 默认分支变化（{fork_branch} vs {parent_branch}），暂停等待确认")
        _set_repo(db, repo_id, **fields, **version)
        return "needs_attention"
    fork_sha = fork_ref["target"]["oid"]
    upstream_sha = parent_ref["target"]["oid"]
    now = utcnow()
    if fork_sha == upstream_sha:
        if row["status"] not in ("synced",):
            log_event(db, repo_id, "synced", f"{row['full_name']} 已与上游一致")
        _set_repo(db, repo_id, status="synced", fork_sha=fork_sha, upstream_sha=upstream_sha,
                  upstream_ahead=0, fork_ahead=0,
                  last_check_at=now, last_success_at=now, last_error=None,
                  deletion_review=None, deletion_approval=None, **version)
        return "synced"
    if not _fast_forward_guard(db, client, row, fork_sha, upstream_sha):
        return "paused"
    if not _deletion_guard(db, client, row, fork_sha, upstream_sha):
        return "needs_attention"
    try:
        recovered_object = False
        try:
            _update_fast_forward(client, row, upstream_sha, fork_sha)
        except RefObjectNotFound:
            # The batched observation may be stale; refresh once, without
            # treating an unavailable object as permission to force a ref.
            refreshed_sha = client.get_ref(row["upstream_full_name"], row["branch"])
            if refreshed_sha != upstream_sha:
                version = {"fork_tag": None, "upstream_tag": None,
                           "fork_commit_at": None, "upstream_commit_at": None}
            upstream_sha = refreshed_sha
            if not _fast_forward_guard(db, client, row, fork_sha, upstream_sha):
                return "paused"
            if not _deletion_guard(db, client, row, fork_sha, upstream_sha):
                return "needs_attention"
            _update_fast_forward(client, row, upstream_sha, fork_sha)
            recovered_object = True
        confirmed = client.get_ref(row["full_name"], row["branch"])
        if confirmed != upstream_sha:
            raise RuntimeError(f"推送后 fork HEAD {confirmed[:12]} 与目标 {upstream_sha[:12]} 不一致")
        _set_repo(db, repo_id, status="synced", fork_sha=upstream_sha, upstream_sha=upstream_sha,
                  upstream_ahead=0, fork_ahead=0,
                  last_check_at=utcnow(), last_success_at=utcnow(), last_error=None,
                  deletion_review=None, deletion_approval=None,
                  diverged_at=None, diverged_upstream_sha=None, **version)
        message = f"{row['full_name']} 快进到 {upstream_sha[:12]}"
        if recovered_object:
            message += "（已刷新上游提交并自动恢复）"
        log_event(db, repo_id, "synced", message)
        return "synced"
    except RefNotFastForward:
        return _protect_repository(db, row, "远端拒绝非快进更新，已禁止同步；不会强制覆盖或自动合并。")
    except Forbidden as error:
        return _classify_forbidden(db, client, row, upstream_sha, error, version)
    except (RateLimited, AuthError):
        raise
    except Exception as error:  # per-repo isolation: other repos continue
        message = str(error)[:300]
        if row["status"] != "error" or row["last_error"] != message:
            log_event(db, repo_id, "error", f"{row['full_name']} 同步失败：{message}")
        _set_repo(db, repo_id, status="error", last_check_at=utcnow(), last_error=message,
                  **version)
        return "error"


def _retry_due(row):
    """Error repos retry at most every ERROR_RETRY_SECONDS to avoid hammering."""
    if row["status"] != "error" or not row["last_check_at"]:
        return True
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["last_check_at"])
        return age.total_seconds() >= ERROR_RETRY_SECONDS
    except ValueError:
        return True


def run_cycle(db, client):
    """One full pass over managed repositories. Returns True when a sync happened."""
    rows = db.all("SELECT * FROM repositories ORDER BY id")
    if not rows:
        return False
    did_work = False
    for start in range(0, len(rows), BATCH):
        chunk = [row["node_id"] for row in rows[start:start + BATCH] if row["node_id"]]
        nodes = {}
        if chunk:
            data = client.graphql(NODES_QUERY, {"ids": chunk})
            for node in data.get("nodes") or []:
                if node:
                    nodes[node["id"]] = node
        for row in rows[start:start + BATCH]:
            row = db.one("SELECT * FROM repositories WHERE id=?", (row["id"],))
            if not row or row["restore_backup_id"] is not None:
                continue
            node = nodes.get(row["node_id"]) if row["node_id"] else None
            initial_sha = (((node or {}).get("defaultBranchRef") or {}).get("target") or {}).get("oid")
            if initial_sha and not db.one(
                    "SELECT id FROM repository_backups WHERE full_name=? AND status='ready' LIMIT 1",
                    (row["full_name"],)):
                if not _backup_guard(db, client, row, initial_sha):
                    continue
                row = db.one("SELECT * FROM repositories WHERE id=?", (row["id"],))
                if not row or row["restore_backup_id"] is not None:
                    continue
            if not row["enabled"]:
                continue
            if row["status"] == "paused":
                _set_repo(db, row["id"], last_check_at=utcnow())
                continue
            forced = bool(row["sync_requested"])
            if not forced and not _retry_due(row):
                continue
            if forced:
                _set_repo(db, row["id"], sync_requested=0)
            node = nodes.get(row["node_id"]) if row["node_id"] else None
            try:
                status = sync_one(db, client, row, node, forced=forced)
                did_work |= status == "synced"
            except (RateLimited, AuthError):
                raise
            except Exception as error:
                _set_repo(db, row["id"], status="error", last_check_at=utcnow(),
                          last_error=str(error)[:300])
                log_event(db, row["id"], "error", f"{row['full_name']} 检查失败：{error}")
    return did_work


def run_restore_requests(db, client):
    # The same worker performs syncs and restores, so a restore cannot race a
    # panel-originated ref update. External Git writers are guarded by a lease.
    from .restore import rollback_backup
    for row in db.all("SELECT * FROM repositories WHERE restore_backup_id IS NOT NULL ORDER BY id"):
        try:
            rollback_backup(db, client, row, row["restore_backup_id"], row["restore_expected_sha"])
        except Exception:
            message = "回滚未完成，仓库保持暂停；请核对 GitHub 分支和备份后重试。"
            _set_repo(db, row["id"], enabled=0, status="paused", last_error=message)
            log_event(db, row["id"], "error", message)
        finally:
            _set_repo(db, row["id"], restore_backup_id=None, restore_expected_sha=None)


class Worker(threading.Thread):
    def __init__(self, state_dir, pem_path):
        super().__init__(daemon=True, name="forkiq-worker")
        self.state_dir = Path(state_dir)
        self.pem_path = pem_path
        self.wake = threading.Event()
        self.stop_flag = threading.Event()
        self.lock_path = self.state_dir / "worker.lock"

    def request_sync(self):
        self.wake.set()

    def run(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock = open(self.lock_path, "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("forkiq: another worker holds the lock; exiting", flush=True)
            return
        db = DB(self.state_dir / "forkiq.db")
        db.set_setting("github_pem_path", str(self.pem_path))
        while not self.stop_flag.is_set():
            interval = 30
            try:
                interval = max(15, min(300, int(db.get_setting("check_interval", "30"))))
            except ValueError:
                pass
            try:
                self._tick(db)
            except Exception:
                traceback.print_exc()
            self.wake.wait(timeout=interval)
            self.wake.clear()

    def _tick(self, db):
        client = client_from_settings(db)
        if client is None:
            return
        run_restore_requests(db, client)
        discover_requested = db.get_setting("discover_requested") == "1"
        last_discover = db.get_setting("last_discover_at")
        due = True
        if last_discover and not discover_requested:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(last_discover)
                due = age.total_seconds() >= 3600
            except ValueError:
                due = True
        try:
            if discover_requested or (db.get_setting("auto_discover", "1") == "1" and due):
                discover(db, client)
            if discover_requested:
                db.set_setting("discover_requested", "0")
            run_cycle(db, client)
            if db.get_setting("auth_error"):
                db.set_setting("auth_error", "")
                log_event(db, None, "auth", "GitHub 连接已恢复")
        except AuthError as error:
            if not _throttled(db, "auth"):
                db.set_setting("auth_error", str(error)[:300])
                log_event(db, None, "auth", f"GitHub 认证失败：{error}")
        except RateLimited as error:
            if not _throttled(db, "rate"):
                log_event(db, None, "error", f"GitHub 限流：{error}")
        except Exception as error:
            if not _throttled(db, "network"):
                log_event(db, None, "error", f"检查失败：{error}")
