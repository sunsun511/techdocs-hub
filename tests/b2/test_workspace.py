import b2_support as support
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import threading
import time
import uuid
import numpy as np
import pytest
from fastapi.testclient import TestClient
from app import auth as a
from app import migration as m
from app.main import create_app as integrated_app
from app.config import Settings
from app.search import LexicalIndex, ranked

RID = os.environ.get("WORKSPACE_RUN_ID", "manual")
RUN = support.ROOT / RID
RUN.mkdir(exist_ok=True)
PRIVATE = support.ROOT / "private_data" / RID
PRIVATE.mkdir(parents=True, exist_ok=True)
BASE = Path(json.loads((support.ROOT / "baseline.json").read_text())["prior_backup_path"])
ORIGIN = "http://127.0.0.1:8379"
SECRET_VALUES = []
RESULTS = []


def secret(value):
    if value and value not in SECRET_VALUES:
        SECRET_VALUES.append(value)
        (PRIVATE / "security-secrets.json").write_text(
            json.dumps(SECRET_VALUES, ensure_ascii=False), encoding="utf-8"
        )
    return value


def password():
    return secret(secrets.token_urlsafe(24))


def evidence(name, value):
    (RUN / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class DeterministicModels:
    """Explicit retrieval-only fixture; authentication and permissions are never mocked."""

    dense_ready = True
    translation_ready = True
    model_tag = "permission-test-fixture"

    def __init__(self):
        self.calls = 0

    def embed(self, texts, prefix):
        self.calls += 1
        return np.array([[1, 0, 0] for _ in texts], dtype=np.float32)

    def translate(self, text):
        self.calls += 1
        return {"text": "sharedterm", "complete": True}


class Actor:
    def __init__(self, app, name, pw, base=ORIGIN, client=None):
        self.name = name
        self.pw = pw
        self.client = client or TestClient(
            app, base_url=base, headers={"Origin": base}, raise_server_exceptions=False
        )
        response = self.client.post("/api/auth/login", json={"username": name, "password": pw})
        assert response.status_code == 200, "Independent real login must succeed"
        self.uid = response.json()["user"]["id"]
        self.csrf = secret(response.json()["csrf_token"])
        self.token = secret(self.client.cookies.get(a.COOKIE))

    def request(self, method, path, body=None, headers=None):
        kwargs = {"headers": {"X-CSRF-Token": self.csrf, **(headers or {})}}
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            kwargs["json"] = {} if body is None else body
        return self.client.request(method, path, **kwargs)


def protected(env):
    with closing(m.connect(env["path"], readonly=True)) as db:
        rows = {
            t: db.execute("SELECT * FROM " + t + " ORDER BY 1").fetchall()
            for t in (
                "documents",
                "versions",
                "tasks",
                "chunks",
                "idempotency",
                "meta",
                "workspaces",
                "memberships",
                "users",
                "sessions",
            )
        }
    return {
        "db": hashlib.sha256(
            json.dumps(rows, sort_keys=True, default=lambda x: x.hex()).encode()
        ).hexdigest(),
        "cache": env["rt"].cache.digest(),
        "notify": env["rt"].notify_calls,
    }


def denied(env, actor, method, path, body, status, headers=None):
    before = protected(env)
    r = actor.request(method, path, body, headers)
    assert r.status_code == status, "Permission status contract violated"
    assert r.headers["cache-control"] == "no-store"
    assert protected(env) == before, (
        "Denied request changed protected rows/vectors/index/pointers/auth or notified worker"
    )
    RESULTS.append(
        {"actor": actor.name, "method": method, "path": path, "status": status, "protected_unchanged": True}
    )
    return r


def payload(ws, key="shared", label="v1"):
    return {
        "key": key,
        "title": "Scoped document",
        "version_label": label,
        "source_url": "https://example.org/doc",
        "format": "markdown",
        "content": f"# First\n\nsharedterm TENANT_{ws} proof {label}.\n\n## Neighbor\n\nsharedterm TENANT_{ws} neighbor.\n\n## Third\n\nsharedterm TENANT_{ws} last.",
    }


def import_ready(env, actor, ws, key="shared", label="v1"):
    r = actor.request(
        "POST",
        f"/api/workspaces/{ws}/documents",
        payload("A" if ws == env["A"] else "B", key, label),
        headers={"Idempotency-Key": f"idem-{key}-{label}"},
    )
    assert r.status_code in (200, 202)
    task = r.json()["task"]
    while env["rt"].worker.process_once():
        pass
    return task


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "workspace.sqlite3"
    m.backup(BASE, path)
    with closing(m.connect(BASE, readonly=True)) as db:
        old = m.old_values(db)
    pw = password()
    owner = a.create_owner(path, "alice", "Alice", pw)
    stamp = a.encoded_time(time.time())
    passwords = {"alice": pw}
    with closing(m.connect(path)) as db:
        for name in ["bob", "carol", "dave", "eve"]:
            passwords[name] = password()
            h = secret(a.hash_password(passwords[name]))
            db.execute(
                "INSERT INTO users VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), name, name.title(), h, "active", 0, stamp, stamp),
            )
    models = DeterministicModels()
    app = integrated_app(
        Settings(db_path=path, origin=ORIGIN, models_enabled=False, worker_enabled=False), models=models
    )
    actors = {name: Actor(app, name, pw) for name, pw in passwords.items()}
    e = {
        "path": path,
        "app": app,
        "rt": app.state.runtime,
        "actors": actors,
        "A": owner["workspace_id"],
        "models": models,
    }
    alice = actors["alice"]
    dave = actors["dave"]
    assert alice.request("PATCH", f"/api/workspaces/{e['A']}", {"name": "A"}).status_code == 200
    r = dave.request("POST", "/api/workspaces", {"name": "B"})
    assert r.status_code == 201
    e["B"] = r.json()["id"]
    for ws, owneractor, roles in [
        (e["A"], alice, {"bob": "editor", "carol": "viewer"}),
        (e["B"], dave, {"bob": "viewer", "carol": "editor"}),
    ]:
        for name, role in roles.items():
            assert (
                owneractor.request(
                    "POST", f"/api/workspaces/{ws}/members", {"username": name, "role": role}
                ).status_code
                == 201
            )
    e["resources"] = {}
    for ws, owneractor in [(e["A"], alice), (e["B"], dave)]:
        one = import_ready(e, owneractor, ws, label="v1")
        two = import_ready(e, owneractor, ws, label="v2")
        other = import_ready(e, owneractor, ws, key="other")
        r = owneractor.request("POST", f"/api/workspaces/{ws}/documents", payload("FAIL", key="failed"))
        assert r.status_code == 202
        failed = r.json()["task"]
        claimed = e["rt"].internal.claim_task()
        assert claimed["id"] == failed["id"]
        e["rt"].internal.fail(claimed, RuntimeError("Synthetic indexing failure"))
        with closing(m.connect(path, readonly=True)) as db:
            chunks = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM chunks WHERE workspace_id=? AND version_id=? ORDER BY ordinal",
                    (ws, two["version_id"]),
                )
            ]
            assert len(chunks) >= 2
        e["resources"][ws] = {
            "doc": two["document_id"],
            "v1": one["version_id"],
            "v2": two["version_id"],
            "task": two["id"],
            "chunk": chunks[0],
            "other_doc": other["document_id"],
            "other_version": other["version_id"],
            "failed": failed["id"],
            "failed_doc": failed["document_id"],
        }
    anonymous = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}, raise_server_exceptions=False)

    class Anonymous:
        name = "anonymous"

        def request(self, method, path, body=None, headers=None):
            return (
                anonymous.request(method, path, headers=headers, json={} if body is None else body)
                if method != "GET"
                else anonymous.get(path, headers=headers)
            )

    e["anonymous"] = Anonymous()
    yield e
    # Only original v1 rows: authorized synthetic fixture writes must not rewrite historic IDs/body/vector bytes.
    with closing(m.connect(path, readonly=True)) as db:
        subset = {}
        for table, cols in m.OLD.items():
            keys = [r[0] for r in old[table]]
            where = '"' + cols[0] + '" IN (' + ",".join("?" for _ in keys) + ")"
            args = keys
            if table == "idempotency":
                where += " AND workspace_id=?"
                args = keys + [e["A"]]
            subset[table] = db.execute(
                "SELECT " + ",".join('"' + c + '"' for c in cols) + " FROM " + table + " WHERE " + where, args
            ).fetchall()
        assert m.fingerprint(subset) == m.fingerprint(old), "Original migrated records changed"
        m.integrity(db)
    for actor in actors.values():
        actor.client.close()
    anonymous.close()
    e["rt"].worker.close()


