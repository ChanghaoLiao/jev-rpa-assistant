"""Immutable reply versions. Import never activates or changes merchant configuration."""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import sqlite3
import time
import uuid
from pathlib import Path

from .audit import record
from .knowledge import Knowledge, KnowledgeError, Reply
from .paths import DATA_ROOT, knowledge_file


class ReplyLibrary:
    def __init__(self, root: Path = DATA_ROOT):
        self.root = Path(root)
        self.directory = self.root / 'knowledge' / 'versions'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'knowledge' / 'versions.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE IF NOT EXISTS versions (
            id TEXT PRIMARY KEY, created REAL NOT NULL, filename TEXT NOT NULL,
            replies_path TEXT NOT NULL, categories_path TEXT NOT NULL,
            digest TEXT NOT NULL, count INTEGER NOT NULL, approved INTEGER NOT NULL)''')
        self.db.commit()

    def close(self):
        self.db.close()

    def list(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM versions ORDER BY created DESC')]

    def get(self, version):
        row = self.db.execute('SELECT * FROM versions WHERE id=?', (version,)).fetchone()
        if not row:
            raise KnowledgeError('话术版本不存在，请刷新后重试。')
        return dict(row)

    @staticmethod
    def parse(path: Path):
        path = Path(path)
        if path.suffix.lower() == '.xlsx':
            from openpyxl import load_workbook
            book = load_workbook(path, read_only=True, data_only=False)
            try:
                values = iter(book.worksheets[0].iter_rows(values_only=True))
                headers = [str(v or '').strip() for v in next(values, ())]
                if any(k not in headers for k in Knowledge.FIELDS):
                    raise KnowledgeError('Excel 第一张工作表表头不正确，请下载七列模板。')
                if len(headers) != len(set(headers)):
                    raise KnowledgeError('Excel 表头有重复列。')
                indices = [headers.index(k) for k in Knowledge.FIELDS]
                rows = []
                seen = set()
                for number, row in enumerate(values, 2):
                    if not any(v is not None for v in row):
                        continue
                    if any(i >= len(row) or row[i] is None for i in indices[:3] + [indices[4]]):
                        raise KnowledgeError(f'Excel 第 {number} 行有必填字段为空。')
                    item = {}
                    for field, index in zip(Knowledge.FIELDS, indices):
                        value = row[index] if index < len(row) else None
                        if isinstance(value, str) and value.startswith('='):
                            raise KnowledgeError(f'Excel 第 {number} 行包含公式，请转为明确的文字或布尔值。')
                        item[field] = ('true' if value else 'false') if isinstance(value, bool) else str(value or '')
                    for field in ('approved_for_auto', 'requires_human'):
                        if item[field].strip().lower() not in ('true', 'false'):
                            raise KnowledgeError(f'Excel 第 {number} 行 {field} 必须为 true / false。')
                    ReplyLibrary.validate_row(item, number, seen, 'Excel')
                    rows.append(item)
                    if len(rows) > 20000:
                        raise KnowledgeError('话术超过 20000 条，请拆分文件。')
                return rows
            finally:
                book.close()
        if path.suffix.lower() != '.csv':
            raise KnowledgeError('请使用 .xlsx 或 UTF-8 CSV 文件。')
        with path.open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or any(k not in reader.fieldnames for k in Knowledge.FIELDS):
                raise KnowledgeError('CSV 表头不正确，请使用七列模板。')
            rows = []
            seen = set()
            for number, item in enumerate(reader, 2):
                if None in item or any(item.get(k) is None for k in Knowledge.FIELDS):
                    raise KnowledgeError(f'CSV 第 {number} 行列数不正确。')
                row = {k: item[k] for k in Knowledge.FIELDS}
                ReplyLibrary.validate_row(row, number, seen, 'CSV')
                rows.append(row)
                if len(rows) > 20000: raise KnowledgeError('话术超过 20000 条，请拆分文件。')
            return rows

    @staticmethod
    def validate_row(row, number, seen, source):
        try:
            if row['reply_id'] in seen: raise KnowledgeError('重复话术编号：' + row['reply_id'])
            for k in ('approved_for_auto', 'requires_human'):
                if row[k].strip().lower() not in ('true', 'false'): raise KnowledgeError(k + ' 必须为 true / false。')
            Knowledge([Reply(row['reply_id'], row['category_path'], row['when_to_use'], row['when_not_to_use'],
                row['reply_text'], row['approved_for_auto'].strip().lower() == 'true', row['requires_human'].strip().lower() == 'true')])
            seen.add(row['reply_id'])
        except KnowledgeError as error:
            raise KnowledgeError(f'{source} 第 {number} 行：{error}') from error

    def import_file(self, path, categories: Path):
        path = Path(path)
        rows = self.parse(path)
        text = io.StringIO(newline='')
        writer = csv.DictWriter(text, fieldnames=Knowledge.FIELDS, lineterminator='\r\n')
        writer.writeheader()
        writer.writerows(rows)
        csv_bytes = text.getvalue().encode('utf-8-sig')
        categories = Path(categories)
        cat_bytes = categories.read_bytes() if categories.exists() else b'{}'
        return self._store(path.name, csv_bytes, cat_bytes)

    def snapshot_current(self, cfg):
        source = knowledge_file(cfg['knowledge']['replies_csv'])
        categories = knowledge_file(cfg['knowledge']['categories_json'])
        csv_bytes = source.read_bytes()
        cat_bytes = categories.read_bytes() if categories.exists() else b'{}'
        digest = hashlib.sha256(csv_bytes + b'\0' + cat_bytes).hexdigest()
        row = self.db.execute('SELECT * FROM versions WHERE digest=? LIMIT 1', (digest,)).fetchone()
        return dict(row) if row else self._store('原有话术 · ' + source.name, csv_bytes, cat_bytes)

    def _store(self, filename, csv_bytes, cat_bytes):
        id_ = uuid.uuid4().hex
        relative = Path('knowledge') / 'versions' / id_
        directory = self.root / relative
        directory.mkdir()
        replies, categories = directory / 'replies.csv', directory / 'categories.json'
        try:
            replies.write_bytes(csv_bytes)
            categories.write_bytes(cat_bytes)
            knowledge = Knowledge.load(replies, categories)
            approved = sum(r.approved and not r.manual_only for r in knowledge.replies.values())
            digest = hashlib.sha256(csv_bytes + b'\0' + cat_bytes).hexdigest()
            record('library.import.validated', details={'filename': filename, 'count': len(knowledge.replies), 'approved': approved, 'version': id_})
            self.db.execute('INSERT INTO versions VALUES (?,?,?,?,?,?,?,?)',
                (id_, time.time(), filename, (relative / 'replies.csv').as_posix(),
                 (relative / 'categories.json').as_posix(), digest, len(knowledge.replies), approved))
            self.db.commit()
        except Exception:
            self.db.rollback()
            # Only our newly-created version directory; no merchant file is removed.
            for p in (replies, categories):
                p.unlink(missing_ok=True)
            directory.rmdir()
            raise
        return self.get(id_)

    def preview(self, id_):
        version = self.get(id_)
        replies, categories = self.root / version['replies_path'], self.root / version['categories_path']
        digest = hashlib.sha256(replies.read_bytes() + b'\0' + categories.read_bytes()).hexdigest()
        if digest != version['digest']:
            raise KnowledgeError('此历史版本文件已被外部修改，不能启用；请作为新文件重新导入。')
        kb = Knowledge.load(replies, categories)
        return version, [dict(zip(Knowledge.FIELDS, [r.path, r.id, r.when, r.exclude, r.text, r.approved, r.manual_only])) for r in kb.replies.values()]

    def store_review(self, csv_bytes, version):
        return self._store('审核 · ' + version['filename'], csv_bytes, (self.root / version['categories_path']).read_bytes())

    def activate_config(self, cfg, id_):
        version, _ = self.preview(id_)
        result = copy.deepcopy(cfg)
        result['knowledge'].update(replies_csv=version['replies_path'], categories_json=version['categories_path'])
        return result

    @staticmethod
    def export_template(path):
        from openpyxl import Workbook
        book = Workbook()
        sheet = book.active
        sheet.title = '话术'
        sheet.append(Knowledge.FIELDS)
        sheet.append(['物流/配送查询', 'shipping_example', '客户询问在哪里查看物流', '',
                      '请在订单详情页查看物流信息。', False, False])
        for col, width in zip('ABCDEFG', [26, 24, 40, 36, 64, 22, 22]):
            sheet.column_dimensions[col].width = width
        sheet.freeze_panes = 'A2'
        book.save(path)
