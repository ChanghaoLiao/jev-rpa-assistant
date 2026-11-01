"""Conservative visual detection of Xianyu's red unread badges.

The caller supplies the bottom of the list header: its red total is not a
conversation. Detection alone must never be treated as permission to send.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from io import BytesIO

from PIL import Image


@dataclass(frozen=True)
class RedBadge:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class RowBounds:
    top: int
    bottom: int


def match_red_badges(badges: tuple[RedBadge, ...], rows: tuple[RowBounds, ...],
                      *, list_width: int) -> tuple[int, ...]:
    """Match each visible badge to exactly one row, or reject the whole scan."""
    if list_width <= 0 or any(row.top >= row.bottom for row in rows):
        raise ValueError('会话列表行边界不合法。')
    matched = []
    for badge in badges:
        if not list_width * .10 <= badge.x <= list_width * .22:
            raise ValueError('红点不在预期的头像右上方，需人工核查。')
        choices = [index for index, row in enumerate(rows)
                   if row.top <= badge.y < row.bottom and
                   badge.y - row.top <= (row.bottom - row.top) * .42]
        if len(choices) != 1 or choices[0] in matched:
            raise ValueError('红点不能唯一对应一条可见会话，需人工核查。')
        matched.append(choices[0])
    return tuple(matched)


def find_red_badges(png: bytes, *, header_bottom: int) -> tuple[RedBadge, ...]:
    """Return circular red marks in the avatar column below the header.

    Coordinates are relative to the screenshot of the conversation-list
    element. Ambiguous or partially clipped shapes are ignored.
    """
    with Image.open(BytesIO(png)) as source:
        image = source.convert('RGB')
    width, height = image.size
    if width < 160 or height < 100 or not 0 <= header_bottom < height:
        raise ValueError('消息列表截图或列表头边界不合法。')
    scan_width = min(width, round(width * .25))
    pixels = image.load()
    red = set()
    for y in range(header_bottom, height):
        for x in range(scan_width):
            r, g, b = pixels[x, y]
            if r >= 175 and r > g * 1.35 and r > b * 1.35 and g < 165:
                red.add((x, y))

    minimum = max(9, round(width * .028))
    maximum = max(26, round(width * .09))
    found: list[RedBadge] = []
    while red:
        start = red.pop()
        queue = deque((start,))
        component = [start]
        while queue:
            x, y = queue.popleft()
            for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if neighbor in red:
                    red.remove(neighbor)
                    queue.append(neighbor)
                    component.append(neighbor)
        xs = [point[0] for point in component]
        ys = [point[1] for point in component]
        left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
        badge_width, badge_height = right - left + 1, bottom - top + 1
        if not (minimum <= badge_width <= maximum and minimum <= badge_height <= maximum):
            continue
        if top <= header_bottom or bottom >= height - 1:
            continue
        if not .72 <= badge_width / badge_height <= 1.38:
            continue
        fill = len(component) / (badge_width * badge_height)
        if not .42 <= fill <= .92:
            continue
        inner_left = left + badge_width // 4
        inner_right = right - badge_width // 4
        inner_top = top + badge_height // 4
        inner_bottom = bottom - badge_height // 4
        white_digit = sum(
            min(pixels[x, y]) > 205
            for y in range(inner_top, inner_bottom + 1)
            for x in range(inner_left, inner_right + 1)
        )
        if white_digit < 3:
            continue
        found.append(RedBadge((left + right) // 2, (top + bottom) // 2,
                              badge_width, badge_height))
    return tuple(sorted(found, key=lambda badge: badge.y))
