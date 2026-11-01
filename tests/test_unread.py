"""Badge detection must ignore the header counter and unrelated red imagery."""
from io import BytesIO
import unittest

from PIL import Image, ImageDraw

from jevtool.unread import RowBounds, find_red_badges, match_red_badges


def screenshot(scale=1):
    image = Image.new('RGB', (330, 513), '#f7f7f7')
    draw = ImageDraw.Draw(image)
    for x, y in ((66, 29), (45, 77), (45, 281), (45, 349), (45, 417)):
        draw.ellipse((x - 8, y - 8, x + 7, y + 7), fill='#ed5c63')
        draw.text((x - 2, y - 5), '1', fill='white')
    draw.rectangle((275, 185, 316, 210), fill='#ed5c63')  # Product image.
    draw.ellipse((37, 188, 53, 204), fill='#ed5c63')  # Red avatar, no digit.
    draw.ellipse((38, 502, 53, 517), fill='#ed5c63')  # Cropped at bottom.
    if scale != 1:
        image = image.resize((330 * scale, 513 * scale), Image.Resampling.NEAREST)
    output = BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


class RedBadgeTests(unittest.TestCase):
    rows = tuple(RowBounds(60 + 68 * index, 128 + 68 * index) for index in range(7))

    def test_realistic_sidebar_excludes_header_product_and_avatar(self):
        badges = find_red_badges(screenshot(), header_bottom=60)
        self.assertEqual(len(badges), 4)
        for badge, expected_y in zip(badges, (77, 281, 349, 417)):
            self.assertLessEqual(abs(badge.y - expected_y), 2)
        self.assertTrue(all(35 <= badge.x <= 55 for badge in badges))
        self.assertEqual(match_red_badges(badges, self.rows, list_width=330), (0, 3, 4, 5))

    def test_zoomed_sidebar(self):
        badges = find_red_badges(screenshot(2), header_bottom=120)
        self.assertEqual(len(badges), 4)
        for badge, expected_y in zip(badges, (155, 563, 699, 835)):
            self.assertLessEqual(abs(badge.y - expected_y), 2)

    def test_bad_header_boundary_fails_closed(self):
        with self.assertRaises(ValueError):
            find_red_badges(screenshot(), header_bottom=513)

    def test_ambiguous_or_shifted_rows_cannot_be_clicked(self):
        badges = find_red_badges(screenshot(), header_bottom=60)
        with self.assertRaisesRegex(ValueError, '唯一对应'):
            match_red_badges(badges, (RowBounds(60, 128), RowBounds(60, 128)), list_width=330)
        with self.assertRaisesRegex(ValueError, '唯一对应'):
            match_red_badges(badges, (RowBounds(100, 168),), list_width=330)


if __name__ == '__main__':
    unittest.main()
