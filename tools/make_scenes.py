"""Generate the weather views for the home screen window (run on a computer).

    python3 tools/make_scenes.py [--preview out.png]

The home screen art (home.bmp) shows M-01 looking out a window at a rainy
future city. This script keeps every foreground pixel of that art (robot,
plant, window frame, desk, mug) and repaints only the outdoor view behind
them, once per weather type. The results are stacked into scenes.bmp, one
tile per weather, each covering just the window area; code.py draws the
matching tile over home.bmp.

The original rainy view is kept, untouched, as the RAIN tile.
"""

import argparse
import math
import os
import random

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEVELS = (0, 85, 170, 255)
BLACK, DARK, LIGHT, WHITE = LEVELS

# Window area on home.bmp (inclusive). Must match SCENE_X/SCENE_Y in code.py.
X0, Y0, X1, Y1 = 28, 22, 161, 100
W, H = X1 - X0 + 1, Y1 - Y0 + 1

# Tile order must match SCENE_INDEX in code.py.
SCENES = ("CLEAR", "PARTLY", "CLOUDY", "FOG", "RAIN", "SNOW", "STORM",
          "NIGHT", "NIGHT_PARTLY", "NIGHT_CLOUDY")

BAYER = (
    (0, 8, 2, 10),
    (12, 4, 14, 6),
    (3, 11, 1, 9),
    (15, 7, 13, 5),
)


