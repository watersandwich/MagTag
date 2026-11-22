"""Render the MagTag screens on a computer, using the real code.py drawing code.

    python3 tools/preview.py                 # live Open-Meteo data
    python3 tools/preview.py --json f.json   # a saved API response
    python3 tools/preview.py --out DIR       # where PNGs go (default: preview/)

CircuitPython-only modules (board, displayio, wifi, ...) are replaced with small
stand-ins so code.py runs under desktop Python; the main loop is not started.
Needs Pillow.
"""

import argparse
import json
import os
import sys
import types
import urllib.request

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------ fake modules
class _Pin:
    pass


class _Display:
    time_to_refresh = 0

    def refresh(self):
        pass


def make_stubs(saved_json):
    m = {}
    os.environ.setdefault("CIRCUITPY_WIFI_SSID", "preview")
    os.environ.setdefault("CIRCUITPY_WIFI_PASSWORD", "preview")
    board = types.ModuleType("board")
    for name in ("NEOPIXEL_POWER", "NEOPIXEL", "BUTTON_A", "BUTTON_B",
                 "BUTTON_C", "BUTTON_D", "BATTERY", "SPEAKER", "SPEAKER_ENABLE"):
        setattr(board, name, _Pin())
    board.DISPLAY = _Display()
    m["board"] = board

    dio = types.ModuleType("digitalio")

    class DigitalInOut:
        value = True

        def __init__(self, pin):
            pass

        def switch_to_output(self, value=False):
            pass

        def switch_to_input(self, pull=None):
            pass
    dio.DigitalInOut = DigitalInOut
    dio.Pull = types.SimpleNamespace(UP=1)
    m["digitalio"] = dio

    aio = types.ModuleType("analogio")

    class AnalogIn:
        value = int(3.95 / 6.6 * 65535)

        def __init__(self, pin):
            pass
    aio.AnalogIn = AnalogIn
    m["analogio"] = aio

    dsp = types.ModuleType("displayio")

    class OnDiskBitmap:
        def __init__(self, path):
            self.image = Image.open(os.path.join(ROOT, path.lstrip("/"))).convert("L")
            self.pixel_shader = None

    class Bitmap:
        def __init__(self, w, h, n):
            self.w, self.h = w, h

    class Palette(list):
        def __init__(self, n):
            super().__init__([0] * n)

    class TileGrid:
        def __init__(self, bitmap, pixel_shader=None, width=1, height=1,
                     tile_width=None, tile_height=None, x=0, y=0, default_tile=0):
            self.bitmap, self.shader = bitmap, pixel_shader
            self.width, self.height = width, height
            self.tw = tile_width or getattr(bitmap, "w", None)
            self.th = tile_height or getattr(bitmap, "h", None)
            self.x, self.y = x, y
            self.tiles = [default_tile] * (width * height)

        def __setitem__(self, i, v):
            self.tiles[i] = v

    class Group(list):
        pass
    dsp.OnDiskBitmap, dsp.Bitmap, dsp.Palette = OnDiskBitmap, Bitmap, Palette
    dsp.TileGrid, dsp.Group = TileGrid, Group
    m["displayio"] = dsp

    pwm = types.ModuleType("pwmio")

    class PWMOut:
        def __init__(self, pin, frequency=440, duty_cycle=0, variable_frequency=False):
            self.frequency, self.duty_cycle = frequency, duty_cycle
    pwm.PWMOut = PWMOut
    m["pwmio"] = pwm

    mc = types.ModuleType("microcontroller")
    mc.nvm = bytearray(256)
    m["microcontroller"] = mc

    wifi = types.ModuleType("wifi")
    wifi.radio = types.SimpleNamespace(connected=True, connect=lambda s, p: None)
    m["wifi"] = wifi

    neo = types.ModuleType("neopixel")

    class NeoPixel(list):
        def __init__(self, pin, n, brightness=1):
            super().__init__([(0, 0, 0)] * n)

        def fill(self, c):
            pass
    neo.NeoPixel = NeoPixel
    m["neopixel"] = neo

    cm = types.ModuleType("adafruit_connection_manager")
    cm.get_radio_socketpool = lambda r: None
    cm.get_radio_ssl_context = lambda r: None
    m["adafruit_connection_manager"] = cm

    req = types.ModuleType("adafruit_requests")

    class Response:
        def __init__(self, body):
            self.status_code = 200
            self._body = body

        def json(self):
            return json.loads(self._body)

        def close(self):
            pass

    class Session:
        def __init__(self, pool, ssl):
            pass

        def get(self, url):
            if saved_json:
                return Response(open(saved_json).read())
            with urllib.request.urlopen(url, timeout=20) as r:
                return Response(r.read().decode())
    req.Session = Session
    m["adafruit_requests"] = req
    sys.modules.update(m)


# ------------------------------------------------------------ rendering
def render(group):
    canvas = Image.new("L", (296, 128), 255)
    for tg in group:
        if hasattr(tg.bitmap, "image"):
            src = tg.bitmap.image
            tw, th = tg.tw or src.width, tg.th or src.height
            per_row = src.width // tw
            for i, t in enumerate(tg.tiles):
                sx, sy = (t % per_row) * tw, (t // per_row) * th
                tile = src.crop((sx, sy, sx + tw, sy + th))
                canvas.paste(tile, (tg.x + (i % tg.width) * tw, tg.y + (i // tg.width) * th))
        else:  # solid rectangle from rect()
            color = tg.shader[0]
            gray = ((color >> 16) & 255) * 299 // 1000 + ((color >> 8) & 255) * 587 // 1000 + (color & 255) * 114 // 1000
            canvas.paste(gray, (tg.x, tg.y, tg.x + tg.bitmap.w, tg.y + tg.bitmap.h))
    return canvas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    ap.add_argument("--out", default=os.path.join(ROOT, "preview"))
    ap.add_argument("--scale", type=int, default=3)
    args = ap.parse_args()
    make_stubs(args.json)

    src = open(os.path.join(ROOT, "code.py")).read()
    src = src[:src.index("\nfetch_weather()\ndraw()")]   # stop before the main loop
    env = {"__name__": "magtag"}
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        exec(compile(src, "code.py", "exec"), env)
        env["fetch_weather"]()
    finally:
        os.chdir(cwd)
    if env["weather"] is None:
        sys.exit("weather fetch failed")
    os.makedirs(args.out, exist_ok=True)
    overlays = {"HOME": "home_overlay", "FORECAST": "forecast_overlay",
                "TODAY": "today_overlay"}
    shots = [(n, f, n.lower()) for n, f in overlays.items()]
    shots.append(("GAME", "game_overlay", "game_start"))
    shots.append(("GAME", "game_overlay", "game_over"))
    for name, fn, out_name in shots:
        if out_name == "game_over":
            env["game_state"], env["game_score"], env["game_best"] = "OVER", 7, 9
            env["sequence"][:] = [0] * 8
            env["lost_on"] = 6
        group = env["displayio"].Group()
        bg = env["displayio"].OnDiskBitmap(env["backgrounds"][name])
        group.append(env["displayio"].TileGrid(bg))
        env[fn](group)
        img = render(group)
        path = os.path.join(args.out, out_name + ".png")
        img.resize((296 * args.scale, 128 * args.scale), Image.NEAREST).save(path)
        print("wrote", path)
    print("\n".join(env["narrative"](env["weather"])))


if __name__ == "__main__":
    main()
