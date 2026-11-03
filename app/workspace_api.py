"""Isolated workspace APIs with real login and fresh request authorization."""

from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import re
import threading
from typing import Literal
import uuid
from fastapi import Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from . import auth as a
from . import migration as m
from .workspace_store import Guard, ScopedStore, InternalStore, now
from .storage import DomainError
from .search import SearchEngine, LexicalIndex
from .worker import Worker
from .models import Models
from .schemas import ImportDocument, SearchRequest, SwitchVersion


class Cache:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}

    def digest(self):
        with self.lock:
            value = [
                (
                    repr(k),
                    m.fingerprint({"chunks": [tuple(c.values()) for c in v.chunks]})["chunks"][
                        "table_sha256"
                    ],
                )
                for k, v in self.entries.items()
            ]
        return hashlib.sha256(json.dumps(sorted(value)).encode()).hexdigest()

    def publish(self, ws, generation, staged):
        with self.lock:
            self.entries = {k: v for k, v in self.entries.items() if k[0] != ws or k[1] == generation}
            for key, index in staged.items():
                if key[1] != generation:
                    continue
                if key not in self.entries and len(self.entries) >= 32:
                    self.entries.pop(next(iter(self.entries)))
                self.entries[key] = index


class ScopedSearch(SearchEngine):
    def __init__(self, rt, store):
        self.store, self.models, self.rt = store, rt.models, rt
        self.staged = {}

    def index(self, did, vid):
        gen = self.store.generation()
        key = (self.store.ws, gen, did, vid)
        with self.rt.cache.lock:
            index = self.rt.cache.entries.get(key)
        if index is not None:
            return gen, index
        if key in self.staged:
            return gen, self.staged[key]
        actual, chunks = self.store.snapshot(did, vid)
        index = LexicalIndex(chunks)
        self.staged[(self.store.ws, actual, did, vid)] = index
        return actual, index


