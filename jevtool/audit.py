"""Durable, thread-safe application audit, independent of a configured Service."""
from __future__ import annotations
import contextvars
import functools
import json
import re
import sqlite3
import threading
import time
import traceback
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from .paths import DATA_ROOT

operation_id = contextvars.ContextVar('audit_operation', default=None)
platform_id = contextvars.ContextVar('audit_platform', default=None)
event_id = contextvars.ContextVar('audit_event', default=None)
_lock = threading.RLock()
_secrets = set()
_instance = None

def register_secrets(cfg):
    with _lock:
        for section in ('jev', 'vision', 'desktop'):
            for field in ('api_key', 'license_key'):
                value = cfg.get(section, {}).get(field)
                if value:
                    _secrets.add(value)

def clean(value):
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if any(w in str(k).lower() for w in
                ('api_key', 'authorization', 'license_key', 'password', 'access_token', 'secret'))
                else clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, bytes):
        return {'bytes': len(value)}
    if isinstance(value, str):
        with _lock:
            for secret in sorted(_secrets, key=len, reverse=True):
                value = value.replace(secret, '[REDACTED]')
        value = re.sub(r'Bearer\s+[^\s"\'<>]+', 'Bearer [REDACTED]', value, flags=re.I)
        value = re.sub(r'([?&](?:token|key|access_token)=)[^&\s]+', r'\1[REDACTED]', value, flags=re.I)
        value = re.sub(r'("(?:api_key|authorization|license_key|password|access_token)"\s*:\s*")[^"\n]*', r'\1[REDACTED]', value, flags=re.I)
        value = re.sub(r'\bsk-[A-Za-z0-9_-]{8,}', '[REDACTED]', value)
        return value
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return clean(str(value))

def exception_details(error):
    return clean({'type': type(error).__name__, 'message': str(error),
                  'traceback': ''.join(traceback.format_exception(type(error), error, error.__traceback__)),
                  'evidence': getattr(error, 'evidence', None)})