OPS = [
    "workspace",
    "summary",
    "documents",
    "versions",
    "tasks",
    "task",
    "search",
    "passage",
    "members",
    "rename",
    "add_member",
    "role",
    "remove_member",
    "import",
    "switch",
    "delete",
    "retry",
    "leave",
    "create",
]
READ = {"workspace", "summary", "documents", "versions", "tasks", "task", "search", "passage"}
OWNER = {"members", "rename", "add_member", "role", "remove_member"}


def operation(env, op, ws=None):
    ws = ws or env["A"]
    r = env["resources"][ws]
    prefix = f"/api/workspaces/{ws}"
    mapping = {
        "workspace": ("GET", prefix, None),
        "summary": ("GET", prefix + "/summary", None),
        "documents": ("GET", prefix + "/documents", None),
        "versions": ("GET", prefix + f"/documents/{r['doc']}/versions", None),
        "tasks": ("GET", prefix + "/tasks", None),
        "task": ("GET", prefix + f"/tasks/{r['task']}", None),
        "search": ("POST", prefix + "/search", {"query": "sharedterm", "method": "bm25"}),
        "passage": ("GET", prefix + f"/passages/{r['chunk']}", None),
        "members": ("GET", prefix + "/members", None),
        "rename": ("PATCH", prefix, {"name": "Renamed"}),
        "add_member": ("POST", prefix + "/members", {"username": "eve", "role": "viewer"}),
        "role": ("PUT", prefix + f"/members/{env['actors']['bob'].uid}", {"role": "viewer"}),
        "remove_member": ("DELETE", prefix + f"/members/{env['actors']['carol'].uid}", None),
        "import": ("POST", prefix + "/documents", payload("A", key="matrix-added")),
        "switch": ("POST", prefix + f"/documents/{r['doc']}/active-version", {"version_id": r["v1"]}),
        "delete": ("DELETE", prefix + f"/documents/{r['doc']}", None),
        "retry": ("POST", prefix + f"/tasks/{r['failed']}/retry", None),
        "leave": ("DELETE", prefix + "/membership", None),
        "create": ("POST", "/api/workspaces", {"name": "Created"}),
    }
    return mapping[op]


