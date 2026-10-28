import json
import sqlite3
from pathlib import Path

from .models import now


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS articles (
          id TEXT PRIMARY KEY, source_url TEXT, title TEXT, account TEXT, published_at TEXT,
          local_path TEXT, first_discovered_at TEXT NOT NULL, last_checked_at TEXT NOT NULL,
          archived_at TEXT, status TEXT NOT NULL, content_hash TEXT, error TEXT,
          input_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS article_content_hash ON articles(content_hash);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, article_id TEXT, created_at TEXT NOT NULL,
          status TEXT NOT NULL, detail TEXT
        );
        CREATE TABLE IF NOT EXISTS cache (
          key TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candidates (
          id TEXT PRIMARY KEY, first_discovered_at TEXT NOT NULL, payload TEXT NOT NULL
        );
        """)
        self.connection.commit()

    def get(self, key):
        row = self.connection.execute("SELECT * FROM articles WHERE id=?", (key,)).fetchone()
        return dict(row) if row else None

    def find_content(self, content_hash, exclude):
        row = self.connection.execute(
            "SELECT * FROM articles WHERE content_hash=? AND status='success' AND id<>? LIMIT 1",
            (content_hash, exclude),
        ).fetchone()
        return dict(row) if row else None

    def save(self, key: str, values: dict):
        current = self.get(key) or {"first_discovered_at": now()}
        current.update(values, id=key, last_checked_at=now())
        columns = ["id", "source_url", "title", "account", "published_at", "local_path",
                   "first_discovered_at", "last_checked_at", "archived_at", "status", "content_hash",
                   "error", "input_json"]
        with self.connection:
            self.connection.execute(
                f"INSERT OR REPLACE INTO articles ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                [current.get(column) for column in columns],
            )
            self.connection.execute("INSERT INTO events(article_id,created_at,status,detail) VALUES(?,?,?,?)",
                                    (key, now(), current["status"], current.get("error")))

    def rows(self, failed_only=False):
        where = "WHERE status IN ('failed','partial','interrupted','running')" if failed_only else ""
        return [dict(row) for row in self.connection.execute(
            f"SELECT * FROM articles {where} ORDER BY first_discovered_at"
        )]

    def cache_get(self, key):
        row = self.connection.execute("SELECT * FROM cache WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def cache_put(self, key, payload):
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)",
                                    (key, now(), json.dumps(payload, ensure_ascii=False)))

    def save_candidate(self, key, item):
        existing = self.connection.execute("SELECT first_discovered_at FROM candidates WHERE id=?", (key,)).fetchone()
        if existing:
            item["first_discovered_at"] = existing[0]
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO candidates VALUES(?,?,?)",
                                    (key, item["first_discovered_at"], json.dumps(item, ensure_ascii=False)))

    def close(self):
        self.connection.close()

    def recover_running(self):
        with self.connection:
            self.connection.execute("UPDATE articles SET status='interrupted', error=? WHERE status='running'",
                                    ("前次进程异常退出，可使用 retry --failed 恢复",))