class Audit:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.session = uuid.uuid4().hex
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS trace (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, operation TEXT,
          parent TEXT, created REAL NOT NULL, platform TEXT, customer TEXT,
          stage TEXT NOT NULL, result TEXT NOT NULL, elapsed REAL, details TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS trace_operation ON trace(operation,seq);
        CREATE TABLE IF NOT EXISTS attachments (
          id TEXT PRIMARY KEY, trace_seq INTEGER NOT NULL, name TEXT NOT NULL,
          mime TEXT NOT NULL, data BLOB NOT NULL);
        ''')
        # A process can end without an exception (power loss). Preserve unfinished operations.
        unfinished = self.db.execute("SELECT operation, MAX(seq) seq FROM trace WHERE operation IS NOT NULL GROUP BY operation HAVING SUM(stage='operation.finished')=0 AND SUM(stage='operation.started')>0").fetchall()
        for row in unfinished:
            last = self.db.execute('SELECT * FROM trace WHERE seq=?', (row['seq'],)).fetchone()
            self.record('recovery.interrupted', 'unknown', {'previous_operation': row['operation'],
                        'last_stage': last['stage'], 'last_result': last['result']})
            self.record('operation.finished', 'interrupted', {'recovered': True}, op=row['operation'])
        self.record('application.started', 'success')

    def record(self, stage, result='success', details=None, *, customer=None, parent=None, elapsed=None, png=None, op=None):
        with self.lock:
            try:
                cur = self.db.execute('INSERT INTO trace(session,operation,parent,created,platform,customer,stage,result,elapsed,details) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (self.session, op or operation_id.get(), parent, time.time(), platform_id.get(), customer,
                     stage, result, elapsed, json.dumps(clean({**(details or {}), 'event_id': event_id.get()} if event_id.get() else details or {}), ensure_ascii=False)))
                if png:
                    self.db.execute('INSERT INTO attachments VALUES (?,?,?,?,?)',
                        (uuid.uuid4().hex, cur.lastrowid, 'capture.png', 'image/png', png))
                self.db.commit()
                return cur.lastrowid
            except Exception:
                self.db.rollback()
                raise

    def rows(self, filters=None):
        filters = filters or {}
        clauses, args = [], []
        for key in ('customer', 'operation', 'stage', 'result'):
            if filters.get(key):
                clauses.append(key + ' LIKE ?')
                args.append('%' + filters[key] + '%')
        if filters.get('platform'):
            clauses.append('platform=?'); args.append(filters['platform'])
        for key, sign in (('from', '>='), ('to', '<=')):
            if filters.get(key):
                clauses.append('created ' + sign + ' ?')
                args.append(float(filters[key]))
        with self.lock:
            where = ' AND '.join(clauses)
            # A match selects the entire operation, so filtering a customer never hides its API steps.
            if clauses:
                where = ' WHERE (' + where + ') OR operation IN (SELECT operation FROM trace WHERE ' + where + ')'
                args = args + args
            return [dict(r) for r in self.db.execute('SELECT * FROM trace' +
                    (where if clauses else '') + ' ORDER BY seq DESC', args)]

    def attachments(self, seq):
        with self.lock:
            return [dict(r) for r in self.db.execute('SELECT * FROM attachments WHERE trace_seq=?', (seq,))]

    def page_rows(self, filters=None, page=0):
        filters = filters or {}
        clauses, args = [], []
        for key in ('customer', 'operation', 'stage', 'result'):
            if filters.get(key):
                clauses.append(key + ' LIKE ?'); args.append('%' + filters[key] + '%')
        if filters.get('platform'):
            clauses.append('platform=?'); args.append(filters['platform'])
        for key, sign in (('from','>='),('to','<=')):
            if filters.get(key):
                clauses.append('created ' + sign + ' ?'); args.append(float(filters[key]))
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        with self.lock:
            groups = self.db.execute("SELECT COALESCE(operation,'session:' || session) AS grp FROM trace" + where +
                ' GROUP BY grp ORDER BY MAX(seq) DESC LIMIT 101 OFFSET ?', [*args,max(0,page)*100]).fetchall()
            keys = [r[0] for r in groups[:100]]
            rows = [dict(r) for r in self.db.execute("SELECT * FROM trace WHERE COALESCE(operation,'session:' || session) IN (" +
                ','.join('?' for _ in keys) + ') ORDER BY seq DESC', keys)] if keys else []
        return rows, len(groups)>100

    def last_send_stage(self, id_):
        with self.lock:
            row = self.db.execute("SELECT stage,result,seq,created FROM trace WHERE json_extract(details,'$.event_id')=? AND stage LIKE 'send.%' ORDER BY seq DESC LIMIT 1", (id_,)).fetchone()
            return dict(row) if row else {'stage': 'legacy_evidence_missing'}

    def operation_result(self, id_):
        with self.lock:
            results = {r[0] for r in self.db.execute('SELECT result FROM trace WHERE operation=?', (id_,))}
        if results.intersection({'failed','vision_failed','send_unknown','send_aborted_before_click'}):
            return 'failed'
        return 'cancelled' if 'cancelled' in results else 'success'

    def export(self, path, filters=None):
        rows = self.rows(filters)
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as out:
            out.writestr('trace.json', json.dumps(clean(rows), ensure_ascii=False, indent=2))
            out.writestr('README.txt', '本包仅含本地实际记录，旧版缺失证据无法还原。截图和聊天可能含客户信息。')
            for row in rows:
                for a in self.attachments(row['seq']):
                    out.writestr('attachments/' + a['id'] + '.png', a['data'])

def get_audit():
    global _instance
    with _lock:
        if _instance is None:
            _instance = Audit(DATA_ROOT / 'audit.sqlite3')
        return _instance

def record(stage, result='success', details=None, **kwargs):
    return get_audit().record(stage, result, details, **kwargs)

@contextmanager
def operation(name, parent=None):
    id_ = uuid.uuid4().hex
    token = operation_id.set(id_)
    start = time.monotonic()
    status = 'success'
    try:
        record('operation.started', details={'name': name}, parent=parent)
        yield id_
    except BaseException as error:
        status = 'cancelled' if type(error).__name__ == 'Cancelled' else 'failed'
        record('operation.exception', status, exception_details(error))
        raise
    finally:
        try:
            if status == 'success':
                status = get_audit().operation_result(id_)
            record('operation.finished', status, {'name': name}, elapsed=time.monotonic()-start)
        finally:
            operation_id.reset(token)

def traced(stage):
    def decorate(fn):
        @functools.wraps(fn)
        def call(*args, **kwargs):
            cfg = getattr(args[0], 'cfg', {}) if args else {}
            register_secrets(cfg)
            event_token = event_id.set(args[1]) if stage == 'service.send' and len(args)>1 else None
            record(stage + '.started')
            start = time.monotonic()
            try:
                value = fn(*args, **kwargs)
            except BaseException as error:
                record(stage + '.failed', 'failed', exception_details(error),
                       elapsed=time.monotonic()-start)
                raise
            finally:
                if event_token is not None:
                    event_id.reset(event_token)
            record(stage + '.finished', details={'returned': value if isinstance(value, (bool, str, int, dict)) else type(value).__name__}, elapsed=time.monotonic()-start)
            return value
        return call
    return decorate
