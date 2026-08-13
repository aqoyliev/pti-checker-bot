"""Generate the PTI-bot logo in the Gurman Trucking visual language.

Design language borrowed from the parent mark:
  * one very thick stroke with round caps/joins
  * a thin negative-space "groove" running down its centre
  * two dark-red dots set diagonally (the umlaut of GURMAN)
  * coral field, slate/sage typography

Everything is drawn at 4x and downsampled, so the round caps stay clean.
"""

from __future__ import annotations

import os
from PIL import Image, ImageDraw, ImageFont

SS = 4  # supersample factor
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo")
os.makedirs(OUT, exist_ok=True)

CORAL_LIGHT = (247, 96, 68)
CORAL = (244, 67, 46)
CORAL_DEEP = (232, 52, 38)
DOT_DARK = (201, 39, 29)
DOT_ON_CORAL = (194, 34, 24)
SLATE = (76, 95, 108)
SAGE = (143, 165, 163)
WHITE = (255, 255, 255)

# --- geometry, expressed in a 512 x 512 design space -------------------------
CHECK = [(126.0, 266.0), (212.0, 354.0), (386.0, 152.0)]
STROKE = 78.0
GROOVE = 13.0
GROOVE_INSET = 26.0  # groove stops short of the caps, like the parent S
DOT_R = 33.0
DOTS = [(146.0, 142.0), (414.0, 356.0)]

# ink extent of the whole mark (check stroke + both dots), design space
MARK_BBOX = (87.0, 109.0, 447.0, 393.0)


# --- helpers -----------------------------------------------------------------
def _shorten(pts, inset):
    """Pull a polyline in from both ends by `inset` px along its own direction."""

    def step(a, b, d):
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = (dx * dx + dy * dy) ** 0.5
        return (a[0] + dx / n * d, a[1] + dy / n * d)

    out = list(pts)
    out[0] = step(out[0], out[1], inset)
    out[-1] = step(out[-1], out[-2], inset)
    return out


def stroke_mask(size, pts, width, scale=1.0, offset=(0.0, 0.0)):
    """Polyline with genuinely round caps and joins, rendered into an L mask."""
    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    p = [(x * scale + offset[0], y * scale + offset[1]) for x, y in pts]
    w = width * scale
    d.line(p, fill=255, width=int(round(w)), joint="curve")
    r = w / 2.0
    for x, y in p:  # round the caps and firm up the joins
        d.ellipse([x - r, y - r, x + r, y + r], fill=255)
    return m


def gradient(size, c0, c1, steps=128):
    """Diagonal coral gradient: light top-left, deep bottom-right.

    Built small and scaled up — a linear ramp survives interpolation exactly,
    and a per-pixel loop over a 2048px canvas does not survive impatience.
    """
    small = Image.new("RGB", (steps, steps))
    px = small.load()
    for y in range(steps):
        for x in range(steps):
            t = (x + y) / (2.0 * (steps - 1))
            px[x, y] = tuple(int(round(a + (b - a) * t)) for a, b in zip(c0, c1))
    return small.resize(size, Image.BICUBIC)


def subtract(base, cut):
    from PIL import ImageChops

    return ImageChops.subtract(base, cut)


def save(img, name, size):
    img.resize((size, size), Image.LANCZOS).save(os.path.join(OUT, name))
    print("wrote", name)


def mark_masks(size, scale, offset, with_groove=True):
    """The check as (solid mask, mask with the groove cut out)."""
    solid = stroke_mask(size, CHECK, STROKE, scale, offset)
    if not with_groove:
        return solid, solid
    groove = stroke_mask(
        size, _shorten(CHECK, GROOVE_INSET), GROOVE, scale, offset
    )
    return solid, subtract(solid, groove)


def dot_mask(size, scale, offset):
    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    for x, y in DOTS:
        cx, cy = x * scale + offset[0], y * scale + offset[1]
        r = DOT_R * scale
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    return m


# --- 1. avatar: white check knocked out of a full-bleed coral disc ------------
def build_avatar(square=False):
    n = 512 * SS
    size = (n, n)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))

    field = gradient(size, CORAL_LIGHT, CORAL_DEEP).convert("RGBA")
    shape = Image.new("L", size, 0)
    d = ImageDraw.Draw(shape)
    if square:
        d.rounded_rectangle([0, 0, n - 1, n - 1], radius=int(n * 0.235), fill=255)
    else:
        d.ellipse([0, 0, n - 1, n - 1], fill=255)
    canvas.paste(field, (0, 0), shape)

    # the mark sits a touch smaller than the disc so it survives the circle crop
    scale = SS * 0.86
    offset = (n * 0.5 - 256 * scale - 10 * SS, n * 0.5 - 256 * scale + 6 * SS)

    dots = dot_mask(size, scale, offset)
    canvas.paste(Image.new("RGBA", size, DOT_ON_CORAL + (255,)), (0, 0), dots)

    _, cut = mark_masks(size, scale, offset)
    canvas.paste(Image.new("RGBA", size, WHITE + (255,)), (0, 0), cut)
    return canvas


