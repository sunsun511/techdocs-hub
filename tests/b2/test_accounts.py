import b2_support as support
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
import httpx
import pytest
from fastapi.testclient import TestClient
from argon2 import extract_parameters, Type
from app import auth as a
from app import migration as m

RID = os.environ.get("ACCOUNT_RUN_ID", "manual")
RUN = support.ROOT / RID
PRIVATE = support.ROOT / "private_data" / RID
RUN.mkdir(exist_ok=True)
PRIVATE.mkdir(parents=True, exist_ok=True)
BASE = Path(json.loads((support.ROOT / "baseline.json").read_text())["prior_backup_path"])
ORIGIN = "http://127.0.0.1:8378"
SECRETS = []
EVENTS = []


def sensitive(value):
    if value and value not in SECRETS:
        SECRETS.append(value)
        (PRIVATE / "security-secrets.json").write_text(
            json.dumps(SECRETS, ensure_ascii=False), encoding="utf-8"
        )
    return value


def new_password():
    return sensitive(secrets.token_urlsafe(24))


def evidence(name, value):
    (RUN / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def digest_rows(path, tables):
    with closing(m.connect(path, readonly=True)) as db:
        data = {t: db.execute("SELECT * FROM " + t + " ORDER BY 1").fetchall() for t in tables}
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=lambda v: v.hex()).encode()).hexdigest()


def auth_digest(path):
    return digest_rows(path, ["users", "sessions"])


class Clock:
    def __init__(self):
        self.value = time.time()

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def client_for(app, origin=ORIGIN):
    return TestClient(app, base_url=origin, headers={"Origin": origin}, raise_server_exceptions=False)


