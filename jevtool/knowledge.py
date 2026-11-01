from __future__ import annotations
import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

class KnowledgeError(ValueError):
    pass

@dataclass(frozen=True)
class Reply:
    id: str
    path: str
    when: str
    exclude: str
    text: str
    approved: bool
    manual_only: bool

@dataclass
class Node:
    path: str
    children: dict[str, 'Node'] = field(default_factory=dict)
    replies: dict[str, Reply] = field(default_factory=dict)

class Knowledge:
    FIELDS = ['category_path', 'reply_id', 'when_to_use', 'when_not_to_use', 'reply_text',
              'approved_for_auto', 'requires_human']
    def __init__(self, replies: list[Reply], descriptions: dict[str, str] | None = None):
        self.replies = {}
        self.descriptions = descriptions or {}
        if not isinstance(self.descriptions, dict) or not all(isinstance(k, str) and isinstance(v, str) and len(v) <= 2000 for k, v in self.descriptions.items()):
            raise KnowledgeError('categories.json 必须为“分类路径: 说明”的字符串映射。')
        self.root = Node('')
        for r in replies:
            if r.id in self.replies:
                raise KnowledgeError(f'重复话术编号：{r.id}')
            if not r.id or len(r.id) > 80 or not r.text.strip() or not r.when.strip():
                raise KnowledgeError('编号、适用条件和回复内容不能为空；编号不超过 80 字符。')
            if len(r.text) > 3000 or len(r.when) > 2000 or len(r.exclude) > 2000:
                raise KnowledgeError(f'话术 {r.id} 字段过长。')
            parts = r.path.split('/')
            if not 1 <= len(parts) <= 8 or any(not p.strip() or len(p) > 80 for p in parts):
                raise KnowledgeError(f'分类路径不合法（最多8层，不能有空层）：{r.path}')
            self.replies[r.id] = r
            node = self.root
            for i, part in enumerate(parts):
                path = '/'.join(parts[:i+1])
                node = node.children.setdefault(part, Node(path))
            node.replies[r.id] = r
        if not self.replies:
            raise KnowledgeError('话术库为空。')
        self._validate_node(self.root)

    def _validate_node(self, node: Node):
        if len(node.children) + len(node.replies) > 254:
            raise KnowledgeError(f'分类 {node.path or "根目录"} 超过254个选项；请细分类别，为人工选项留1位。')
        for child in node.children.values():
            self._validate_node(child)

    @classmethod
    def load(cls, csv_path: Path, descriptions_path: Path):
        desc = json.loads(descriptions_path.read_text(encoding='utf-8-sig')) if descriptions_path.exists() else {}
        replies = []
        def flag(raw: str, row: int):
            if raw.strip().lower() not in ('true', 'false'):
                raise KnowledgeError(f'CSV 第 {row} 行的布尔值只能为 true / false。')
            return raw.strip().lower() == 'true'
        with csv_path.open(encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or any(key not in reader.fieldnames for key in cls.FIELDS):
                raise KnowledgeError('CSV 表头不正确，请使用随附模板。')
            for n, row in enumerate(reader, start=2):
                if None in row or any(row.get(k) is None for k in cls.FIELDS):
                    raise KnowledgeError(f'CSV 第 {n} 行列数不正确。')
                replies.append(Reply(row['reply_id'].strip(), row['category_path'].strip(),
                    row['when_to_use'].strip(), row['when_not_to_use'].strip(), row['reply_text'],
                    flag(row['approved_for_auto'], n), flag(row['requires_human'], n)))
        return cls(replies, desc)

    def options(self, node: Node):
        criteria, targets = {}, {}
        for i, (label, child) in enumerate(node.children.items()):
            key = f'C{i}'
            criteria[key] = {'category': child.path,
                             'description': self.descriptions.get(child.path, f'属于“{label}”的问题；只在明确符合时选择。')}
            targets[key] = child
        for i, reply in enumerate(node.replies.values()):
            key = f'R{i}'
            criteria[key] = {'reply_id': reply.id, 'applies_when': reply.when,
                             'does_not_apply_when': reply.exclude, 'exact_reply': reply.text}
            targets[key] = reply
        criteria['HUMAN'] = '无法明确归类、不在目录、关键信息缺失、有多个未解决意图、需要查订单/退款审批、需人工处理。'
        return criteria, targets