def tone(v, x, y):
    """Ordered-dither a 0-255 value onto the panel's four gray levels."""
    v = max(0, min(255, v))
    lo = min(3, int(v // 85))
    if lo == 3:
        return WHITE
    frac = (v - lo * 85) / 85.0
    return LEVELS[lo + 1] if frac * 16 > BAYER[y % 4][x % 4] + 0.5 else LEVELS[lo]


# ----------------------------------------------------------------- the mask
def point_in_poly(x, y, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


# Generous outline around M-01 (antenna, head, body). Pixels inside it that
# are reachable from its edge without crossing the robot's dark outline are
# sky; everything else inside is robot.
ROBOT = [(43, 48), (55, 48), (55, 58), (60, 58), (72, 57), (81, 60), (89, 64),
         (97, 67), (101, 72), (101, 94), (97, 98), (95, 101), (29, 101),
         (29, 83), (31, 75), (34, 70), (40, 66), (44, 63), (46, 59)]
# Underside of the dark arch at the window's top-left corner.
ARCH = [(27, 21), (27, 34), (44, 34), (50, 31), (55, 29), (59, 27), (62, 24), (64, 21)]


def foreground_mask(home):
    px = home.load()
    fg = [[False] * W for _ in range(H)]
    for y in range(Y0, Y1 + 1):
        for x in range(X0, X1 + 1):
            if point_in_poly(x + 0.5, y + 0.5, ARCH):
                fg[y - Y0][x - X0] = True
    inside = set()
    for y in range(Y0, Y1 + 1):
        for x in range(X0, X1 + 1):
            if point_in_poly(x + 0.5, y + 0.5, ROBOT):
                inside.add((x, y))
    # flood from the polygon's edge through light pixels
    sky = set()
    stack = []
    for (x, y) in inside:
        edge = any((x + dx, y + dy) not in inside
                   for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
        if edge and px[x, y] > DARK and y < Y1:
            stack.append((x, y))
    while stack:
        p = stack.pop()
        if p in sky:
            continue
        sky.add(p)
        x, y = p
        for q in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if q in inside and q not in sky and px[q] > DARK:
                stack.append(q)
    for (x, y) in inside:
        if (x, y) not in sky:
            fg[y - Y0][x - X0] = True
    # isolated dark sky pixels left right next to the robot: treat as sky
    for (x, y) in inside:
        if fg[y - Y0][x - X0] and px[x, y] <= DARK:
            n = [(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))]
            if sum(1 for q in n if q in sky) >= 3:
                fg[y - Y0][x - X0] = False
    # the window sill row and the mug rim belong to the room
    for x in range(X0, X1 + 1):
        if px[x, Y1] <= DARK and x > 100:
            fg[Y1 - Y0][x - X0] = True
    return fg


# ------------------------------------------------------------- the outdoors
# The view is built in two steps. First the city's geometry is laid out once
# as a grid of materials (sky, far building, near building, window, ...).
# Then each weather paints those materials with its own grays, adds its sky
# and effects, and the result is dithered down to the panel's four grays.

SKY, FAR, FARWIN, NEAR, SIDE, WIN, EDGE, ROOF, GROUND, TRACK, CAR, BEACON, DECK, DECKLIGHT = range(14)
HORIZON = 92


def hash01(*args):
    h = 2166136261
    for a in args:
        h = ((h ^ (a & 0xFFFFFFFF)) * 16777619) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 1274126177) & 0xFFFFFFFF
    return (h & 0xFFFF) / 65535.0


def noise(x, y, seed=0):
    """Smooth value noise, roughly 0..1."""
    xi, yi = int(math.floor(x)), int(math.floor(y))
    fx, fy = x - xi, y - yi
    fx, fy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
    a = hash01(xi, yi, seed)
    b = hash01(xi + 1, yi, seed)
    c = hash01(xi, yi + 1, seed)
    d = hash01(xi + 1, yi + 1, seed)
    return (a + (b - a) * fx) + ((c + (d - c) * fx) - (a + (b - a) * fx)) * fy


def fbm(x, y, seed=0):
    return 0.6 * noise(x, y, seed) + 0.3 * noise(x * 2.1, y * 2.1, seed + 1) + 0.1 * noise(x * 4.3, y * 4.3, seed + 2)


class City:
    def __init__(self):
        self.m = {}

    def put(self, x, y, mat):
        if X0 <= x <= X1 and Y0 <= y <= Y1:
            self.m[(x, y)] = mat

    def get(self, x, y):
        return self.m.get((x, y), SKY)

    def far(self, x0, x1, top):
        for y in range(top, HORIZON + 1):
            for x in range(x0, x1 + 1):
                win = (y - top) % 3 == 2 and (x - x0) % 2 == 1 and x0 < x < x1 and y < HORIZON - 1
                self.put(x, y, FARWIN if win else FAR)

    def block(self, x0, x1, top, bottom=HORIZON, windows=True, outline=True):
        for y in range(top, bottom + 1):
            for x in range(x0, x1 + 1):
                mat = NEAR
                if x >= x1 - 1:
                    mat = SIDE
                if windows and (y - top) % 3 == 2 and x0 + 1 < x < x1 - 1 and (x - x0) % 2 == 0 and y < bottom - 1:
                    mat = WIN
                self.put(x, y, mat)
        if outline:
            for y in range(top, bottom + 1):
                self.put(x0, y, EDGE)
                self.put(x1, y, EDGE)
            for x in range(x0, x1 + 1):
                self.put(x, top, ROOF)

    def mast(self, x, y0, y1, beacon=True):
        for y in range(y0, y1 + 1):
            self.put(x, y, EDGE)
        if beacon:
            self.put(x, y0 - 1, BEACON)


def build_city():
    c = City()
    # Far skyline: a dense band of towers across the whole view.
    x = X0
    i = 0
    while x < X1:
        w = 4 + int(hash01(i, 1) * 6)
        top = 70 + int(hash01(i, 2) * 14)
        c.far(x, min(X1, x + w), top)
        x += w + (1 if hash01(i, 3) < 0.5 else 0)
        i += 1
    # Tall far towers that peek over M-01's head.
    c.far(34, 40, 47)
    c.mast(37, 40, 46)
    c.far(78, 84, 54)
    c.far(88, 92, 58)
    c.far(60, 65, 52)
    c.mast(62, 45, 51, beacon=False)
    # Near towers on the right.
    c.block(101, 110, 70)                       # low block
    c.block(103, 108, 64, 70)
    # needle tower with an observation deck
    for y in range(52, HORIZON + 1):
        half = 2 + (y - 52) * 4 // (HORIZON - 52)
        for x in range(118 - half, 119 + half):
            mat = SIDE if x >= 118 + half - 1 else NEAR
            if (y - 52) % 4 == 3 and x not in (118 - half, 118 + half) and x % 2 == 0:
                mat = WIN
            c.put(x, y, mat)
        c.put(118 - half, y, EDGE)
        c.put(118 + half, y, EDGE)
    for x in range(110, 127):                   # deck
        c.put(x, 48, EDGE)
        c.put(x, 49, DECK)
        c.put(x, 50, DECKLIGHT if x % 3 == 0 else DECK)
        c.put(x, 51, EDGE)
    for x in range(114, 123):
        c.put(x, 47, ROOF)
    c.mast(118, 30, 46)
    # stepped skyscraper
    c.block(128, 140, 68)
    c.block(130, 138, 57, 68)
    c.block(132, 136, 48, 57)
    c.mast(134, 40, 47)
    # twin towers joined by skybridges, with the hover platform
    c.block(143, 149, 50)
    c.block(152, 158, 58)
    for x in range(149, 153):
        c.put(x, 63, EDGE)
        c.put(x, 64, NEAR)
        c.put(x, 65, EDGE)
        c.put(x, 74, EDGE)
        c.put(x, 75, EDGE)
    for x in range(140, 162):                   # platform
        c.put(x, 42, ROOF)
        c.put(x, 43, DECK)
        c.put(x, 44, EDGE)
    for x in range(142, 161, 4):
        c.put(x, 45, DECKLIGHT)
    for y in range(45, 50):
        c.put(146, y, EDGE)
    # monorail
    for x in range(100, X1 + 1):
        c.put(x, 83, TRACK)
    for x in range(104, X1 + 1, 14):
        for y in range(84, HORIZON + 1):
            c.put(x, y, TRACK)
    for x in range(139, 152):
        c.put(x, 81, CAR)
        c.put(x, 82, CAR)
    for x in range(140, 151, 2):
        c.put(x, 81, DECKLIGHT)
    # hover cars
    for (cx, cy) in ((92, 44), (110, 37), (70, 40)):
        for dx in range(5):
            c.put(cx + dx, cy, CAR)
        for dx in (1, 2, 3):
            c.put(cx + dx, cy - 1, CAR)
    # ground
    for y in range(HORIZON + 1, Y1 + 1):
        for x in range(X0, X1 + 1):
            c.put(x, y, GROUND)
    return c


CITY = None


def city_geometry():
    global CITY
    if CITY is None:
        CITY = build_city()
    return CITY


# Grays for each material, per lighting.
DAY = {FAR: 178, FARWIN: 178, NEAR: 118, SIDE: 80, WIN: 205, EDGE: 0, ROOF: 0,
       GROUND: 150, TRACK: 0, CAR: 0, BEACON: 0, DECK: 85, DECKLIGHT: 170}
OVERCAST = dict(DAY, FAR=160)
OVERCAST[FAR] = 158
OVERCAST[FARWIN] = 158
OVERCAST[GROUND] = 135
NIGHT = {FAR: 22, FARWIN: 22, NEAR: 0, SIDE: 0, WIN: 0, EDGE: 0, ROOF: 0,
         GROUND: 20, TRACK: 0, CAR: 170, BEACON: 255, DECK: 30, DECKLIGHT: 255}


class Scene:
    def __init__(self):
        self.v = [[255.0] * 296 for _ in range(128)]

    def set(self, x, y, val):
        if X0 <= x <= X1 and Y0 <= y <= Y1:
            self.v[y][x] = val

    def get(self, x, y):
        return self.v[y][x]

    def sky(self, fn):
        for y in range(Y0, Y1 + 1):
            for x in range(X0, X1 + 1):
                self.v[y][x] = fn(x, y)

    def city(self, palette, night=False, fog=0.0, snow=False):
        geo = city_geometry()
        for (x, y), mat in geo.m.items():
            val = palette[mat]
            if night and mat == WIN:
                r = hash01(x, y, 9)
                val = 255 if r < 0.35 else (170 if r < 0.5 else palette[NEAR])
            if night and mat == FARWIN:
                val = 170 if hash01(x, y, 4) < 0.25 else palette[FAR]
            if snow and mat == ROOF:
                val = 255
            if snow and mat == GROUND:
                val = 255
            if fog:
                depth = 1.0 if mat in (FAR, FARWIN) else 0.62
                if mat == GROUND:
                    depth = 0.75
                val = val + (245 - val) * min(1.0, fog * depth)
            self.v[y][x] = val

    def disc(self, cx, cy, r, val):
        for y in range(int(cy - r - 1), int(cy + r + 2)):
            for x in range(int(cx - r - 1), int(cx + r + 2)):
                if (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= r * r:
                    self.set(x, y, val)

    def sun(self, cx, cy):
        for i in range(8):
            a = i * math.pi / 4 + math.pi / 8
            for t in (9.5, 10.5, 11.5):
                self.set(int(round(cx + t * math.cos(a))), int(round(cy + t * math.sin(a))), 0)
        self.disc(cx, cy, 7.4, 0)
        self.disc(cx, cy, 6.1, 255)
        for y in range(cy - 7, cy + 8):
            for x in range(cx - 7, cx + 8):
                d = (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2
                if d <= 6.1 ** 2 and (x - cx) + (y - cy) > 2:
                    self.set(x, y, 215)

    def moon(self, cx, cy):
        r = 7.0
        for y in range(cy - 8, cy + 9):
            for x in range(cx - 8, cx + 9):
                d1 = (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2
                d2 = (x + 0.5 - cx - 4) ** 2 + (y + 0.5 - cy + 3) ** 2
                if d1 <= r * r and d2 > 5.6 ** 2:
                    inner = d1 <= (r - 1.3) ** 2 and d2 > 6.9 ** 2
                    self.set(x, y, 255 if inner else 170)

    def stars(self, count, ymax):
        rng = random.Random(3)
        geo = city_geometry()
        for _ in range(count):
            x, y = rng.randint(X0 + 1, X1 - 1), rng.randint(Y0 + 1, ymax)
            if geo.get(x, y) == SKY and self.get(x, y) < 130:
                self.set(x, y, 255 if rng.random() < 0.55 else 170)

    def cloud(self, cx, cy, w, h, body, shade, edge, seed=0):
        """Cumulus: overlapping puffs over a flat base, shaded underneath."""
        n = max(3, int(w // 7))
        puffs = []
        for i in range(n):
            px = cx - w / 2 + (i + 0.5) * w / n
            bump = math.sin(math.pi * (i + 0.5) / n)
            r = h * (0.45 + 0.55 * bump) * (0.85 + 0.3 * hash01(i, seed))
            puffs.append((px, cy - r * 0.4, r))
        inside = set()
        for y in range(int(cy - h * 1.6), int(cy) + 1):
            for x in range(int(cx - w / 2 - h), int(cx + w / 2 + h) + 1):
                for (px, py, r) in puffs:
                    if (x + 0.5 - px) ** 2 + (y + 0.5 - py) ** 2 <= r * r:
                        inside.add((x, y))
                        break
        top = min(y for _, y in inside) if inside else cy
        for (x, y) in inside:
            if any((x + dx, y + dy) not in inside for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                if edge is not None:
                    self.set(x, y, edge)
                    continue
            t = (y - top) / max(1.0, cy - top)
            self.set(x, y, body + (shade - body) * max(0.0, t - 0.45) / 0.55)

    def rain_bolt(self, x, y):
        pts = [(x + 14, y), (x + 6, y + 14), (x + 11, y + 14), (x + 4, y + 30),
               (x + 18, y + 11), (x + 12, y + 11), (x + 19, y)]
        img = Image.new("L", (296, 128), 0)
        d = ImageDraw.Draw(img)
        d.polygon([(px + 0.5, py + 0.5) for px, py in pts], fill=1)
        inner = Image.new("L", (296, 128), 0)
        ImageDraw.Draw(inner).polygon(
            [(x + 14.5, y + 1.5), (x + 8, y + 13), (x + 12.5, y + 13), (x + 7, y + 25),
             (x + 16, y + 12), (x + 10.5, y + 12), (x + 17.5, y + 1.5)], fill=1)
        for py in range(128):
            for px in range(296):
                if img.getpixel((px, py)):
                    self.set(px, py, 255 if inner.getpixel((px, py)) else 0)

    def to_image(self):
        img = Image.new("L", (296, 128), 255)
        p = img.load()
        for y in range(128):
            for x in range(296):
                p[x, y] = tone(self.v[y][x], x, y)
        return img


def gradient(top, bottom):
    return lambda x, y: top + (bottom - top) * (y - Y0) / (HORIZON - Y0)


def overcast(base, depth, seed, darker_top=20):
    def fn(x, y):
        n = fbm(x / 18.0, y / 6.0, seed)
        return base - darker_top * (1 - (y - Y0) / (HORIZON - Y0)) + (n - 0.5) * depth
    return fn


def make_scene(kind, home):
    if kind in ("RAIN", "STORM"):
        img = home.copy()
        if kind == "STORM":
            s = Scene()
            s.v = [[float(home.getpixel((x, y))) for x in range(296)] for y in range(128)]
            s.rain_bolt(64, 24)
            # flash: brighten the sky band around the bolt a touch
            img = s.to_image()
        return img
    s = Scene()
    if kind == "CLEAR":
        s.sky(gradient(236, 255))
        s.sun(76, 36)
        s.city(DAY)
    elif kind == "PARTLY":
        s.sky(gradient(200, 240))
        s.sun(70, 34)
        s.cloud(92, 45, 32, 8, 255, 200, 85, seed=1)
        s.cloud(142, 33, 30, 7, 255, 200, 85, seed=2)
        s.cloud(46, 31, 18, 5, 255, 205, 85, seed=3)
        s.city(DAY)
    elif kind == "CLOUDY":
        s.sky(overcast(195, 70, 5))
        s.cloud(66, 34, 44, 9, 225, 150, None, seed=4)
        s.cloud(128, 30, 52, 10, 220, 140, None, seed=5)
        s.city(OVERCAST)
    elif kind == "FOG":
        s.sky(overcast(232, 25, 6, darker_top=0))
        s.city(DAY, fog=0.9)
        for y in range(Y0, Y1 + 1):          # drifting bands of thicker fog
            for x in range(X0, X1 + 1):
                band = math.sin((y + 6 * math.sin(x / 17.0)) / 4.0)
                if band > 0.55:
                    s.v[y][x] = s.v[y][x] + (250 - s.v[y][x]) * 0.7
    elif kind == "SNOW":
        s.sky(overcast(200, 40, 7))
        s.city(OVERCAST, snow=True)
        rng = random.Random(11)
        for _ in range(140):
            x, y = rng.randint(X0, X1), rng.randint(Y0, Y1 - 2)
            big = rng.random() < 0.25
            s.set(x, y, 255)
            if big:
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    s.set(x + dx, y + dy, 255)
    elif kind in ("NIGHT", "NIGHT_PARTLY", "NIGHT_CLOUDY"):
        if kind == "NIGHT_CLOUDY":
            s.sky(overcast(75, 50, 8, darker_top=25))
            s.cloud(66, 34, 44, 9, 95, 45, None, seed=6)
            s.cloud(128, 30, 52, 10, 90, 40, None, seed=7)
        else:
            s.sky(gradient(45, 100))
            s.stars(55, 75)
            s.moon(78, 34)
            if kind == "NIGHT_PARTLY":
                s.cloud(100, 46, 30, 7, 125, 80, 20, seed=8)
                s.cloud(142, 32, 28, 6, 125, 80, 20, seed=9)
        s.city(NIGHT, night=True)
    else:
        raise ValueError(kind)
    return s.to_image()


# ---------------------------------------------------------------- output
def save_sheet(tiles, name):
    sheet = Image.new("L", (W, H * len(tiles)), WHITE)
    for i, t in enumerate(tiles):
        sheet.paste(t, (0, i * H))
    idx = sheet.point(lambda v: min(range(4), key=lambda i: abs(LEVELS[i] - v)))
    out = Image.new("P", sheet.size)
    out.putdata(list(idx.tobytes()))
    pal = []
    for v in LEVELS:
        pal += [v, v, v]
    out.putpalette(pal + [0] * (768 - len(pal)))
    out.save(os.path.join(ROOT, name))
    print("wrote", name, sheet.size)
    return sheet


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", help="PNG with every scene on the full home screen")
    ap.add_argument("--mask", help="PNG showing the foreground mask (debug)")
    args = ap.parse_args()
    home = Image.open(os.path.join(ROOT, "home.bmp")).convert("L")
    fg = foreground_mask(home)
    hp = home.load()
    tiles = []
    for kind in SCENES:
        scene = make_scene(kind, home)
        sp = scene.load()
        tile = Image.new("L", (W, H))
        tp = tile.load()
        for y in range(H):
            for x in range(W):
                keep = fg[y][x]
                tp[x, y] = hp[X0 + x, Y0 + y] if keep else sp[X0 + x, Y0 + y]
        tiles.append(tile)
    save_sheet(tiles, "scenes.bmp")
    if args.mask:
        m = home.convert("RGB")
        for y in range(H):
            for x in range(W):
                if not fg[y][x]:
                    r, g, b = m.getpixel((X0 + x, Y0 + y))
                    m.putpixel((X0 + x, Y0 + y), (255, g // 2, b // 2))
        m.resize((296 * 4, 128 * 4), Image.NEAREST).save(args.mask)
    if args.preview:
        cols = 2
        rows = (len(tiles) + 1) // 2
        sc = 2
        out = Image.new("L", (cols * 296 * sc + 8, rows * 128 * sc + 4 * rows), 200)
        for i, t in enumerate(tiles):
            full = home.copy()
            full.paste(t, (X0, Y0))
            full = full.resize((296 * sc, 128 * sc), Image.NEAREST)
            out.paste(full, ((i % cols) * (296 * sc + 8), (i // cols) * (128 * sc + 4)))
        out.save(args.preview)
        print("wrote", args.preview)


if __name__ == "__main__":
    main()
