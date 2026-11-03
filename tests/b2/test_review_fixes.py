"""Acceptance gaps: real CLI credentials, marker binding, and actual connectors."""

from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import uuid
import pytest
from fastapi.testclient import TestClient
from app import auth as a, migration as m
from app.config import Settings
from app.main import create_app
from app.db_policy import DatabaseBoundaryError
from tools.api_client import bootstrap_database
from tools.cutover_guard import snapshot, decision


def sensitive(tmp, value):
    registry = tmp / "security-secrets-review.json"
    values = json.loads(registry.read_text()) if registry.exists() else []
    values.append(value)
    registry.write_text(json.dumps(values), encoding="utf-8")
    return value


@pytest.fixture
def instance(tmp_path):
    path = tmp_path / "app.sqlite3"
    owner, pw = bootstrap_database(path)
    sensitive(tmp_path, pw)
    return {"path": path, "owner": owner, "password": pw, "tmp": tmp_path}


def cli(instance, action, *args, password=None, confirm=None):
    # Exercise the real argument parser/CLI in a separate process. Only the
    # trusted operator's getpass provider is automated; crypto/auth are real.
    code = """import json,sys
from unittest.mock import patch
from app.cli import main
value=json.loads(sys.stdin.read())
answers=iter([value.get('password'),value.get('confirm',value.get('password'))])
with patch('app.cli.interactive_terminal',return_value=True),patch('getpass.getpass',side_effect=lambda prompt:next(answers)):
    sys.exit(main())
"""
    if password:
        sensitive(instance["tmp"], password)
    if confirm:
        sensitive(instance["tmp"], confirm)
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-B", "-c", code, action, "--database", str(instance["path"]), *args],
        input=json.dumps({"password": password, **({"confirm": confirm} if confirm else {})}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )


def client(instance):
    origin = "http://127.0.0.1:8377"
    return TestClient(
        create_app(
            Settings(db_path=instance["path"], origin=origin, models_enabled=False, worker_enabled=False)
        ),
        base_url=origin,
        headers={"Origin": origin},
    )


def login(c, instance, name="test-owner", password=None):
    r = c.post("/api/auth/login", json={"username": name, "password": password or instance["password"]})
    assert r.status_code == 200
    sensitive(instance["tmp"], c.cookies.get(a.COOKIE))
    sensitive(instance["tmp"], r.json()["csrf_token"])
    return r.json()["csrf_token"]


def rows(path):
    with closing(m.connect(path, readonly=True)) as db:
        return m.fingerprint(
            {
                t: db.execute("SELECT * FROM " + t).fetchall()
                for t in (*m.OLD, "users", "sessions", "workspaces", "memberships")
            }
        )


