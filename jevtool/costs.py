"""Usage-based estimates, never a claim about the provider's actual invoice."""
from __future__ import annotations
from decimal import Decimal, InvalidOperation

PRICE_DATE = '2026-10-05'

def validate_rates(rates):
    if rates.get('currency') not in ('CNY', 'USD'):
        raise ValueError('币种只能为 CNY 或 USD。')
    if rates.get('billing') not in ('subscription', 'payg', 'unknown'):
        raise ValueError('计费方式不合法。')
    result = dict(rates)
    for field in ('input', 'output', 'cached'):
        value = rates.get(field)
        if value in (None, ''):
            result[field] = None
            continue
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise ValueError('单价必须为非负数字，留空表示未知。') from None
        if not number.is_finite() or number < 0:
            raise ValueError('单价必须为非负有限数字。')
        result[field] = str(number)
    return result

def default_rates(provider, endpoint, model, subscription=False):
    if provider == 'jev' and model.startswith('jev-'):
        return {'currency':'USD', 'billing':'payg', 'input':'0.042', 'output':'0',
                'cached':'0.042', 'checked':PRICE_DATE,
                'source':'https://typesafe.ai/blog/introducing-system-one-models-and-jev'}
    if provider == 'minimax' and model == 'MiniMax-M3':
        cn = '.cn' in endpoint
        return {'currency': 'CNY' if cn else 'USD',
                'billing': 'subscription' if subscription else 'payg',
                'input': '2.10' if cn else '0.30', 'output': '8.40' if cn else '1.20',
                'cached': '0.42' if cn else '0.06', 'checked': PRICE_DATE,
                'source': 'https://platform.minimax.cn/docs/pricing/overview' if cn else
                          'https://platform.minimax.io/docs/pricing/overview'}
    return {'currency': 'USD', 'billing': 'unknown', 'input': None, 'output': None,
            'cached': None, 'source': '尚未配置合同/账户单价'}

def count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

def calculate(usage, rates, *, model='', priority=False):
    usage = usage if isinstance(usage, dict) else {}
    inp = count(usage.get('prompt_tokens', usage.get('input_tokens')))
    out = count(usage.get('completion_tokens', usage.get('output_tokens')))
    details = usage.get('prompt_tokens_details') or usage.get('input_tokens_details') or {}
    if not isinstance(details, dict):
        details = {}
    cached = count(details.get('cached_tokens', usage.get('cache_read_input_tokens', 0)))
    result = {'input_tokens': inp, 'output_tokens': out, 'cached_tokens': cached,
              'estimated': None, 'unknown_reason': ''}
    if inp is None or out is None or cached is None or cached > inp:
        result['unknown_reason'] = '服务未返回完整合法用量；不计作零费用'
        return result
    if usage.get('cache_creation_input_tokens', 0) or details.get('cache_creation_tokens', 0):
        result['unknown_reason'] = '存在尚未配置的缓存写入费用'
        return result
    components = [(inp - cached, 'input'), (out, 'output'), (cached, 'cached')]
    if any(tokens and rates.get(field) is None for tokens, field in components):
        result['unknown_reason'] = '所需单价未配置'
        return result
    factor = Decimal('1')
    if model == 'MiniMax-M3' and rates.get('checked') == PRICE_DATE:
        if inp > 512000:
            factor *= 2
        if priority:
            factor *= Decimal('1.5')
    total = sum((Decimal(tokens) * Decimal(str(rates.get(field) or 0))
                 for tokens, field in components), Decimal(0)) * factor / Decimal(1000000)
    result['estimated'] = str(total)
    return result
