"""Standalone isolated v1->v2 validation tool. No application or model imports."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid
from .db_policy import safe_target

BUSINESS = ("documents", "versions", "tasks", "chunks", "idempotency")
OLD = {
    "meta": ("key", "value"),
    "documents": (
        "id",
        "key",
        "deleted",
        "desired_revision",
        "active_version_id",
        "created_at",
        "updated_at",
    ),
    "versions": (
        "id",
        "document_id",
        "label",
        "title",
        "source_url",
        "format",
        "content",
        "content_hash",
        "revision",
        "status",
        "model_tag",
        "chunk_count",
        "created_at",
    ),
    "tasks": (
        "id",
        "document_id",
        "version_id",
        "revision",
        "state",
        "attempts",
        "error",
        "created_at",
        "updated_at",
    ),
    "chunks": ("id", "document_id", "version_id", "ordinal", "heading", "text", "vector"),
    "idempotency": ("key", "request_hash", "task_id"),
}
PRECOMMIT_POINTS = (
    "after_auth",
    "after_copy_documents",
    "after_copy_versions",
    "after_copy_tasks",
    "after_copy_chunks",
    "after_copy_idempotency",
    "after_drop",
    "after_rename",
    "after_indexes",
    "after_validate",
    "after_user_version",
    "before_commit",
)


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Seed:
    username: str = "migration-test-owner"
    display_name: str = "Isolated migration fixture"
    password_hash: str = "!UNUSABLE:B2-1-isolated-fixture"
    user_id: str = "b21f0000-0000-4000-8000-000000000001"
    workspace_id: str = "b21f0000-0000-4000-8000-000000000002"
    grant_id: str = "b21f0000-0000-4000-8000-000000000003"


def initialize_empty_v1(path):
    """Explicit offline bootstrap; never invoked by application startup."""
    path = safe_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents overwriting or silently accepting old data.
    with path.open("xb"):
        pass
    schema = json.loads(Path(__file__).with_name("v1-schema.json").read_text(encoding="utf-8"))
    with closing(connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for kind in ("table", "index"):
                for row in schema:
                    if row[0] == kind:
                        db.execute(row[3])
            db.execute("INSERT INTO meta VALUES('generation',0)")
            db.execute("PRAGMA user_version=1")
            db.execute("COMMIT")
            preflight(db)
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise


def connect(path, readonly=False, immutable=False):
    path = Path(path).resolve() if readonly else safe_target(path)
    if readonly:
        if immutable:
            wal = Path(str(path) + "-wal")
            journal = Path(str(path) + "-journal")
            if (wal.exists() and wal.stat().st_size) or (journal.exists() and journal.stat().st_size):
                raise MigrationError("Immutable source has WAL or recovery journal")
        uri = path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
        db = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)
        db.execute("PRAGMA query_only=ON")
    else:
        path = safe_target(path)
        db = sqlite3.connect(path, isolation_level=None, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA foreign_keys=ON")
    if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        db.close()
        raise MigrationError("Foreign keys not enabled")
    return db


def backup(source, target, immutable=False):
    target = safe_target(target)
    if target.exists():
        raise MigrationError("Backup target exists; never overwrite")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(source, readonly=True, immutable=immutable)) as src, closing(connect(target)) as dst:
        src.backup(dst, pages=128)
        if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise MigrationError("Backup integrity failure")


def old_values(db):
    return {
        table: db.execute(
            "SELECT " + ",".join('"' + x + '"' for x in cols) + ' FROM "' + table + '"'
        ).fetchall()
        for table, cols in OLD.items()
    }


def fingerprint(values):
    result = {}
    for table, rows in values.items():
        hashes = []
        for row in rows:
            typed = [[type(v).__name__, v.hex() if isinstance(v, bytes) else v] for v in row]
            hashes.append(
                hashlib.sha256(
                    json.dumps(typed, ensure_ascii=False, separators=(",", ":")).encode()
                ).hexdigest()
            )
        hashes.sort()
        result[table] = {
            "count": len(rows),
            "row_sha256": hashes,
            "table_sha256": hashlib.sha256("\n".join(hashes).encode()).hexdigest(),
        }
    return result


def logical_digest(values):
    return hashlib.sha256(json.dumps(fingerprint(values), sort_keys=True).encode()).hexdigest()


def integrity(db):
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise MigrationError("integrity_check failed")
    if db.execute("PRAGMA foreign_key_check").fetchall():
        raise MigrationError("foreign_key_check failed")


def application_checks(db):
    sql = [
        "SELECT COUNT(*) FROM documents d LEFT JOIN versions v ON v.id=d.active_version_id WHERE d.active_version_id IS NOT NULL AND (v.id IS NULL OR v.document_id!=d.id OR v.status!='ready' OR v.revision>d.desired_revision)",
        "SELECT COUNT(*) FROM documents WHERE deleted NOT IN (0,1) OR desired_revision<0 OR (deleted=1 AND active_version_id IS NOT NULL)",
        "SELECT COUNT(*) FROM versions WHERE revision<0 OR chunk_count<0 OR status NOT IN ('queued','running','ready','failed','superseded','cancelled')",
        "SELECT COUNT(*) FROM tasks WHERE attempts<0 OR revision<0 OR state NOT IN ('queued','running','succeeded','failed','superseded','cancelled')",
        "SELECT COUNT(*) FROM chunks WHERE ordinal<0",
        "SELECT COUNT(*) FROM tasks t JOIN versions v ON t.version_id=v.id WHERE t.document_id!=v.document_id",
        "SELECT COUNT(*) FROM chunks c JOIN versions v ON c.version_id=v.id WHERE c.document_id!=v.document_id",
        "SELECT COUNT(*) FROM (SELECT version_id FROM tasks GROUP BY version_id HAVING COUNT(*)>1)",
        "SELECT COUNT(*) FROM (SELECT version_id,ordinal FROM chunks GROUP BY version_id,ordinal HAVING COUNT(*)>1)",
        "SELECT COUNT(*) FROM versions v LEFT JOIN tasks t ON t.version_id=v.id WHERE t.id IS NULL",
        "SELECT COUNT(*) FROM versions v JOIN documents d ON d.id=v.document_id WHERE d.deleted=0 AND v.status='ready' AND v.chunk_count!=(SELECT COUNT(*) FROM chunks c WHERE c.version_id=v.id)",
    ]
    counts = [db.execute(x).fetchone()[0] for x in sql]
    if any(counts):
        raise MigrationError("Application preconditions failed: " + str(counts))
    return counts


AUTH_DDL = [
    """CREATE TABLE users(id TEXT PRIMARY KEY NOT NULL,username_normalized TEXT UNIQUE NOT NULL,display_name TEXT NOT NULL,password_hash TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('active','disabled')),credential_epoch INTEGER NOT NULL CHECK(credential_epoch>=0),created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
    """CREATE TABLE sessions(id TEXT PRIMARY KEY NOT NULL,token_hash TEXT UNIQUE NOT NULL,user_id TEXT NOT NULL REFERENCES users(id),credential_epoch INTEGER NOT NULL CHECK(credential_epoch>=0),csrf_token TEXT NOT NULL,created_at TEXT NOT NULL,last_activity_at TEXT NOT NULL,idle_expires_at TEXT NOT NULL,absolute_expires_at TEXT NOT NULL,revoked_at TEXT)""",
    """CREATE TABLE workspaces(id TEXT PRIMARY KEY NOT NULL,name TEXT NOT NULL,created_by_user_id TEXT NOT NULL REFERENCES users(id),generation INTEGER NOT NULL CHECK(generation>=0),created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
    """CREATE TABLE memberships(workspace_id TEXT NOT NULL REFERENCES workspaces(id),user_id TEXT NOT NULL REFERENCES users(id),role TEXT NOT NULL CHECK(role IN ('owner','editor','viewer')),grant_id TEXT UNIQUE NOT NULL,revision INTEGER NOT NULL CHECK(revision>=1),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(workspace_id,user_id))""",
    """CREATE TABLE audit_events(id TEXT PRIMARY KEY NOT NULL,occurred_at TEXT NOT NULL,actor_user_id TEXT REFERENCES users(id),workspace_id TEXT REFERENCES workspaces(id),action TEXT NOT NULL,resource_type TEXT,resource_id TEXT,outcome TEXT NOT NULL)""",
    """CREATE TABLE migration_history(version INTEGER PRIMARY KEY,default_workspace_id TEXT NOT NULL REFERENCES workspaces(id),initial_owner_user_id TEXT NOT NULL REFERENCES users(id),source_logical_fingerprint TEXT NOT NULL,completed_at TEXT NOT NULL)""",
]
BUSINESS_DDL = {
    "documents": """CREATE TABLE documents_v2(id TEXT PRIMARY KEY NOT NULL,key TEXT NOT NULL,deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),desired_revision INTEGER NOT NULL DEFAULT 0 CHECK(desired_revision>=0),active_version_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,workspace_id TEXT NOT NULL REFERENCES workspaces(id),UNIQUE(workspace_id,id),UNIQUE(workspace_id,key),FOREIGN KEY(workspace_id,id,active_version_id) REFERENCES versions_v2(workspace_id,document_id,id) DEFERRABLE INITIALLY DEFERRED)""",
    "versions": """CREATE TABLE versions_v2(id TEXT PRIMARY KEY NOT NULL,document_id TEXT NOT NULL,label TEXT NOT NULL,title TEXT NOT NULL,source_url TEXT NOT NULL,format TEXT NOT NULL,content TEXT NOT NULL,content_hash TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>=0),status TEXT NOT NULL CHECK(status IN ('queued','running','ready','failed','superseded','cancelled')),model_tag TEXT,chunk_count INTEGER NOT NULL DEFAULT 0 CHECK(chunk_count>=0),created_at TEXT NOT NULL,workspace_id TEXT NOT NULL,UNIQUE(workspace_id,document_id,id),UNIQUE(workspace_id,document_id,label),FOREIGN KEY(workspace_id,document_id) REFERENCES documents_v2(workspace_id,id))""",
    "tasks": """CREATE TABLE tasks_v2(id TEXT PRIMARY KEY NOT NULL,document_id TEXT NOT NULL,version_id TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>=0),state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','superseded','cancelled')),attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,workspace_id TEXT NOT NULL,requested_by_user_id TEXT REFERENCES users(id),UNIQUE(workspace_id,id),UNIQUE(workspace_id,version_id),FOREIGN KEY(workspace_id,document_id,version_id) REFERENCES versions_v2(workspace_id,document_id,id))""",
    "chunks": """CREATE TABLE chunks_v2(id TEXT PRIMARY KEY NOT NULL,document_id TEXT NOT NULL,version_id TEXT NOT NULL,ordinal INTEGER NOT NULL CHECK(ordinal>=0),heading TEXT NOT NULL,text TEXT NOT NULL,vector BLOB,workspace_id TEXT NOT NULL,UNIQUE(workspace_id,version_id,ordinal),FOREIGN KEY(workspace_id,document_id,version_id) REFERENCES versions_v2(workspace_id,document_id,id))""",
    "idempotency": """CREATE TABLE idempotency_v2(key TEXT NOT NULL,request_hash TEXT NOT NULL,task_id TEXT NOT NULL,workspace_id TEXT NOT NULL,PRIMARY KEY(workspace_id,key),FOREIGN KEY(workspace_id,task_id) REFERENCES tasks_v2(workspace_id,id))""",
}
INDEX_DDL = [
    "CREATE INDEX documents_scope ON documents(workspace_id,deleted)",
    "CREATE INDEX versions_scope ON versions(workspace_id,document_id,created_at)",
    "CREATE INDEX tasks_scope ON tasks(workspace_id,state,created_at)",
    "CREATE INDEX tasks_state ON tasks(state,created_at)",
    "CREATE INDEX memberships_user ON memberships(user_id,workspace_id)",
    "CREATE INDEX sessions_user ON sessions(user_id,revoked_at)",
]


