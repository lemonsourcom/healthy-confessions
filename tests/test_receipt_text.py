from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import unittest
from PIL import ImageOps
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from receipt_text import cairo, pango, make_layout, render_text, TEXT_WIDTH


class UnicodeReceiptTests(unittest.TestCase):
    def layout(self, text):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
        return make_layout(cairo.Context(surface), text)

    def test_japanese_has_no_missing_glyphs(self):
        text = '日本語 ひらがな カタカナ レモンサワー「乾杯！」１２３、。漢字が混ざった文章です。'
        block = render_text(text)
        self.assertEqual(block.unknown_glyphs, 0)
        self.assertGreater(block.lines, 1)

    def test_arabic_direction_and_right_alignment(self):
        text = 'مرحبا'
        layout = self.layout(text)
        self.assertEqual(pango.pango.pango_layout_get_direction(layout.pointer, 0), 1)
        block = render_text(text)
        self.assertEqual(block.unknown_glyphs, 0)
        bbox = ImageOps.invert(block.image).getbbox()
        self.assertGreater(bbox[0], TEXT_WIDTH // 2)
        english = render_text('Hello')
        self.assertLess(ImageOps.invert(english.image).getbbox()[0], 20)

    def test_arabic_lam_alef_is_shaped(self):
        def ink_width(text):
            ink, _ = self.layout(text).get_extents()
            return ink.width
        self.assertLess(ink_width('لا'), ink_width('ل') + ink_width('ا'))

    def test_mixed_paragraph_directions_and_plain_text(self):
        text = 'IPPAI レモンサワー 001\nالعربية with 123\n<span>not markup & not HTML</span>'
        layout = self.layout(text)
        self.assertEqual(layout.text, text)
        self.assertEqual(pango.pango.pango_layout_get_direction(layout.pointer, 0), 0)
        arabic_offset = len(text.split('\n')[0].encode('utf-8')) + 1
        self.assertEqual(pango.pango.pango_layout_get_direction(layout.pointer, arabic_offset), 1)
        self.assertEqual(render_text(text).unknown_glyphs, 0)

    def test_300_japanese_characters_wrap_without_spaces_or_clipping(self):
        text = ('帰ると言ったけど朝まで踊っていた。' * 30)[:300]
        self.assertEqual(len(text), 300)
        block = render_text(text)
        self.assertGreater(block.lines, 10)
        self.assertEqual(block.unknown_glyphs, 0)
        bbox = ImageOps.invert(block.image).getbbox()
        self.assertGreater(bbox[0], 0)
        self.assertGreater(bbox[1], 0)
        self.assertLess(bbox[2], block.image.width)
        self.assertLess(bbox[3], block.image.height)

    def test_arabic_marks_not_clipped(self):
        block = render_text('اَلْعَرَبِيَّةُ لُغَةٌ جَمِيلَةٌ\nأنا أحب الرقص في الحفلة.')
        self.assertEqual(block.unknown_glyphs, 0)
        bbox = ImageOps.invert(block.image).getbbox()
        self.assertGreater(bbox[1], 0)
        self.assertLess(bbox[3], block.image.height)

    def test_concurrent_rendering_is_deterministic(self):
        text = '日本語 & العربية\nIPPAI 001'
        expected = render_text(text).image.tobytes()
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: render_text(text).image.tobytes(), range(12)))
        self.assertTrue(all(data == expected for data in results))


if __name__ == '__main__':
    unittest.main(verbosity=2)