class Runtime:
    def __init__(self, auth, models=None, hook=None):
        self.auth = auth
        self.guard = Guard(auth)
        self.models = models or Models(Path(__file__).resolve().parents[1] / "models", enabled=False)
        self.hook = hook or (lambda stage: None)
        self.cache = Cache()
        self.notify_calls = 0
        self.internal = InternalStore(self)
        self.worker = Worker(self.internal, self.models)

    def store(self, ws, token, csrf=None):
        return ScopedStore(self, ws, token, csrf)

    def notify(self):
        self.notify_calls += 1
        self.worker.notify()

    def search(self, ws, token, csrf, request):
        store = self.store(ws, token, csrf)
        with self.auth.transaction() as db:
            initial = self.guard.scope(db, token, ws, csrf)
            if request.document_id:
                self.guard.resource(db, ws, "documents", request.document_id, True)
            if request.version_id:
                if not request.document_id:
                    raise DomainError(422, "筛选历史版本时必须同时指定文档")
                v = self.guard.resource(db, ws, "versions", request.version_id)
                if v["document_id"] != request.document_id or v["status"] != "ready":
                    raise DomainError(404, "Resource not found")
        engine = ScopedSearch(self, store)
        try:
            result = engine.search(request)
        except DomainError:
            self.hook("search.error_before_final")
            with self.auth.transaction() as db:
                self.guard.scope(db, token, ws, csrf, expected=initial)
            raise
        self.hook("search.before_final")
        with self.auth.transaction(write=True) as db:
            g = self.guard.scope(db, token, ws, csrf, expected=initial)
            if request.document_id:
                self.guard.resource(db, ws, "documents", request.document_id, True)
            if store.generation(db) != result["generation"]:
                raise DomainError(409, "Workspace data changed; retry request")
            self.guard.interactive(db, g)
        self.cache.publish(ws, result["generation"], engine.staged)
        self.hook("search.after_final")
        return result

    def workspace_list(self, token):
        with self.auth.transaction() as db:
            self.auth.current(db, token)
        self.hook("read.before_final")
        with self.auth.transaction() as db:
            session = self.auth.current(db, token)
            return {
                "workspaces": [
                    dict(r)
                    for r in db.execute(
                        """SELECT w.id,w.name,w.created_at,m.role FROM workspaces w
                   JOIN memberships m ON m.workspace_id=w.id WHERE m.user_id=? ORDER BY w.created_at""",
                        (session["user_id"],),
                    )
                ]
            }

    def create_workspace(self, token, csrf, name):
        with self.auth.transaction(write=True) as db:
            session = self.auth.current(db, token, csrf)
            ws = str(uuid.uuid4())
            stamp = now()
            db.execute(
                "INSERT INTO workspaces VALUES(?,?,?,?,?,?)", (ws, name, session["user_id"], 0, stamp, stamp)
            )
            db.execute(
                "INSERT INTO memberships VALUES(?,?,?,?,?,?,?)",
                (ws, session["user_id"], "owner", str(uuid.uuid4()), 1, stamp, stamp),
            )
            self.hook("workspace.after_create")
            return {"id": ws, "name": name, "role": "owner"}

    def workspace_read(self, ws, token):
        store = self.store(ws, token)
        return store.read(
            lambda db: {
                "id": ws,
                "name": db.execute("SELECT name FROM workspaces WHERE id=?", (ws,)).fetchone()[0],
                "role": self.guard.scope(db, token, ws).role,
            }
        )

    def rename(self, ws, token, csrf, name):
        with self.auth.transaction(write=True) as db:
            g = self.guard.scope(db, token, ws, csrf, "owner")
            db.execute("UPDATE workspaces SET name=?,updated_at=? WHERE id=?", (name, now(), ws))
            self.guard.interactive(db, g)
            return {"id": ws, "name": name}

    def members(self, ws, token):
        return self.store(ws, token).read(
            lambda db: {
                "members": [
                    dict(r)
                    for r in db.execute(
                        """SELECT m.user_id,m.role,m.revision,
              u.username_normalized AS username,u.display_name,u.status FROM memberships m JOIN users u ON u.id=m.user_id
              WHERE m.workspace_id=? ORDER BY m.created_at""",
                        (ws,),
                    )
                ]
            },
            need="owner",
        )

    def add_member(self, ws, token, csrf, username, role):
        name = a.normalize(username)
        with self.auth.transaction(write=True) as db:
            g = self.guard.scope(db, token, ws, csrf, "owner")
            u = db.execute(
                "SELECT id FROM users WHERE username_normalized=? AND status='active'", (name,)
            ).fetchone()
            if not u:
                raise a.AuthError(422, "Target not available")
            if db.execute(
                "SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=?", (ws, u["id"])
            ).fetchone():
                raise a.AuthError(409, "Already a workspace member")
            stamp = now()
            db.execute(
                "INSERT INTO memberships VALUES(?,?,?,?,?,?,?)",
                (ws, u["id"], role, str(uuid.uuid4()), 1, stamp, stamp),
            )
            self.guard.owners(db, ws)
            self.guard.interactive(db, g)
            self.hook("member.after_change")
            return {"user_id": u["id"], "role": role}

    def change_member(self, ws, token, csrf, uid, role=None, self_leave=False):
        with self.auth.transaction(write=True) as db:
            g = self.guard.scope(db, token, ws, csrf, "viewer" if self_leave else "owner")
            uid = g.uid if self_leave else uid
            member = db.execute(
                "SELECT * FROM memberships WHERE workspace_id=? AND user_id=?", (ws, uid)
            ).fetchone()
            if not member:
                raise a.AuthError(404, "Resource not found")
            if role is None:
                db.execute("DELETE FROM memberships WHERE workspace_id=? AND user_id=?", (ws, uid))
            else:
                u = db.execute("SELECT status FROM users WHERE id=?", (uid,)).fetchone()
                if u[0] != "active":
                    raise a.AuthError(422, "Target not available")
                db.execute(
                    "UPDATE memberships SET role=?,revision=revision+1,updated_at=? WHERE workspace_id=? AND user_id=?",
                    (role, now(), ws, uid),
                )
            self.guard.owners(db, ws)
            self.guard.interactive(db, g)
            self.hook("member.after_change")
            return {"user_id": uid, "role": role}

    def set_user_status(self, uid, status):
        """Trusted offline operation, not an HTTP route. Checks every affected WS."""
        from .account_admin import set_status

        return set_status(self.auth, uid, status, self.hook)


class NameInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)


class MemberInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=64)
    role: Literal["owner", "editor", "viewer"]


class RoleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["owner", "editor", "viewer"]


