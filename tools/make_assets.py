"""Generate the MagTag bitmap assets (run on a computer, not on the MagTag).

    python3 tools/make_assets.py

Writes into the repo root:
  icons.bmp      forecast icons, 10 tiles of 30x24 (4-level grayscale)
  icons_big.bmp  Today-screen icons, 10 tiles of 50x40
  text.bmp       6x13 mixed-case text font, ASCII 32-126 plus a degree sign
  text_inv.bmp   the same font, white on black (home screen header bar)
  today.bmp      Button C "Today" screen background
  signal.bmp     Button D game screen background
  forecast.bmp   footer label updated to "C TODAY" (rest left untouched)

Icons are drawn as vector shapes at 8x resolution, then shrunk and snapped
to the four grays the MagTag e-ink panel can show (0, 85, 170, 255).
That gives smooth curves and even stroke weights instead of hand-placed pixels.

The text font is the public-domain X11 "6x13" fixed font. The script looks for
it at /usr/share/fonts/X11/cyrillic/koi6x13.pcf.gz (Debian/Ubuntu package
xfonts-cyrillic); pass --font PATH to use another copy of 6x13.pcf(.gz).
"""

import argparse
import gzip
import io
import math
import os
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SS = 8  # supersampling factor
LEVELS = (0, 85, 170, 255)
BLACK, DARK, LIGHT, WHITE = LEVELS

# Tile order must match ICON_INDEX in code.py.
ICON_ORDER = (
    "CLEAR", "PARTLY", "CLOUDY", "FOG", "DRIZZLE",
    "RAIN", "SNOW", "STORM", "CLEAR_NIGHT", "PARTLY_NIGHT",
)

# Icons are designed on a 30 x 24 unit grid.
UNIT_W, UNIT_H = 30, 24


# ------------------------------------------------------------------ helpers
def save_bmp(img, name):
    """Save an 'L' image as an 8-bit paletted BMP using the 4-gray palette
    the existing CircuitPython assets use (index 0 = black ... 3 = white)."""
    idx = img.point(lambda v: min(range(4), key=lambda i: abs(LEVELS[i] - v)))
    out = Image.new("P", img.size)
    out.putdata(list(idx.tobytes()))
    pal = []
    for v in LEVELS:
        pal += [v, v, v]
    out.putpalette(pal + [0] * (768 - len(pal)))
    path = os.path.join(ROOT, name)
    out.save(path)
    print("wrote", path, img.size)


def quantize(img, dark_bias=0):
    """Snap to the 4 panel grays. dark_bias pushes edge pixels toward black so
    thin outlines stay solid rather than dissolving into gray."""
    def q(v):
        v = max(0, v - dark_bias) if v < 250 else v
        return min(LEVELS, key=lambda lv: abs(lv - v))
    return img.point(q)


