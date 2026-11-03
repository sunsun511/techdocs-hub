"""Explicit workspace-scoped data paths. No formal database/code imports."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import uuid
from . import auth as a
from .storage import DomainError


def now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, repr=False)
class Grant:
    ws: str
    uid: str
    sid: str
    epoch: int
    grant_id: str
    revision: int
    role: str

    def stamp(self):
        return (self.ws, self.uid, self.sid, self.epoch, self.grant_id, self.revision, self.role)


class Guard:
    levels = {"viewer": 1, "editor": 2, "owner": 3}

    def __init__(self, auth):
        self.auth = auth

    def scope(self, db, token, ws, csrf=None, need="viewer", expected=None):
        session = self.auth.current(db, token, csrf)
        member = db.execute(
            """SELECT m.* FROM memberships m JOIN workspaces w ON w.id=m.workspace_id
                            WHERE m.workspace_id=? AND m.user_id=?""",
            (ws, session["user_id"]),
        ).fetchone()
        if not member:
            raise a.AuthError(404, "Resource not found")
        g = Grant(
            ws,
            session["user_id"],
            session["id"],
            session["user_epoch"],
            member["grant_id"],
            member["revision"],
            member["role"],
        )
        self.require(g, need)
        if expected and g.stamp() != expected.stamp():
            raise a.AuthError(409, "Permission state changed; retry request")
        return g

    def require(self, g, need):
        if self.levels.get(g.role, 0) < self.levels[need]:
            raise a.AuthError(403, "Insufficient workspace role")

    @staticmethod
    def owners(db, ws):
        n = db.execute(
            """SELECT COUNT(*) FROM memberships m JOIN users u ON u.id=m.user_id
             WHERE m.workspace_id=? AND m.role='owner' AND u.status='active'""",
            (ws,),
        ).fetchone()[0]
        if n < 1:
            raise a.AuthError(409, "Workspace must retain an enabled owner")

    @staticmethod
    def resource(db, ws, table, identifier, live=False):
        if table not in ("documents", "versions", "tasks", "chunks"):
            raise ValueError("Internal resource kind")
        row = db.execute(
            "SELECT * FROM "
            + table
            + " WHERE workspace_id=? AND id=?"
            + (" AND deleted=0" if live and table == "documents" else ""),
            (ws, identifier),
        ).fetchone()
        if not row:
            raise a.AuthError(404, "Resource not found")
        return row

    def interactive(self, db, grant):
        stamp = a.encoded_time(self.auth.clock())
        row = db.execute("SELECT absolute_expires_at FROM sessions WHERE id=?", (grant.sid,)).fetchone()
        deadline = min(
            datetime.fromtimestamp(self.auth.clock() + a.IDLE, timezone.utc), datetime.fromisoformat(row[0])
        )
        db.execute(
            "UPDATE sessions SET last_activity_at=?,idle_expires_at=? WHERE id=?",
            (stamp, deadline.isoformat(), grant.sid),
        )


class ScopedStore:
    def __init__(self, runtime, ws, token=None, csrf=None):
        self.rt, self.ws, self.token, self.csrf = runtime, ws, token, csrf

    @contextmanager
    def write(self, resources=()):
        self.rt.hook("write.before_tx")
        with self.rt.auth.transaction(write=True) as db:
            grant = self.rt.guard.scope(db, self.token, self.ws, self.csrf)
            for table, identifier, live in resources:
                self.rt.guard.resource(db, self.ws, table, identifier, live)
            self.rt.guard.require(grant, "editor")
            self.rt.hook("write.after_authorize")
            yield db, grant
            self.rt.guard.interactive(db, grant)
        self.rt.hook("write.after_commit")

    def read(self, fn, resources=(), need="viewer", interactive=False, refresh_at_final=False):
        with self.rt.auth.transaction() as db:
            initial = self.rt.guard.scope(db, self.token, self.ws, self.csrf, need)
            for table, identifier, live in resources:
                self.rt.guard.resource(db, self.ws, table, identifier, live)
            gen = self.generation(db)
            value = fn(db)
        self.rt.hook("read.before_final")
        with self.rt.auth.transaction(write=interactive) as db:
            g = self.rt.guard.scope(db, self.token, self.ws, self.csrf, need, initial)
            for table, identifier, live in resources:
                self.rt.guard.resource(db, self.ws, table, identifier, live)
            if refresh_at_final:
                # Task polling reads the current row in the same snapshot as
                # final authorization; normal index publication is not a conflict.
                value = fn(db)
            elif self.generation(db) != gen:
                raise a.AuthError(409, "Workspace data changed; retry request")
            if interactive:
                self.rt.guard.interactive(db, g)
        self.rt.hook("read.after_final")
        return value

    def generation(self, db=None):
        if db is not None:
            row = db.execute("SELECT generation FROM workspaces WHERE id=?", (self.ws,)).fetchone()
            if not row:
                raise a.AuthError(404, "Resource not found")
            return row[0]
        with self.rt.auth.transaction() as db:
            return self.generation(db)

    def bump(self, db):
        db.execute("UPDATE workspaces SET generation=generation+1,updated_at=? WHERE id=?", (now(), self.ws))

    def import_document(self, payload, key=None):
        if len(payload["content"].encode()) > 1_000_000:
            raise DomainError(413, "正文超过1MB")
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        content_hash = hashlib.sha256(payload["content"].encode()).hexdigest()
        with self.write() as (db, g):
            if key:
                prior = db.execute(
                    "SELECT * FROM idempotency WHERE workspace_id=? AND key=?", (self.ws, key)
                ).fetchone()
                if prior:
                    if prior["request_hash"] != fingerprint:
                        raise DomainError(409, "Idempotency key conflict")
                    result = {
                        "task": dict(self.rt.guard.resource(db, self.ws, "tasks", prior["task_id"])),
                        "duplicate": True,
                    }
                    return result
            doc = db.execute(
                "SELECT * FROM documents WHERE workspace_id=? AND key=?", (self.ws, payload["key"])
            ).fetchone()
            stamp = now()
            if doc and doc["deleted"]:
                raise DomainError(409, "Document deleted; use a new key")
            if not doc:
                did = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO documents VALUES(?,?,0,0,NULL,?,?,?)",
                    (did, payload["key"], stamp, stamp, self.ws),
                )
                doc = self.rt.guard.resource(db, self.ws, "documents", did)
            old = db.execute(
                "SELECT * FROM versions WHERE workspace_id=? AND document_id=? AND label=?",
                (self.ws, doc["id"], payload["version_label"]),
            ).fetchone()
            if old:
                if old["content_hash"] != content_hash or any(
                    old[k] != payload[k] for k in ["title", "source_url", "format"]
                ):
                    raise DomainError(409, "Version label conflict")
                task = db.execute(
                    "SELECT * FROM tasks WHERE workspace_id=? AND version_id=?", (self.ws, old["id"])
                ).fetchone()
                if key:
                    db.execute(
                        "INSERT INTO idempotency VALUES(?,?,?,?)", (key, fingerprint, task["id"], self.ws)
                    )
                return {"task": dict(task), "duplicate": True}
            revision = doc["desired_revision"] + 1
            vid, tid = uuid.uuid4().hex, uuid.uuid4().hex
            db.execute(
                "UPDATE documents SET desired_revision=?,updated_at=? WHERE workspace_id=? AND id=?",
                (revision, stamp, self.ws, doc["id"]),
            )
            db.execute(
                "UPDATE tasks SET state='superseded',updated_at=? WHERE workspace_id=? AND document_id=? AND state='queued'",
                (stamp, self.ws, doc["id"]),
            )
            db.execute(
                "UPDATE versions SET status='superseded' WHERE workspace_id=? AND document_id=? AND status='queued'",
                (self.ws, doc["id"]),
            )
            db.execute(
                "INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?,?,NULL,0,?,?)",
                (
                    vid,
                    doc["id"],
                    payload["version_label"],
                    payload["title"],
                    payload["source_url"],
                    payload["format"],
                    payload["content"],
                    content_hash,
                    revision,
                    "queued",
                    stamp,
                    self.ws,
                ),
            )
            db.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,0,NULL,?,?,?,?)",
                (tid, doc["id"], vid, revision, "queued", stamp, stamp, self.ws, g.uid),
            )
            if key:
                db.execute("INSERT INTO idempotency VALUES(?,?,?,?)", (key, fingerprint, tid, self.ws))
            result = {"task": dict(self.rt.guard.resource(db, self.ws, "tasks", tid)), "duplicate": False}
            self.rt.hook("import.after_insert")
        return result

    def documents(self):
        return self.read(
            lambda db: {
                "documents": [
                    dict(r)
                    for r in db.execute(
                        """SELECT d.*,v.title,v.label AS version_label,v.chunk_count,
          (SELECT COUNT(*) FROM versions x WHERE x.workspace_id=d.workspace_id AND x.document_id=d.id) AS version_count,
          (SELECT state FROM tasks t WHERE t.workspace_id=d.workspace_id AND t.document_id=d.id ORDER BY t.created_at DESC LIMIT 1) AS task_state
          FROM documents d LEFT JOIN versions v ON v.workspace_id=d.workspace_id AND v.id=d.active_version_id
          WHERE d.workspace_id=? AND d.deleted=0 ORDER BY d.created_at""",
                        (self.ws,),
                    )
                ]
            }
        )

    def versions_in_db(self, db, did):
        doc = self.rt.guard.resource(db, self.ws, "documents", did, True)
        rows = db.execute(
            """SELECT v.id,v.label,v.title,v.source_url,v.status,v.chunk_count,v.content_hash,v.created_at,v.model_tag,
          t.id AS task_id,t.state AS task_state,CASE WHEN t.error IS NOT NULL THEN 'Indexing failed' END AS error,t.attempts
          FROM versions v JOIN tasks t ON t.workspace_id=v.workspace_id AND t.version_id=v.id AND t.document_id=v.document_id
          WHERE v.workspace_id=? AND v.document_id=? ORDER BY v.created_at DESC""",
            (self.ws, did),
        )
        return {"document": dict(doc), "versions": [dict(r) for r in rows]}

    def versions(self, did):
        return self.read(lambda db: self.versions_in_db(db, did), [("documents", did, True)])

    @staticmethod
    def task_public(row):
        result = dict(row)
        if result.get("error"):
            result["error"] = "Indexing failed"
        return result

    def tasks(self):
        return self.read(
            lambda db: {
                "tasks": [
                    self.task_public(r)
                    for r in db.execute(
                        """SELECT t.*,d.key,v.label AS version_label,v.title
          FROM tasks t JOIN documents d ON d.workspace_id=t.workspace_id AND d.id=t.document_id
          JOIN versions v ON v.workspace_id=t.workspace_id AND v.id=t.version_id AND v.document_id=t.document_id
          WHERE t.workspace_id=? ORDER BY t.created_at DESC LIMIT 100""",
                        (self.ws,),
                    )
                ]
            }
        )

    def task(self, tid):
        return self.read(
            lambda db: self.task_public(self.rt.guard.resource(db, self.ws, "tasks", tid)),
            [("tasks", tid, False)],
            refresh_at_final=True,
        )

    def delete_document(self, did):
        with self.write([("documents", did, False)]) as (db, g):
            doc = self.rt.guard.resource(db, self.ws, "documents", did)
            if not doc["deleted"]:
                stamp = now()
                db.execute(
                    "UPDATE documents SET deleted=1,desired_revision=desired_revision+1,active_version_id=NULL,updated_at=? WHERE workspace_id=? AND id=?",
                    (stamp, self.ws, did),
                )
                db.execute("DELETE FROM chunks WHERE workspace_id=? AND document_id=?", (self.ws, did))
                db.execute(
                    "UPDATE tasks SET state='cancelled',updated_at=? WHERE workspace_id=? AND document_id=? AND state IN ('queued','running')",
                    (stamp, self.ws, did),
                )
                db.execute(
                    "UPDATE versions SET status='cancelled' WHERE workspace_id=? AND document_id=? AND status IN ('queued','running')",
                    (self.ws, did),
                )
                self.bump(db)
            result = {"deleted": True, "generation": self.generation(db)}
        return result

    def switch_version(self, did, vid):
        with self.write([("documents", did, True), ("versions", vid, False)]) as (db, g):
            version = self.rt.guard.resource(db, self.ws, "versions", vid)
            if version["document_id"] != did or version["status"] != "ready":
                raise DomainError(409, "只能切换到未删除文档的已就绪版本")
            stamp = now()
            db.execute(
                "UPDATE documents SET active_version_id=?,desired_revision=desired_revision+1,updated_at=? WHERE workspace_id=? AND id=?",
                (vid, stamp, self.ws, did),
            )
            db.execute(
                "UPDATE tasks SET state='superseded',updated_at=? WHERE workspace_id=? AND document_id=? AND state IN ('queued','running')",
                (stamp, self.ws, did),
            )
            db.execute(
                "UPDATE versions SET status='superseded' WHERE workspace_id=? AND document_id=? AND status IN ('queued','running')",
                (self.ws, did),
            )
            self.bump(db)
            result = self.versions_in_db(db, did)
        return result

    def retry(self, tid):
        with self.write([("tasks", tid, False)]) as (db, g):
            row = self.rt.guard.resource(db, self.ws, "tasks", tid)
            doc = self.rt.guard.resource(db, self.ws, "documents", row["document_id"])
            if row["state"] != "failed" or doc["deleted"] or row["revision"] != doc["desired_revision"]:
                raise DomainError(409, "Only current failed task can retry")
            db.execute(
                "UPDATE tasks SET state='queued',error=NULL,updated_at=? WHERE workspace_id=? AND id=?",
                (now(), self.ws, tid),
            )
            db.execute(
                "UPDATE versions SET status='queued' WHERE workspace_id=? AND id=?",
                (self.ws, row["version_id"]),
            )
            result = self.task_public(self.rt.guard.resource(db, self.ws, "tasks", tid))
        return result

    def snapshot(self, did=None, vid=None):
        with self.rt.auth.transaction() as db:
            generation = self.generation(db)
            args = [self.ws]
            where = "c.workspace_id=? AND d.deleted=0 AND v.status='ready'"
            if did:
                self.rt.guard.resource(db, self.ws, "documents", did, True)
                where += " AND d.id=?"
                args.append(did)
            if vid:
                if not did:
                    raise DomainError(422, "筛选历史版本时必须同时指定文档")
                version = self.rt.guard.resource(db, self.ws, "versions", vid)
                if version["document_id"] != did or version["status"] != "ready":
                    raise DomainError(404, "Resource not found")
                where += " AND v.id=?"
                args.append(vid)
            else:
                where += " AND v.id=d.active_version_id"
            rows = db.execute(
                """SELECT c.*,d.key,v.label AS version_label,v.title,v.source_url,v.model_tag
              FROM chunks c JOIN documents d ON d.workspace_id=c.workspace_id AND d.id=c.document_id
              JOIN versions v ON v.workspace_id=c.workspace_id AND v.id=c.version_id AND v.document_id=c.document_id
              WHERE """
                + where
                + " ORDER BY d.created_at,c.ordinal",
                args,
            )
            return generation, [dict(r) for r in rows]

    def passage(self, cid):
        def read(db):
            c = self.rt.guard.resource(db, self.ws, "chunks", cid)
            self.rt.guard.resource(db, self.ws, "documents", c["document_id"], True)
            v = self.rt.guard.resource(db, self.ws, "versions", c["version_id"])
            if v["status"] != "ready":
                raise DomainError(404, "Resource not found")
            doc = self.rt.guard.resource(db, self.ws, "documents", c["document_id"])
            result = dict(c)
            result.pop("vector")
            result.update(
                version_label=v["label"],
                title=v["title"],
                source_url=v["source_url"],
                is_active=doc["active_version_id"] == v["id"],
            )
            result["neighbors"] = [
                dict(r)
                for r in db.execute(
                    "SELECT id,ordinal,heading,text FROM chunks WHERE workspace_id=? AND version_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal",
                    (self.ws, v["id"], max(0, c["ordinal"] - 1), c["ordinal"] + 1),
                )
            ]
            return result

        return self.read(read, [("chunks", cid, False)], interactive=True)


class InternalStore:
    """Server-owned worker only. No HTTP route or caller-supplied bypass mode."""

    def __init__(self, rt):
        self.rt = rt

    def recover_tasks(self):
        with self.rt.auth.transaction(write=True) as db:
            stamp = now()
            db.execute(
                """UPDATE tasks SET state='superseded',updated_at=? WHERE state IN ('queued','running') AND
              EXISTS(SELECT 1 FROM documents d WHERE d.workspace_id=tasks.workspace_id AND d.id=tasks.document_id AND
                (d.deleted=1 OR d.desired_revision!=tasks.revision))""",
                (stamp,),
            )
            db.execute(
                "UPDATE versions SET status='superseded' WHERE status!='ready' AND EXISTS(SELECT 1 FROM tasks t WHERE t.workspace_id=versions.workspace_id AND t.version_id=versions.id AND t.state='superseded')"
            )
            n = db.execute("SELECT COUNT(*) FROM tasks WHERE state='running'").fetchone()[0]
            db.execute(
                "UPDATE versions SET status='queued' WHERE EXISTS(SELECT 1 FROM tasks t WHERE t.workspace_id=versions.workspace_id AND t.version_id=versions.id AND t.state='running')"
            )
            db.execute("UPDATE tasks SET state='queued',updated_at=? WHERE state='running'", (stamp,))
            return n

    def claim_task(self):
        with self.rt.auth.transaction(write=True) as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE state='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            v = self.rt.guard.resource(db, row["workspace_id"], "versions", row["version_id"])
            if v["document_id"] != row["document_id"]:
                raise DomainError(409, "Internal task association invalid")
            db.execute(
                "UPDATE tasks SET state='running',attempts=attempts+1,updated_at=? WHERE workspace_id=? AND id=?",
                (now(), row["workspace_id"], row["id"]),
            )
            db.execute(
                "UPDATE versions SET status='running' WHERE workspace_id=? AND id=?",
                (row["workspace_id"], row["version_id"]),
            )
            result = dict(row)
            result["version"] = dict(v)
            return result

    def validate(self, db, task):
        row = self.rt.guard.resource(db, task["workspace_id"], "tasks", task["id"])
        if any(row[k] != task[k] for k in ("workspace_id", "document_id", "version_id", "revision")):
            raise DomainError(409, "Internal task association invalid")
        return row, self.rt.guard.resource(db, task["workspace_id"], "documents", task["document_id"])

    def publish(self, task, chunks, tag):
        with self.rt.auth.transaction(write=True) as db:
            current, doc = self.validate(db, task)
            ws = task["workspace_id"]
            if current["state"] != "running":
                return False
            if doc["deleted"] or doc["desired_revision"] != task["revision"]:
                db.execute(
                    "UPDATE tasks SET state='superseded',updated_at=? WHERE workspace_id=? AND id=?",
                    (now(), ws, task["id"]),
                )
                db.execute(
                    "UPDATE versions SET status='superseded' WHERE workspace_id=? AND id=?",
                    (ws, task["version_id"]),
                )
                return False
            db.execute("DELETE FROM chunks WHERE workspace_id=? AND version_id=?", (ws, task["version_id"]))
            db.executemany(
                "INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)",
                [
                    (
                        f"{task['version_id']}:{i}",
                        task["document_id"],
                        task["version_id"],
                        i,
                        c["heading"],
                        c["text"],
                        c.get("vector"),
                        ws,
                    )
                    for i, c in enumerate(chunks)
                ],
            )
            self.rt.hook("publish.after_chunks")
            db.execute(
                "UPDATE versions SET status='ready',chunk_count=?,model_tag=? WHERE workspace_id=? AND id=?",
                (len(chunks), tag, ws, task["version_id"]),
            )
            db.execute(
                "UPDATE documents SET active_version_id=?,updated_at=? WHERE workspace_id=? AND id=?",
                (task["version_id"], now(), ws, task["document_id"]),
            )
            db.execute(
                "UPDATE tasks SET state='succeeded',error=NULL,updated_at=? WHERE workspace_id=? AND id=?",
                (now(), ws, task["id"]),
            )
            ScopedStore(self.rt, ws).bump(db)
            return True

    def fail(self, task, error):
        with self.rt.auth.transaction(write=True) as db:
            row, doc = self.validate(db, task)
            if row["state"] != "running":
                return
            state = (
                "superseded" if doc["deleted"] or doc["desired_revision"] != task["revision"] else "failed"
            )
            db.execute(
                "UPDATE tasks SET state=?,error=?,updated_at=? WHERE workspace_id=? AND id=?",
                (state, type(error).__name__, now(), task["workspace_id"], task["id"]),
            )
            db.execute(
                "UPDATE versions SET status=? WHERE workspace_id=? AND id=?",
                (state, task["workspace_id"], task["version_id"]),
            )