def login(client, password, username="owner"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    EVENTS.append({"path": "/api/auth/login", "status": r.status_code})
    assert r.status_code == 200, "Real login must succeed"
    body = r.json()
    sensitive(client.cookies.get(a.COOKIE))
    sensitive(body["csrf_token"])
    return body["csrf_token"]


def mutate(client, path, csrf, body=None, headers=None):
    return client.post(
        path, json={} if body is None else body, headers={"X-CSRF-Token": csrf, **(headers or {})}
    )


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "auth.sqlite3"
    m.backup(BASE, path)
    pw = new_password()
    owner = a.create_owner(path, "Owner", "Validation owner", pw)
    clock = Clock()
    app = a.create_app(path, ORIGIN, clock=clock)
    scope = digest_rows(path, ["workspaces", "memberships"])
    with closing(m.connect(BASE, readonly=True)) as db:
        old = m.fingerprint(m.old_values(db))
    yield {
        "path": path,
        "password": pw,
        "owner": owner,
        "clock": clock,
        "app": app,
        "service": app.state.auth,
    }
    with closing(m.connect(path, readonly=True)) as db:
        assert m.fingerprint(m.old_values(db)) == old, (
            "Legacy business IDs/body/vectors/pointers must not change"
        )
        m.integrity(db)
    assert digest_rows(path, ["workspaces", "memberships"]) == scope, (
        "No workspace/role operations this phase"
    )


def test_real_owner_prompt_argon_salt_profile_and_data(tmp_path):
    path = tmp_path / "owner.sqlite3"
    m.backup(BASE, path)
    pw = new_password()
    answers = iter([pw, pw])
    calls = []

    def provider(prompt):
        calls.append(prompt)
        return next(answers)

    result = a.create_owner_from_prompt(path, "  Owner  ", "Validation owner", provider)
    assert len(calls) == 2 and result["test_credential_unusable"] is False
    with closing(m.connect(path, readonly=True)) as db:
        stored = db.execute("SELECT password_hash FROM users").fetchone()[0]
        assert a.check_password(stored, pw)
        assert not a.check_password(stored, new_password())
        p = extract_parameters(stored)
        assert (p.type, p.memory_cost, p.time_cost, p.parallelism, p.salt_len, p.hash_len) == (
            Type.ID,
            19456,
            2,
            1,
            16,
            32,
        )
        sensitive(stored)
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        with closing(m.connect(BASE, readonly=True)) as old:
            assert m.fingerprint(m.old_values(db)) == m.fingerprint(m.old_values(old))
    other = sensitive(a.hash_password(pw))
    assert other != stored
    evidence(
        "real-owner",
        {
            "actual_argon_verified": True,
            "random_salt_distinct": True,
            "memory_KiB": 19456,
            "time_cost": 2,
            "parallelism": 1,
            "getpass_provider_calls": 2,
            "real_getpass_manual_input": False,
            "legacy_bytes_preserved": True,
        },
    )


@pytest.mark.parametrize("kind", ["mismatch", "short", "long", "invalid_username"])
def test_owner_bad_input_no_partial_migration(tmp_path, kind):
    path = tmp_path / "bad-owner.sqlite3"
    m.backup(BASE, path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    if kind == "mismatch":
        values = iter([new_password(), new_password()])
        with pytest.raises(a.AuthError):
            a.create_owner_from_prompt(path, "owner", "Owner", lambda _: next(values))
    else:
        password = "x" * 11 if kind == "short" else "x" * 129 if kind == "long" else new_password()
        with pytest.raises(a.AuthError):
            a.create_owner(path, "invalid user" if kind == "invalid_username" else "owner", "Owner", password)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("stage", ["after_auth", "before_commit"])
def test_real_owner_atomic_fault_rolls_back(tmp_path, stage):
    path = tmp_path / "fault-owner.sqlite3"
    m.backup(BASE, path)
    with closing(m.connect(path, readonly=True)) as db:
        before = m.fingerprint(m.old_values(db))
        schema = m.schema_signature(db)

    def hook(s):
        if s == stage:
            raise RuntimeError("owner-atomic-injected")

    with pytest.raises(RuntimeError, match="owner-atomic-injected"):
        a.create_owner(path, "owner", "Owner", new_password(), hook)
    with closing(m.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert m.fingerprint(m.old_values(db)) == before and m.schema_signature(db) == schema
        m.integrity(db)


def test_unusable_owner_fixture_refused(tmp_path):
    path = tmp_path / "fixture-owner.sqlite3"
    m.backup(BASE, path)
    m.migrate(path, allow_test_seed=True)
    with pytest.raises(a.AuthError, match="Real Argon2"):
        a.AuthService(path)


def test_cli_no_password_arguments_and_non_tty_rejected(tmp_path):
    path = tmp_path / "cli-owner.sqlite3"
    m.backup(BASE, path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "app.cli",
            "create-owner",
            "--database",
            str(path),
            "--username",
            "owner",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1 and "Interactive terminal required" in result.stderr
    helptext = subprocess.run(
        [sys.executable, "-B", "-m", "app.cli", "--help"], capture_output=True, text=True, timeout=10
    )
    assert "--password" not in helptext.stdout
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_login_cookie_db_token_hash_me_no_hash_leak_and_fixation(env):
    c = client_for(env["app"])
    fixation = sensitive(secrets.token_urlsafe(32))
    c.cookies.set(a.COOKIE, fixation, domain="127.0.0.1", path="/")
    csrf = login(c, env["password"], " OWNER ")
    token = sensitive(c.cookies.get(a.COOKIE))
    assert token != fixation
    r = c.get("/api/auth/me")
    assert r.status_code == 200
    assert set(r.json()) == {"user", "csrf_token"}
    assert set(r.json()["user"]) == {"id", "username", "display_name"}
    assert r.json()["user"]["username"] == "owner"
    assert r.headers["cache-control"] == "no-store"
    with closing(m.connect(env["path"], readonly=True)) as db:
        raw = db.execute("SELECT token_hash,csrf_token FROM sessions").fetchone()
        assert raw[0] == hashlib.sha256(token.encode()).hexdigest() and raw[0] != token and raw[1] == csrf
    # Read set-cookie on a separate genuine login, never include its value in evidence.
    r = c.post("/api/auth/login", json={"username": "owner", "password": env["password"]})
    sensitive(r.cookies.get(a.COOKIE))
    sensitive(r.json()["csrf_token"])
    flags = r.headers["set-cookie"].lower()
    assert "httponly" in flags and "samesite=lax" in flags and "path=/" in flags and "secure" not in flags


@pytest.mark.parametrize("kind", ["wrong", "unknown", "disabled", "malformed", "sql_injection"])
def test_invalid_credentials_uniform_and_no_session_created(env, kind):
    if kind == "disabled":
        with closing(m.connect(env["path"])) as db:
            db.execute("UPDATE users SET status='disabled'")
    c = client_for(env["app"])
    before = auth_digest(env["path"])
    name = (
        "missing"
        if kind == "unknown"
        else "bad name"
        if kind == "malformed"
        else "' OR 1=1 --"
        if kind == "sql_injection"
        else "owner"
    )
    pw = new_password() if kind == "wrong" else env["password"]
    r = c.post("/api/auth/login", json={"username": name, "password": pw})
    assert r.status_code == 401 and r.json() == {"detail": "Invalid credentials"}
    assert auth_digest(env["path"]) == before


@pytest.mark.parametrize("kind", ["missing", "forged", "truncated", "duplicate"])
def test_invalid_cookie_401(env, kind):
    c = client_for(env["app"])
    token = secrets.token_urlsafe(32) if kind == "forged" else "short"
    headers = {} if kind == "missing" else {"Cookie": a.COOKIE + "=" + token}
    if kind == "duplicate":
        headers = {"Cookie": a.COOKIE + "=" + token + "; " + a.COOKIE + "=" + token}
    r = c.get("/api/auth/me", headers=headers)
    assert r.status_code == 401
    assert r.headers["cache-control"] == "no-store"


def test_logout_only_current_not_second_or_other_user(env):
    c1 = client_for(env["app"])
    c2 = client_for(env["app"])
    csrf1 = login(c1, env["password"])
    login(c2, env["password"])
    stale = sensitive(c1.cookies.get(a.COOKIE))
    assert mutate(c1, "/api/auth/logout", csrf1).status_code == 204
    assert c1.get("/api/auth/me").status_code == 401
    assert c2.get("/api/auth/me").status_code == 200
    assert c1.get("/api/auth/me", headers={"Cookie": a.COOKIE + "=" + stale}).status_code == 401


def add_second_user(env):
    pw = new_password()
    uid = str(uuid.uuid4())
    stored = sensitive(a.hash_password(pw))
    stamp = a.encoded_time(env["clock"]())
    with closing(m.connect(env["path"])) as db:
        db.execute(
            "INSERT INTO users VALUES(?,?,?,?,?,?,?,?)",
            (uid, "second", "Second validation user", stored, "active", 0, stamp, stamp),
        )
    return uid, pw


def test_logout_all_epoch_and_other_user_unchanged(env):
    _, otherpw = add_second_user(env)
    c1 = client_for(env["app"])
    c2 = client_for(env["app"])
    other = client_for(env["app"])
    csrf1 = login(c1, env["password"])
    login(c2, env["password"])
    login(other, otherpw, "second")
    assert mutate(c1, "/api/auth/logout-all", csrf1).status_code == 204
    assert c1.get("/api/auth/me").status_code == 401 and c2.get("/api/auth/me").status_code == 401
    assert other.get("/api/auth/me").status_code == 200
    with closing(m.connect(env["path"], readonly=True)) as db:
        assert (
            db.execute("SELECT credential_epoch FROM users WHERE username_normalized='owner'").fetchone()[0]
            == 1
        )
        assert (
            db.execute("SELECT credential_epoch FROM users WHERE username_normalized='second'").fetchone()[0]
            == 0
        )


def test_change_password_revokes_all_old_sessions_old_password_fails(env):
    c1 = client_for(env["app"])
    c2 = client_for(env["app"])
    csrf = login(c1, env["password"])
    login(c2, env["password"])
    new = new_password()
    assert (
        mutate(
            c1, "/api/auth/change-password", csrf, {"current_password": env["password"], "new_password": new}
        ).status_code
        == 204
    )
    assert c1.get("/api/auth/me").status_code == 401 and c2.get("/api/auth/me").status_code == 401
    assert (
        c1.post("/api/auth/login", json={"username": "owner", "password": env["password"]}).status_code == 401
    )
    login(c1, new)
    assert c1.get("/api/auth/me").status_code == 200


def test_wrong_current_password_no_credential_or_session_changes(env):
    c = client_for(env["app"])
    csrf = login(c, env["password"])
    before = auth_digest(env["path"])
    r = mutate(
        c,
        "/api/auth/change-password",
        csrf,
        {"current_password": new_password(), "new_password": new_password()},
    )
    assert r.status_code == 403 and auth_digest(env["path"]) == before
    assert c.get("/api/auth/me").status_code == 200


@pytest.mark.parametrize("kind", ["missing", "incorrect", "other_session", "non_ascii"])
@pytest.mark.parametrize("path", ["/api/auth/logout", "/api/auth/logout-all", "/api/auth/change-password"])
def test_csrf_denials_preserve_account_sessions(env, kind, path):
    c = client_for(env["app"])
    csrf = login(c, env["password"])
    if kind == "other_session":
        csrf = login(client_for(env["app"]), env["password"])
    elif kind == "missing":
        csrf = ""
    elif kind == "incorrect":
        csrf = secrets.token_urlsafe(32)
    else:
        csrf = "非ASCII".encode("utf-8")
    before = auth_digest(env["path"])
    body = (
        {"current_password": env["password"], "new_password": new_password()}
        if "change-password" in path
        else {}
    )
    r = mutate(c, path, csrf, body)
    assert r.status_code == 403 and auth_digest(env["path"]) == before


@pytest.mark.parametrize("origin", [None, "http://evil.example", "http://localhost:8378"])
def test_login_origin_denied(env, origin):
    c = client_for(env["app"])
    before = auth_digest(env["path"])
    headers = {} if origin is None else {"Origin": origin}
    c.headers.pop("origin", None)
    r = c.post("/api/auth/login", json={"username": "owner", "password": env["password"]}, headers=headers)
    assert r.status_code == 403 and auth_digest(env["path"]) == before


@pytest.mark.parametrize("path", ["/api/auth/logout", "/api/auth/logout-all", "/api/auth/change-password"])
def test_mutation_wrong_origin_denied(env, path):
    c = client_for(env["app"])
    csrf = login(c, env["password"])
    before = auth_digest(env["path"])
    r = mutate(
        c,
        path,
        csrf,
        {"current_password": env["password"], "new_password": new_password()} if "change" in path else {},
        headers={"Origin": "http://evil.example"},
    )
    assert r.status_code == 403 and auth_digest(env["path"]) == before


def test_validation_body_host_and_no_secret_echo(env):
    c = client_for(env["app"])
    pw = new_password()
    assert (
        c.post(
            "/api/auth/login",
            content="username=owner",
            headers={"content-type": "application/x-www-form-urlencoded"},
        ).status_code
        == 415
    )
    r = c.post("/api/auth/login", json={"username": "owner", "password": pw, "user_id": "other-user"})
    assert r.status_code == 422 and pw not in r.text and r.json() == {"detail": "Invalid request"}
    assert (
        c.post(
            "/api/auth/login", content="x" * 17000, headers={"content-type": "application/json"}
        ).status_code
        == 413
    )
    assert c.get("/api/health", headers={"host": "evil.example"}).status_code == 400
    assert c.post("/api/auth/login", json={"username": "owner", "password": "x" * 129}).status_code == 422


@pytest.mark.parametrize(
    "path",
    ["/", "/docs", "/openapi.json", "/api/documents", "/api/workspaces", "/api/users", "/api/auth/register"],
)
def test_no_ui_workspace_business_or_registration_routes(env, path):
    c = client_for(env["app"])
    login(c, env["password"])
    assert c.get(path).status_code == 404


def test_idle_timeout_me_poll_does_not_extend(env):
    c = client_for(env["app"])
    login(c, env["password"])
    for _ in range(5):
        env["clock"].advance(299)
        assert c.get("/api/auth/me").status_code == 200
    env["clock"].advance(305)
    assert c.get("/api/auth/me").status_code == 401


def test_absolute_timeout_even_with_future_idle_deadline(env):
    c = client_for(env["app"])
    login(c, env["password"])
    with closing(m.connect(env["path"])) as db:
        db.execute(
            "UPDATE sessions SET idle_expires_at=?", (a.encoded_time(env["clock"]() + a.ABSOLUTE + 1000),)
        )
    env["clock"].advance(a.ABSOLUTE)
    assert c.get("/api/auth/me").status_code == 401


def test_epoch_mismatch_and_disabled_account_reject_current_session(env):
    c = client_for(env["app"])
    login(c, env["password"])
    with closing(m.connect(env["path"])) as db:
        db.execute("UPDATE users SET credential_epoch=credential_epoch+1")
    assert c.get("/api/auth/me").status_code == 401
    login(c, env["password"])
    with closing(m.connect(env["path"])) as db:
        db.execute("UPDATE users SET status='disabled'")
    assert c.get("/api/auth/me").status_code == 401


def test_offline_reset_revoke_and_other_user(env):
    _, otherpw = add_second_user(env)
    c = client_for(env["app"])
    other = client_for(env["app"])
    login(c, env["password"])
    login(other, otherpw, "second")
    new = new_password()
    env["service"].offline_revoke(env["owner"]["owner_id"], new)
    assert c.get("/api/auth/me").status_code == 401 and other.get("/api/auth/me").status_code == 200
    login(c, new)
    env["service"].offline_revoke(env["owner"]["owner_id"])
    assert c.get("/api/auth/me").status_code == 401
    before = auth_digest(env["path"])
    with pytest.raises(a.AuthError):
        env["service"].offline_revoke(str(uuid.uuid4()))
    assert auth_digest(env["path"]) == before


@pytest.mark.parametrize(
    "point",
    [
        "login.after_insert",
        "logout.after_revoke",
        "logout_all.after_revoke",
        "password.after_update",
        "password.after_revoke",
        "offline.after_revoke",
    ],
)
def test_account_transaction_faults_rollback(env, point):
    c = client_for(env["app"])
    csrf = login(c, env["password"])
    token = c.cookies.get(a.COOKIE)
    before = auth_digest(env["path"])

    def hook(stage):
        if stage == point:
            raise RuntimeError("account-fault-injected")

    env["service"].hook = hook
    with pytest.raises(RuntimeError, match="account-fault-injected"):
        if point == "login.after_insert":
            env["service"].login("owner", env["password"], "fault-source")
        elif point == "logout.after_revoke":
            env["service"].logout(token, csrf)
        elif point == "logout_all.after_revoke":
            env["service"].logout_all(token, csrf)
        elif point.startswith("password."):
            env["service"].change_password(token, csrf, env["password"], new_password())
        else:
            env["service"].offline_revoke(env["owner"]["owner_id"], new_password())
    assert auth_digest(env["path"]) == before
    assert c.get("/api/auth/me").status_code == 200


@pytest.mark.parametrize("operation", ["login", "password"])
def test_concurrent_revoke_after_verify_blocks_stale_operation(env, operation):
    c = client_for(env["app"])
    csrf = login(c, env["password"])
    reached = threading.Event()
    release = threading.Event()
    point = "login.after_verify" if operation == "login" else "password.after_verify"

    def hook(stage):
        if stage == point:
            reached.set()
            if not release.wait(5):
                raise RuntimeError("barrier timeout")

    env["service"].hook = hook

    def request():
        if operation == "login":
            return c.post("/api/auth/login", json={"username": "owner", "password": env["password"]})
        return mutate(
            c,
            "/api/auth/change-password",
            csrf,
            {"current_password": env["password"], "new_password": new_password()},
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(request)
        try:
            assert reached.wait(5)
            env["service"].offline_revoke(env["owner"]["owner_id"])
            expected = auth_digest(env["path"])
        finally:
            release.set()
        r = future.result(timeout=5)
    assert r.status_code == 401 and auth_digest(env["path"]) == expected


def test_login_rate_account_clear_and_window(env):
    c = client_for(env["app"])
    for _ in range(5):
        assert (
            c.post("/api/auth/login", json={"username": "owner", "password": new_password()}).status_code
            == 401
        )
    r = c.post("/api/auth/login", json={"username": "owner", "password": env["password"]})
    assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= 600
    env["clock"].advance(601)
    login(c, env["password"])
    assert ("account", "owner") not in env["service"].limiter.buckets


def test_source_limit_and_bounded_memory(env):
    c = client_for(env["app"])
    for i in range(30):
        assert (
            c.post(
                "/api/auth/login", json={"username": "missing-" + str(i), "password": new_password()}
            ).status_code
            == 401
        )
    assert (
        c.post("/api/auth/login", json={"username": "owner", "password": env["password"]}).status_code == 429
    )
    limiter = a.LoginLimiter(env["clock"])
    for i in range(2200):
        limiter.reserve("account-" + str(i), "source-" + str(i))
    assert len(limiter.buckets) <= 2048


def test_https_flags_config_and_remote_origin_forbidden(env):
    app = a.create_app(env["path"], "https://localhost:8443", clock=env["clock"])
    c = client_for(app, "https://localhost:8443")
    r = c.post("/api/auth/login", json={"username": "owner", "password": env["password"]})
    assert r.status_code == 200
    sensitive(r.cookies.get(a.COOKIE))
    sensitive(r.json()["csrf_token"])
    assert "secure" in r.headers["set-cookie"].lower()
    with pytest.raises(a.AuthError):
        a.create_app(env["path"], "http://0.0.0.0:8378")


def test_real_loopback_http_restart_sessions_and_revocations(tmp_path):
    path = tmp_path / "real-http.sqlite3"
    m.backup(BASE, path)
    pw = new_password()
    a.create_owner(path, "owner", "Owner", pw)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = "http://127.0.0.1:" + str(port)
    children = []
    observations = []

    def start():
        child = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(support.CODE_ROOT / "tools/serve_accounts.py"),
                "--database",
                str(path),
                "--port",
                str(port),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        children.append(child)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and child.poll() is None:
            try:
                if httpx.get(origin + "/api/health", timeout=0.3, trust_env=False).status_code == 200:
                    return child
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise AssertionError("Loopback server did not become ready")

    def stop(child):
        child.terminate()
        out, err = child.communicate(timeout=5)
        assert not out and not err, "Server must not log credentials or access paths"

    try:
        child = start()
        c1 = httpx.Client(base_url=origin, headers={"Origin": origin}, trust_env=False)
        c2 = httpx.Client(base_url=origin, headers={"Origin": origin}, trust_env=False)
        csrf1 = login(c1, pw)
        login(c2, pw)
        live = sensitive(c2.cookies.get(a.COOKIE))
        dead = sensitive(c1.cookies.get(a.COOKIE))
        assert mutate(c1, "/api/auth/logout", csrf1).status_code == 204
        stop(child)
        child = start()
        assert c2.get("/api/auth/me").status_code == 200
        assert c1.get("/api/auth/me", headers={"Cookie": a.COOKIE + "=" + dead}).status_code == 401
        csrf2 = c2.get("/api/auth/me").json()["csrf_token"]
        assert mutate(c2, "/api/auth/logout-all", csrf2).status_code == 204
        assert c2.get("/api/auth/me", headers={"Cookie": a.COOKIE + "=" + live}).status_code == 401
        with closing(m.connect(path, readonly=True)) as db:
            assert db.execute("SELECT COUNT(*) FROM sessions WHERE revoked_at IS NULL").fetchone()[0] == 0
        with pytest.raises(a.AuthError):
            a.AuthService(path).me(live)
        observations = [{"pid": p.pid, "host": "127.0.0.1", "port": port} for p in children]
        c1.close()
        c2.close()
        stop(child)
    finally:
        for p in children:
            if p.poll() is None:
                p.terminate()
                p.communicate(timeout=5)
    assert len(children) == 2
    evidence(
        "real-http-restart",
        {
            "service_processes": observations,
            "starts": 2,
            "stopped": True,
            "live_session_persisted": True,
            "revoked_session_stayed_revoked": True,
            "all_revoked_in_sqlite": True,
            "no_server_output": True,
        },
    )


def test_final_protection_and_protocol():
    baseline = json.loads((support.ROOT / "baseline.json").read_text())
    assert all(
        hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in baseline["protected_files"].items()
    )
    assert all(
        hashlib.sha256(Path(p).read_bytes()).hexdigest() == h
        for p, h in baseline["prior_migration_files"].items()
    )
    assert (
        hashlib.sha256(Path(baseline["prior_backup_path"]).read_bytes()).hexdigest()
        == baseline["prior_backup_sha256"]
    )
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=support.FORMAL_ROOT) == b""
    evidence(
        "protection",
        {
            "formal_files": 64,
            "unchanged": True,
            "previous_migration_assets_unchanged": True,
            "business_preservation_each_fixture": True,
        },
    )
