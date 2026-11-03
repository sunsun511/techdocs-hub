"""Migration and write-boundary checks against disposable independent copies."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import secrets
import pytest
from app import migration as m, auth as a
from app.db_policy import safe_target, DatabaseBoundaryError
from app.main import create_app
from app.config import Settings
import b2_support as support

BASE = Path(json.loads((support.ROOT / "baseline.json").read_text())["prior_backup_path"])


def copied(tmp_path):
    path = tmp_path / "copy.sqlite3"
    m.backup(BASE, path)
    return path


def legacy(path):
    with closing(m.connect(path, readonly=True)) as db:
        return m.fingerprint(m.old_values(db))


def test_real_migration_preserves_every_old_typed_value_and_blob(tmp_path):
    path = copied(tmp_path)
    before = legacy(path)
    with closing(m.connect(path, readonly=True)) as db:
        vectors = [
            (r[0], hashlib.sha256(r[1]).hexdigest())
            for r in db.execute("SELECT id,vector FROM chunks WHERE vector IS NOT NULL ORDER BY id")
        ]
        source_version = db.execute("PRAGMA user_version").fetchone()[0]
    owner = a.create_owner(path, "owner", "Owner", secrets.token_urlsafe(24))
    assert legacy(path) == before
    with closing(m.connect(path, readonly=True)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert vectors == [
            (r[0], hashlib.sha256(r[1]).hexdigest())
            for r in db.execute("SELECT id,vector FROM chunks WHERE vector IS NOT NULL ORDER BY id")
        ]
        for table in m.BUSINESS:
            assert [r[0] for r in db.execute("SELECT DISTINCT workspace_id FROM " + table)] == [
                owner["workspace_id"]
            ]
        assert (
            db.execute("SELECT COUNT(*) FROM tasks WHERE requested_by_user_id IS NOT NULL").fetchone()[0] == 0
        )
        m.integrity(db)
    assert source_version == 1
    report = {
        "old_columns": before,
        "vector_blobs": vectors,
        "preserved_ids_body_blobs_pointers_tasks": True,
        "default_workspace": owner["workspace_id"],
        "vector_reindexing": False,
    }
    (support.ROOT / "migration-values.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


@pytest.mark.parametrize("point", m.PRECOMMIT_POINTS)
def test_every_migration_fault_restores_v1_and_exact_legacy_values(tmp_path, point):
    path = copied(tmp_path)
    before = legacy(path)

    def fault(stage):
        if stage == point:
            raise RuntimeError("injected migration rollback")

    with pytest.raises(RuntimeError):
        m.migrate(path, allow_test_seed=True, hook=fault)
    assert legacy(path) == before
    with closing(m.connect(path, readonly=True)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert not db.execute("SELECT name FROM sqlite_schema WHERE name='users'").fetchall()
        m.integrity(db)
    assert not Path(str(path) + ".migration-verified.json").exists()


def test_postcommit_failure_quarantines_and_refuses_authentication(tmp_path):
    path = copied(tmp_path)

    def fault(stage):
        if stage == "after_commit":
            raise RuntimeError("injected after commit")

    with pytest.raises(RuntimeError):
        a.create_owner(path, "owner", "Owner", secrets.token_urlsafe(24), hook=fault)
    assert Path(str(path) + ".quarantined.json").exists()
    with pytest.raises(a.AuthError):
        a.AuthService(path)
    restored = tmp_path / "restored-v1.sqlite3"
    m.backup(BASE, restored)
    assert legacy(restored) == legacy(BASE)
    with closing(m.connect(restored, readonly=True)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_live_entry_refuses_v1_without_modifying_it(tmp_path):
    path = copied(tmp_path)
    before = legacy(path)
    with pytest.raises(a.AuthError):
        create_app(Settings(db_path=path, models_enabled=False, worker_enabled=False))
    assert legacy(path) == before


def test_offline_empty_initialization_is_exclusive_and_real_owner_required(tmp_path):
    path = tmp_path / "fresh.sqlite3"
    m.initialize_empty_v1(path)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        m.initialize_empty_v1(path)
    assert path.read_bytes() == before
    m.migrate(path, allow_test_seed=True)
    with pytest.raises(a.AuthError):
        a.AuthService(path)


@pytest.mark.parametrize("kind", ["formal", "checkout", "outside", "protected-backup", "hardlink", "symlink"])
def test_write_boundary_rejects_originals_aliases_and_wrong_roots(tmp_path, kind):
    if kind == "formal":
        target = support.FORMAL_ROOT / "data/techdocs.sqlite3"
    elif kind == "checkout":
        target = support.CODE_ROOT / "data/test.sqlite3"
    elif kind == "outside":
        target = support.ROOT / "outside.sqlite3"
    elif kind == "protected-backup":
        target = BASE
    elif kind == "hardlink":
        target = tmp_path / "hard.sqlite3"
        os.link(BASE, target)
    else:
        target = tmp_path / "linked.sqlite3"
        # Windows permits file symlinks in developer mode; do not skip if unavailable.
        try:
            target.symlink_to(BASE)
        except OSError:
            # Test the equivalent reparse-path rejection with an actual directory junction.
            import subprocess

            target = tmp_path / "junction"
            destination = tmp_path / "empty-link-destination"
            destination.mkdir()
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(target), str(destination)], capture_output=True
            )
            assert result.returncode == 0
            target = target / BASE.name
    with pytest.raises(DatabaseBoundaryError):
        safe_target(target)


def test_unknown_schema_rejected_without_automatic_repair(tmp_path):
    path = copied(tmp_path)
    with closing(m.connect(path)) as db:
        db.execute("CREATE TABLE unexpected(value TEXT)")
    before = path.read_bytes()
    with pytest.raises(m.MigrationError):
        m.migrate(path, allow_test_seed=True)
    assert path.read_bytes() == before


def test_copy_never_overwrites_a_backup(tmp_path):
    path = copied(tmp_path)
    before = path.read_bytes()
    with pytest.raises(m.MigrationError):
        m.backup(BASE, path)
    assert path.read_bytes() == before


def test_integrated_entry_openapi_requires_real_single_session(tmp_path):
    from fastapi.testclient import TestClient
    from tools.api_client import bootstrap_database

    path = tmp_path / "entry.sqlite3"
    owner, pw = bootstrap_database(path)
    origin = "http://127.0.0.1:8377"
    app = create_app(Settings(db_path=path, origin=origin, models_enabled=False, worker_enabled=False))
    with TestClient(app, base_url=origin, headers={"Origin": origin}) as client:
        assert client.get("/openapi.json").status_code == 401
        response = client.post("/api/auth/login", json={"username": "test-owner", "password": pw})
        assert response.status_code == 200
        token = client.cookies.get(a.COOKIE)
        schema = client.get("/openapi.json")
        assert schema.status_code == 200
        assert "/api/workspaces/{ws}/documents" in schema.json()["paths"]
        assert "/api/documents" not in schema.json()["paths"]
        assert (
            client.get(
                "/openapi.json", headers={"Cookie": a.COOKIE + "=" + token + "; " + a.COOKIE + "=" + token}
            ).status_code
            == 401
        )
        assert client.get("/api/health").json() == {"status": "ok"}


def test_startup_refuses_missing_database_without_creating_one(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(a.AuthError):
        create_app(Settings(db_path=path, models_enabled=False, worker_enabled=False))
    assert not path.exists()


def test_missing_write_root_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("TECHDOCS_DATA_ROOT")
    with pytest.raises(DatabaseBoundaryError):
        safe_target(tmp_path / "test.sqlite3")
