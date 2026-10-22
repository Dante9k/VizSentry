from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Small indexed queries; records are never materialized as one task-sized list."""
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  status TEXT NOT NULL, source TEXT NOT NULL, criteria TEXT NOT NULL DEFAULT '',
                  total INTEGER NOT NULL DEFAULT 0, imported INTEGER NOT NULL DEFAULT 0,
                  import_complete INTEGER NOT NULL DEFAULT 0, error TEXT, demo INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS rows (
                  job_id TEXT NOT NULL, record_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending', source TEXT NOT NULL,
                  result TEXT NOT NULL DEFAULT '{}', image_verdict TEXT NOT NULL DEFAULT '待确认',
                  error TEXT, PRIMARY KEY (job_id,record_id), UNIQUE(job_id,ordinal),
                  FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                CREATE INDEX IF NOT EXISTS row_status ON rows(job_id,status,ordinal);
                CREATE INDEX IF NOT EXISTS row_verdict ON rows(job_id,image_verdict,ordinal);
                CREATE TABLE IF NOT EXISTS artifacts (
                  job_id TEXT NOT NULL, id TEXT NOT NULL, path TEXT NOT NULL,
                  kind TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY(job_id,id)
                );
            """)

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def recover(self):
        with self.connect() as db:
            # Cancellation can be followed by shutdown while work is in flight,
            # or between updating the job and cancelling its pending rows.
            db.execute("UPDATE rows SET status='cancelled' WHERE status IN ('processing','pending') AND job_id IN (SELECT id FROM jobs WHERE status='cancelled')")
            db.execute("UPDATE rows SET status='pending' WHERE status='processing'")
            db.execute("UPDATE jobs SET status='paused',error='上次运行中断，点击继续从已保存记录恢复' WHERE status IN ('running','queued') AND import_complete=1")
            db.execute("UPDATE jobs SET status='import_failed',error='导入被中断，记录清单尚未完整；请重新创建任务' WHERE status IN ('importing','queued') AND import_complete=0")

    def create_job(self, source: dict, criteria: str = '', demo: bool = False) -> dict:
        identity = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO jobs(id,created_at,updated_at,status,source,criteria,demo) VALUES(?,?,?,?,?,?,?)",
                       (identity, now(), now(), 'queued', json.dumps(source, ensure_ascii=False), criteria, int(demo)))
        return self.job(identity)

    def job(self, identity: str, *, counts: bool = True) -> dict:
        with self.connect() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
            if job is None:
                raise KeyError(identity)
            status_counts = dict(db.execute("SELECT status,COUNT(*) FROM rows WHERE job_id=? GROUP BY status", (identity,)).fetchall()) if counts else {}
        data = dict(job)
        data['source'] = json.loads(data['source'])
        data['completed'] = status_counts.get('done', 0) + status_counts.get('error', 0)
        data['failed'] = status_counts.get('error', 0)
        data['processing'] = status_counts.get('processing', 0)
        data['pending'] = status_counts.get('pending', 0)
        data['cancelled'] = status_counts.get('cancelled', 0)
        data['import_complete'] = bool(data['import_complete'])
        data['demo'] = bool(data['demo'])
        return data

    def list_jobs(self, limit: int = 50) -> list[dict]:
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))]
        return [self.job(identity) for identity in ids]

    def update_job(self, identity: str, **changes):
        allowed = {'status', 'total', 'imported', 'import_complete', 'error'}
        changes = {k: v for k, v in changes.items() if k in allowed}
        changes['updated_at'] = now()
        with self.connect() as db:
            db.execute('UPDATE jobs SET ' + ','.join(f'{k}=?' for k in changes) + ' WHERE id=?', (*changes.values(), identity))

    def status(self, identity: str) -> str:
        with self.connect() as db:
            result = db.execute('SELECT status FROM jobs WHERE id=?', (identity,)).fetchone()
        if not result:
            raise KeyError(identity)
        return result[0]

    def add_records(self, identity: str, records: list[dict], offset: int):
        with self.connect() as db:
            db.executemany('INSERT INTO rows(job_id,record_id,ordinal,source) VALUES(?,?,?,?)',
                [(identity, str(r['record_id']), offset + i + 1, json.dumps(r, ensure_ascii=False)) for i, r in enumerate(records)])
            db.execute('UPDATE jobs SET imported=imported+?,updated_at=? WHERE id=?', (len(records), now(), identity))

    def claim(self, identity: str) -> dict | None:
        with self.connect() as db:
            if db.execute('SELECT status FROM jobs WHERE id=?', (identity,)).fetchone()[0] != 'running':
                return None
            row = db.execute("SELECT * FROM rows WHERE job_id=? AND status='pending' ORDER BY ordinal LIMIT 1", (identity,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE rows SET status='processing' WHERE job_id=? AND record_id=?", (identity, row['record_id']))
            result = dict(row)
            result['source'] = json.loads(result['source'])
            return result

    def finish_row(self, identity: str, record_id: str, result: dict, error: str | None = None):
        with self.connect() as db:
            db.execute('UPDATE rows SET status=?,result=?,image_verdict=?,error=? WHERE job_id=? AND record_id=?',
                ('error' if error else 'done', json.dumps(result, ensure_ascii=False), result.get('image_verdict', '待确认'), error, identity, record_id))

    def retry_rows(self, identity: str):
        with self.connect() as db:
            db.execute("UPDATE rows SET status='pending',error=NULL WHERE job_id=? AND status IN ('error','cancelled')", (identity,))
            db.execute("UPDATE jobs SET status='running',error=NULL WHERE id=?", (identity,))

    def cancel_pending(self, identity: str):
        with self.connect() as db:
            db.execute("UPDATE rows SET status='cancelled' WHERE job_id=? AND status='pending'", (identity,))

    def rows(self, identity: str, offset: int = 0, limit: int = 50, verdict: str = '') -> dict:
        where = 'job_id=?'
        args: list = [identity]
        if verdict:
            where += ' AND image_verdict=?'
            args.append(verdict)
        with self.connect() as db:
            total = db.execute(f'SELECT COUNT(*) FROM rows WHERE {where}', args).fetchone()[0]
            records = db.execute(f'SELECT * FROM rows WHERE {where} ORDER BY ordinal LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
        items = []
        for row in records:
            result = json.loads(row['result'])
            source = json.loads(row['source'])
            items.append({**result, 'record_id': row['record_id'], 'ordinal': row['ordinal'], 'status': row['status'],
                          'image_verdict': row['image_verdict'], 'error': row['error'], 'label': source.get('label', ''),
                          'log_urls': source.get('log_urls', []), 'images': result.get('images', []), 'logs': result.get('logs', [])})
        return {'items': items, 'total': total}

    def export_rows(self, identity: str):
        offset = 0
        while True:
            rows = self.rows(identity, offset, 200)['items']
            if not rows:
                break
            yield from rows
            offset += len(rows)

    def artifact(self, identity: str, asset_id: str) -> dict:
        with self.connect() as db:
            row = db.execute('SELECT * FROM artifacts WHERE job_id=? AND id=?', (identity, asset_id)).fetchone()
        if not row:
            raise KeyError(asset_id)
        return dict(row)

    def add_artifact(self, identity: str, asset_id: str, path: Path, kind: str, name: str):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO artifacts(job_id,id,path,kind,name) VALUES(?,?,?,?,?)',
                       (identity, asset_id, str(path.resolve()), kind, name))