class Pen:
    """Draws in unit coordinates onto a supersampled canvas."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.k = w * SS / UNIT_W
        self.img = Image.new("L", (w * SS, h * SS), WHITE)
        self.d = ImageDraw.Draw(self.img)

    def p(self, v):
        return v * self.k

    def circle(self, cx, cy, r, fill):
        k = self.k
        self.d.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=fill)

    def rrect(self, x0, y0, x1, y1, r, fill):
        k = self.k
        self.d.rounded_rectangle([x0 * k, y0 * k, x1 * k, y1 * k], radius=r * k, fill=fill)

    def line(self, x0, y0, x1, y1, width, fill=BLACK):
        k = self.k
        self.d.line([x0 * k, y0 * k, x1 * k, y1 * k], fill=fill, width=max(1, round(width * k)))
        # round caps
        for x, y in ((x0, y0), (x1, y1)):
            self.circle(x, y, width / 2, fill)

    def poly(self, pts, fill):
        k = self.k
        self.d.polygon([(x * k, y * k) for x, y in pts], fill=fill)

    def finish(self, dark_bias=40):
        small = self.img.resize((self.w, self.h), Image.BOX)
        return quantize(small, dark_bias)


STROKE = 1.7  # outline weight in units (~1.7 px on the small icons)


def cloud(pen, ox, oy, scale=1.0, fill=WHITE, stroke=STROKE):
    """A cloud: three puffs over a flat rounded base. (ox, oy) is the
    bottom-left of the base in units."""
    s = scale
    parts = [
        ("c", ox + 5.0 * s, oy - 4.2 * s, 4.2 * s),
        ("c", ox + 11.5 * s, oy - 7.6 * s, 6.0 * s),
        ("c", ox + 17.6 * s, oy - 4.9 * s, 4.6 * s),
        ("r", ox + 0.8 * s, oy - 6.0 * s, ox + 22.2 * s, oy, 3.0 * s),
    ]
    for grow, color in ((stroke, BLACK), (0, fill)):
        for part in parts:
            if part[0] == "c":
                pen.circle(part[1], part[2], part[3] + grow, color)
            else:
                _, x0, y0, x1, y1, r = part
                pen.rrect(x0 - grow, y0 - grow, x1 + grow, y1 + grow, r + grow, color)


def sun(pen, cx, cy, r, ray_in, ray_out, rays=8, start=0.0, skip=()):
    for i in range(rays):
        if i in skip:
            continue
        a = start + i * 2 * math.pi / rays
        pen.line(cx + ray_in * math.cos(a), cy + ray_in * math.sin(a),
                 cx + ray_out * math.cos(a), cy + ray_out * math.sin(a), STROKE)
    pen.circle(cx, cy, r + STROKE, BLACK)
    pen.circle(cx, cy, r, LIGHT)


def moon(pen, cx, cy, r):
    """Crescent: a disc with an offset disc carved out, outline kept only
    inside the moon so the carving reads as the shadowed edge."""
    k = pen.k
    layer = Pen(pen.w, pen.h)
    layer.circle(cx, cy, r + STROKE, BLACK)
    layer.circle(cx, cy, r, LIGHT)
    ox, oy, orad = cx + r * 0.55, cy - r * 0.45, r * 0.85
    layer.circle(ox, oy, orad + STROKE, BLACK)
    layer.circle(ox, oy, orad, WHITE)
    mask = Image.new("L", pen.img.size, 0)
    ImageDraw.Draw(mask).ellipse([(cx - r - STROKE) * k, (cy - r - STROKE) * k,
                                  (cx + r + STROKE) * k, (cy + r + STROKE) * k], fill=255)
    pen.img.paste(layer.img, (0, 0), mask)


def drops(pen, xs, y0, length, slant=1.4, width=STROKE):
    for x in xs:
        pen.line(x, y0, x - slant, y0 + length, width)


def flake(pen, cx, cy, r):
    w = 1.25
    for a in (90, 30, -30):
        t = math.radians(a)
        dx, dy = r * math.cos(t), r * math.sin(t)
        pen.line(cx - dx, cy - dy, cx + dx, cy + dy, w)


def draw_icon(kind, w, h):
    pen = Pen(w, h)
    if kind == "CLEAR":
        sun(pen, 15, 12, 5.0, 7.6, 10.6, start=math.pi / 8)
    elif kind == "CLEAR_NIGHT":
        moon(pen, 14, 12, 8.2)
        for x, y, r in ((24.5, 5, 1.3), (25.5, 14.5, 0.9), (5, 4, 0.8)):
            pen.line(x - r, y, x + r, y, 0.9)
            pen.line(x, y - r, x, y + r, 0.9)
    elif kind == "PARTLY":
        sun(pen, 10.5, 8.8, 4.2, 6.2, 8.4, start=math.pi / 8, skip=(0, 1, 2))
        cloud(pen, 6.2, 21.5, 0.92)
    elif kind == "PARTLY_NIGHT":
        moon(pen, 10.5, 8.5, 6.2)
        cloud(pen, 6.2, 21.5, 0.92)
    elif kind == "CLOUDY":
        # a second, gray cloud behind gives depth
        cloud(pen, 9.0, 13.5, 0.72, fill=LIGHT)
        cloud(pen, 2.6, 21.5, 1.0)
    elif kind == "FOG":
        cloud(pen, 5.0, 13.0, 0.85, fill=LIGHT)
        for y, x0, x1 in ((16.4, 3, 22), (19.6, 7, 27), (22.6, 2, 19)):
            pen.line(x0, y, x1, y, STROKE)
    elif kind == "DRIZZLE":
        cloud(pen, 3.6, 15.5, 1.0)
        for x, y in ((9, 19.2), (15, 19.2), (21, 19.2), (12, 22.4), (18, 22.4)):
            pen.circle(x, y, 1.05, BLACK)
    elif kind == "RAIN":
        cloud(pen, 3.6, 15.0, 1.0, fill=LIGHT)
        drops(pen, (9.5, 15.5, 21.5), 18.0, 4.6)
    elif kind == "SNOW":
        cloud(pen, 3.6, 15.0, 1.0)
        for cx, cy in ((8.5, 20.0), (15.0, 21.5), (21.5, 20.0)):
            flake(pen, cx, cy, 2.2)
    elif kind == "STORM":
        cloud(pen, 3.6, 14.0, 1.0, fill=LIGHT)
        bolt = [(17.6, 9.5), (9.8, 18.4), (14.2, 18.4), (11.0, 24.0),
                (21.2, 14.6), (16.4, 14.6), (19.6, 9.5)]
        # white halo then black bolt so it reads against the dark cloud
        k = pen.k
        pen.d.line([(x * k, y * k) for x, y in bolt + bolt[:1]], fill=WHITE,
                   width=round(1.6 * k), joint="curve")
        pen.poly(bolt, BLACK)
    else:
        raise ValueError(kind)
    return pen.finish()


def icon_sheet(w, h, name):
    sheet = Image.new("L", (w * len(ICON_ORDER), h), WHITE)
    for i, kind in enumerate(ICON_ORDER):
        sheet.paste(draw_icon(kind, w, h), (i * w, 0))
    save_bmp(sheet, name)
    return sheet


# --------------------------------------------------------------------- font
def load_pcf(path):
    from PIL.PcfFontFile import PcfFontFile
    raw = open(path, "rb").read()
    if path.endswith(".gz"):
        raw = gzip.decompress(raw)
    pcf = PcfFontFile(io.BytesIO(raw), "iso8859-1")
    pcf.compile()
    return pcf


DEGREE = [  # 6x13 degree sign, rows 0-12
    "......", "..##..", ".#..#.", ".#..#.", "..##..",
] + ["......"] * 8


def font_sheet(path, name):
    pcf = load_pcf(path)
    cw, ch = 6, 13
    chars = [chr(c) for c in range(32, 127)]
    sheet = Image.new("L", (cw * (len(chars) + 1), ch), WHITE)
    from PIL import ImageFont
    tmp = os.path.join(ROOT, "tools", "_6x13")
    pcf.save(tmp)
    font = ImageFont.load(tmp + ".pil")
    d = ImageDraw.Draw(sheet)
    for i, c in enumerate(chars):
        d.text((i * cw, 0), c, font=font, fill=BLACK)
    for ext in (".pil", ".pbm"):
        try:
            os.remove(tmp + ext)
        except OSError:
            pass
    x = len(chars) * cw
    for y, row in enumerate(DEGREE):
        for dx, px in enumerate(row):
            if px == "#":
                sheet.putpixel((x + dx, y), BLACK)
    save_bmp(sheet, name)
    return sheet


# --------------------------------------------------------------- backgrounds
def frame_from(src):
    """Copy a screen's outer frame and header rule, wiping its contents."""
    img = Image.open(os.path.join(ROOT, src)).convert("L")
    d = ImageDraw.Draw(img)
    d.rectangle([5, 5, 290, 25], fill=WHITE)   # header text
    d.rectangle([5, 27, 290, 124], fill=WHITE)  # body and old footer text
    d.line([4, 123, 291, 123], fill=LIGHT)      # restore inner gray rule
    return img