@pytest.mark.parametrize("role,name", [("owner", "alice"), ("editor", "bob"), ("viewer", "carol")])
@pytest.mark.parametrize("op", OPS)
def test_role_matrix(env, role, name, op):
    actor = env["actors"][name]
    method, path, body = operation(env, op)
    if op in OWNER and role != "owner" or op in {"import", "switch", "delete", "retry"} and role == "viewer":
        denied(env, actor, method, path, body, 403)
        return
    if op == "leave" and role == "owner":
        denied(env, actor, method, path, body, 409)
        return
    expected = (
        201
        if op in {"add_member", "create"}
        else 202
        if op == "import"
        else 204
        if op in {"remove_member", "leave"}
        else 200
    )
    r = actor.request(method, path, body)
    assert r.status_code == expected
    if op == "search":
        assert all(row["document_id"] != env["resources"][env["B"]]["doc"] for row in r.json()["results"])
    if op == "passage":
        assert "TENANT_B" not in r.text and len(r.json()["neighbors"]) >= 2
    RESULTS.append({"actor": name, "operation": op, "role": role, "status": expected, "authorized": True})


@pytest.mark.parametrize("name", ["dave", "eve", "anonymous"])
@pytest.mark.parametrize("op", [op for op in OPS if op != "create"])
def test_nonmember_or_anonymous_denied_all_tenant_routes(env, name, op):
    actor = env["anonymous"] if name == "anonymous" else env["actors"][name]
    method, path, body = operation(env, op)
    denied(env, actor, method, path, body, 401 if name == "anonymous" else 404)


@pytest.mark.parametrize(
    "kind",
    [
        "versions",
        "switch",
        "delete",
        "task",
        "retry",
        "search_doc",
        "search_version",
        "passage",
        "member_role",
        "member_delete",
    ],
)
def test_foreign_real_ids_blocked_before_mutation(env, kind):
    A, B = env["A"], env["B"]
    ra, rb = env["resources"][A], env["resources"][B]
    prefix = f"/api/workspaces/{A}"
    mapping = {
        "versions": ("GET", prefix + f"/documents/{rb['doc']}/versions", None),
        "switch": ("POST", prefix + f"/documents/{ra['doc']}/active-version", {"version_id": rb["v1"]}),
        "delete": ("DELETE", prefix + f"/documents/{rb['doc']}", None),
        "task": ("GET", prefix + f"/tasks/{rb['task']}", None),
        "retry": ("POST", prefix + f"/tasks/{rb['failed']}/retry", None),
        "search_doc": ("POST", prefix + "/search", {"query": "sharedterm", "document_id": rb["doc"]}),
        "search_version": (
            "POST",
            prefix + "/search",
            {"query": "sharedterm", "document_id": ra["doc"], "version_id": rb["v1"]},
        ),
        "passage": ("GET", prefix + f"/passages/{rb['chunk']}", None),
        "member_role": ("PUT", prefix + f"/members/{env['actors']['dave'].uid}", {"role": "viewer"}),
        "member_delete": ("DELETE", prefix + f"/members/{env['actors']['dave'].uid}", None),
    }
    calls = env["models"].calls
    method, path, body = mapping[kind]
    denied(env, env["actors"]["alice"], method, path, body, 404)
    assert env["models"].calls == calls, "Foreign resource must be rejected before inference"