def test_cli_creates_second_user_login_membership_and_status_revokes_sessions(instance):
    pw = sensitive(instance["tmp"], secrets.token_urlsafe(24))
    r = cli(instance, "create-user", "--username", "Second.User", "--display-name", "Second", password=pw)
    assert r.returncode == 0, r.stderr
    uid = json.loads(r.stdout)["user_id"]
    with closing(m.connect(instance["path"], readonly=True)) as db:
        stored = db.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()[0]
        sensitive(instance["tmp"], stored)
        assert a.check_password(stored, pw)
    with client(instance) as owner, client(instance) as user:
        csrf = login(owner, instance)
        login(user, instance, "second.user", pw)
        ws = instance["owner"]["workspace_id"]
        prefix = "/api/workspaces/" + ws
        assert user.get(prefix + "/documents").status_code == 404
        assert (
            owner.post(
                prefix + "/members",
                json={"username": "second.user", "role": "viewer"},
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 201
        )
        assert user.get(prefix + "/documents").status_code == 200
        assert user.get(prefix + "/members").status_code == 403
        assert cli(instance, "disable-user", "--user-id", uid).returncode == 0
        assert user.get(prefix + "/documents").status_code == 401
        assert (
            user.post("/api/auth/login", json={"username": "second.user", "password": pw}).status_code == 401
        )
        assert cli(instance, "enable-user", "--user-id", uid).returncode == 0
        assert user.get(prefix + "/documents").status_code == 401
        login(user, instance, "second.user", pw)
        assert user.get(prefix + "/documents").status_code == 200


def test_cli_duplicate_and_last_owner_denials_preserve_all_records(instance):
    before = rows(instance["path"])
    duplicate = cli(instance, "create-user", "--username", "TEST-OWNER", password=secrets.token_urlsafe(24))
    assert duplicate.returncode == 1 and "already exists" in duplicate.stderr
    assert rows(instance["path"]) == before
    disabled = cli(instance, "disable-user", "--user-id", instance["owner"]["owner_id"])
    assert disabled.returncode == 1 and "enabled owner" in disabled.stderr
    assert rows(instance["path"]) == before


def test_cli_does_not_recover_queued_tasks(instance):
    with client(instance) as c:
        csrf = login(c, instance)
        r = c.post(
            "/api/workspaces/" + instance["owner"]["workspace_id"] + "/documents",
            json={"key": "queued", "title": "Queued", "version_label": "v1", "content": "Queued evidence"},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 202
        with closing(m.connect(instance["path"], readonly=True)) as db:
            before = m.fingerprint(m.old_values(db))
        assert (
            cli(
                instance, "create-user", "--username", "second", password=secrets.token_urlsafe(24)
            ).returncode
            == 0
        )
        with closing(m.connect(instance["path"], readonly=True)) as db:
            assert m.fingerprint(m.old_values(db)) == before


@pytest.mark.parametrize("kind", ["short", "long", "mismatch", "invalid-name"])
def test_cli_invalid_new_user_preserves_records(instance, kind):
    before = rows(instance["path"])
    pw = secrets.token_urlsafe(24)
    if kind == "short":
        pw = secrets.token_urlsafe(4)
    if kind == "long":
        pw = "x" * 129
    result = cli(
        instance,
        "create-user",
        "--username",
        "bad name" if kind == "invalid-name" else "new-user",
        password=pw,
        confirm=secrets.token_urlsafe(24) if kind == "mismatch" else pw,
    )
    assert result.returncode == 1
    assert rows(instance["path"]) == before


def test_new_user_actual_noninteractive_cli_refuses_secret_input(instance):
    before = rows(instance["path"])
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "app.cli",
            "create-user",
            "--database",
            str(instance["path"]),
            "--username",
            "new-user",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1 and "Interactive terminal required" in result.stderr
    assert rows(instance["path"]) == before


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "truncated",
        "owner",
        "workspace",
        "source",
        "schema",
        "state",
        "test-seed",
        "missing-field",
        "wrong-type",
        "quarantine",
    ],
)
def test_actual_startup_rejects_missing_or_misbound_marker(instance, kind):
    marker = Path(str(instance["path"]) + ".migration-verified.json")
    original = marker.read_text()
    value = json.loads(original)
    before = rows(instance["path"])
    if kind == "missing":
        marker.rename(marker.with_suffix(".saved"))
    elif kind == "truncated":
        marker.write_text("{")
    elif kind == "quarantine":
        Path(str(instance["path"]) + ".quarantined.json").write_text("{}")
    else:
        field = {
            "owner": "owner_id",
            "workspace": "workspace_id",
            "source": "source_logical_fingerprint",
            "schema": "schema_sha256",
            "state": "state",
            "test-seed": "test_credential_unusable",
            "missing-field": "owner_id",
            "wrong-type": "owner_id",
        }[kind]
        if kind == "missing-field":
            value.pop(field)
        else:
            value[field] = (
                True
                if kind == "test-seed"
                else 9
                if kind == "wrong-type"
                else "bad-state"
                if kind == "state"
                else "f" * 64
                if kind in ("source", "schema")
                else str(uuid.uuid4())
            )
        marker.write_text(json.dumps(value))
    with pytest.raises(a.AuthError):
        a.AuthService(instance["path"])
    assert rows(instance["path"]) == before


