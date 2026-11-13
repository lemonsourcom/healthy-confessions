import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from receipt_text import render_text, TEXT_WIDTH
from PIL import ImageOps

class EmojiTests(unittest.TestCase):
    def test_common_and_combined_emoji_have_ink_and_no_missing_glyphs(self):
        for text in ['🍋', '😂', '❤️', '🙏🏽', '👩‍💻', '👨‍👩‍👧‍👦', '🇯🇵', '1️⃣', '👍🏻', '👍🏿', '🥹', '🫶']:
            with self.subTest(text=text):
                block = render_text(text)
                self.assertEqual(block.unknown_glyphs, 0)
                self.assertIsNotNone(ImageOps.invert(block.image).getbbox())

    def test_keycap_presentation_selector_does_not_split_ligature(self):
        self.assertEqual(render_text('1️⃣').image.tobytes(), render_text('1⃣').image.tobytes())
        bbox=ImageOps.invert(render_text('1️⃣').image).getbbox()
        self.assertLess(bbox[2]-bbox[0], 65)

    def test_zwj_family_is_one_symbol(self):
        bbox=ImageOps.invert(render_text('👨‍👩‍👧‍👦').image).getbbox()
        self.assertLess(bbox[2]-bbox[0], 65)

    def test_300_utf16_units_wrap_and_do_not_clip(self):
        text='🍋'*150
        self.assertEqual(len(text.encode('utf-16-le'))//2, 300)
        block=render_text(text)
        self.assertEqual(block.unknown_glyphs,0)
        self.assertGreater(block.lines, 5)
        bbox=ImageOps.invert(block.image).getbbox()
        self.assertGreater(bbox[0],0)
        self.assertLess(bbox[2],block.image.width)
        self.assertLess(bbox[3],block.image.height)

    def test_mixed_scripts_and_emoji(self):
        self.assertEqual(render_text('Ich ging tanzen 😂🍋.\n乾杯 ❤️\nأنا أحب الرقص 🙏🏽').unknown_glyphs,0)
