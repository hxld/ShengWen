from __future__ import annotations
import json, os, sqlite3, time, uuid
from pathlib import Path
from contextlib import contextmanager


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs(task_id TEXT PRIMARY KEY,stage TEXT,payload TEXT,cancelled INTEGER DEFAULT 0,updated REAL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,task_id TEXT,stage TEXT,message TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,task_id TEXT,kind TEXT,content TEXT,meta TEXT,created REAL);
            CREATE INDEX IF NOT EXISTS versions_task ON versions(task_id,kind,created);
            CREATE TABLE IF NOT EXISTS metadata(task_id TEXT PRIMARY KEY,data TEXT);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS segments(task_id TEXT PRIMARY KEY,data TEXT);
            CREATE TABLE IF NOT EXISTS artifacts(path TEXT PRIMARY KEY,task_id TEXT,kind TEXT,created REAL);
            """)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def setting(self, key, default=None):
        with self.connect() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_setting(self, key, value):
        with self.connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO settings VALUES (?,?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def record(self, task_id, stage, payload):
        if not task_id:
            return
        clean = {
            k: v
            for k, v in payload.items()
            if not any(
                s in k.lower() for s in ("cookie", "sessdata", "api_key", "token")
            )
        }
        clean.setdefault("template", self.setting("template", "course"))
        with self.connect() as c:
            c.execute(
                "INSERT INTO jobs VALUES (?,?,?,0,?) ON CONFLICT(task_id) DO UPDATE SET stage=excluded.stage,payload=excluded.payload,updated=excluded.updated",
                (task_id, stage, json.dumps(clean, ensure_ascii=False), time.time()),
            )
            c.execute(
                "INSERT INTO events(task_id,stage,message,created) VALUES (?,?,?,?)",
                (task_id, stage, "已进入队列", time.time()),
            )
        for k, v in clean.items():
            if (
                k.endswith(("_file", "_path"))
                and isinstance(v, str)
                and Path(v).is_file()
            ):
                self.artifact(task_id, v, k)

    def job(self, task_id):
        with self.connect() as c:
            r = c.execute("SELECT * FROM jobs WHERE task_id=?", (task_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        return d

    def cancel(self, task_id, value=True):
        with self.connect() as c:
            c.execute(
                "UPDATE jobs SET cancelled=?,updated=? WHERE task_id=?",
                (int(value), time.time(), task_id),
            )

    def meta(self, task_id, patch=None):
        with self.connect() as c:
            r = c.execute(
                "SELECT data FROM metadata WHERE task_id=?", (task_id,)
            ).fetchone()
            data = json.loads(r[0]) if r else {}
            if patch is not None:
                data.update(patch)
                c.execute(
                    "INSERT OR REPLACE INTO metadata VALUES (?,?)",
                    (task_id, json.dumps(data, ensure_ascii=False)),
                )
        return data

    def version(self, task_id, kind, content, meta=None):
        if not content:
            return
        with self.connect() as c:
            last = c.execute(
                "SELECT content FROM versions WHERE task_id=? AND kind=? ORDER BY created DESC LIMIT 1",
                (task_id, kind),
            ).fetchone()
            if last and last[0] == content:
                return
            c.execute(
                "INSERT INTO versions VALUES (?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex,
                    task_id,
                    kind,
                    content,
                    json.dumps(meta or {}, ensure_ascii=False),
                    time.time(),
                ),
            )

    def versions(self, task_id):
        with self.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT id,kind,content,meta,created FROM versions WHERE task_id=? ORDER BY created DESC LIMIT 30",
                    (task_id,),
                )
            ]

    def segments(self, task_id, data=None):
        with self.connect() as c:
            if data is not None:
                c.execute(
                    "INSERT OR REPLACE INTO segments VALUES (?,?)",
                    (task_id, json.dumps(data, ensure_ascii=False)),
                )
            r = c.execute(
                "SELECT data FROM segments WHERE task_id=?", (task_id,)
            ).fetchone()
        return json.loads(r[0]) if r else None

    def artifact(self, task_id, path, kind):
        with self.connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?)",
                (str(Path(path).resolve()), task_id, kind, time.time()),
            )

    def delete_task(self, task_id):
        with self.connect() as c:
            for table in ("jobs", "events", "versions", "metadata", "segments"):
                c.execute(f"DELETE FROM {table} WHERE task_id=?", (task_id,))

    def artifact_owner(self, path):
        with self.connect() as c:
            r = c.execute(
                "SELECT task_id FROM artifacts WHERE path=?",
                (str(Path(path).resolve()),),
            ).fetchone()
        return r[0] if r else None


_store = None


def get_store():
    global _store
    if _store is None:
        from ..utils.project_root import get_project_root

        _store = Store(
            Path(os.getenv("SHENGWEN_DATA_DIR", str(get_project_root() / "data")))
            / "workbench.sqlite"
        )
    return _store