def test_same_workspace_wrong_parent_old_contract(env):
    r = env["resources"][env["A"]]
    p = f"/api/workspaces/{env['A']}"
    denied(
        env,
        env["actors"]["alice"],
        "POST",
        p + f"/documents/{r['doc']}/active-version",
        {"version_id": r["other_version"]},
        409,
    )
    denied(
        env,
        env["actors"]["alice"],
        "POST",
        p + "/search",
        {"query": "sharedterm", "document_id": r["doc"], "version_id": r["other_version"]},
        404,
    )


def test_workspace_list_roles_and_global_owner_not_superuser(env):
    A, B = env["A"], env["B"]
    actors = env["actors"]
    assert {
        x["id"]: x["role"] for x in actors["bob"].request("GET", "/api/workspaces").json()["workspaces"]
    } == {A: "editor", B: "viewer"}
    assert {
        x["id"]: x["role"] for x in actors["carol"].request("GET", "/api/workspaces").json()["workspaces"]
    } == {A: "viewer", B: "editor"}
    assert actors["eve"].request("GET", "/api/workspaces").json() == {"workspaces": []}
    assert {x["id"] for x in actors["alice"].request("GET", "/api/workspaces").json()["workspaces"]} == {A}
    denied(env, actors["alice"], "GET", f"/api/workspaces/{B}/documents", None, 404)
    denied(env, actors["bob"], "POST", f"/api/workspaces/{B}/documents", payload("B", "cross-role"), 403)
    assert (
        actors["carol"]
        .request("POST", f"/api/workspaces/{B}/documents", payload("B", "cross-role"))
        .status_code
        == 202
    )


@pytest.mark.parametrize("method", ["auto", "bm25", "dense", "hybrid"])
def test_cache_same_generation_and_retrieval_isolation(env, method):
    A, B = env["A"], env["B"]
    with closing(m.connect(env["path"])) as db:
        db.execute("UPDATE workspaces SET generation=99 WHERE id IN (?,?)", (A, B))
    for ws, actor, foreign in [
        (A, env["actors"]["alice"], "TENANT_B"),
        (B, env["actors"]["dave"], "TENANT_A"),
        (A, env["actors"]["carol"], "TENANT_B"),
    ]:
        r = actor.request(
            "POST", f"/api/workspaces/{ws}/search", {"query": "sharedterm", "method": method, "top_k": 20}
        )
        assert r.status_code == 200 and r.json()["results"] and foreign not in r.text
    assert {key[0] for key in env["rt"].cache.entries} == {A, B}
    if method == "bm25":
        _, chunks = env["rt"].store(A, None).snapshot()
        index = LexicalIndex(chunks)
        scores = index.score("sharedterm")
        expect = {chunks[i]["id"]: float(scores[i]) for i in ranked(scores, True)}
        r = env["actors"]["alice"].request(
            "POST", f"/api/workspaces/{A}/search", {"query": "sharedterm", "method": "bm25", "top_k": 20}
        )
        assert {x["chunk_id"]: x["score"] for x in r.json()["results"]} == expect


def test_document_keys_idempotency_separate_workspace_and_conflict_no_changes(env):
    A, B = env["A"], env["B"]
    alice, dave = env["actors"]["alice"], env["actors"]["dave"]
    with closing(m.connect(env["path"], readonly=True)) as db:
        assert db.execute("SELECT COUNT(*) FROM documents WHERE key='shared'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM idempotency WHERE key='idem-shared-v1'").fetchone()[0] == 2
    repeated = alice.request(
        "POST", f"/api/workspaces/{A}/documents", payload("A"), headers={"Idempotency-Key": "idem-shared-v1"}
    )
    assert repeated.status_code == 200 and repeated.json()["duplicate"] is True
    denied(
        env,
        alice,
        "POST",
        f"/api/workspaces/{A}/documents",
        payload("changed"),
        409,
        {"Idempotency-Key": "idem-shared-v1"},
    )
    assert dave.request("GET", f"/api/workspaces/{B}/documents").status_code == 200


