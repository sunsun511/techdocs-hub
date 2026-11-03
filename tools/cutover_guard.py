"""Conservative local pre-write rollback decision; never restores a file."""

from contextlib import closing
import hashlib
import json
from app import migration as m

TABLES = (*m.OLD, "users", "workspaces", "memberships", "audit_events")


def snapshot(path):
    path = m.safe_target(path)
    with closing(m.connect(path, readonly=True)) as db:
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise m.MigrationError("v2 candidate required")
        marker = m.validate_marker(db, path)
        values = {table: db.execute("SELECT * FROM " + table).fetchall() for table in TABLES}
        proofs = m.fingerprint(values)
        tables = {
            table: {"count": value["count"], "sha256": value["table_sha256"]}
            for table, value in proofs.items()
        }
        db.execute("COMMIT")
    return {
        "database": str(path),
        "migration_source": marker["source_logical_fingerprint"],
        "workspace_id": marker["workspace_id"],
        "business_tables": tables,
        "business_sha256": hashlib.sha256(json.dumps(tables, sort_keys=True).encode()).hexdigest(),
        "sessions_excluded": True,
    }


def decision(before, after):
    same = all(
        before.get(key) == after.get(key)
        for key in ("database", "migration_source", "workspace_id", "business_sha256")
    )
    changed = [t for t, v in before["business_tables"].items() if v != after["business_tables"].get(t)]
    return {
        "allow_pre_write_restore": same,
        "decision": "PRE_WRITE_RESTORE_ALLOWED" if same else "STOP_REQUIRES_OPERATOR_DECISION",
        "changed_tables": changed,
        "automatic_restore_performed": False,
    }
