"""Deterministic wiring demo. This is NOT Jev inference or an accuracy measurement."""
from .config import ROOT, DEFAULTS
from .knowledge import Knowledge
from .router import Router, ChoiceResult

class DemoClient:
    def choose(self, state, instructions, criteria):
        # Intentionally predetermined path: 物流 -> 发货时间 -> first reply -> approve.
        choice = 'SEND' if 'SEND' in criteria else next(k for k in criteria if k != 'HUMAN')
        return ChoiceResult(choice, .95, {k: 1.0 if k == choice else 0.0 for k in criteria}, 'MOCK-NOT-JEV', {})

def run_demo():
    kb = Knowledge.load(ROOT / 'knowledge/replies.csv', ROOT / 'knowledge/categories.json')
    router = Router(kb, DemoClient(), DEFAULTS['jev'])
    return router.decide({'pending_buyer_messages': [{'role': 'buyer', 'text': '怎么查看预计发货时间？', 'timestamp': ''}],
                          'history': [], 'verified_order_facts': {}})