def test_removed_member_loses_only_one_workspace_and_role_downgrade(env):
    A, B = env["A"], env["B"]
    actors = env["actors"]
    prefix = f"/api/workspaces/{A}"
    assert actors["alice"].request("DELETE", prefix + f"/members/{actors['bob'].uid}").status_code == 204
    denied(env, actors["bob"], "GET", prefix + "/documents", None, 404)
    assert actors["bob"].request("GET", f"/api/workspaces/{B}/documents").status_code == 200
    assert (
        actors["dave"]
        .request("PUT", f"/api/workspaces/{B}/members/{actors['carol'].uid}", {"role": "viewer"})
        .status_code
        == 200
    )
    denied(env, actors["carol"], "POST", f"/api/workspaces/{B}/documents", payload("B", "after-demote"), 403)
    assert actors["carol"].request("GET", f"/api/workspaces/{B}/documents").status_code == 200


@pytest.mark.parametrize("kind", ["remove", "downgrade", "leave", "disable"])
def test_last_enabled_owner_protected_and_exact_rollback(env, kind):
    A = env["A"]
    alice = env["actors"]["alice"]
    prefix = f"/api/workspaces/{A}"
    if kind == "remove":
        denied(env, alice, "DELETE", prefix + f"/members/{alice.uid}", None, 409)
    elif kind == "downgrade":
        denied(env, alice, "PUT", prefix + f"/members/{alice.uid}", {"role": "viewer"}, 409)
    elif kind == "leave":
        denied(env, alice, "DELETE", prefix + "/membership", None, 409)
    else:
        before = protected(env)
        with pytest.raises(a.AuthError, match="enabled owner"):
            env["rt"].set_user_status(alice.uid, "disabled")
        assert protected(env) == before


def test_disabled_coowner_does_not_count_and_reenable_old_cookie_invalid(env):
    A = env["A"]
    alice, bob = env["actors"]["alice"], env["actors"]["bob"]
    p = f"/api/workspaces/{A}"
    assert alice.request("PUT", p + f"/members/{bob.uid}", {"role": "owner"}).status_code == 200
    env["rt"].set_user_status(bob.uid, "disabled")
    denied(env, alice, "PUT", p + f"/members/{alice.uid}", {"role": "viewer"}, 409)
    assert bob.request("GET", p + "/documents").status_code == 401
    env["rt"].set_user_status(bob.uid, "active")
    assert (
        bob.request("GET", p + "/documents", headers={"Cookie": a.COOKIE + "=" + bob.token}).status_code
        == 401
    )
    fresh = Actor(env["app"], "bob", bob.pw)
    assert fresh.request("GET", p + "/documents").status_code == 200
    fresh.client.close()


@pytest.mark.parametrize("kind", ["self_demote", "disable"])
def test_concurrent_two_owners_cannot_orphan_workspace(env, kind):
    A = env["A"]
    alice, bob = env["actors"]["alice"], env["actors"]["bob"]
    p = f"/api/workspaces/{A}"
    assert alice.request("PUT", p + f"/members/{bob.uid}", {"role": "owner"}).status_code == 200
    barrier = threading.Barrier(2)

    def operation(actor):
        barrier.wait(timeout=5)
        if kind == "self_demote":
            return actor.request("PUT", p + f"/members/{actor.uid}", {"role": "editor"}).status_code
        try:
            env["rt"].set_user_status(actor.uid, "disabled")
            return 200
        except a.AuthError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(operation, [alice, bob]))
    assert sorted(results) == [200, 409]
    with closing(m.connect(env["path"], readonly=True)) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=? AND m.role='owner' AND u.status='active'",
                (A,),
            ).fetchone()[0]
            == 1
        )
    evidence("concurrent-owner-" + kind, {"statuses": results, "enabled_owners": 1})


def test_global_disable_checks_all_owned_workspaces(env):
    alice, dave = env["actors"]["alice"], env["actors"]["dave"]
    A, B = env["A"], env["B"]
    assert (
        dave.request(
            "POST", f"/api/workspaces/{B}/members", {"username": "alice", "role": "owner"}
        ).status_code
        == 201
    )
    before = protected(env)
    with pytest.raises(a.AuthError, match="enabled owner"):
        env["rt"].set_user_status(alice.uid, "disabled")
    assert protected(env) == before
    assert alice.request("GET", f"/api/workspaces/{A}/documents").status_code == 200


