"""Local repository controls; batch actions never write to GitHub directly."""
from collections import Counter

from .db import utcnow


BULK_ACTIONS = {
    "pause": "暂停同步", "resume": "恢复同步", "sync": "安排同步",
    "protect": "设为独立维护", "remove": "移出管理",
}
MAX_BATCH = 500


def bulk_update(db, ids, action):
    if action not in BULK_ACTIONS:
        raise ValueError("不支持的批量操作")
    if not ids or len(ids) > MAX_BATCH:
        raise ValueError(f"请选择 1–{MAX_BATCH} 个仓库")
    if any(not isinstance(value, str) or not value.isascii() or not value.isdecimal()
           or len(value) > 18 or int(value) < 1 for value in ids):
        raise ValueError("仓库编号无效，请刷新列表后重试")
    ids = list(dict.fromkeys(int(value) for value in ids))
    conn = db.connect()
    changed, skipped = [], []
    now = utcnow()
    try:
        # Read and update under one write lock. A stale browser cannot bypass a
        # protection or rollback state committed after the page was rendered.
        conn.execute("BEGIN IMMEDIATE")
        rows = {row["id"]: row for row in conn.execute(
            "SELECT * FROM repositories WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids)}
        for repo_id in ids:
            row = rows.get(repo_id)
            reason = None
            if row is None:
                reason = "仓库不存在"
            elif row["restore_backup_id"] is not None:
                reason = "回滚处理中"
            elif action in ("resume", "sync") and row["mode"] == "protected":
                reason = "独立维护，禁止同步"
            elif action == "pause" and not row["enabled"] and row["status"] == "paused" and not row["sync_requested"]:
                reason = "已暂停"
            elif action == "resume" and row["enabled"] and row["status"] != "paused":
                reason = "已启用同步"
            elif action == "sync" and (not row["enabled"] or row["status"] == "paused"):
                reason = "需先恢复同步"
            elif action == "sync" and row["sync_requested"]:
                reason = "已在同步队列"
            elif action == "protect" and row["mode"] == "protected" and not row["enabled"] and row["status"] == "paused":
                reason = "已独立维护"
            if reason:
                skipped.append({"id": repo_id, "name": row["full_name"] if row else str(repo_id), "reason": reason})
                continue
            fields = {"updated_at": now}
            if action == "pause":
                fields.update(enabled=0, status="paused", sync_requested=0, deletion_approval=None)
            elif action == "resume":
                fields.update(enabled=1, status="pending", sync_requested=1, last_error=None, deletion_approval=None)
            elif action == "sync":
                fields.update(sync_requested=1)
            elif action == "protect":
                fields.update(mode="protected", enabled=0, status="paused", sync_requested=0,
                              self_update_interval=None, deletion_review=None, deletion_approval=None,
                              last_error="独立维护仓库：禁止自动同步上游。")
            if action == "remove":
                # Snapshots live independently of the managed row and stay intact.
                conn.execute("DELETE FROM repositories WHERE id=?", (repo_id,))
            else:
                conn.execute("UPDATE repositories SET " + ",".join(key + "=?" for key in fields)
                             + " WHERE id=?", (*fields.values(), repo_id))
            conn.execute("INSERT INTO events(repo_id,at,kind,message) VALUES(?,?,?,?)",
                         (None if action == "remove" else repo_id, now, "check",
                          f"{row['full_name']} 批量操作：{BULK_ACTIONS[action]}"))
            changed.append(repo_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    reasons = Counter(item["reason"] for item in skipped)
    notice = f"批量{BULK_ACTIONS[action]}：已处理 {len(changed)} 个，跳过 {len(skipped)} 个。"
    if reasons:
        notice += " " + "；".join(f"{reason} {count} 个" for reason, count in reasons.items()) + "。"
    if action == "remove" and changed:
        notice += " GitHub 仓库和服务器备份未删除；自动发现开启时可能重新加入。"
    return {"changed": changed, "skipped": skipped, "notice": notice}
