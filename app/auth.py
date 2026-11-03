"""Isolated account/session implementation; no workspace authorization or UI."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import getpass
import hashlib
import hmac
import math
import re
import secrets
import sqlite3
import threading
import time
import uuid
from urllib.parse import urlsplit
from collections import OrderedDict
from argon2 import PasswordHasher, Type, extract_parameters
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from . import migration as m

COOKIE = "techdocs_session"
ABSOLUTE = 8 * 3600
IDLE = 30 * 60
PH = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, hash_len=32, salt_len=16, type=Type.ID)
CPU = threading.BoundedSemaphore(4)


class AuthError(Exception):
    def __init__(self, status, detail, retry_after=None):
        self.status, self.detail, self.retry_after = status, detail, retry_after
        super().__init__(detail)


def normalize(username):
    value = username.strip().lower()
    if not re.fullmatch("[a-z0-9._-]{3,64}", value):
        raise AuthError(422, "Invalid account name")
    return value


def password_policy(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise AuthError(422, "Password length must be 12 to 128 characters")
    return password


def hash_password(password):
    password_policy(password)
    with CPU:
        return PH.hash(password)


def check_password(stored, password):
    try:
        with CPU:
            return PH.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


def encoded_time(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def parsed_time(value):
    return datetime.fromisoformat(value).timestamp()


def constant_equal(a, b):
    return hmac.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))


def create_owner(db_path, username, display_name, password, hook=None):
    """Hash before migration lock; create owner/workspace atomically on new v1 copy."""
    path = m.safe_target(db_path)
    username = normalize(username)
    if not 1 <= len(display_name) <= 128:
        raise AuthError(422, "Invalid display name")
    hashed = hash_password(password)
    seed = m.Seed(
        username=username,
        display_name=display_name,
        password_hash=hashed,
        user_id=str(uuid.uuid4()),
        workspace_id=str(uuid.uuid4()),
        grant_id=str(uuid.uuid4()),
    )
    return m.migrate(path, seed, allow_test_seed=False, hook=hook)


def create_owner_from_prompt(db_path, username, display_name, prompt=None):
    prompt = prompt or getpass.getpass
    first = prompt("New owner password: ")
    second = prompt("Confirm password: ")
    if not constant_equal(first, second):
        raise AuthError(422, "Password confirmation mismatch")
    return create_owner(db_path, username, display_name, first)


class LoginLimiter:
    """Bounded local single-process attempt reservations. Restarts reset it."""

    def __init__(self, clock):
        self.clock = clock
        self.lock = threading.Lock()
        self.buckets = OrderedDict()

    def reserve(self, account, source):
        now = self.clock()
        keys = [("account", account), ("source", source)]
        rid = uuid.uuid4().hex
        with self.lock:
            for key in keys:
                self.buckets[key] = [(t, r) for t, r in self.buckets.get(key, []) if t > now - 600]
            for key, limit in zip(keys, (5, 30)):
                bucket = self.buckets[key]
                if len(bucket) >= limit:
                    raise AuthError(
                        429, "Too many login attempts", max(1, math.ceil(bucket[0][0] + 600 - now))
                    )
            for key in keys:
                self.buckets[key].append((now, rid))
                self.buckets.move_to_end(key)
            while len(self.buckets) > 2048:
                self.buckets.popitem(last=False)
        return keys, rid

    def success(self, reservation):
        keys, rid = reservation
        with self.lock:
            self.buckets.pop(keys[0], None)
            self.buckets[keys[1]] = [(t, r) for t, r in self.buckets.get(keys[1], []) if r != rid]


class AuthService:
    def __init__(self, path, clock=time.time, hook=None):
        self.path = m.safe_target(path)
        if not self.path.is_file():
            raise AuthError(409, "Explicit offline initialization required")
        self.clock = clock
        self.hook = hook or (lambda stage: None)
        self.limiter = LoginLimiter(clock)
        # Authenticating an unusable migration fixture is expressly forbidden.
        with closing(m.connect(self.path)) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise AuthError(409, "v2 account database required")
            for (stored,) in db.execute("SELECT password_hash FROM users"):
                try:
                    p = extract_parameters(stored)
                except InvalidHashError:
                    raise AuthError(409, "Real Argon2 credentials required") from None
                if (p.type, p.memory_cost, p.time_cost, p.parallelism, p.salt_len, p.hash_len) != (
                    Type.ID,
                    19456,
                    2,
                    1,
                    16,
                    32,
                ):
                    raise AuthError(409, "Unexpected password hashing profile")
            try:
                m.validate_marker(db, self.path)
            except m.MigrationError as exc:
                raise AuthError(409, "Verified migration binding required") from exc
            db.execute("PRAGMA journal_mode=WAL")
        self.dummy_hash = hash_password(secrets.token_urlsafe(32))

    @contextmanager
    def transaction(self, write=False):
        with closing(m.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def current(self, db, token, csrf=None):
        if not token or not re.fullmatch("[A-Za-z0-9_-]{43}", token):
            raise AuthError(401, "Authentication required")
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = db.execute(
            """SELECT s.*,u.username_normalized,u.display_name,u.status,
              u.credential_epoch AS user_epoch,u.password_hash FROM sessions s
              JOIN users u ON u.id=s.user_id WHERE s.token_hash=?""",
            (digest,),
        ).fetchone()
        # Stored ISO timestamps have microsecond precision. Compare aware UTC
        # datetimes at that same precision, not subtly different float epochs.
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        if (
            not row
            or row["status"] != "active"
            or row["revoked_at"] is not None
            or row["credential_epoch"] != row["user_epoch"]
            or now >= datetime.fromisoformat(row["idle_expires_at"])
            or now >= datetime.fromisoformat(row["absolute_expires_at"])
        ):
            raise AuthError(401, "Authentication required")
        if csrf is not None and not constant_equal(csrf, row["csrf_token"]):
            raise AuthError(403, "Invalid CSRF token")
        return row

    @staticmethod
    def public(row):
        return {
            "id": row["user_id"],
            "username": row["username_normalized"],
            "display_name": row["display_name"],
        }

    def login(self, username, password, source):
        try:
            name = normalize(username)
            valid_name = True
        except AuthError:
            name = "!invalid"
            valid_name = False
        reservation = self.limiter.reserve(name, source)
        with self.transaction() as db:
            user = (
                db.execute("SELECT * FROM users WHERE username_normalized=?", (name,)).fetchone()
                if valid_name
                else None
            )
        stored = user["password_hash"] if user else self.dummy_hash
        valid = check_password(stored, password)
        if not valid or not user or user["status"] != "active":
            raise AuthError(401, "Invalid credentials")
        self.hook("login.after_verify")
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = self.clock()
        stamp = encoded_time(now)
        with self.transaction(write=True) as db:
            fresh = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            if (
                not fresh
                or fresh["status"] != "active"
                or fresh["credential_epoch"] != user["credential_epoch"]
                or fresh["password_hash"] != user["password_hash"]
            ):
                raise AuthError(401, "Invalid credentials")
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                (
                    str(uuid.uuid4()),
                    hashlib.sha256(token.encode()).hexdigest(),
                    user["id"],
                    user["credential_epoch"],
                    csrf,
                    stamp,
                    stamp,
                    encoded_time(now + IDLE),
                    encoded_time(now + ABSOLUTE),
                ),
            )
            self.hook("login.after_insert")
        self.limiter.success(reservation)
        return token, {
            "user": {"id": user["id"], "username": name, "display_name": user["display_name"]},
            "csrf_token": csrf,
        }

    def me(self, token):
        with self.transaction() as db:
            row = self.current(db, token)
            return {"user": self.public(row), "csrf_token": row["csrf_token"]}

    def logout(self, token, csrf):
        with self.transaction(write=True) as db:
            row = self.current(db, token, csrf)
            db.execute("UPDATE sessions SET revoked_at=? WHERE id=?", (encoded_time(self.clock()), row["id"]))
            self.hook("logout.after_revoke")

    @staticmethod
    def revoke_all(db, uid, stamp):
        db.execute(
            "UPDATE users SET credential_epoch=credential_epoch+1,updated_at=? WHERE id=?", (stamp, uid)
        )
        db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (stamp, uid))

    def logout_all(self, token, csrf):
        with self.transaction(write=True) as db:
            row = self.current(db, token, csrf)
            self.revoke_all(db, row["user_id"], encoded_time(self.clock()))
            self.hook("logout_all.after_revoke")

    def change_password(self, token, csrf, current_password, new_password):
        password_policy(new_password)
        with self.transaction() as db:
            initial = self.current(db, token, csrf)
        if not check_password(initial["password_hash"], current_password):
            raise AuthError(403, "Current password verification failed")
        hashed = hash_password(new_password)
        self.hook("password.after_verify")
        with self.transaction(write=True) as db:
            fresh = self.current(db, token, csrf)
            if (
                fresh["password_hash"] != initial["password_hash"]
                or fresh["user_epoch"] != initial["user_epoch"]
            ):
                raise AuthError(401, "Authentication required")
            stamp = encoded_time(self.clock())
            db.execute(
                "UPDATE users SET password_hash=?,updated_at=? WHERE id=?", (hashed, stamp, fresh["user_id"])
            )
            self.hook("password.after_update")
            self.revoke_all(db, fresh["user_id"], stamp)
            self.hook("password.after_revoke")

    def offline_revoke(self, uid, new_password=None):
        # Trusted local operator only. No HTTP endpoint or workspace-role bypass.
        hashed = hash_password(new_password) if new_password is not None else None
        with self.transaction(write=True) as db:
            if not db.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone():
                raise AuthError(404, "Account not found")
            stamp = encoded_time(self.clock())
            if hashed:
                db.execute("UPDATE users SET password_hash=? WHERE id=?", (hashed, uid))
            self.revoke_all(db, uid, stamp)
            self.hook("offline.after_revoke")


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(max_length=64)
    password: SecretStr = Field(max_length=128)


class ChangeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: SecretStr = Field(max_length=128)
    new_password: SecretStr = Field(min_length=12, max_length=128)


def create_app(db_path, origin, *, clock=time.time, hook=None):
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in ("http", "https")
        or parsed.hostname not in ("127.0.0.1", "localhost")
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise AuthError(422, "Explicit loopback origin required")
    secure = parsed.scheme == "https"
    service = AuthService(db_path, clock, hook)
    app = FastAPI(
        title="TechDocs isolated accounts", docs_url=None, redoc_url=None, openapi_url=None, debug=False
    )
    app.state.auth = service

    def clear(response):
        response.delete_cookie(COOKIE, path="/", secure=secure, httponly=True, samesite="lax")
        return response

    @app.exception_handler(AuthError)
    async def auth_error(request, exc):
        response = JSONResponse({"detail": exc.detail}, status_code=exc.status)
        if exc.retry_after:
            response.headers["Retry-After"] = str(exc.retry_after)
        if exc.status == 401:
            clear(response)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": "Invalid request"}, status_code=422)

    @app.middleware("http")
    async def boundary(request, call_next):
        if request.headers.get("host") != parsed.netloc:
            response = JSONResponse({"detail": "Invalid host"}, status_code=400)
        elif request.method in ("POST", "PUT", "PATCH", "DELETE") and request.headers.get("origin") != origin:
            response = JSONResponse({"detail": "Invalid origin"}, status_code=403)
        elif (
            request.method in ("POST", "PUT", "PATCH")
            and request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json"
        ):
            response = JSONResponse({"detail": "JSON required"}, status_code=415)
        else:
            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                limit = (
                    7_010_000
                    if request.method == "POST"
                    and re.fullmatch("/api/workspaces/[^/]+/documents", request.url.path)
                    else 16384
                )
                length = request.headers.get("content-length")
                if length and (not length.isdigit() or int(length) > limit):
                    response = JSONResponse({"detail": "Request too large"}, status_code=413)
                    response.headers["Cache-Control"] = "no-store"
                    return response
                body = bytearray()
                async for part in request.stream():
                    body.extend(part)
                    if len(body) > limit:
                        response = JSONResponse({"detail": "Request too large"}, status_code=413)
                        response.headers["Cache-Control"] = "no-store"
                        return response
                request._body = bytes(body)
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    def token(request):
        # Do not silently choose one of conflicting duplicate session cookies.
        if len(re.findall(r"(?:^|;)\s*" + COOKIE + r"=", request.headers.get("cookie", ""))) > 1:
            raise AuthError(401, "Authentication required")
        return request.cookies.get(COOKIE)

    def csrf(request):
        return request.headers.get("x-csrf-token", "")

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/auth/login")
    def login(payload: LoginInput, request: Request):
        raw, body = service.login(payload.username, payload.password.get_secret_value(), request.client.host)
        response = JSONResponse(body)
        response.set_cookie(
            COOKIE, raw, max_age=ABSOLUTE, path="/", secure=secure, httponly=True, samesite="lax"
        )
        return response

    @app.get("/api/auth/me")
    def me(request: Request):
        return service.me(token(request))

    @app.post("/api/auth/logout")
    def logout(request: Request):
        service.logout(token(request), csrf(request))
        return clear(Response(status_code=204))

    @app.post("/api/auth/logout-all")
    def logout_all(request: Request):
        service.logout_all(token(request), csrf(request))
        return clear(Response(status_code=204))

    @app.post("/api/auth/change-password")
    def change_password(payload: ChangeInput, request: Request):
        service.change_password(
            token(request),
            csrf(request),
            payload.current_password.get_secret_value(),
            payload.new_password.get_secret_value(),
        )
        return clear(Response(status_code=204))

    return app