@pytest.mark.parametrize(
    "op",
    [
        "rename",
        "add_member",
        "role",
        "remove_member",
        "import",
        "switch",
        "delete",
        "retry",
        "leave",
        "create",
        "search",
    ],
)
@pytest.mark.parametrize("bad", ["csrf", "origin"])
def test_new_mutations_csrf_origin_no_side_effects(env, op, bad):
    method, path, body = operation(env, op)
    headers = {"X-CSRF-Token": ""} if bad == "csrf" else {"Origin": "http://evil.example"}
    denied(env, env["actors"]["alice"], method, path, body, 403, headers)


@pytest.mark.parametrize("op", ["add_member", "role", "remove_member"])
def test_forged_owner_header_or_directory_probe_not_privilege(env, op):
    method, path, body = operation(env, op)
    denied(
        env,
        env["actors"]["bob"],
        method,
        path,
        body,
        403,
        {"X-Role": "owner", "X-User-Id": env["actors"]["alice"].uid},
    )
    denied(
        env,
        env["actors"]["bob"],
        "PUT",
        f"/api/workspaces/{env['A']}/members/not-a-real-user",
        {"role": "owner"},
        403,
    )


@pytest.mark.parametrize(
    "branch",
    [
        "search_cold",
        "search_hot",
        "search_empty",
        "passage",
        "list",
        "owner_members",
        "search_error",
        "aba",
        "search_downgrade",
        "session_revoke",
    ],
)
def test_inflight_member_or_role_change_rechecked_before_return(env, branch):
    actors = env["actors"]
    A = env["A"]
    p = f"/api/workspaces/{A}"
    target = actors["bob"]
    point = "search.before_final"
    body = {"query": "sharedterm", "method": "bm25"}
    path = p + "/search"
    method = "POST"
    expected = 404
    if branch == "search_hot":
        assert target.request(method, path, body).status_code == 200
    if branch == "search_empty":
        body["document_id"] = env["resources"][A]["failed_doc"]
    if branch == "passage":
        point = "read.before_final"
        method = "GET"
        path = p + f"/passages/{env['resources'][A]['chunk']}"
        body = None
    if branch == "list":
        point = "read.before_final"
        method = "GET"
        path = p + "/documents"
        body = None
    if branch == "owner_members":
        assert (
            actors["alice"].request("PUT", p + f"/members/{target.uid}", {"role": "owner"}).status_code == 200
        )
        point = "read.before_final"
        method = "GET"
        path = p + "/members"
        body = None
        expected = 403
    if branch == "search_error":
        point = "search.error_before_final"
        body = {"query": "中文", "method": "bm25"}

        def fail_translate(text):
            raise RuntimeError("Chinese translation unavailable")

        env["models"].translate = fail_translate
    if branch == "aba":
        expected = 409
    if branch == "search_downgrade":
        expected = 409
    if branch == "session_revoke":
        expected = 401
    reached = threading.Event()
    release = threading.Event()

    def hook(stage):
        if stage == point:
            reached.set()
            if not release.wait(5):
                raise RuntimeError("Read barrier timeout")

    env["rt"].hook = hook
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(target.request, method, path, body)
        try:
            assert reached.wait(5)
            if branch == "owner_members":
                assert (
                    actors["alice"]
                    .request("PUT", p + f"/members/{target.uid}", {"role": "editor"})
                    .status_code
                    == 200
                )
            elif branch == "search_downgrade":
                assert (
                    actors["alice"]
                    .request("PUT", p + f"/members/{target.uid}", {"role": "viewer"})
                    .status_code
                    == 200
                )
            elif branch == "session_revoke":
                env["rt"].auth.offline_revoke(target.uid)
            else:
                assert actors["alice"].request("DELETE", p + f"/members/{target.uid}").status_code == 204
                if branch == "aba":
                    assert (
                        actors["alice"]
                        .request("POST", p + "/members", {"username": "bob", "role": "editor"})
                        .status_code
                        == 201
                    )
            before = protected(env)
        finally:
            release.set()
        response = future.result(timeout=5)
    assert response.status_code == expected and protected(env) == before
    assert response.json().get("results") is None


