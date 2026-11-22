# OUTPOST // 01 v0.5 | Light contrast & larger 5-day forecast
# A: Home (hold ~2 sec to retry weather) | B: Forecast | C: Logs | D: Reactor
# In reactor: press A-D for cells, hold D ~2 sec to exit.
# Original v0.3 M-01 robot and window kept pixel-identical (weather scene static).

import os
import gc
import time
import random
import board
import digitalio
import analogio
import displayio
import wifi
import neopixel
import adafruit_connection_manager
import adafruit_requests

# ------------------- CONFIGURATION -----------------------------------
# Coordinates are configured directly, to bypass the earlier OUTPOST_CITY issue.
# We start with New York City, as shown in the previous settings screenshot.
# To change location, edit these two numbers (latitude and longitude).
LATITUDE = 40.964534
LONGITUDE = -74.155012
# This is the label for the SAME coordinates above, not the device's GPS position.
# Update all three when monitoring another location.
LOCATION_LABEL = "Hawthorne"
UPDATE_SECONDS = 30 * 60
RETRY_SECONDS = 5 * 60

SSID = os.getenv("CIRCUITPY_WIFI_SSID")
PASSWORD = os.getenv("CIRCUITPY_WIFI_PASSWORD")
print("OUTPOST // 01 v0.5 booting - LIGHT INTERFACE")
print("Weather location:", LOCATION_LABEL, "coordinates:", LATITUDE, LONGITUDE)
print("Wi-Fi credentials found:", bool(SSID), bool(PASSWORD))

# ------------------- HARDWARE ----------------------------------------
# This board revision has an active-low NeoPixel power enable.
led_power = digitalio.DigitalInOut(board.NEOPIXEL_POWER)
led_power.switch_to_output(value=False)
leds = neopixel.NeoPixel(board.NEOPIXEL, 4, brightness=0.12)
leds.fill((0, 0, 0))
led_until = 0

buttons = []
for pin in (board.BUTTON_A, board.BUTTON_B, board.BUTTON_C, board.BUTTON_D):
    b = digitalio.DigitalInOut(pin)
    b.switch_to_input(pull=digitalio.Pull.UP)
    buttons.append(b)
last_pressed = [False] * 4
button_down_at = [0.0] * 4

# The MagTag exposes LiPo battery voltage through an analog pin.
# This is NOT a fuel gauge: voltage-based bars are only an estimate.
battery_adc = None
try:
    battery_adc = analogio.AnalogIn(board.BATTERY)
    print("Battery analog monitor ready")
except Exception as error:
    print("BATTERY MONITOR ERROR:", repr(error))

# ------------------- DISPLAY -----------------------------------------
display = board.DISPLAY
backgrounds = {
    "HOME": "/home.bmp",
    "FORECAST": "/forecast.bmp",
    "LOGS": "/logs.bmp",
    "GAME": "/reactor.bmp",
}
# Each tile sheet is a crisp raster monospace font on an appropriate background.
# All BMP files stay on disk; only their active tiles are displayed.
font_files = {
    "small": ("/small.bmp", "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-:.%+!? _", 7, 13),
    "big": ("/big.bmp", "0123456789- ", 23, 46),
    "header": ("/header.bmp", "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789- ", 9, 14),
    "day": ("/day.bmp", "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789- ", 9, 17),
    "high": ("/high.bmp", "0123456789- ", 15, 27),
    "low": ("/low.bmp", "0123456789- ", 11, 18),
}
fonts = {}
for style in font_files:
    path, chars, w, h = font_files[style]
    fonts[style] = (displayio.OnDiskBitmap(path), chars, w, h)
icons_bitmap = displayio.OnDiskBitmap("/icons.bmp")

screen = "HOME"
weather = None
status = "BOOT"
last_fetch = -UPDATE_SECONDS
next_retry = 0
session = None

reactor_cells = [False] * 4
reactor_moves = 0
reactor_won = False
reactor_best = None
ignore_entry_release = False


def flash(color, duration=0.4):
    global led_until
    if screen == "GAME":
        return
    leds.fill(color)
    led_until = time.monotonic() + duration


