"""Trusted offline account operations; no Runtime, models or worker startup."""

import sqlite3
import uuid
from .auth import AuthError, encoded_time, hash_password, normalize
from .workspace_store import Guard


def create_user(service, username, display_name, password):
    name = normalize(username)
    if not 1 <= len(display_name) <= 128:
        raise AuthError(422, "Invalid display name")
    stored = hash_password(password)
    uid = str(uuid.uuid4())
    stamp = encoded_time(service.clock())
    try:
        with service.transaction(write=True) as db:
            db.execute(
                "INSERT INTO users VALUES(?,?,?,?,?,?,?,?)",
                (uid, name, display_name, stored, "active", 0, stamp, stamp),
            )
    except sqlite3.IntegrityError as exc:
        raise AuthError(409, "Account already exists") from exc
    return {"user_id": uid, "username": name, "status": "active"}


def set_status(service, uid, status, hook=None):
    if status not in ("active", "disabled"):
        raise AuthError(422, "Invalid account state")
    with service.transaction(write=True) as db:
        if not db.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone():
            raise AuthError(404, "Account not found")
        spaces = [
            r[0]
            for r in db.execute(
                "SELECT workspace_id FROM memberships WHERE user_id=? AND role='owner'", (uid,)
            )
        ]
        db.execute("UPDATE users SET status=? WHERE id=?", (status, uid))
        service.revoke_all(db, uid, encoded_time(service.clock()))
        for ws in spaces:
            Guard.owners(db, ws)
        if hook:
            hook("status.after_change")
