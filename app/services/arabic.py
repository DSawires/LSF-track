"""Arabic on paper.

A PDF draws glyphs left to right, exactly as given. Arabic needs two things done
to it first: every letter takes the form its neighbours call for (shaping), and
the line is put into the order it is *seen* in, right to left, with any numbers
and Latin words inside it still reading left to right (bidi). reportlab can do
both only with extra native libraries that are not published where we install
from, so this does it in Python: `arabic-reshaper` picks the letter forms,
`python-bidi` orders the line.

That has one consequence worth knowing. A line already in visual order cannot be
re-wrapped by anyone downstream -- break it and the first words land on the last
line -- so the wrapping is done here, on the logical text, before shaping
(`wrap`). Callers get finished lines and must draw them as they are.

The font is IBM Plex Sans Arabic (SIL Open Font License, app/assets/fonts). It
carries Latin as well, so one font draws a mixed line, and it has the Arabic
presentation forms the reshaper produces, which many Arabic fonts do not.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

_FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"

REGULAR = "LSF-Arabic"
BOLD = "LSF-Arabic-Bold"
_FILES = {REGULAR: "IBMPlexSansArabic-Regular.ttf", BOLD: "IBMPlexSansArabic-Bold.ttf"}

# The one ligature the reshaper makes by default that is a single glyph a font
# may not have.
_ALLAH = 0xFDF2


def has_arabic(text: str | None) -> bool:
    """Any character from the Arabic blocks, presentation forms included."""
    for char in text or "":
        code = ord(char)
        if (
            0x0600 <= code <= 0x06FF
            or 0x0750 <= code <= 0x077F
            or 0x08A0 <= code <= 0x08FF
            or 0xFB50 <= code <= 0xFDFF
            or 0xFE70 <= code <= 0xFEFF
        ):
            return True
    return False


@lru_cache
def _ready() -> tuple[frozenset[int], arabic_reshaper.ArabicReshaper]:
    """Register the two faces once, and build a reshaper that only makes
    ligatures the font can draw."""
    glyphs: set[int] | None = None
    for name, filename in _FILES.items():
        font = TTFont(name, str(_FONTS / filename))
        pdfmetrics.registerFont(font)
        have = set(font.face.charToGlyph)
        glyphs = have if glyphs is None else glyphs & have
    assert glyphs is not None
    reshaper = arabic_reshaper.ArabicReshaper(
        configuration={"ARABIC LIGATURE ALLAH": _ALLAH in glyphs}
    )
    return frozenset(glyphs), reshaper


def font_for(base_font: str) -> str:
    """The Arabic face standing in for one of the built-in ones."""
    _ready()
    return BOLD if "Bold" in base_font else REGULAR


def is_rtl(text: str) -> bool:
    """Does the paragraph read right to left? Decided the way the bidi rules
    decide it: by its first letter that has a direction at all."""
    for char in text:
        if has_arabic(char):
            return True
        if char.isalpha():
            return False
    return False


def visual(text: str, rtl: bool | None = None) -> str:
    """One logical line, shaped and in the order it is drawn. Anything the font
    has no glyph for becomes '?': a visibly wrong character gets fixed, a
    missing one does not."""
    glyphs, reshaper = _ready()
    if rtl is None:
        rtl = is_rtl(text)
    shaped = reshaper.reshape(text)
    line = get_display(shaped, base_dir="R" if rtl else "L")
    return "".join(
        char if (ord(char) in glyphs or char.isspace()) else "?" for char in line
    )


def width_of(line: str, font: str, size: float) -> float:
    return pdfmetrics.stringWidth(line, font, size)


def wrap(
    text: str, font: str, size: float, width: float | None, rtl: bool | None = None
) -> list[str]:
    """The text as finished lines that each fit `width` (points), top to bottom.

    Line breaks in the text are kept. With no width, each of those is one line.
    A single word wider than the column is left to overflow rather than be cut
    in the middle of a joined letter. `rtl` forces the reading direction (an
    Arabic sheet reads right to left even where a line starts with a code);
    left unset, each paragraph decides by its own first letter.
    """
    _ready()
    out: list[str] = []
    for paragraph in (text or "").split("\n"):
        words = paragraph.split()
        if not words:
            out.append("")
            continue
        direction = is_rtl(paragraph) if rtl is None else rtl
        line: list[str] = []
        for word in words:
            if line and width is not None:
                trial = visual(" ".join([*line, word]), direction)
                if width_of(trial, font, size) > width:
                    out.append(visual(" ".join(line), direction))
                    line = []
            line.append(word)
        out.append(visual(" ".join(line), direction))
    return out