def tile_text(group, message, x, y, style="small"):
    bmp, chars, w, h = fonts[style]
    for ch in str(message).upper():
        index = chars.find(ch)
        if index < 0:
            index = chars.find(" ")
        tile = displayio.TileGrid(
            bmp, pixel_shader=bmp.pixel_shader,
            width=1, height=1, tile_width=w, tile_height=h,
            x=x, y=y, default_tile=index
        )
        group.append(tile)
        x += w


def center_text(group, message, center, y, style):
    width = fonts[style][2] * len(str(message))
    tile_text(group, message, center - width // 2, y, style)


def weather_icon(group, code, center, y):
    state = condition(code)
    index = {"CLEAR": 0, "CLOUDY": 1, "RAIN": 2, "SNOW": 3}[state]
    group.append(displayio.TileGrid(
        icons_bitmap, pixel_shader=icons_bitmap.pixel_shader,
        width=1, height=1, tile_width=22, tile_height=20,
        x=center-11, y=y, default_tile=index
    ))

def rect(group, x, y, w, h, color=0xFFFFFF):
    b = displayio.Bitmap(w, h, 1)
    p = displayio.Palette(1)
    p[0] = color
    group.append(displayio.TileGrid(b, pixel_shader=p, x=x, y=y))


def condition(code):
    if code in (0, 1):
        return "CLEAR"
    if code in (2, 3, 45, 48):
        return "CLOUDY"  # includes fog until we make a dedicated fog scene
    if code in (71, 73, 75, 77, 85, 86):
        return "SNOW"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67,
                80, 81, 82, 95, 96, 99):
        return "RAIN"  # storm gets rainy art for now
    return "CLOUDY"


def readable_condition(code):
    if code in (45, 48):
        return "FOG"
    if code in (95, 96, 99):
        return "STORM"
    return condition(code)


