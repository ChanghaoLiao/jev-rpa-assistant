from __future__ import annotations
import json
import sqlite3
import time
import uuid
from pathlib import Path
from .audit import record, clean, operation_id, get_audit

class Store:
    """Worker-thread SQLite ledger. A unique key prevents repeats across restarts."""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, customer TEXT NOT NULL,
            created REAL NOT NULL, status TEXT NOT NULL, decision TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS blocked (customer TEXT PRIMARY KEY, reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS frames (key TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS preview_whitelist (customer TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS vision_audit (
            id TEXT PRIMARY KEY, created REAL NOT NULL, source TEXT NOT NULL,
            customer TEXT NOT NULL, image_png BLOB NOT NULL, response_json TEXT,
            transcript_json TEXT, decision_json TEXT, status TEXT NOT NULL, error TEXT);
        CREATE TABLE IF NOT EXISTS run_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL,
            level TEXT NOT NULL, message TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS operation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL,
            action TEXT NOT NULL, customer TEXT, event_id TEXT,
            details_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS api_calls (
            id TEXT PRIMARY KEY, created REAL NOT NULL, finished REAL,
            provider TEXT NOT NULL, model TEXT NOT NULL, purpose TEXT NOT NULL,
            audit_id TEXT, attempt INTEGER NOT NULL, status TEXT NOT NULL,
            http_status INTEGER, elapsed REAL, usage_json TEXT, input_tokens INTEGER,
            output_tokens INTEGER, cached_tokens INTEGER, currency TEXT NOT NULL,
            billing TEXT NOT NULL, estimated TEXT, unknown_reason TEXT, rates_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS api_calls_time ON api_calls(created);
        CREATE TABLE IF NOT EXISTS api_rates (provider TEXT PRIMARY KEY, rates_json TEXT NOT NULL);
        ''')
        self.db.execute("UPDATE api_calls SET status='interrupted_unknown', unknown_reason='上次程序结束前未收到结果，实际消耗未知' WHERE status='started'")
        # A crash mid-send is never treated as safe to retry.
        for row in self.db.execute("SELECT id,customer FROM events WHERE status='sending'").fetchall():
            self.block(row['customer'], '上次发送被中断，发送结果未知，需人工核查。')
            self.record_action('crash_recovery_send_unknown', customer=row['customer'], event_id=row['id'],
                               details={'last_send_stage': get_audit().last_send_stage(row['id'])})
        self.db.commit()
        for table in ('vision_audit', 'operation_log', 'api_calls', 'run_logs'):
            columns = {r[1] for r in self.db.execute('PRAGMA table_info(' + table + ')')}
            if 'operation_id' not in columns:
                self.db.execute('ALTER TABLE ' + table + ' ADD COLUMN operation_id TEXT')
        self.db.commit()

    def seen(self, fingerprint):
        return self.db.execute('SELECT id FROM events WHERE fingerprint=?', (fingerprint,)).fetchone() is not None

    def get_rates(self, provider):
        row = self.db.execute('SELECT rates_json FROM api_rates WHERE provider=?', (provider,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_rates(self, provider, rates):
        from .costs import validate_rates
        rates = validate_rates(rates)
        self.db.execute('INSERT OR REPLACE INTO api_rates VALUES (?,?)',
                        (provider, json.dumps(rates, ensure_ascii=False)))
        self.db.commit()
        self.record_action('cost_rates_saved', details={'provider': provider, 'rates': rates})

    def start_api_call(self, item, rates):
        id_ = uuid.uuid4().hex
        self.db.execute('''INSERT INTO api_calls
            (id,created,provider,model,purpose,audit_id,attempt,status,currency,billing,rates_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (id_, time.time(), item['provider'], item['model'],
            item.get('purpose', 'API调用'), item.get('audit_id'), item['attempt'], 'started',
            rates['currency'], rates['billing'], json.dumps(rates, ensure_ascii=False)))
        self.db.execute('UPDATE api_calls SET operation_id=? WHERE id=?', (operation_id.get(), id_))
        self.db.commit()
        return id_

    def finish_api_call(self, id_, item):
        from .costs import calculate
        row = self.db.execute('SELECT model,rates_json FROM api_calls WHERE id=?', (id_,)).fetchone()
        usage = item.get('usage')
        result = calculate(usage, json.loads(row['rates_json']), model=row['model'],
                           priority=item.get('priority', False))
        self.db.execute('''UPDATE api_calls SET finished=?, status=?, http_status=?, elapsed=?,
            usage_json=?,input_tokens=?,output_tokens=?,cached_tokens=?,estimated=?,unknown_reason=? WHERE id=?''',
            (time.time(), item['status'], item.get('http_status'), item.get('elapsed'),
             json.dumps(usage, ensure_ascii=False), result['input_tokens'], result['output_tokens'],
             result['cached_tokens'], result['estimated'], result['unknown_reason'], id_))
        self.db.commit()
        record('api.cost', details={'call_id': id_, 'usage': usage, 'cost': result})

    def cost_report(self, page=0):
        summary = [dict(row) for row in self.db.execute('''SELECT provider,currency,billing,
            COUNT(*) AS attempts, COUNT(estimated) AS priced, SUM(input_tokens) AS input_tokens,
            SUM(output_tokens) AS output_tokens, SUM(cached_tokens) AS cached_tokens,
            SUM(CAST(estimated AS REAL)) AS estimated FROM api_calls
            GROUP BY provider,currency,billing''')]
        rows = [dict(row) for row in self.db.execute(
            'SELECT * FROM api_calls ORDER BY created DESC,id DESC LIMIT 101 OFFSET ?', (page * 100,))]
        return {'summary': summary, 'rows': rows[:100], 'page': page, 'has_more': len(rows) > 100}

    def start_vision(self, source, customer, png):
        id_ = uuid.uuid4().hex
        self.db.execute('INSERT INTO vision_audit(id,created,source,customer,image_png,status) VALUES (?,?,?,?,?,?)',
                        (id_, time.time(), source, customer, sqlite3.Binary(png), 'captured'))
        self.db.execute('UPDATE vision_audit SET operation_id=? WHERE id=?', (operation_id.get(), id_))
        self.db.commit()
        return id_

    def update_vision(self, id_, *, response=None, transcript=None, decision=None, status=None, error=None):
        record('vision.record', status or 'updated', {'audit_id': id_, 'response': response,
               'transcript': transcript, 'decision': decision, 'error': error})
        fields = []
        values = []
        for name, value in (('response_json', response), ('transcript_json', transcript),
                            ('decision_json', decision), ('status', status), ('error', error)):
            if value is not None:
                fields.append(name + '=?')
                values.append(json.dumps(clean(value), ensure_ascii=False) if name.endswith('_json') else clean(str(value)))
        if fields:
            self.db.execute('UPDATE vision_audit SET ' + ', '.join(fields) + ' WHERE id=?', (*values, id_))
            self.db.commit()

    def latest_vision(self):
        row = self.db.execute('SELECT * FROM vision_audit ORDER BY created DESC LIMIT 1').fetchone()
        return dict(row) if row else None

    def vision_history(self):
        return [dict(row) for row in self.db.execute(
            'SELECT id,created,source,customer,status,error FROM vision_audit ORDER BY created DESC')]

    def vision_record(self, id_):
        row = self.db.execute('SELECT * FROM vision_audit WHERE id=?', (id_,)).fetchone()
        return dict(row) if row else None

    def append_log(self, level, message):
        message = clean(message)
        record('ui.' + level, details={'message': message})
        self.db.execute('INSERT INTO run_logs(created,level,message) VALUES (?,?,?)',
                        (time.time(), level, message))
        self.db.execute('UPDATE run_logs SET operation_id=? WHERE id=last_insert_rowid()', (operation_id.get(),))
        self.db.commit()

    def record_action(self, action, *, customer=None, event_id=None, details=None):
        details = clean({**(details or {}), 'operation_id': operation_id.get()})
        record(action, details={**details, 'event_id': event_id}, customer=customer)
        self.db.execute('INSERT INTO operation_log(created,action,customer,event_id,details_json) VALUES (?,?,?,?,?)',
                        (time.time(), action, customer, event_id,
                         json.dumps(details or {}, ensure_ascii=False)))
        self.db.execute('UPDATE operation_log SET operation_id=? WHERE id=last_insert_rowid()', (operation_id.get(),))
        self.db.commit()

    def recent_actions(self, limit=100, offset=0):
        return [dict(row) for row in self.db.execute(
            'SELECT created,action,customer,event_id,details_json FROM operation_log ORDER BY id DESC LIMIT ? OFFSET ?',
            (limit, offset))]

    def frame_seen(self, key):
        return self.db.execute('SELECT key FROM frames WHERE key=?', (key,)).fetchone() is not None

    def mark_frame(self, key):
        self.db.execute('INSERT OR IGNORE INTO frames VALUES (?)', (key,))
        self.db.commit()

    def add(self, fingerprint, customer, decision):
        id_ = uuid.uuid4().hex
        try:
            self.db.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',
                (id_, fingerprint, customer, time.time(), 'preview' if decision.action == 'candidate' else 'human',
                 json.dumps(decision.as_dict(), ensure_ascii=False)))
            self.db.commit()
        except sqlite3.IntegrityError:
            return None
        return id_

    def status(self, id_):
        row = self.db.execute('SELECT status FROM events WHERE id=?', (id_,)).fetchone()
        return row['status'] if row else None

    def begin_send(self, id_):
        cursor = self.db.execute("UPDATE events SET status='sending' WHERE id=? AND status='preview'", (id_,))
        self.db.commit()
        return cursor.rowcount == 1

    def set_status(self, id_, value):
        self.db.execute('UPDATE events SET status=? WHERE id=?', (value, id_))
        self.db.commit()

    def block(self, customer, reason):
        self.db.execute('INSERT OR REPLACE INTO blocked VALUES (?,?)', (customer, reason))
        self.db.commit()

    def is_blocked(self, customer):
        row = self.db.execute('SELECT reason FROM blocked WHERE customer=?', (customer,)).fetchone()
        return row['reason'] if row else None

    def resume(self, customer):
        self.db.execute('DELETE FROM blocked WHERE customer=?', (customer,))
        self.db.commit()

    def recent(self):
        return [dict(r) for r in self.db.execute('SELECT id,customer,status,created,decision FROM events ORDER BY created DESC LIMIT 100')]

    def blocked_customers(self):
        return [dict(r) for r in self.db.execute('SELECT customer,reason FROM blocked ORDER BY customer')]

    def attempted_last_minute(self):
        # created is the event time; include preview-approved sends later via a timestamp table below.
        self.db.execute('CREATE TABLE IF NOT EXISTS send_attempts (at REAL NOT NULL)')
        return self.db.execute('SELECT COUNT(*) FROM send_attempts WHERE at>?', (time.time()-60,)).fetchone()[0]

    def record_attempt(self):
        self.db.execute('CREATE TABLE IF NOT EXISTS send_attempts (at REAL NOT NULL)')
        self.db.execute('INSERT INTO send_attempts VALUES (?)', (time.time(),))
        self.db.commit()

    def close(self):
        self.db.close()
