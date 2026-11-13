"""Unicode receipt text layout with font fallback, shaping and paragraph bidi.

Pango handles script shaping and Unicode line breaking. Text is passed as plain
text; user input is never interpreted as markup. All font sizes are in pixels.
"""
from dataclasses import dataclass
import io
import math
import threading
import unicodedata
import regex

import cairocffi as cairo
import pangocffi as pango
import pangocairocffi as pangocairo
from PIL import Image

FONT_SIZE = 52
TEXT_WIDTH = 804
PADDING = 5
# Serialize access to the shared native font map; server requests may be threaded.
_LOCK = threading.RLock()
_GRAPHEME = regex.compile(r'\X')
_EMOJI = regex.compile(r'\p{Extended_Pictographic}|\p{Regional_Indicator}|\u20e3')


def script_for(character, previous='latin'):
    name = unicodedata.name(character, '')
    if 'ARABIC' in name:
        return 'arabic'
    if any(part in name for part in ('CJK', 'HIRAGANA', 'KATAKANA', 'IDEOGRAPHIC')):
        return 'japanese'
    if unicodedata.category(character).startswith(('M', 'P', 'Z')) or character in '\n\r\t\u200c\u200d':
        return previous
    return 'latin'


def font_description(script):
    font = pango.FontDescription()
    font.family = {'latin': 'Liberation Sans', 'japanese': 'Noto Sans CJK JP', 'arabic': 'Noto Sans Arabic', 'emoji': 'Noto Emoji'}[script]
    font.style = pango.Style.ITALIC if script == 'latin' else pango.Style.NORMAL
    font.set_absolute_size(pango.units_from_double(FONT_SIZE))
    return font


def make_layout(context, text, width=TEXT_WIDTH):
    # Noto Emoji is monochrome already. Removing the color presentation selector
    # lets its keycap/ZWJ ligatures shape correctly on Debian's Pango version.
    # This affects rendering only; the server stores the original confession.
    text = ''.join(cluster.replace('\ufe0f', '') if _EMOJI.search(cluster) else cluster
                   for cluster in _GRAPHEME.findall(text))
    layout = pangocairo.create_layout(context)
    layout.font_description = font_description('latin')
    layout.width = pango.units_from_double(width)
    layout.wrap = pango.WrapMode.WORD_CHAR
    layout.spacing = pango.units_from_double(4)
    layout.alignment = pango.Alignment.LEFT
    pango.pango.pango_layout_set_auto_dir(layout.pointer, True)
    layout.text = text
    # Font ranges are UTF-8 byte offsets, never Unicode character indices.
    attributes = pango.AttrList()
    start = offset = 0
    previous = 'latin'
    for cluster in _GRAPHEME.findall(text):
        # Keep ZWJ families, skin tones, flags, keycaps and variation selectors
        # in a single font run so Pango/HarfBuzz can shape the whole emoji.
        is_emoji = bool(_EMOJI.search(cluster))
        for character in cluster:
            script = 'emoji' if is_emoji else script_for(character, 'latin' if previous == 'emoji' else previous)
            if script != previous:
                if offset > start:
                    attributes.insert(pango.Attribute.from_font_desc(font_description(previous), start, offset))
                start = offset
            previous = script
            offset += len(character.encode('utf-8'))
    if offset > start:
        attributes.insert(pango.Attribute.from_font_desc(font_description(previous), start, offset))
    layout.attributes = attributes
    return layout


@dataclass
class TextBlock:
    image: Image.Image
    # Position of the logical layout origin and first baseline within image.
    origin_x: int
    baseline: int
    lines: int
    unknown_glyphs: int


def render_text(text, width=TEXT_WIDTH):
    if '\x00' in text:
        raise ValueError('NUL characters are not supported in receipt text')
    with _LOCK:
        measure = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
        context = cairo.Context(measure)
        layout = make_layout(context, text, width)
        ink, logical = layout.get_extents()
        unit = pango.units_to_double
        left = min(0, math.floor(unit(ink.x)))
        top = min(0, math.floor(unit(ink.y)), math.floor(unit(logical.y)))
        right = max(width, math.ceil(unit(ink.x + ink.width)))
        bottom = max(1, math.ceil(unit(ink.y + ink.height)), math.ceil(unit(logical.y + logical.height)))
        pixel_width = right - left + PADDING * 2
        pixel_height = bottom - top + PADDING * 2
        if pixel_width > 1200 or pixel_height > 40000:
            raise ValueError('Receipt text exceeds supported image bounds')
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pixel_width, pixel_height)
        drawing = cairo.Context(surface)
        drawing.set_source_rgb(1, 1, 1)
        drawing.paint()
        drawing.set_source_rgb(0, 0, 0)
        origin_x, origin_y = PADDING - left, PADDING - top
        drawing.move_to(origin_x, origin_y)
        pangocairo.update_layout(drawing, layout)
        pangocairo.show_layout(drawing, layout)
        output = io.BytesIO()
        surface.write_to_png(output)
        output.seek(0)
        with Image.open(output) as image:
            pixels = image.convert('L')
        return TextBlock(
            image=pixels,
            origin_x=origin_x,
            baseline=round(unit(layout.get_baseline())) + origin_y,
            lines=layout.get_line_count(),
            unknown_glyphs=pango.pango.pango_layout_get_unknown_glyphs_count(layout.pointer),
        )


def validate_fonts():
    # Fail at startup if a deployment omitted the required fonts.
    sample = render_text('日本語 ひらがな カタカナ العربية 🍋 😂 ❤️ 🙏🏽 👩‍💻 🇯🇵 1️⃣')
    if sample.unknown_glyphs:
        raise RuntimeError('Missing receipt fonts. Deploy the updated Dockerfile and fonts/NotoEmoji.ttf alongside receipt_text.py.')
