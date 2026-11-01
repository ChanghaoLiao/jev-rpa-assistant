"""Readiness checks are shared by the wizard and the sending service."""
from __future__ import annotations

from .knowledge import Knowledge
from .paths import knowledge_file
from .config import MINIMAX_ENDPOINTS, MINIMAX_MODEL
from .vision import vision_signature


def approved_replies(config: dict) -> int:
    kb = Knowledge.load(knowledge_file(config['knowledge']['replies_csv']),
                        knowledge_file(config['knowledge']['categories_json']))
    return sum(reply.approved and not reply.manual_only for reply in kb.replies.values())


def checks(config: dict, *, approved: int | None = None) -> dict[str, bool]:
    desktop, vision = config['desktop'], config['vision']
    onboarding = config.get('onboarding', {})
    if approved is None:
        approved = approved_replies(config)
    return {
        'jev': bool(config['jev']['api_key']) and onboarding.get('jev_verified') is True,
        'vision': (bool(vision['api_key']) and onboarding.get('vision_verified') is True and
                   onboarding.get('vision_signature') == vision_signature(vision) and
                   vision['provider'] == 'minimax' and vision['model'] == MINIMAX_MODEL and
                   vision['endpoint'] in MINIMAX_ENDPOINTS.values()),
        'platform': bool(desktop['shop_id'].strip()),
        'locators': desktop['locators_reviewed'] is True,
        'unread': desktop['unread_locator_reviewed'] is True,
        'conversation_identity': desktop.get('unread_mode', 'locator') != 'red_badge',
        'knowledge': approved > 0,
        'test_send': desktop.get('test_send_verified') is True,
        'consent': desktop['data_upload_authorized'] is True,
    }


def can_auto_send(config: dict, *, approved: int | None = None) -> bool:
    return config['desktop']['auto_send_enabled'] is True and all(checks(config, approved=approved).values())