def today_background():
    img = frame_from("forecast.bmp")
    d = ImageDraw.Draw(img)
    d.line([61, 30, 61, 120], fill=LIGHT)        # left panel divider
    d.line([66, 96, 287, 96], fill=LIGHT)        # narrative / stats rule
    save_bmp(img, "today.bmp")


def signal_background():
    """Button D game screen: frame, header rule, and a rule above the
    A/B/C/D color labels that code.py prints along the bottom."""
    img = frame_from("forecast.bmp")
    d = ImageDraw.Draw(img)
    d.line([8, 103, 287, 103], fill=LIGHT)
    for x in (74, 148, 222):                     # dividers between labels
        d.line([x, 106, x, 119], fill=LIGHT)
    save_bmp(img, "signal.bmp")


def forecast_footer(font_img):
    """Repaint the forecast footer cleanly with the new button names."""
    img = Image.open(os.path.join(ROOT, "forecast.bmp")).convert("L")
    d = ImageDraw.Draw(img)
    d.rectangle([5, 110, 290, 124], fill=WHITE)  # column rules now end at y109
    d.line([4, 123, 291, 123], fill=LIGHT)
    # cap-height rows of the 6x13 font, set between the rules' end and y123
    label = "A HOME   B 5-DAY   C TODAY   D GAME"
    x = 10
    for c in label:
        i = ord(c) - 32
        glyph = font_img.crop((i * 6, 2, i * 6 + 6, 11))   # rows 2-10 = cap height
        mask = glyph.point(lambda v: 255 if v < 128 else 0)
        img.paste(Image.new("L", glyph.size, DARK), (x, 113), mask)
        x += 6
    save_bmp(img, "forecast.bmp")


def preview(sheets, path):
    """Enlarged preview PNG for checking the art on a computer."""
    pad = 6
    width = max(s.width for s in sheets) * 4 + pad * 2
    height = sum(s.height * 4 + pad for s in sheets) + pad
    out = Image.new("L", (width, height), 230)
    y = pad
    for s in sheets:
        out.paste(s.resize((s.width * 4, s.height * 4), Image.NEAREST), (pad, y))
        y += s.height * 4 + pad
    out.save(path)
    print("wrote", path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--font", default="/usr/share/fonts/X11/cyrillic/koi6x13.pcf.gz")
    ap.add_argument("--preview", help="optional PNG path for an enlarged preview")
    ap.add_argument("--skip-forecast", action="store_true",
                    help="do not touch forecast.bmp")
    args = ap.parse_args()

    small = icon_sheet(30, 24, "icons.bmp")
    big = icon_sheet(50, 40, "icons_big.bmp")
    if os.path.exists(args.font):
        font = font_sheet(args.font, "text.bmp")
        save_bmp(font.point(lambda v: 255 - v), "text_inv.bmp")
    else:
        print("font not found, keeping existing text.bmp:", args.font)
        font = Image.open(os.path.join(ROOT, "text.bmp")).convert("L")
    today_background()
    signal_background()
    if not args.skip_forecast:
        forecast_footer(font)
    if args.preview:
        preview([small, big], args.preview)


if __name__ == "__main__":
    sys.exit(main())