def schema_signature(db):
    return db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()


def schema_digest(db):
    return hashlib.sha256(json.dumps(schema_signature(db), sort_keys=True).encode()).hexdigest()


def validate_marker(db, path):
    """Bind a successful offline verification record to immutable history fields.

    Live rows may change and initial owners may be demoted. Neither changes the
    historical migration binding; do not compare the live dataset to v1 here.
    """
    marker = Path(str(path) + ".migration-verified.json")
    if Path(str(path) + ".quarantined.json").exists() or not marker.is_file():
        raise MigrationError("Verified migration required")
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
        required = {
            "state",
            "owner_id",
            "workspace_id",
            "source_logical_fingerprint",
            "schema_sha256",
            "test_credential_unusable",
        }
        if not isinstance(value, dict) or not required.issubset(value):
            raise ValueError("Invalid marker fields")
        if (
            value["state"] != "migration_verified_not_deployed"
            or value["test_credential_unusable"] is not False
        ):
            raise ValueError("Unverified marker state")
        for field in ("owner_id", "workspace_id"):
            if not isinstance(value[field], str):
                raise ValueError("Invalid identifier")
            uuid.UUID(value[field])
        for field in ("source_logical_fingerprint", "schema_sha256"):
            if not isinstance(value[field], str) or not re.fullmatch("[0-9a-f]{64}", value[field]):
                raise ValueError("Invalid digest")
        history = db.execute(
            "SELECT initial_owner_user_id,default_workspace_id,source_logical_fingerprint FROM migration_history WHERE version=2"
        ).fetchone()
        if not history or tuple(history) != (
            value["owner_id"],
            value["workspace_id"],
            value["source_logical_fingerprint"],
        ):
            raise ValueError("Migration binding mismatch")
        if not db.execute("SELECT 1 FROM users WHERE id=?", (value["owner_id"],)).fetchone():
            raise ValueError("Initial owner reference missing")
        if not db.execute("SELECT 1 FROM workspaces WHERE id=?", (value["workspace_id"],)).fetchone():
            raise ValueError("Default workspace reference missing")
        if value["schema_sha256"] != schema_digest(db):
            raise ValueError("Database schema mismatch")
    except (ValueError, TypeError, KeyError, OSError, sqlite3.DatabaseError) as exc:
        raise MigrationError("Invalid migration verification binding") from exc
    return value


