import sqlite3

import pytest

from wechat_archiver.database import MIGRATIONS, Database
from wechat_archiver.models import ArchiveError

LEGACY_SCHEMA = MIGRATIONS[0]


def version(path):
    connection = sqlite3.connect(path)
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


def test_new_database_is_at_latest_version(tmp_path):
    Database(tmp_path / "a.db").close()
    assert version(tmp_path / "a.db") == len(MIGRATIONS)


def test_legacy_database_upgrades_and_keeps_data(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(LEGACY_SCHEMA)
    connection.execute("INSERT INTO articles(id, first_discovered_at, last_checked_at, status, input_json) "
                       "VALUES ('k', 't', 't', 'success', '{}')")
    connection.commit()
    connection.close()
    assert version(path) == 0
    database = Database(path)
    try:
        assert database.get("k")["status"] == "success"
        indexes = {row[0] for row in database.connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "article_status" in indexes
    finally:
        database.close()
    assert version(path) == len(MIGRATIONS)


def test_reopening_does_not_rerun_migrations(tmp_path):
    Database(tmp_path / "a.db").close()
    database = Database(tmp_path / "a.db")
    database.close()
    assert version(tmp_path / "a.db") == len(MIGRATIONS)


def test_newer_database_is_rejected(tmp_path):
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    connection.close()
    with pytest.raises(ArchiveError, match="升级"):
        Database(path)


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    import wechat_archiver.database as module
    monkeypatch.setattr(module, "MIGRATIONS", [*MIGRATIONS, "CREATE TABLE extra(x); SELECT * FROM missing_table;"])
    path = tmp_path / "a.db"
    with pytest.raises(sqlite3.Error):
        Database(path)
    assert version(path) == len(MIGRATIONS)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='extra'").fetchone() is None
    finally:
        connection.close()
