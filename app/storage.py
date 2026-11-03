"""SQLite is both the metadata store and the atomic source of the retrieval index."""
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


class DomainError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    """Legacy v1 diagnostics only; the HTTP service uses workspace-scoped v2 storage."""
    def __init__(self, path: Path):
        from .db_policy import safe_target
        self.path = safe_target(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in [0, 1]:
                raise RuntimeError(f"Unsupported database schema: {version}")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                INSERT OR IGNORE INTO meta VALUES('generation', 0);
                CREATE TABLE IF NOT EXISTS documents(
                    id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                    desired_revision INTEGER NOT NULL DEFAULT 0, active_version_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS versions(
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
                    label TEXT NOT NULL, title TEXT NOT NULL, source_url TEXT NOT NULL,
                    format TEXT NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL,
                    revision INTEGER NOT NULL, status TEXT NOT NULL, model_tag TEXT,
                    chunk_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    UNIQUE(document_id,label));
                CREATE TABLE IF NOT EXISTS tasks(
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
                    version_id TEXT NOT NULL REFERENCES versions(id), revision INTEGER NOT NULL,
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS idempotency(
                    key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(id));
                CREATE TABLE IF NOT EXISTS chunks(
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
                    version_id TEXT NOT NULL REFERENCES versions(id), ordinal INTEGER NOT NULL,
                    heading TEXT NOT NULL, text TEXT NOT NULL, vector BLOB);
                CREATE INDEX IF NOT EXISTS chunks_version ON chunks(version_id,ordinal);
                CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state,created_at);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _bump_generation(db):
        db.execute("UPDATE meta SET value=value+1 WHERE key='generation'")

    def generation(self):
        with self.connection() as db:
            return db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]

    def import_document(self, payload: dict, idempotency_key: str | None):
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        content_hash = hashlib.sha256(payload["content"].encode()).hexdigest()
        with self.transaction() as db:
            if idempotency_key:
                prior = db.execute("SELECT * FROM idempotency WHERE key=?", (idempotency_key,)).fetchone()
                if prior:
                    if prior["request_hash"] != fingerprint:
                        raise DomainError(409, "相同 Idempotency-Key 已用于不同请求")
                    task = dict(db.execute("SELECT * FROM tasks WHERE id=?", (prior["task_id"],)).fetchone())
                    return {"task": task, "duplicate": True}
            doc = db.execute("SELECT * FROM documents WHERE key=?", (payload["key"],)).fetchone()
            stamp = now()
            if doc and doc["deleted"]:
                raise DomainError(409, "该文档已删除；重新导入请使用新的文档标识")
            if not doc:
                doc_id = uuid.uuid4().hex
                db.execute("INSERT INTO documents VALUES(?,?,0,0,NULL,?,?)", (doc_id, payload["key"], stamp, stamp))
                doc = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
            old = db.execute("SELECT * FROM versions WHERE document_id=? AND label=?", (doc["id"], payload["version_label"])).fetchone()
            if old:
                if old["content_hash"] != content_hash or any(old[k] != payload[k] for k in ["title", "source_url", "format"]):
                    raise DomainError(409, "该版本标签已有不同内容或元数据；请使用新版本标签")
                task = dict(db.execute("SELECT * FROM tasks WHERE version_id=?", (old["id"],)).fetchone())
                if idempotency_key:
                    db.execute("INSERT INTO idempotency VALUES(?,?,?)", (idempotency_key, fingerprint, task["id"]))
                return {"task": task, "duplicate": True}
            revision = doc["desired_revision"] + 1
            version_id, task_id = uuid.uuid4().hex, uuid.uuid4().hex
            db.execute("UPDATE documents SET desired_revision=?,updated_at=? WHERE id=?", (revision, stamp, doc["id"]))
            db.execute("UPDATE tasks SET state='superseded',updated_at=? WHERE document_id=? AND state='queued'", (stamp, doc["id"]))
            db.execute("UPDATE versions SET status='superseded' WHERE document_id=? AND status='queued'", (doc["id"],))
            db.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?,? ,NULL,0,?)", (
                version_id, doc["id"], payload["version_label"], payload["title"], payload["source_url"],
                payload["format"], payload["content"], content_hash, revision, "queued", stamp,
            ))
            db.execute("INSERT INTO tasks VALUES(?,?,?,?, 'queued',0,NULL,?,?)", (task_id, doc["id"], version_id, revision, stamp, stamp))
            if idempotency_key:
                db.execute("INSERT INTO idempotency VALUES(?,?,?)", (idempotency_key, fingerprint, task_id))
            task = dict(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
            return {"task": task, "duplicate": False}

    def documents(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute("""
                SELECT d.*,v.title,v.label AS version_label,v.chunk_count,
                  (SELECT COUNT(*) FROM versions x WHERE x.document_id=d.id) AS version_count,
                  (SELECT state FROM tasks t WHERE t.document_id=d.id ORDER BY t.created_at DESC LIMIT 1) AS task_state
                FROM documents d LEFT JOIN versions v ON v.id=d.active_version_id
                WHERE d.deleted=0 ORDER BY d.created_at
            """)]

    def versions(self, doc_id):
        with self.connection() as db:
            doc = db.execute("SELECT * FROM documents WHERE id=? AND deleted=0", (doc_id,)).fetchone()
            if not doc:
                raise DomainError(404, "文档不存在或已删除")
            rows = db.execute("""SELECT v.id,v.label,v.title,v.source_url,v.status,v.chunk_count,v.content_hash,
                v.created_at,v.model_tag,t.id AS task_id,t.state AS task_state,t.error,t.attempts
                FROM versions v JOIN tasks t ON t.version_id=v.id WHERE v.document_id=? ORDER BY v.created_at DESC""", (doc_id,))
            return {"document": dict(doc), "versions": [dict(r) for r in rows]}

    def tasks(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute("""SELECT t.*,d.key,v.label AS version_label,v.title
                FROM tasks t JOIN documents d ON d.id=t.document_id JOIN versions v ON v.id=t.version_id
                ORDER BY t.created_at DESC LIMIT 100""")]

    def task(self, task_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise DomainError(404, "任务不存在")
            return dict(row)

    def delete_document(self, doc_id):
        with self.transaction() as db:
            doc = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
            if not doc:
                raise DomainError(404, "文档不存在")
            if not doc["deleted"]:
                stamp = now()
                db.execute("UPDATE documents SET deleted=1,desired_revision=desired_revision+1,active_version_id=NULL,updated_at=? WHERE id=?", (stamp, doc_id))
                db.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
                db.execute("UPDATE tasks SET state='cancelled',updated_at=? WHERE document_id=? AND state IN ('queued','running')", (stamp, doc_id))
                db.execute("UPDATE versions SET status='cancelled' WHERE document_id=? AND status IN ('queued','running')", (doc_id,))
                self._bump_generation(db)

    def switch_version(self, doc_id, version_id):
        with self.transaction() as db:
            doc = db.execute("SELECT * FROM documents WHERE id=? AND deleted=0", (doc_id,)).fetchone()
            version = db.execute("SELECT * FROM versions WHERE id=? AND document_id=? AND status='ready'", (version_id, doc_id)).fetchone()
            if not doc or not version:
                raise DomainError(409, "只能切换到未删除文档的已就绪版本")
            stamp = now()
            db.execute("UPDATE documents SET active_version_id=?,desired_revision=desired_revision+1,updated_at=? WHERE id=?", (version_id, stamp, doc_id))
            db.execute("UPDATE tasks SET state='superseded',updated_at=? WHERE document_id=? AND state IN ('queued','running')", (stamp, doc_id))
            db.execute("UPDATE versions SET status='superseded' WHERE document_id=? AND status IN ('queued','running')", (doc_id,))
            self._bump_generation(db)

    def recover_tasks(self):
        # A single app process owns this worker (deployment uses one uvicorn worker).
        with self.transaction() as db:
            stamp = now()
            db.execute("""UPDATE tasks SET state='superseded',updated_at=? WHERE state IN ('queued','running') AND
                EXISTS(SELECT 1 FROM documents d WHERE d.id=tasks.document_id AND
                  (d.deleted=1 OR d.desired_revision!=tasks.revision))""", (stamp,))
            db.execute("UPDATE versions SET status='superseded' WHERE id IN (SELECT version_id FROM tasks WHERE state='superseded') AND status!='ready'")
            count = db.execute("SELECT COUNT(*) FROM tasks WHERE state='running'").fetchone()[0]
            db.execute("UPDATE versions SET status='queued' WHERE id IN (SELECT version_id FROM tasks WHERE state='running')")
            db.execute("UPDATE tasks SET state='queued',updated_at=? WHERE state='running'", (stamp,))
            return count

    def claim_task(self):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE tasks SET state='running',attempts=attempts+1,updated_at=? WHERE id=?", (now(), row["id"]))
            db.execute("UPDATE versions SET status='running' WHERE id=?", (row["version_id"],))
            task = dict(row)
            task["version"] = dict(db.execute("SELECT * FROM versions WHERE id=?", (row["version_id"],)).fetchone())
            return task

    def publish(self, task, chunks: list, model_tag: str | None):
        with self.transaction() as db:
            doc = db.execute("SELECT * FROM documents WHERE id=?", (task["document_id"],)).fetchone()
            current = db.execute("SELECT state FROM tasks WHERE id=?", (task["id"],)).fetchone()[0]
            if current != "running":
                return False
            if doc["deleted"] or doc["desired_revision"] != task["revision"]:
                db.execute("UPDATE tasks SET state='superseded',updated_at=? WHERE id=?", (now(), task["id"]))
                db.execute("UPDATE versions SET status='superseded' WHERE id=?", (task["version_id"],))
                return False
            db.execute("DELETE FROM chunks WHERE version_id=?", (task["version_id"],))
            db.executemany("INSERT INTO chunks VALUES(?,?,?,?,?,?,?)", [
                (f"{task['version_id']}:{i}", task["document_id"], task["version_id"], i,
                 c["heading"], c["text"], c.get("vector")) for i, c in enumerate(chunks)
            ])
            db.execute("UPDATE versions SET status='ready',chunk_count=?,model_tag=? WHERE id=?", (len(chunks), model_tag, task["version_id"]))
            db.execute("UPDATE documents SET active_version_id=?,updated_at=? WHERE id=?", (task["version_id"], now(), task["document_id"]))
            db.execute("UPDATE tasks SET state='succeeded',error=NULL,updated_at=? WHERE id=?", (now(), task["id"]))
            self._bump_generation(db)
            return True

    def fail(self, task, error):
        with self.transaction() as db:
            row = db.execute("SELECT t.*,d.deleted,d.desired_revision FROM tasks t JOIN documents d ON d.id=t.document_id WHERE t.id=?", (task["id"],)).fetchone()
            if row["state"] != "running":
                return
            stale = row["deleted"] or row["desired_revision"] != row["revision"]
            state = "superseded" if stale else "failed"
            db.execute("UPDATE tasks SET state=?,error=?,updated_at=? WHERE id=?", (state, str(error)[:500], now(), task["id"]))
            db.execute("UPDATE versions SET status=? WHERE id=?", (state, task["version_id"]))

    def retry(self, task_id):
        with self.transaction() as db:
            row = db.execute("SELECT t.*,d.deleted,d.desired_revision FROM tasks t JOIN documents d ON d.id=t.document_id WHERE t.id=?", (task_id,)).fetchone()
            if not row:
                raise DomainError(404, "任务不存在")
            if row["state"] != "failed" or row["deleted"] or row["desired_revision"] != row["revision"]:
                raise DomainError(409, "只能重试当前文档的失败版本；已被新请求替代的任务不可发布")
            db.execute("UPDATE tasks SET state='queued',error=NULL,updated_at=? WHERE id=?", (now(), task_id))
            db.execute("UPDATE versions SET status='queued' WHERE id=?", (row["version_id"],))

    def snapshot(self, doc_id=None, version_id=None):
        with self.connection() as db:
            db.execute("BEGIN")
            generation = db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]
            where = "d.deleted=0 AND v.status='ready'"
            args = []
            if doc_id:
                where += " AND d.id=?"
                args.append(doc_id)
            if version_id:
                if not doc_id:
                    raise DomainError(422, "筛选历史版本时必须同时指定文档")
                version = db.execute("SELECT id FROM versions WHERE id=? AND document_id=? AND status='ready'", (version_id, doc_id)).fetchone()
                if not version:
                    raise DomainError(404, "所选版本未就绪或不存在")
                where += " AND v.id=?"
                args.append(version_id)
            else:
                where += " AND v.id=d.active_version_id"
            rows = db.execute(f"""SELECT c.*,d.key,v.label AS version_label,v.title,v.source_url,v.model_tag
                FROM chunks c JOIN documents d ON d.id=c.document_id JOIN versions v ON v.id=c.version_id
                WHERE {where} ORDER BY d.created_at,c.ordinal""", args).fetchall()
            return generation, [dict(r) for r in rows]

    def passage(self, chunk_id):
        with self.connection() as db:
            row = db.execute("""SELECT c.*,v.label AS version_label,v.title,v.source_url,d.active_version_id
                FROM chunks c JOIN versions v ON v.id=c.version_id JOIN documents d ON d.id=c.document_id
                WHERE c.id=? AND d.deleted=0 AND v.status='ready'""", (chunk_id,)).fetchone()
            if not row:
                raise DomainError(404, "原文片段不存在或所属文档已删除")
            result = dict(row)
            result.pop("vector")
            result["is_active"] = result["version_id"] == result.pop("active_version_id")
            result["neighbors"] = [dict(r) for r in db.execute("SELECT id,ordinal,heading,text FROM chunks WHERE version_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal", (row["version_id"], max(0, row["ordinal"] - 1), row["ordinal"] + 1))]
            return result