def test_read_linearization_after_final_revoke_next_request_denied(env):
    A = env["A"]
    p = f"/api/workspaces/{A}"
    bob, alice = env["actors"]["bob"], env["actors"]["alice"]
    reached = threading.Event()
    release = threading.Event()

    def hook(stage):
        if stage == "search.after_final":
            reached.set()
            assert release.wait(5)

    env["rt"].hook = hook
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(bob.request, "POST", p + "/search", {"query": "sharedterm", "method": "bm25"})
        try:
            assert reached.wait(5)
            assert alice.request("DELETE", p + f"/members/{bob.uid}").status_code == 204
        finally:
            release.set()
        assert future.result(timeout=5).status_code == 200
    env["rt"].hook = lambda _: None
    denied(env, bob, "POST", p + "/search", {"query": "sharedterm", "method": "bm25"}, 404)


@pytest.mark.parametrize("order", ["revoke_first", "demote_first", "write_first"])
def test_write_and_member_revoke_serialized(env, order):
    A = env["A"]
    p = f"/api/workspaces/{A}"
    bob, alice = env["actors"]["bob"], env["actors"]["alice"]
    reached = threading.Event()
    release = threading.Event()
    point = "write.after_authorize" if order == "write_first" else "write.before_tx"

    def hook(stage):
        if stage == point:
            reached.set()
            assert release.wait(5)

    env["rt"].hook = hook
    with ThreadPoolExecutor(max_workers=2) as pool:
        write = pool.submit(bob.request, "POST", p + "/documents", payload("A", "concurrent-write"))
        try:
            assert reached.wait(5)
            if order in ("revoke_first", "demote_first"):
                if order == "revoke_first":
                    assert alice.request("DELETE", p + f"/members/{bob.uid}").status_code == 204
                else:
                    assert (
                        alice.request("PUT", p + f"/members/{bob.uid}", {"role": "viewer"}).status_code == 200
                    )
                before = protected(env)
            else:
                removal = pool.submit(alice.request, "DELETE", p + f"/members/{bob.uid}")
                assert not removal.done()
        finally:
            release.set()
        response = write.result(timeout=5)
        if order in ("revoke_first", "demote_first"):
            assert (
                response.status_code == (404 if order == "revoke_first" else 403) and protected(env) == before
            )
        else:
            assert response.status_code == 202 and removal.result(timeout=5).status_code == 204
    env["rt"].hook = lambda _: None
    denied(
        env,
        bob,
        "POST",
        p + "/documents",
        payload("A", "later-write"),
        403 if order == "demote_first" else 404,
    )


def test_creator_field_does_not_keep_access_after_owner_handoff(env):
    A = env["A"]
    alice, bob = env["actors"]["alice"], env["actors"]["bob"]
    p = f"/api/workspaces/{A}"
    assert alice.request("PUT", p + f"/members/{bob.uid}", {"role": "owner"}).status_code == 200
    assert alice.request("DELETE", p + "/membership").status_code == 204
    denied(env, alice, "GET", p + "/documents", None, 404)
    assert bob.request("GET", p + "/members").status_code == 200
    with closing(m.connect(env["path"], readonly=True)) as db:
        assert (
            db.execute("SELECT created_by_user_id FROM workspaces WHERE id=?", (A,)).fetchone()[0]
            == alice.uid
        )


def test_worker_tenant_fence_creator_removed_and_deleted_late_publish(env):
    A, B = env["A"], env["B"]
    actors = env["actors"]
    rt = env["rt"]
    p = f"/api/workspaces/{A}"
    r = actors["bob"].request("POST", p + "/documents", payload("A", "removed-creator"))
    assert r.status_code == 202
    task = rt.internal.claim_task()
    assert task["workspace_id"] == A
    assert actors["alice"].request("DELETE", p + f"/members/{actors['bob'].uid}").status_code == 204
    assert rt.internal.publish(task, [{"heading": "A", "text": "TENANT_A accepted job"}], None) is True
    assert actors["alice"].request("GET", p + f"/tasks/{task['id']}").json()["state"] == "succeeded"
    r = actors["dave"].request("POST", f"/api/workspaces/{B}/documents", payload("B", "deleted-job"))
    claimed = rt.internal.claim_task()
    assert claimed["workspace_id"] == B
    assert (
        actors["dave"]
        .request("DELETE", f"/api/workspaces/{B}/documents/{claimed['document_id']}")
        .status_code
        == 200
    )
    before = protected(env)
    assert rt.internal.publish(claimed, [{"heading": "B", "text": "TENANT_B late"}], None) is False
    assert protected(env) == before
    forged = dict(task, workspace_id=B)
    with pytest.raises(a.AuthError):
        rt.internal.publish(forged, [], None)