def test_same_schema_other_database_marker_is_rejected(instance):
    other = instance["tmp"] / "other.sqlite3"
    bootstrap_database(other)
    first = Path(str(instance["path"]) + ".migration-verified.json")
    second = Path(str(other) + ".migration-verified.json")
    assert json.loads(first.read_text())["schema_sha256"] == json.loads(second.read_text())["schema_sha256"]
    first.write_text(second.read_text())
    with pytest.raises(a.AuthError):
        a.AuthService(instance["path"])


def test_normal_restart_accepts_live_changes_and_initial_owner_demotion(instance):
    pw = secrets.token_urlsafe(24)
    r = cli(instance, "create-user", "--username", "co-owner", password=pw)
    uid = json.loads(r.stdout)["user_id"]
    with client(instance) as c:
        csrf = login(c, instance)
        ws = instance["owner"]["workspace_id"]
        prefix = "/api/workspaces/" + ws
        headers = {"X-CSRF-Token": csrf}
        assert (
            c.post(
                prefix + "/members", json={"username": "co-owner", "role": "owner"}, headers=headers
            ).status_code
            == 201
        )
        assert (
            c.put(
                prefix + "/members/" + instance["owner"]["owner_id"], json={"role": "viewer"}, headers=headers
            ).status_code
            == 200
        )
    service = a.AuthService(instance["path"])
    token, _ = service.login("co-owner", pw, "local")
    sensitive(instance["tmp"], token)
    assert service.me(token)["user"]["id"] == uid


@pytest.mark.parametrize("entry", ["connect", "backup", "initialize", "cli-copy"])
def test_actual_write_entry_rejects_junction_before_opening_file(instance, entry):
    destination = instance["tmp"] / "destination"
    destination.mkdir()
    link = instance["tmp"] / "link"
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(destination)], capture_output=True
        )
        assert result.returncode == 0
    else:
        link.symlink_to(destination, target_is_directory=True)
    target = link / "forbidden.sqlite3"
    if entry == "cli-copy":
        r = cli({**instance, "path": target}, "copy-v1", "--source", str(instance["path"]))
        assert r.returncode == 1
    else:
        with pytest.raises(DatabaseBoundaryError):
            if entry == "connect":
                m.connect(target)
            elif entry == "backup":
                m.backup(instance["path"], target)
            else:
                m.initialize_empty_v1(target)
    assert not (destination / "forbidden.sqlite3").exists()


def test_default_pytest_collects_without_machine_specific_configuration():
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("TECHDOCS_") and k not in ("ACCOUNT_RUN_ID", "WORKSPACE_RUN_ID")
    }
    result = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "--collect-only", "-vv", "-p", "no:cacheprovider"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_real_loopback_roles_revocations_owner_handoff_persist_across_restart" in result.stdout
    assert "test_pre_b1_ranking_scores_warnings_and_errors_are_identical" in result.stdout


def test_container_disposable_initializer_uses_real_owner_and_refuses_overwrite(instance):
    path = instance["tmp"] / "container.sqlite3"
    pw = sensitive(instance["tmp"], secrets.token_urlsafe(24))
    command = [sys.executable, "-B", "-m", "tools.container_fixture", "--database", str(path), "--disposable"]
    result = subprocess.run(
        command, input=json.dumps({"password": pw}), capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0
    service = a.AuthService(path)
    token, _ = service.login("test-owner", pw, "local")
    sensitive(instance["tmp"], token)
    assert service.me(token)["user"]["username"] == "test-owner"
    before = rows(path)
    result = subprocess.run(
        command, input=json.dumps({"password": pw}), capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 1 and rows(path) == before


def test_rollback_guard_ignores_sessions_but_stops_after_business_or_account_changes(instance):
    before = snapshot(instance["path"])
    with client(instance) as c:
        login(c, instance)
    assert decision(before, snapshot(instance["path"]))["allow_pre_write_restore"]
    assert (
        cli(instance, "create-user", "--username", "new-user", password=secrets.token_urlsafe(24)).returncode
        == 0
    )
    result = decision(before, snapshot(instance["path"]))
    assert result["decision"] == "STOP_REQUIRES_OPERATOR_DECISION"
    assert result["automatic_restore_performed"] is False