def preflight(db):
    if db.execute("PRAGMA user_version").fetchone()[0] != 1:
        raise MigrationError("Not a v1 database")
    expected = json.loads((Path(__file__).with_name("v1-schema.json")).read_text(encoding="utf-8"))
    if [list(x) for x in schema_signature(db)] != expected:
        raise MigrationError("Unknown v1 schema; no automatic repair")
    integrity(db)
    application_checks(db)


def verify_v2(db, before, seed):
    integrity(db)
    application_checks(db)
    now = old_values(db)
    if fingerprint(now) != fingerprint(before):
        raise MigrationError("Original data fingerprint changed")
    # Explicit typed direct value equality, including exact Python bytes for BLOBs.
    for table in OLD:

        def sortkey(r):
            return json.dumps(
                [[type(v).__name__, v.hex() if isinstance(v, bytes) else v] for v in r], ensure_ascii=False
            )

        if sorted(now[table], key=sortkey) != sorted(before[table], key=sortkey):
            raise MigrationError("Original bytes or IDs changed: " + table)
    for table in BUSINESS:
        if db.execute(
            "SELECT COUNT(*) FROM " + table + " WHERE workspace_id IS NULL OR workspace_id!=?",
            (seed.workspace_id,),
        ).fetchone()[0]:
            raise MigrationError("Incorrect workspace binding")
    if db.execute("SELECT COUNT(*) FROM tasks WHERE requested_by_user_id IS NOT NULL").fetchone()[0]:
        raise MigrationError("Historical authors invented")
    if (
        db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        or db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    ):
        raise MigrationError("Unexpected login/audit records")
    if (
        db.execute(
            "SELECT COUNT(*) FROM memberships m JOIN users u ON m.user_id=u.id WHERE m.workspace_id=? AND m.user_id=? AND m.role='owner' AND u.status='active'",
            (seed.workspace_id, seed.user_id),
        ).fetchone()[0]
        != 1
    ):
        raise MigrationError("Initial owner missing")
    if (
        db.execute("SELECT COUNT(*) FROM users").fetchone()[0] != 1
        or db.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] != 1
    ):
        raise MigrationError("Bootstrap is not singular")
    oldgen = dict(before["meta"])["generation"]
    if (
        db.execute("SELECT generation FROM workspaces WHERE id=?", (seed.workspace_id,)).fetchone()[0]
        != oldgen
    ):
        raise MigrationError("Initial generation changed")
    if any("_v2" in (sql or "") for _, _, _, sql in schema_signature(db)):
        raise MigrationError("Temporary schema references remain")