def test_publish_fault_rollback_retry_and_tenant_generation(env):
    A, B = env["A"], env["B"]
    rt = env["rt"]
    actors = env["actors"]
    r = actors["alice"].request("POST", f"/api/workspaces/{A}/documents", payload("A", "fault-job"))
    assert r.status_code == 202
    task = rt.internal.claim_task()
    before = protected(env)

    def hook(stage):
        if stage == "publish.after_chunks":
            raise RuntimeError("publish-fault")

    rt.hook = hook
    with pytest.raises(RuntimeError):
        rt.internal.publish(task, [{"heading": "A", "text": "A"}], None)
    assert protected(env) == before
    rt.hook = lambda _: None
    rt.internal.fail(task, RuntimeError("expected"))
    with closing(m.connect(env["path"], readonly=True)) as db:
        bgen = db.execute("SELECT generation FROM workspaces WHERE id=?", (B,)).fetchone()[0]
    assert actors["alice"].request("POST", f"/api/workspaces/{A}/tasks/{task['id']}/retry").status_code == 200
    assert rt.worker.process_once()
    with closing(m.connect(env["path"], readonly=True)) as db:
        assert db.execute("SELECT generation FROM workspaces WHERE id=?", (B,)).fetchone()[0] == bgen


def test_member_mutation_failure_rolls_back_and_bad_role_422(env):
    A = env["A"]
    alice = env["actors"]["alice"]
    p = f"/api/workspaces/{A}"
    before = protected(env)

    def hook(stage):
        if stage == "member.after_change":
            raise RuntimeError("member-fault")

    env["rt"].hook = hook
    r = alice.request("PUT", p + f"/members/{env['actors']['bob'].uid}", {"role": "viewer"})
    assert r.status_code == 500 and protected(env) == before
    env["rt"].hook = lambda _: None
    denied(env, alice, "PUT", p + f"/members/{env['actors']['bob'].uid}", {"role": "superuser"}, 422)
    denied(env, alice, "POST", p + "/documents", {**payload("A", "spoof"), "workspace_id": env["B"]}, 422)


def test_legacy_global_business_routes_closed_and_health_minimal(env):
    alice = env["actors"]["alice"]
    r = env["resources"][env["A"]]
    routes = [
        ("GET", "/api/documents"),
        ("POST", "/api/documents"),
        ("GET", f"/api/documents/{r['doc']}/versions"),
        ("POST", f"/api/documents/{r['doc']}/active-version"),
        ("DELETE", f"/api/documents/{r['doc']}"),
        ("GET", "/api/tasks"),
        ("GET", f"/api/tasks/{r['task']}"),
        ("POST", f"/api/tasks/{r['task']}/retry"),
        ("POST", "/api/search"),
        ("GET", f"/api/passages/{r['chunk']}"),
    ]
    for method, path in routes:
        denied(env, alice, method, path, None, 404)
    assert alice.request("GET", "/api/health").json() == {"status": "ok"}
    denied(env, alice, "DELETE", f"/api/workspaces/{env['A']}", None, 405)


def test_scope_cache_size_bounded_and_deleted_results_gone(env):
    A = env["A"]
    alice = env["actors"]["alice"]
    r = env["resources"][A]
    p = f"/api/workspaces/{A}"
    assert (
        alice.request(
            "POST", p + "/search", {"query": "sharedterm", "method": "bm25", "document_id": r["doc"]}
        ).status_code
        == 200
    )
    assert alice.request("DELETE", p + f"/documents/{r['doc']}").status_code == 200
    denied(env, alice, "GET", p + f"/passages/{r['chunk']}", None, 404)
    result = alice.request("POST", p + "/search", {"query": "sharedterm", "method": "bm25"})
    assert result.status_code == 200 and all(x["document_id"] != r["doc"] for x in result.json()["results"])
    cache = env["rt"].cache
    for i in range(100):
        cache.publish("synthetic-" + str(i), 1, {("synthetic-" + str(i), 1, None, None): LexicalIndex([])})
    assert len(cache.entries) <= 32


def test_final_protection_and_emit_matrix():
    baseline = json.loads((support.ROOT / "baseline.json").read_text())
    assert all(
        hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in baseline["protected_files"].items()
    )
    assert all(
        hashlib.sha256(Path(p).read_bytes()).hexdigest() == h
        for p, h in baseline["previous_account_files"].items()
    )
    assert all(
        hashlib.sha256(Path(p).read_bytes()).hexdigest() == h
        for p, h in baseline["shared_vendor_files"].items()
    )
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=support.FORMAL_ROOT) == b""
    evidence(
        "matrix-observations",
        {"observations": RESULTS, "authentication_mocked": False, "embedding_fixture": True},
    )