def create_workspace_app(path, origin, *, models=None, hook=None, worker_enabled=False):
    app = a.create_app(path, origin)
    rt = Runtime(app.state.auth, models, hook)
    app.state.runtime = rt

    @asynccontextmanager
    async def lifespan(app):
        if worker_enabled:
            rt.worker.start()
        try:
            yield
        finally:
            rt.worker.close()

    app.router.lifespan_context = lifespan

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    def token(request):
        if len(re.findall(r"(?:^|;)\s*" + a.COOKIE + r"=", request.headers.get("cookie", ""))) > 1:
            raise a.AuthError(401, "Authentication required")
        return request.cookies.get(a.COOKIE)

    def csrf(request):
        return request.headers.get("x-csrf-token", "")

    def store(request, ws):
        return rt.store(
            ws,
            token(request),
            csrf(request) if request.method in ("POST", "PUT", "PATCH", "DELETE") else None,
        )

    @app.get("/api/workspaces")
    def workspace_list(request: Request):
        return rt.workspace_list(token(request))

    @app.post("/api/workspaces", status_code=201)
    def workspace_create(payload: NameInput, request: Request):
        return rt.create_workspace(token(request), csrf(request), payload.name)

    @app.get("/api/workspaces/{ws}")
    def workspace_detail(ws: str, request: Request):
        return rt.workspace_read(ws, token(request))

    @app.patch("/api/workspaces/{ws}")
    def workspace_rename(ws: str, payload: NameInput, request: Request):
        return rt.rename(ws, token(request), csrf(request), payload.name)

    @app.get("/api/workspaces/{ws}/summary")
    def summary(ws: str, request: Request):
        scoped = store(request, ws)
        return scoped.read(
            lambda db: {
                "generation": scoped.generation(db),
                "documents": db.execute(
                    "SELECT COUNT(*) FROM documents WHERE workspace_id=? AND deleted=0", (ws,)
                ).fetchone()[0],
                "ready_documents": db.execute(
                    "SELECT COUNT(*) FROM documents WHERE workspace_id=? AND deleted=0 AND active_version_id IS NOT NULL",
                    (ws,),
                ).fetchone()[0],
                "dense_ready": rt.models.dense_ready,
                "translation_ready": getattr(rt.models, "translation_ready", False),
                "recovered_running_tasks": rt.worker.recovered,
                "worker_enabled": getattr(getattr(rt, "settings", None), "worker_enabled", False),
                "model_notices": getattr(rt.models, "errors", []),
            }
        )

    @app.get("/api/workspaces/{ws}/members")
    def members(ws: str, request: Request):
        return rt.members(ws, token(request))

    @app.post("/api/workspaces/{ws}/members", status_code=201)
    def member_add(ws: str, payload: MemberInput, request: Request):
        return rt.add_member(ws, token(request), csrf(request), payload.username, payload.role)

    @app.put("/api/workspaces/{ws}/members/{uid}")
    def member_role(ws: str, uid: str, payload: RoleInput, request: Request):
        return rt.change_member(ws, token(request), csrf(request), uid, payload.role)

    @app.delete("/api/workspaces/{ws}/members/{uid}", status_code=204)
    def member_remove(ws: str, uid: str, request: Request):
        rt.change_member(ws, token(request), csrf(request), uid)
        return Response(status_code=204)

    @app.delete("/api/workspaces/{ws}/membership", status_code=204)
    def member_leave(ws: str, request: Request):
        rt.change_member(ws, token(request), csrf(request), None, self_leave=True)
        return Response(status_code=204)

    @app.get("/api/workspaces/{ws}/documents")
    def documents(ws: str, request: Request):
        return store(request, ws).documents()

    @app.post("/api/workspaces/{ws}/documents")
    def import_document(
        ws: str,
        payload: ImportDocument,
        request: Request,
        idempotency_key: str | None = Header(default=None, max_length=160),
    ):
        result = store(request, ws).import_document(payload.model_dump(), idempotency_key)
        rt.notify()
        return JSONResponse(result, status_code=200 if result["duplicate"] else 202)

    @app.get("/api/workspaces/{ws}/documents/{did}/versions")
    def versions(ws: str, did: str, request: Request):
        return store(request, ws).versions(did)

    @app.post("/api/workspaces/{ws}/documents/{did}/active-version")
    def switch(ws: str, did: str, payload: SwitchVersion, request: Request):
        return store(request, ws).switch_version(did, payload.version_id)

    @app.delete("/api/workspaces/{ws}/documents/{did}")
    def delete(ws: str, did: str, request: Request):
        return store(request, ws).delete_document(did)

    @app.get("/api/workspaces/{ws}/tasks")
    def tasks(ws: str, request: Request):
        return store(request, ws).tasks()

    @app.get("/api/workspaces/{ws}/tasks/{tid}")
    def task(ws: str, tid: str, request: Request):
        return store(request, ws).task(tid)

    @app.post("/api/workspaces/{ws}/tasks/{tid}/retry")
    def retry(ws: str, tid: str, request: Request):
        result = store(request, ws).retry(tid)
        rt.notify()
        return result

    @app.post("/api/workspaces/{ws}/search")
    def search(ws: str, payload: SearchRequest, request: Request):
        return rt.search(ws, token(request), csrf(request), payload)

    @app.get("/api/workspaces/{ws}/passages/{cid}")
    def passage(ws: str, cid: str, request: Request):
        return store(request, ws).passage(cid)

    return app