def validate_seed(seed, allow_test_seed):
    if not re.fullmatch("[a-z0-9._-]{3,64}", seed.username):
        raise MigrationError("Invalid normalized owner name")
    for value in (seed.user_id, seed.workspace_id, seed.grant_id):
        uuid.UUID(value)
    if seed.password_hash.startswith("!UNUSABLE:"):
        if not allow_test_seed:
            raise MigrationError("Unusable test credential requires explicit isolated test mode")
    elif not re.match(r"^\$argon2id\$v=19\$m=\d+,t=\d+,p=\d+\$", seed.password_hash):
        raise MigrationError("Expected precomputed Argon2id PHC hash")


def migrate(path, seed=Seed(), *, allow_test_seed=False, hook=None):
    path = safe_target(path)
    validate_seed(seed, allow_test_seed)
    verified = Path(str(path) + ".migration-verified.json")
    quarantine = Path(str(path) + ".quarantined.json")
    hook = hook or (lambda stage: None)
    with closing(connect(path)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 2:
            if quarantine.exists() or not verified.exists():
                raise MigrationError("Unverified/quarantined v2; do not reactivate")
            history = db.execute(
                "SELECT default_workspace_id,initial_owner_user_id,source_logical_fingerprint FROM migration_history WHERE version=2"
            ).fetchone()
            identity = db.execute(
                "SELECT username_normalized FROM users WHERE id=?", (seed.user_id,)
            ).fetchone()
            if (
                not history
                or history[:2] != (seed.workspace_id, seed.user_id)
                or identity != (seed.username,)
            ):
                raise MigrationError("Existing migration owner/workspace mismatch")
            marker = json.loads(verified.read_text())
            if marker["source_logical_fingerprint"] != history[2]:
                raise MigrationError("Migration binding mismatch")
            if marker["schema_sha256"] != schema_digest(db):
                raise MigrationError("Existing v2 schema mismatch")
            integrity(db)
            return {"state": "already_v2", "owner_id": seed.user_id, "workspace_id": seed.workspace_id}
        preflight(db)
        # Only this guarded disposable migration copy is normalized. The v1
        # backup and formal WAL database remain untouched. DELETE gives the
        # kill experiment a real rollback journal and no copied WAL sidecars.
        if db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            raise MigrationError("Cannot normalize isolated migration journal")
        before = old_values(db)
        before_digest = logical_digest(before)
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("PRAGMA legacy_alter_table=OFF")
        if sqlite3.sqlite_version_info < (3, 26):
            raise MigrationError("Unsupported rename behavior")
        committed = False
        try:
            db.execute("BEGIN IMMEDIATE")
            for sql in AUTH_DDL:
                db.execute(sql)
            stamp = datetime.now(timezone.utc).isoformat()
            db.execute(
                "INSERT INTO users VALUES(?,?,?,?,?,?,?,?)",
                (
                    seed.user_id,
                    seed.username,
                    seed.display_name,
                    seed.password_hash,
                    "active",
                    0,
                    stamp,
                    stamp,
                ),
            )
            db.execute(
                "INSERT INTO workspaces VALUES(?,?,?,?,?,?)",
                (
                    seed.workspace_id,
                    "Default legacy workspace",
                    seed.user_id,
                    dict(before["meta"])["generation"],
                    stamp,
                    stamp,
                ),
            )
            db.execute(
                "INSERT INTO memberships VALUES(?,?,?,?,?,?,?)",
                (seed.workspace_id, seed.user_id, "owner", seed.grant_id, 1, stamp, stamp),
            )
            hook("after_auth")
            for table in BUSINESS:
                db.execute(BUSINESS_DDL[table])
            for table in BUSINESS:
                cols = ",".join('"' + c + '"' for c in OLD[table])
                extra_col = ",requested_by_user_id" if table == "tasks" else ""
                extra_value = ",NULL" if table == "tasks" else ""
                db.execute(
                    f"INSERT INTO {table}_v2({cols},workspace_id{extra_col}) SELECT {cols},?{extra_value} FROM {table}",
                    (seed.workspace_id,),
                )
                assert db.in_transaction
                hook("after_copy_" + table)
            for table in ("idempotency", "chunks", "tasks", "versions", "documents"):
                db.execute("DROP TABLE " + table)
            hook("after_drop")
            for table in BUSINESS:
                db.execute(f"ALTER TABLE {table}_v2 RENAME TO {table}")
            hook("after_rename")
            for sql in INDEX_DDL:
                db.execute(sql)
            hook("after_indexes")
            verify_v2(db, before, seed)
            db.execute(
                "INSERT INTO migration_history VALUES(2,?,?,?,?)",
                (seed.workspace_id, seed.user_id, before_digest, stamp),
            )
            hook("after_validate")
            db.execute("PRAGMA user_version=2")
            hook("after_user_version")
            hook("before_commit")
            assert db.in_transaction
            db.execute("COMMIT")
            committed = True
            hook("after_commit")
            db.execute("PRAGMA foreign_keys=ON")
            assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        except BaseException as exc:
            if db.in_transaction:
                db.execute("ROLLBACK")
            if committed:
                quarantine.write_text(
                    json.dumps(
                        {"state": "quarantined", "user_version": 2, "exception_type": type(exc).__name__}
                    ),
                    encoding="utf-8",
                )
            raise
    try:
        with closing(connect(path, readonly=True)) as db:
            verify_v2(db, before, seed)
            assert db.execute("PRAGMA user_version").fetchone()[0] == 2
            final_schema_sha256 = schema_digest(db)
    except BaseException as exc:
        quarantine.write_text(
            json.dumps({"state": "quarantined", "user_version": 2, "exception_type": type(exc).__name__}),
            encoding="utf-8",
        )
        raise
    result = {
        "state": "migration_verified_not_deployed",
        "owner_id": seed.user_id,
        "workspace_id": seed.workspace_id,
        "source_logical_fingerprint": before_digest,
        "schema_sha256": final_schema_sha256,
        "test_credential_unusable": seed.password_hash.startswith("!UNUSABLE:"),
    }
    verified.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
