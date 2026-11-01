"""Worker-owned, parameterized reads for the new presentation layer."""
from __future__ import annotations
import json
from .audit import get_audit


def nickname(value):
    return (value or '').split(' :: ')[-1]


def query_history(store, category='events', filters=None, page=0):
    filters = filters or {}
    allowed = {'events': ('events', 'created'), 'vision': ('vision_audit', 'created'),
               'api': ('api_calls', 'created')}
    table, order = allowed[category]
    clauses, args = [], []
    if filters.get('customer'):
        if category == 'api':
            clauses.append('audit_id IN (SELECT id FROM vision_audit WHERE customer LIKE ?)')
        else:
            clauses.append('customer LIKE ?')
        args.append('%' + filters['customer'] + '%')
    if filters.get('status'):
        clauses.append('status=?'); args.append(filters['status'])
    for key, sign in [('from', '>='), ('to', '<=')]:
        if filters.get(key) is not None:
            clauses.append('created ' + sign + ' ?'); args.append(float(filters[key]))
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    fields = 'id,created,customer,source,status,error,operation_id' if category == 'vision' else '*'
    rows = [dict(r) for r in store.db.execute('SELECT ' + fields + ' FROM ' + table + where +
        ' ORDER BY ' + order + ' DESC,id DESC LIMIT 101 OFFSET ?', [*args, max(0, int(page)) * 100])]
    if category == 'events':
        for r in rows:
            r['decision'] = json.loads(r['decision'])
            proof = store.db.execute('SELECT transcript_json FROM vision_audit WHERE customer IN (?,?) AND created<=? ORDER BY created DESC LIMIT 1',
                (r['customer'], nickname(r['customer']), r['created'] + 1)).fetchone()
            r['question'] = ''
            if proof and proof[0]:
                transcript = json.loads(proof[0])
                pending = transcript.get('pending_buyer_messages', [])
                if not pending:
                    pending = [m for m in transcript.get('history', transcript.get('messages', [])) if m.get('role') == 'buyer'][-1:]
                r['question'] = '\n'.join(str(m.get('text', '')) for m in pending)
    return {'rows': rows[:100], 'has_more': len(rows) > 100, 'page': max(0, int(page)), 'category': category}


def blocked_details(store):
    rows = store.blocked_customers()
    for row in rows:
        last = store.db.execute('SELECT * FROM events WHERE customer=? ORDER BY created DESC LIMIT 1', (row['customer'],)).fetchone()
        row['last_event'] = dict(last) if last else None
        row['possibly_sent'] = bool(last and last['status'] in ('sending', 'send_unknown', 'sent', 'sent_ui_verified')) or '未知' in row['reason']
        row['last_stage'] = get_audit().last_send_stage(last['id']) if last else None
        note = store.db.execute("SELECT created FROM operation_log WHERE customer=? AND action='manual_marked_done' ORDER BY id DESC LIMIT 1", (row['customer'],)).fetchone()
        row['manual_done'] = bool(note and (not last or note[0] >= last['created']))
    return rows