def weekday(iso_date):
    y, m, d = [int(n) for n in iso_date[:10].split("-")]
    offsets = (0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4)
    if m < 3:
        y -= 1
    day = (y + y//4 - y//100 + y//400 + offsets[m-1] + d) % 7
    return ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")[day]


def short_condition(code):
    return {"CLEAR": "SUN", "CLOUDY": "CLD", "RAIN": "RAN", "SNOW": "SNW"}.get(condition(code), "---")


def battery_voltage():
    if battery_adc is None:
        return None
    try:
        avg = 0
        for _ in range(8):
            avg += battery_adc.value
        return (avg / 8 / 65535.0) * 3.3 * 2.0
    except Exception as error:
        print("BATTERY READ ERROR:", repr(error))
        return None


def battery_segments(voltage):
    # Approximation only; lithium voltage changes nonlinearly with charge.
    if voltage is None or voltage < 2.8:
        return 0
    if voltage >= 4.05:
        return 4
    if voltage >= 3.88:
        return 3
    if voltage >= 3.73:
        return 2
    if voltage >= 3.55:
        return 1
    return 0


def battery_overlay(group):
    voltage = battery_voltage()
    bars = battery_segments(voltage)
    for i in range(4):
        rect(group, 234 + i*5, 11, 3, 5,
             0x000000 if i < bars else 0xAAAAAA)
    if voltage is None or voltage < 2.8:
        tile_text(group, "--.-", 258, 7)
    else:
        tile_text(group, "{:.1f}V".format(voltage), 258, 7)
    print("BATTERY:", "unavailable" if voltage is None else "{:.2f} V".format(voltage))

def get_session():
    global session
    if not SSID or not PASSWORD:
        raise ValueError("Wi-Fi missing in settings.toml")
    if not wifi.radio.connected:
        print("WIFI: connecting...")
        wifi.radio.connect(SSID, PASSWORD)
        print("WIFI: connected")
    if session is None:
        pool = adafruit_connection_manager.get_radio_socketpool(wifi.radio)
        ssl_context = adafruit_connection_manager.get_radio_ssl_context(wifi.radio)
        session = adafruit_requests.Session(pool, ssl_context)
    return session


def fetch_weather():
    global weather, status, last_fetch, next_retry
    print("WEATHER: starting Open-Meteo request")
    last_fetch = time.monotonic()
    next_retry = 0
    flash((0, 0, 60), 0.7)
    try:
        s = get_session()
        url = (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude={}&longitude={}"
            "&current=temperature_2m,weather_code"
            "&daily=temperature_2m_max,temperature_2m_min,weather_code"
            "&temperature_unit=fahrenheit&timezone=auto"
            "&forecast_days=5"
        ).format(LATITUDE, LONGITUDE)
        print("WEATHER: requesting forecast")
        response = s.get(url)
        try:
            print("WEATHER: HTTP", response.status_code)
            if response.status_code != 200:
                raise ValueError("Forecast API HTTP " + str(response.status_code))
            data = response.json()
        finally:
            response.close()
        curr = data["current"]
        daily = data["daily"]
        weather = {
            "temp": round(curr["temperature_2m"]),
            "code": int(curr["weather_code"]),
            "high": round(daily["temperature_2m_max"][0]),
            "low": round(daily["temperature_2m_min"][0]),
            "dates": daily["time"],
            "highs": daily["temperature_2m_max"],
            "lows": daily["temperature_2m_min"],
            "codes": daily["weather_code"],
        }
        status = "ONLINE"
        print("WEATHER OK:", weather["temp"], readable_condition(weather["code"]))
        print("SCENE:", condition(weather["code"]))
        flash((0, 65, 0), 0.9)
    except Exception as error:
        status = "OFFLINE"
        next_retry = time.monotonic() + RETRY_SECONDS
        print("WEATHER ERROR:", repr(error))
        print("WEATHER: will retry in five minutes")
        flash((65, 0, 0), 1.0)
    gc.collect()


def home_overlay(group):
    # Original M-01 robot/window pixels are unchanged. Header shows weather location.
    city = "".join(c if c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789- " else " "
                   for c in LOCATION_LABEL.upper()).strip()
    if len(city) <= 11:
        tile_text(group, city, 10, 6, "header")
    else:
        tile_text(group, city[:14], 10, 7, "small")
    battery_overlay(group)
    temp = "--" if weather is None else str(weather["temp"])
    temp = temp[:3]
    # Right-align large temperature before the permanently drawn degree-F unit.
    x = max(182, 255 - 23*len(temp))
    tile_text(group, temp, x, 31, "big")
    if weather:
        name = readable_condition(weather["code"])
        highlow = "H{} L{}".format(weather["high"], weather["low"])
    else:
        name = "NO DATA"
        highlow = "H-- L--"
    tile_text(group, name[:15], 182, 81)
    tile_text(group, highlow[:14], 182, 102)


def forecast_overlay(group):
    if not weather:
        center_text(group, "NO WEATHER DATA", 148, 62, "day")
        return
    for i in range(min(5, len(weather["dates"]))):
        center = 37 + i*56
        center_text(group, weekday(weather["dates"][i]), center, 32, "day")
        weather_icon(group, int(weather["codes"][i]), center, 51)
        hi = str(round(weather["highs"][i]))
        lo = str(round(weather["lows"][i]))
        center_text(group, hi, center, 74, "high")
        center_text(group, lo, center, 100, "low")

def logs_overlay(group):
    c = condition(weather["code"]) if weather else "UNKNOWN"
    lines = {
        "CLEAR": ("SOLAR ARRAYS AT NOMINAL", "HORIZON SCAN: COMPLETE", "M-01 PATROL: IN PROGRESS"),
        "CLOUDY": ("CLOUD COVER ELEVATED", "DISTANT SIGNALS OBSCURED", "M-01 PATROL: STANDBY"),
        "RAIN": ("PRECIPITATION DETECTED", "EXTERIOR PATROL SUSPENDED", "WINDOW SEALS HOLDING"),
        "SNOW": ("SURFACE ICE DETECTED", "THERMAL LOOP ACTIVE", "M-01 PATROL: CANCELLED"),
        "UNKNOWN": ("NO ENVIRONMENTAL FEED", "ANTENNA RETRY SCHEDULED", "M-01 STATUS: OPERATIONAL"),
    }
    for i, line in enumerate(lines.get(c, lines["UNKNOWN"])):
        tile_text(group, line[:36], 23, 43 + i*18)
    tile_text(group, "LINK/" + status, 16, 95)


def game_overlay(group):
    if reactor_won:
        tile_text(group, "CORE STABILIZED", 15, 100)
        tile_text(group, "MOVES " + str(reactor_moves), 195, 100)
    else:
        tile_text(group, "CORE UNSTABLE", 15, 100)


def draw():
    print("DISPLAY:", screen)
    filename = backgrounds[screen]
    background = displayio.OnDiskBitmap(filename)
    group = displayio.Group()
    group.append(displayio.TileGrid(background, pixel_shader=background.pixel_shader))
    if screen == "HOME":
        home_overlay(group)
    elif screen == "FORECAST":
        forecast_overlay(group)
    elif screen == "LOGS":
        logs_overlay(group)
    elif screen == "GAME":
        game_overlay(group)
    display.root_group = group
    while display.time_to_refresh > 0:
        time.sleep(0.2)
    display.refresh()
    print("DISPLAY: refreshed", screen)
    gc.collect()


def toggle_cell(cells, index):
    for j in (index-1, index, index+1):
        if 0 <= j < 4:
            cells[j] = not cells[j]


def show_reactor_leds():
    for j in range(4):
        leds[j] = (90, 0, 0) if reactor_cells[j] else (0, 0, 0)


def start_reactor():
    global reactor_cells, reactor_moves, reactor_won, led_until
    reactor_cells = [False] * 4
    # CircuitPython random may not provide random.sample().
    picks = []
    wanted = random.randint(2, 3)
    while len(picks) < wanted:
        idx = random.randrange(4)
        if idx not in picks:
            picks.append(idx)
            toggle_cell(reactor_cells, idx)
    reactor_moves = 0
    reactor_won = False
    led_until = 0
    show_reactor_leds()
    print("REACTOR: press A-D; hold D to leave")


def reactor_press(index):
    global reactor_moves, reactor_won, reactor_best
    if reactor_won:
        start_reactor()
        draw()
        return
    toggle_cell(reactor_cells, index)
    reactor_moves += 1
    if not any(reactor_cells):
        reactor_won = True
        if reactor_best is None or reactor_moves < reactor_best:
            reactor_best = reactor_moves
        leds.fill((0, 75, 0))
        print("REACTOR: STABILIZED in", reactor_moves, "moves")
        draw()
    else:
        show_reactor_leds()
        print("REACTOR:", reactor_cells)


fetch_weather()
draw()
print("READY: A Home (hold A sync) / B Forecast / C Logs / D Reactor")

while True:
    now = time.monotonic()
    if screen != "GAME" and led_until and now >= led_until:
        leds.fill((0, 0, 0))
        led_until = 0

    dirty = False
    should_retry = next_retry and now >= next_retry
    should_update = now - last_fetch >= UPDATE_SECONDS
    if screen != "GAME" and (should_retry or should_update):
        fetch_weather()
        dirty = True

    for i, button in enumerate(buttons):
        pressed = not button.value
        if pressed and not last_pressed[i]:
            button_down_at[i] = now
            if screen != "GAME":
                if i == 3:
                    screen = "GAME"
                    ignore_entry_release = True
                    start_reactor()
                else:
                    screen = ("HOME", "FORECAST", "LOGS")[i]
                    flash((18, 18, 18), 0.25)
                dirty = True
        elif not pressed and last_pressed[i]:
            held = now - button_down_at[i]
            if screen == "GAME":
                if i == 3 and ignore_entry_release:
                    ignore_entry_release = False
                elif i == 3 and held >= 1.8:
                    screen = "HOME"
                    leds.fill((0, 0, 0))
                    print("REACTOR: exit")
                    dirty = True
                else:
                    reactor_press(i)
            elif i == 0 and held >= 1.8:
                print("MANUAL WEATHER SYNC")
                fetch_weather()
                dirty = True
        last_pressed[i] = pressed

    if dirty:
        draw()
    time.sleep(0.035)