# --- 2. icon: coral check, groove is real negative space ---------------------
def build_icon(on_white=False):
    n = 512 * SS
    size = (n, n)
    bg = WHITE + (255,) if on_white else (0, 0, 0, 0)
    canvas = Image.new("RGBA", size, bg)

    scale = SS * 0.96
    offset = (n * 0.5 - 256 * scale - 8 * SS, n * 0.5 - 256 * scale + 4 * SS)

    dots = dot_mask(size, scale, offset)
    canvas.paste(Image.new("RGBA", size, DOT_DARK + (255,)), (0, 0), dots)

    field = gradient(size, CORAL_LIGHT, CORAL_DEEP).convert("RGBA")
    _, cut = mark_masks(size, scale, offset)
    canvas.paste(field, (0, 0), cut)
    return canvas


# --- 3. horizontal lockup ----------------------------------------------------
def _font(name, px):
    for candidate in (name, os.path.join("C:\\Windows\\Fonts", name)):
        try:
            return ImageFont.truetype(candidate, px)
        except OSError:
            continue
    return ImageFont.load_default()


def _tracked(draw, xy, text, font, fill, tracking):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking
    return x - tracking


def _tracked_width(draw, text, font, tracking):
    return sum(draw.textlength(c, font=font) for c in text) + tracking * (len(text) - 1)


def build_lockup(dark=False):
    W, H = 1600, 520
    w, h = W * SS, H * SS
    ground = (35, 45, 52) if dark else WHITE
    canvas = Image.new("RGBA", (w, h), ground + (255,))
    d = ImageDraw.Draw(canvas)

    # --- mark, flush left, 300 design-px tall, vertically centred
    x0, y0, _, y1 = MARK_BBOX
    scale = (300.0 / (y1 - y0)) * SS
    offset = (90 * SS - x0 * scale, h * 0.5 - ((y0 + y1) / 2.0) * scale)

    canvas.paste(
        Image.new("RGBA", (w, h), DOT_DARK + (255,)),
        (0, 0),
        dot_mask((w, h), scale, offset),
    )
    _, cut = mark_masks((w, h), scale, offset)
    canvas.paste(
        gradient((w, h), CORAL_LIGHT, CORAL_DEEP).convert("RGBA"), (0, 0), cut
    )

    # --- type block, optically centred against the mark
    big = _font("seguibl.ttf", 128 * SS)
    small = _font("seguisb.ttf", 46 * SS)
    line1, track1 = "PTI CHECKER", 2 * SS
    line2, track2 = "GÜRMAN TRUCKING INC", 11 * SS

    tx = 560 * SS
    cap = d.textbbox((0, 0), "H", font=big)
    cap_h = cap[3] - cap[1]
    x_h = d.textbbox((0, 0), "H", font=small)
    small_h = x_h[3] - x_h[1]
    gap = 34 * SS
    block = cap_h + gap + small_h
    top = h * 0.5 - block / 2.0

    _tracked(d, (tx, top - cap[1]), line1, big, WHITE if dark else SLATE, track1)
    _tracked(
        d,
        (tx + 4 * SS, top + cap_h + gap - x_h[1]),
        line2,
        small,
        SAGE,
        track2,
    )

    # trim so the right margin matches the left one instead of trailing off
    from PIL import ImageChops

    ink = ImageChops.difference(
        canvas.convert("RGB"), Image.new("RGB", (w, h), ground)
    ).getbbox()
    if ink:
        canvas = canvas.crop((0, 0, min(w, ink[2] + 90 * SS), h))
    return canvas


if __name__ == "__main__":
    save(build_avatar(), "pti-bot-avatar-512.png", 512)
    save(build_avatar(square=True), "pti-bot-appicon-512.png", 512)
    save(build_icon(), "pti-bot-icon-512.png", 512)
    save(build_icon(on_white=True), "pti-bot-icon-white-512.png", 512)
    save(build_avatar(), "pti-bot-avatar-128.png", 128)
    save(build_avatar(), "pti-bot-avatar-48.png", 48)

    for name, img in (
        ("pti-bot-lockup.png", build_lockup()),
        ("pti-bot-lockup-dark.png", build_lockup(dark=True)),
    ):
        img.resize((img.width // SS, img.height // SS), Image.LANCZOS).save(
            os.path.join(OUT, name)
        )
        print("wrote", name)
