# OUTPOST // 01 v0.7 | Deep sleep between updates for long battery life
# A: Home (hold ~2 sec to retry weather) | B: Forecast | C: Today | D: Signal game
# The board deep-sleeps between weather updates; any button wakes it. The
# e-ink screen keeps its picture with no power, so sleeping is invisible.
# In the game: repeat the light/tone pattern with A-D; hold D ~2 sec to exit.
# Original v0.3 M-01 robot and room kept pixel-identical; the view out the
# window now follows the weather (scenes.bmp, made by tools/make_scenes.py).

import os
import gc
import json
import time
import alarm
import rtc
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
# Game sound. Set SOUND to False to play silently; VOLUME is 0-32768.
SOUND = True
VOLUME = 0x2000
# Which NeoPixel sits above each button (A, B, C, D). If the game's lights
# show up in reverse order on your board, change this to (3, 2, 1, 0).
LED_FOR_BUTTON = (0, 1, 2, 3)
# Power. The board deep-sleeps between updates and wakes on a timer or a button.
UPDATE_SECONDS = 30 * 60        # weather refresh during the day
NIGHT_UPDATE_SECONDS = 60 * 60  # refresh overnight (NIGHT_START to NIGHT_END)
NIGHT_START, NIGHT_END = 23, 6  # local hours
RETRY_SECONDS = 5 * 60          # first retry after a failed update (then doubles)
AWAKE_SECONDS = 60              # stay awake this long after the last button press
GAME_IDLE_SECONDS = 180         # leave the game and sleep after this long idle
LOW_BATTERY_VOLTS = 3.5         # show LOW BAT below this

# Wi-Fi networks from settings.toml. The first is CIRCUITPY_WIFI_SSID /
# CIRCUITPY_WIFI_PASSWORD; add more as WIFI_SSID_2 / WIFI_PASSWORD_2, up to
# WIFI_SSID_9. Leave a password empty ("") for an open network.
NETWORKS = []
for _ssid, _pw in [("CIRCUITPY_WIFI_SSID", "CIRCUITPY_WIFI_PASSWORD")] + [
        ("WIFI_SSID_{}".format(n), "WIFI_PASSWORD_{}".format(n)) for n in range(2, 10)]:
    if os.getenv(_ssid):
        NETWORKS.append((os.getenv(_ssid), os.getenv(_pw) or ""))
last_network = None  # the network that worked last time (kept through sleep)
print("OUTPOST // 01 v0.7 booting - LIGHT INTERFACE")
print("Weather location:", LOCATION_LABEL, "coordinates:", LATITUDE, LONGITUDE)
print("Wi-Fi networks configured:", ", ".join(n[0] for n in NETWORKS) or "none")

# ------------------- HARDWARE ----------------------------------------
# This board revision has an active-low NeoPixel power enable. The lights are
# powered only while they are in use (status flashes and the game).
led_power = digitalio.DigitalInOut(board.NEOPIXEL_POWER)
led_power.switch_to_output(value=True)
leds = neopixel.NeoPixel(board.NEOPIXEL, 4, brightness=0.12)
led_until = 0


def pixels_power(on):
    led_power.value = not on
    if on:
        time.sleep(0.005)
    leds.fill((0, 0, 0))


BUTTON_PINS = (board.BUTTON_A, board.BUTTON_B, board.BUTTON_C, board.BUTTON_D)
buttons = []
for pin in BUTTON_PINS:
    b = digitalio.DigitalInOut(pin)
    b.switch_to_input(pull=digitalio.Pull.UP)
    buttons.append(b)
last_pressed = [False] * 4
# Speaker: a small speaker on board.SPEAKER, with an amplifier switched by
# board.SPEAKER_ENABLE. Tones are square waves from the built-in pwmio module.
speaker = None
speaker_enable = None
try:
    import pwmio
    speaker_enable = digitalio.DigitalInOut(board.SPEAKER_ENABLE)
    speaker_enable.switch_to_output(value=False)
    speaker = pwmio.PWMOut(board.SPEAKER, frequency=440, duty_cycle=0,
                           variable_frequency=True)
    print("Speaker ready")
except Exception as error:
    print("SPEAKER ERROR (game will be silent):", repr(error))
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
    "TODAY": "/today.bmp",
    "GAME": "/signal.bmp",
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
    # Mixed-case 6x13 text (ASCII 32-126 plus a degree sign) for the Today screen.
    "text": ("/text.bmp", "".join(chr(c) for c in range(32, 127)) + "\u00b0", 6, 13),
    # The same font, white on black, for the home screen's dark header bar.
    "text_inv": ("/text_inv.bmp", "".join(chr(c) for c in range(32, 127)) + "\u00b0", 6, 13),
}
fonts = {}
for style in font_files:
    path, chars, w, h = font_files[style]
    fonts[style] = (displayio.OnDiskBitmap(path), chars, w, h)
# Icon sheets: 30x24 tiles for the forecast, 50x40 tiles for the Today screen.
# Tile order is set by tools/make_assets.py and must match ICON_INDEX.
icon_sheets = {
    "small": (displayio.OnDiskBitmap("/icons.bmp"), 30, 24),
    "big": (displayio.OnDiskBitmap("/icons_big.bmp"), 50, 40),
}
# Home-screen window views: one 134x79 tile per weather, drawn over home.bmp.
# Tile order is set by tools/make_scenes.py and must match SCENE_INDEX.
scenes_bitmap = displayio.OnDiskBitmap("/scenes.bmp")
SCENE_X, SCENE_Y, SCENE_W, SCENE_H = 28, 22, 134, 79
SCENE_INDEX = {
    "CLEAR": 0, "PARTLY": 1, "CLOUDY": 2, "FOG": 3, "RAIN": 4, "SNOW": 5,
    "STORM": 6, "NIGHT": 7, "NIGHT_PARTLY": 8, "NIGHT_CLOUDY": 9,
}
ICON_INDEX = {
    "CLEAR": 0, "PARTLY": 1, "CLOUDY": 2, "FOG": 3, "DRIZZLE": 4,
    "RAIN": 5, "SNOW": 6, "STORM": 7, "CLEAR_NIGHT": 8, "PARTLY_NIGHT": 9,
}

screen = "HOME"
weather = None
status = "BOOT"
last_fetch = 0       # RTC time (seconds) of the last update attempt
fail_count = 0       # failed updates in a row, for retry back-off
session = None
quiet = False        # no status flashes (timer wakes, so it never blinks at night)
awake_until = 0      # time.monotonic() deadline before going back to sleep

ignore_entry_release = False


def flash(color, duration=0.4):
    global led_until
    if screen == "GAME" or quiet:
        return
    pixels_power(True)
    leds.fill(color)
    led_until = time.monotonic() + duration


def speaker_power(on):
    if speaker_enable is not None:
        speaker_enable.value = on and SOUND


def tone_on(freq):
    if speaker is not None and SOUND:
        speaker.frequency = int(freq)
        speaker.duty_cycle = VOLUME


def tone_off():
    if speaker is not None:
        speaker.duty_cycle = 0


def beep(freq, duration):
    tone_on(freq)
    time.sleep(duration)
    tone_off()


def tile_text(group, message, x, y, style="small"):
    # One TileGrid per string (not per character) keeps memory use low.
    bmp, chars, w, h = fonts[style]
    message = str(message)
    if not style.startswith("text"):
        message = message.upper()
    if not message:
        return
    blank = chars.find(" ")
    grid = displayio.TileGrid(
        bmp, pixel_shader=bmp.pixel_shader,
        width=len(message), height=1, tile_width=w, tile_height=h,
        x=x, y=y, default_tile=blank
    )
    for i, ch in enumerate(message):
        index = chars.find(ch)
        grid[i] = index if index >= 0 else blank
    group.append(grid)


def center_text(group, message, center, y, style):
    width = fonts[style][2] * len(str(message))
    tile_text(group, message, center - width // 2, y, style)


def weather_icon(group, code, center, y, size="small", night=False):
    bmp, w, h = icon_sheets[size]
    state = condition(code)
    if night and state in ("CLEAR", "PARTLY"):
        state += "_NIGHT"
    group.append(displayio.TileGrid(
        bmp, pixel_shader=bmp.pixel_shader,
        width=1, height=1, tile_width=w, tile_height=h,
        x=center - w // 2, y=y, default_tile=ICON_INDEX[state]
    ))

def rect(group, x, y, w, h, color=0xFFFFFF):
    b = displayio.Bitmap(w, h, 1)
    p = displayio.Palette(1)
    p[0] = color
    group.append(displayio.TileGrid(b, pixel_shader=p, x=x, y=y))


def condition(code):
    # WMO weather codes as used by Open-Meteo.
    if code in (0, 1):
        return "CLEAR"
    if code == 2:
        return "PARTLY"
    if code in (45, 48):
        return "FOG"
    if code in (51, 53, 55, 56, 57):
        return "DRIZZLE"
    if code in (61, 63, 65, 66, 67, 80, 81, 82):
        return "RAIN"
    if code in (71, 73, 75, 77, 85, 86):
        return "SNOW"
    if code in (95, 96, 99):
        return "STORM"
    return "CLOUDY"


def readable_condition(code):
    state = condition(code)
    return "PARTLY CLOUDY" if state == "PARTLY" else state


def weekday(iso_date):
    y, m, d = [int(n) for n in iso_date[:10].split("-")]
    offsets = (0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4)
    if m < 3:
        y -= 1
    day = (y + y//4 - y//100 + y//400 + offsets[m-1] + d) % 7
    return ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")[day]


def num(value, default=0):
    # Open-Meteo sends null for values it cannot forecast.
    return default if value is None else value


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
    return voltage


# ------------------- CLOCK, SLEEP MEMORY, DEEP SLEEP -------------------
# The real-time clock is set from the weather server's reply on every update
# and keeps running through deep sleep. It is used for the "awake" stamp on
# the home screen and for scheduling the next update.
MONTH_NAMES = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
               "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def clock_ok():
    return time.localtime().tm_year >= 2025


def set_clock(date_header, utc_offset):
    # date_header looks like "Sun, 22 Nov 2026 20:15:31 GMT"
    try:
        parts = date_header.split()
        hh, mm, ss = [int(n) for n in parts[4].split(":")]
        month = MONTH_NAMES.index(parts[2].upper()[:3]) + 1
        utc = time.mktime((int(parts[3]), month, int(parts[1]), hh, mm, ss, 0, -1, -1))
        rtc.RTC().datetime = time.localtime(utc + int(utc_offset))
        print("CLOCK: set to", fmt_time(time.localtime()))
    except Exception as error:
        print("CLOCK ERROR:", repr(error), date_header)


def fmt_time(t):
    h = t.tm_hour % 12 or 12
    return "{}:{:02d}{}".format(h, t.tm_min, "a" if t.tm_hour < 12 else "p")


def update_interval():
    if clock_ok():
        h = time.localtime().tm_hour
        if h >= NIGHT_START or h < NIGHT_END:
            return NIGHT_UPDATE_SECONDS
    return UPDATE_SECONDS


def seconds_until_update():
    if fail_count:
        wait = min(RETRY_SECONDS * (2 ** (fail_count - 1)), update_interval())
    else:
        wait = update_interval()
    now = time.time()
    if last_fetch > now:          # clock went backwards: update now
        return 0
    return max(0, last_fetch + wait - now)


# Weather and schedule are kept in alarm.sleep_memory, which survives deep
# sleep (but not a reset or power loss), so a button wake can redraw a screen
# without reconnecting to Wi-Fi.
def save_state():
    try:
        blob = json.dumps({"w": weather, "f": last_fetch, "n": fail_count,
                           "s": status, "k": last_network}).encode()
        mem = alarm.sleep_memory
        if len(blob) + 4 > len(mem):
            print("SLEEP MEMORY: weather too big to keep ({} bytes)".format(len(blob)))
            blob = json.dumps({"w": None, "f": last_fetch, "n": fail_count,
                               "s": status, "k": last_network}).encode()
        mem[0:4] = bytes((0x4F, 0x31, len(blob) >> 8, len(blob) & 255))
        mem[4:4 + len(blob)] = blob
    except Exception as error:
        print("SLEEP MEMORY WRITE ERROR:", repr(error))


def load_state():
    global weather, last_fetch, fail_count, status, last_network
    try:
        mem = alarm.sleep_memory
        if mem[0] != 0x4F or mem[1] != 0x31:
            return
        size = (mem[2] << 8) | mem[3]
        data = json.loads(bytes(mem[4:4 + size]).decode())
        weather = data["w"]
        last_fetch = data["f"]
        fail_count = data["n"]
        status = data["s"]
        last_network = data.get("k")
        print("SLEEP MEMORY: restored, weather", "yes" if weather else "no")
    except Exception as error:
        print("SLEEP MEMORY READ ERROR:", repr(error))


def go_to_sleep():
    global screen
    if screen == "GAME":
        leave_game()
    if screen != "HOME":
        # Always sleep on the home screen, so the awake stamp is visible.
        screen = "HOME"
        draw()
    save_state()
    wait = max(60, seconds_until_update())
    print("SLEEP: next update in {} min; any button wakes".format(int(wait // 60)))
    pixels_power(False)
    if speaker_enable is not None:
        speaker_enable.value = False
    try:
        wifi.radio.enabled = False
    except Exception:
        pass
    for b in buttons:
        b.deinit()
    timer = alarm.time.TimeAlarm(monotonic_time=time.monotonic() + wait)
    keep = [led_power] + ([speaker_enable] if speaker_enable is not None else [])
    # Wake on any of the four buttons. If this chip/firmware cannot wake on
    # several pins from deep sleep, fall back to button A alone.
    for pins in (BUTTON_PINS, BUTTON_PINS[:1]):
        try:
            pin_alarms = [alarm.pin.PinAlarm(pin=p, value=False, pull=True) for p in pins]
            try:
                alarm.exit_and_deep_sleep_until_alarms(timer, *pin_alarms,
                                                       preserve_dios=keep)
            except TypeError:  # older firmware without preserve_dios
                alarm.exit_and_deep_sleep_until_alarms(timer, *pin_alarms)
        except (ValueError, NotImplementedError, RuntimeError) as error:
            # Raised when this pin combination can't wake the chip.
            print("SLEEP: cannot wake on", len(pins), "buttons:", repr(error))
    alarm.exit_and_deep_sleep_until_alarms(timer)

def try_network(net):
    global last_network
    print("WIFI: trying", net[0])
    try:
        wifi.radio.connect(net[0], net[1], timeout=10)
        last_network = net[0]
        print("WIFI: connected to", net[0])
        return True
    except Exception as error:
        print("WIFI: could not join", net[0], repr(error))
        return False


def visible_networks():
    # Strongest signal seen for each network name nearby, or None if the scan failed.
    try:
        seen = {}
        for found in wifi.radio.start_scanning_networks():
            if found.ssid:
                seen[found.ssid] = max(seen.get(found.ssid, -999), found.rssi)
        wifi.radio.stop_scanning_networks()
        return seen
    except Exception as error:
        print("WIFI SCAN ERROR:", repr(error))
        return None


def connect_wifi():
    if not NETWORKS:
        raise ValueError("No Wi-Fi network in settings.toml")
    # Most wakes happen in the same place as last time, so try that network
    # first without scanning; it's the quickest and uses the least battery.
    tried = []
    for net in NETWORKS:
        if net[0] == last_network:
            if try_network(net):
                return
            tried.append(net[0])
    candidates = [n for n in NETWORKS if n[0] not in tried]
    seen = visible_networks()
    if seen:
        nearby = [n for n in candidates if n[0] in seen]
        nearby.sort(key=lambda n: -seen[n[0]])  # strongest signal first
        if nearby:
            print("WIFI: in range:", ", ".join(n[0] for n in nearby))
            candidates = nearby
        # If none of ours showed up, still try them all: hidden networks
        # don't appear in scans.
    for net in candidates:
        if try_network(net):
            return
    raise ConnectionError("could not join any of {} Wi-Fi networks".format(len(NETWORKS)))


def get_session():
    global session
    if not wifi.radio.enabled:
        wifi.radio.enabled = True
    if not wifi.radio.connected:
        connect_wifi()
    if session is None:
        pool = adafruit_connection_manager.get_radio_socketpool(wifi.radio)
        ssl_context = adafruit_connection_manager.get_radio_ssl_context(wifi.radio)
        session = adafruit_requests.Session(pool, ssl_context)
    return session


def wifi_off():
    global session
    try:
        adafruit_connection_manager.connection_manager_close_all(release_references=True)
    except Exception:
        pass
    session = None
    try:
        wifi.radio.enabled = False
    except Exception as error:
        print("WIFI OFF ERROR:", repr(error))


def fetch_weather():
    global weather, status, last_fetch, fail_count
    print("WEATHER: starting Open-Meteo request")
    flash((0, 0, 60), 0.7)
    try:
        s = get_session()
        url = (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude={}&longitude={}"
            "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
            "weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m,is_day"
            "&hourly=temperature_2m,precipitation_probability,precipitation,weather_code"
            "&daily=temperature_2m_max,temperature_2m_min,weather_code,"
            "precipitation_probability_max,sunrise,sunset,uv_index_max,"
            "wind_speed_10m_max,wind_gusts_10m_max"
            "&temperature_unit=fahrenheit&wind_speed_unit=mph"
            "&precipitation_unit=inch&timezone=auto"
            "&forecast_days=5&forecast_hours=24"
        ).format(LATITUDE, LONGITUDE)
        print("WEATHER: requesting forecast")
        response = s.get(url)
        try:
            print("WEATHER: HTTP", response.status_code)
            if response.status_code != 200:
                raise ValueError("Forecast API HTTP " + str(response.status_code))
            data = response.json()
            date_header = None
            for key in response.headers:
                if key.lower() == "date":
                    date_header = response.headers[key]
        finally:
            response.close()
        if date_header:
            set_clock(date_header, data.get("utc_offset_seconds", 0))
        curr = data["current"]
        daily = data["daily"]
        hourly = data["hourly"]
        # Keep the next 24 hours from the current hour (ISO strings sort correctly).
        now_hour = curr["time"][:13]
        keep = [i for i, t in enumerate(hourly["time"]) if t[:13] >= now_hour][:24]
        weather = {
            "time": curr["time"],
            "temp": round(curr["temperature_2m"]),
            "feels": round(curr["apparent_temperature"]),
            "humidity": round(curr["relative_humidity_2m"]),
            "wind": round(curr["wind_speed_10m"]),
            "wind_dir": curr["wind_direction_10m"],
            "gust": round(curr["wind_gusts_10m"]),
            "is_day": bool(curr["is_day"]),
            "code": int(curr["weather_code"]),
            "high": round(daily["temperature_2m_max"][0]),
            "low": round(daily["temperature_2m_min"][0]),
            "dates": daily["time"],
            "highs": daily["temperature_2m_max"],
            "lows": daily["temperature_2m_min"],
            "codes": daily["weather_code"],
            "rain_chance": num(daily["precipitation_probability_max"][0]),
            "sunrise": daily["sunrise"][0],
            "sunset": daily["sunset"][0],
            "uv": num(daily["uv_index_max"][0]),
            "wind_max": num(daily["wind_speed_10m_max"][0]),
            "gust_max": num(daily["wind_gusts_10m_max"][0]),
            "h_time": [hourly["time"][i] for i in keep],
            "h_temp": [num(hourly["temperature_2m"][i]) for i in keep],
            "h_pop": [num(hourly["precipitation_probability"][i]) for i in keep],
            "h_rain": [num(hourly["precipitation"][i]) for i in keep],
            "h_code": [int(num(hourly["weather_code"][i], 3)) for i in keep],
        }
        del data, hourly
        status = "ONLINE"
        fail_count = 0
        print("WEATHER OK:", weather["temp"], readable_condition(weather["code"]))
        flash((0, 65, 0), 0.9)
    except Exception as error:
        status = "OFFLINE"
        fail_count += 1
        print("WEATHER ERROR:", repr(error))
        flash((65, 0, 0), 1.0)
    last_fetch = time.time()
    wifi_off()
    gc.collect()


def scene_for(w):
    state = condition(w["code"])
    if state in ("RAIN", "DRIZZLE"):
        return "RAIN"
    if state in ("STORM", "SNOW"):
        return state
    if not w["is_day"]:
        return {"CLEAR": "NIGHT", "PARTLY": "NIGHT_PARTLY"}.get(state, "NIGHT_CLOUDY")
    return state  # CLEAR, PARTLY, CLOUDY or FOG


def home_overlay(group):
    # The view out the window. With no weather yet, home.bmp's rainy view stays.
    if weather:
        scene = scene_for(weather)
        print("SCENE:", scene)
        group.append(displayio.TileGrid(
            scenes_bitmap, pixel_shader=scenes_bitmap.pixel_shader,
            width=1, height=1, tile_width=SCENE_W, tile_height=SCENE_H,
            x=SCENE_X, y=SCENE_Y, default_tile=SCENE_INDEX[scene]
        ))
    # Header bar: location on the left, and when the MagTag last woke up on the
    # right (in place of the old "M-01 /////" decoration). If this time is
    # well over an hour old, the board has stopped waking up.
    city = "".join(c if c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789- " else " "
                   for c in LOCATION_LABEL.upper()).strip()
    if len(city) <= 9:
        tile_text(group, city, 10, 6, "header")
    else:
        tile_text(group, LOCATION_LABEL[:14], 10, 7, "text_inv")
    rect(group, 100, 5, 70, 15, 0x000000)
    stamp = "awake " + (fmt_time(time.localtime()) if clock_ok() else "--:--")
    tile_text(group, stamp, 168 - 6 * len(stamp), 7, "text_inv")
    voltage = battery_overlay(group)
    # Status slot (where "WX" is printed): problems first.
    flag = None
    if status == "OFFLINE":
        flag = "OFFLINE"
    elif voltage is not None and 2.8 <= voltage < LOW_BATTERY_VOLTS:
        flag = "LOW BAT"
    if flag:
        rect(group, 180, 9, 50, 12)
        tile_text(group, flag, 181, 7)
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
        center_text(group, weekday(weather["dates"][i]), center, 29, "day")
        weather_icon(group, int(weather["codes"][i]), center, 45)
        hi = str(round(weather["highs"][i]))
        lo = str(round(weather["lows"][i]))
        center_text(group, hi, center, 72, "high")
        center_text(group, lo, center, 93, "low")

# ------------------- TODAY SCREEN (button C) --------------------------
# Turns the next ~18 hours of forecast into a few plain-English sentences,
# plus a row of details. Everything is computed on the MagTag from the data
# fetched in fetch_weather().

MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
SEVERITY = {"CLEAR": 0, "PARTLY": 1, "CLOUDY": 2, "FOG": 3,
            "DRIZZLE": 4, "RAIN": 5, "SNOW": 6, "STORM": 7}
WET = ("DRIZZLE", "RAIN", "SNOW", "STORM")
NOUN = {"DRIZZLE": "drizzle", "RAIN": "rain", "SNOW": "snow", "STORM": "storms"}
DEG = "°"
TEXT_COLS = 36   # characters that fit in the narrative column
TEXT_LINES = 5
HORIZON = 18     # hours the sky and rain sentences look ahead


def cap(text):
    return text[:1].upper() + text[1:]


def hour_of(iso):
    return int(iso[11:13])


def fmt_hour(h):
    h %= 24
    if h == 0:
        return "midnight"
    if h == 12:
        return "noon"
    return "{}{}".format(h if h < 12 else h - 12, "am" if h < 12 else "pm")


def fmt_clock(iso):
    h, m = hour_of(iso), int(iso[14:16])
    suffix = "a" if h < 12 else "p"
    h = h % 12 or 12
    return "{}:{:02d}{}".format(h, m, suffix)


def compass(degrees):
    points = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return points[int((num(degrees) + 22.5) // 45) % 8]


def period_name(iso, today):
    h = hour_of(iso)
    if iso[:10] == today:
        if h < 5:
            return "overnight"
        if h < 12:
            return "this morning"
        if h < 17:
            return "this afternoon"
        if h < 21:
            return "this evening"
        return "tonight"
    if h < 5:
        return "tonight"
    if h < 12:
        return "tomorrow morning"
    if h < 17:
        return "tomorrow afternoon"
    return "tomorrow evening"


def is_night_hour(h, w):
    return h < hour_of(w["sunrise"]) or h >= hour_of(w["sunset"])


def periods(w):
    """Group the hourly forecast into named periods with one sky category each."""
    today = w["time"][:10]
    groups = []
    for i, iso in enumerate(w["h_time"][:HORIZON]):
        name = period_name(iso, today)
        if not groups or groups[-1][0] != name:
            groups.append([name, []])
        groups[-1][1].append(i)
    result = []
    for name, idx in groups:
        counts = {}
        wet = {}
        for i in idx:
            c = condition(w["h_code"][i])
            counts[c] = counts.get(c, 0) + 1
            if c in WET:
                wet[c] = wet.get(c, 0) + 1
        pool = counts
        # Two or more wet hours (or one in a short period) make it a wet period.
        if wet and (sum(wet.values()) >= 2 or len(idx) <= 2):
            pool = wet
        else:
            for c in WET:
                pool.pop(c, None)
            if not pool:
                pool = counts if counts else {"CLOUDY": 1}
        best = None
        for c in pool:
            if best is None or (pool[c], SEVERITY[c]) > (pool[best], SEVERITY[best]):
                best = c
        nights = sum(1 for i in idx if is_night_hour(hour_of(w["h_time"][i]), w))
        result.append((name, best, nights * 2 > len(idx)))
    return result


def sky_word(cat, night):
    if cat == "CLEAR":
        return "clear" if night else "sunny"
    if cat == "PARTLY":
        return "partly cloudy"
    if cat == "FOG":
        return "foggy"
    return NOUN.get(cat, "cloudy")


def sky_sentence(w):
    segs = []
    for name, cat, night in periods(w):
        if segs and segs[-1][1] == cat:
            segs[-1][3] = name           # extend the run to this period
            segs[-1][4] = night
        else:
            segs.append([name, cat, night, name, night])
    if not segs:
        return ""
    segs = segs[:3]
    if len(segs) == 1:
        first, cat, night, last, last_night = segs[0]
        word = sky_word(cat, night)
        if first == last:
            return cap("{} {}.".format(word, first))
        if cat == "CLEAR" and night != last_night:
            when = "tomorrow" if last.startswith("tomorrow") else last
            return cap("{} {}, then {} {}.".format(word, first, sky_word(cat, last_night), when))
        return cap("{} through {}.".format(word, last))
    parts = [cap("{} {}".format(sky_word(segs[0][1], segs[0][2]), segs[0][0]))]
    prev = segs[0][1]
    for first, cat, night, last, last_night in segs[1:]:
        if cat == "CLEAR" and SEVERITY[prev] >= 2:
            phrase = "clearing"
        elif cat == "CLOUDY":
            phrase = "drying out" if prev in WET else "clouding over"
        elif cat == "FOG":
            phrase = "fog developing"
        elif cat in WET:
            phrase = ("turning to " + NOUN[cat]) if prev in WET else (NOUN[cat] + " moving in")
        else:
            phrase = sky_word(cat, night)
        parts.append("{} {}".format(phrase, first))
        prev = cat
    if len(parts) == 2:
        return "{}, then {}.".format(parts[0], parts[1])
    return "{}, {}, then {}.".format(parts[0], parts[1], parts[2])


def precip_sentence(w):
    pops = w["h_pop"][:HORIZON]
    if not pops:
        return ""
    pmax = max(pops)
    # Which kind of precipitation? Use the wettest hours' weather codes.
    kind = None
    for i, p in enumerate(pops):
        c = condition(w["h_code"][i])
        if c in WET and p >= 30 and (kind is None or SEVERITY[c] > SEVERITY[kind]):
            kind = c
    if kind is None:
        kind = "SNOW" if min(w["h_temp"]) <= 32 else "RAIN"
    noun = NOUN[kind]
    times = w["h_time"]
    if pmax < 20:
        return "Staying dry."
    if pmax < 50:
        peak = pops.index(pmax)
        word = "Slight chance" if pmax < 35 else "Chance"
        return "{} of {} ({}%) around {}.".format(
            word, noun, pmax, fmt_hour(hour_of(times[peak])))
    start = None
    end = None
    for i, p in enumerate(pops):
        if p >= 50:
            if start is None:
                start = i
            end = i
        elif start is not None:
            break
    last = len(pops) - 1
    if start == 0 and end == last:
        when = "for the next {} hours".format(len(pops))
    elif start == 0:
        when = "until " + fmt_hour(hour_of(times[end]) + 1)
    elif end == last:
        when = "from {} on".format(fmt_hour(hour_of(times[start])))
    else:
        when = "{}-{}".format(fmt_hour(hour_of(times[start])),
                              fmt_hour(hour_of(times[end]) + 1))
    text = "{} likely {} ({}%).".format(cap(noun), when, pmax)
    total = sum(w["h_rain"][:HORIZON])
    if total >= 0.05 and kind != "SNOW":
        text += " About {:.1f}\" total.".format(total)
    return text


def temp_sentence(w):
    temps = w["h_temp"]
    times = w["h_time"]
    if not temps:
        return "High {}{}, low {}{}.".format(w["high"], DEG, w["low"], DEG)
    today = w["time"][:10]
    today_idx = [i for i, t in enumerate(times) if t[:10] == today]
    first = ""
    if today_idx:
        peak = today_idx[0]
        for i in today_idx:
            if temps[i] > temps[peak]:
                peak = i
        if peak > 0 and temps[peak] >= w["temp"] + 2:
            first = "High {}{} around {}".format(
                round(temps[peak]), DEG, fmt_hour(hour_of(times[peak])))
    # Overnight low: search up to noon tomorrow.
    span = len(temps)
    for i, t in enumerate(times):
        if t[:10] != today and hour_of(t) >= 12:
            span = i
            break
    span = max(span, 1)
    lo = 0
    for i in range(span):
        if temps[i] < temps[lo]:
            lo = i
    edge = "by" if lo == span - 1 else "around"
    low_text = "{}{} {} {}".format(round(temps[lo]), DEG, edge, fmt_hour(hour_of(times[lo])))
    if first:
        return "{}; low {}.".format(first, low_text)
    if lo == span - 1:
        return "Falling to {}.".format(low_text)
    if lo == 0:
        return "Temps holding near {}{}, high {}{} tomorrow.".format(
            w["temp"], DEG, round(w["highs"][1]) if len(w["highs"]) > 1 else w["high"], DEG)
    return "Low {}.".format(low_text)


def extra_sentences(w):
    out = []
    diff = w["feels"] - w["temp"]
    if diff <= -5:
        out.append("Feels like {}{} with the wind.".format(w["feels"], DEG))
    elif diff >= 5:
        out.append("Feels like {}{} with the humidity.".format(w["feels"], DEG))
    gust = round(max(w["gust_max"], w["gust"]))
    if gust >= 35:
        out.append("Very windy, gusts to {} mph.".format(gust))
    elif gust >= 25:
        out.append("Windy, gusts to {} mph.".format(gust))
    elif w["wind_max"] >= 12:
        out.append("Breezy at times.")
    uv = round(w["uv"])
    if uv >= 6 and hour_of(w["time"]) < 15:
        out.append("{} UV ({}) midday; use sunscreen.".format(
            "Very high" if uv >= 8 else "High", uv))
    return out


def wrap(text, cols):
    lines = []
    line = ""
    for word in text.split(" "):
        if not word:
            continue
        if line and len(line) + 1 + len(word) > cols:
            lines.append(line)
            line = word
        else:
            line = word if not line else line + " " + word
    if line:
        lines.append(line)
    return lines


def narrative(w):
    """Sentences in priority order; stop adding when the column is full."""
    text = ""
    for sentence in [sky_sentence(w), precip_sentence(w), temp_sentence(w)] + extra_sentences(w):
        if not sentence:
            continue
        candidate = sentence if not text else text + " " + sentence
        if len(wrap(candidate, TEXT_COLS)) > TEXT_LINES:
            continue
        text = candidate
    return wrap(text, TEXT_COLS)


def today_overlay(group):
    if not weather:
        tile_text(group, "TODAY", 10, 6, "day")
        tile_text(group, "No weather data yet.", 70, 50, "text")
        tile_text(group, "Hold A for 2 seconds to retry.", 70, 64, "text")
        return
    w = weather
    date = w["time"][:10]
    title = "TODAY - {} {} {}".format(weekday(date), MONTHS[int(date[5:7]) - 1], int(date[8:10]))
    tile_text(group, title, 10, 6, "day")
    stamp = ("as of " if status == "ONLINE" else "offline ") + fmt_clock(w["time"])
    tile_text(group, stamp, 287 - 6 * len(stamp), 9, "text")

    # Left panel: current conditions.
    weather_icon(group, w["code"], 33, 30, "big", night=not w["is_day"])
    noun = "Snow" if w["low"] <= 32 else "Rain"
    left = ("Now {}{}".format(w["temp"], DEG),
            "Hi {}{}".format(w["high"], DEG),
            "Lo {}{}".format(w["low"], DEG),
            "{} {}%".format(noun, round(w["rain_chance"])))
    for i, line in enumerate(left):
        center_text(group, line, 33, 72 + i * 12, "text")

    # Right panel: the written forecast.
    for i, line in enumerate(narrative(w)):
        tile_text(group, line, 67, 29 + i * 13, "text")

    # Details, two rows of three columns.
    cells = (
        ("Feels {}{}".format(w["feels"], DEG),
         "Wind {} {}".format(compass(w["wind_dir"]), w["wind"]),
         "Gusts {}".format(w["gust"])),
        ("Humid {}%".format(w["humidity"]),
         "UV {}".format(round(w["uv"])),
         "{}-{}".format(fmt_clock(w["sunrise"]), fmt_clock(w["sunset"]))),
    )
    for row, items in enumerate(cells):
        for col, item in enumerate(items):
            tile_text(group, item, 67 + col * 74, 98 + row * 12, "text")


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
    elif screen == "TODAY":
        today_overlay(group)
    elif screen == "GAME":
        game_overlay(group)
    display.root_group = group
    while display.time_to_refresh > 0:
        time.sleep(0.2)
    display.refresh()
    print("DISPLAY: refreshed", screen)
    gc.collect()


# ------------------- SIGNAL GAME (button D) ---------------------------
# A Simon-style memory game. M-01 plays a coded signal on the lights and
# speaker; repeat it with the buttons. Each round adds one step and plays a
# little faster. The e-ink screen only redraws before and after a game.

# Button -> (light color, tone in Hz). Tones are the classic Simon notes.
SIGNALS = (
    ((255, 0, 0), 310),     # A red
    ((255, 170, 0), 252),   # B yellow
    ((0, 255, 0), 415),     # C green
    ((0, 60, 255), 209),    # D blue
)
SIGNAL_NAMES = ("A red", "B yellow", "C green", "D blue")
INPUT_TIMEOUT = 6.0      # seconds of no press before the signal is lost
HOLD_TO_EXIT = 1.8       # hold D this long to leave the game

game_state = "READY"     # READY, PLAYING (your turn) or OVER
sequence = []
step = 0
game_score = 0
game_best = 0
new_best = False
lost_on = 0              # the step number the player missed
input_deadline = 0
exit_armed = False


def load_best():
    # The best score is kept in the board's non-volatile memory, so it
    # survives power-off. Byte 0 marks it as ours, byte 1 is the score.
    try:
        import microcontroller
        if microcontroller.nvm[0] == 0xA5:
            return microcontroller.nvm[1]
    except Exception as error:
        print("NVM READ ERROR:", repr(error))
    return 0


def save_best(value):
    try:
        import microcontroller
        microcontroller.nvm[0:2] = bytes((0xA5, min(255, value)))
    except Exception as error:
        print("NVM WRITE ERROR:", repr(error))


def signal_on(i):
    leds.fill((0, 0, 0))
    leds[LED_FOR_BUTTON[i]] = SIGNALS[i][0]
    tone_on(SIGNALS[i][1])


def signal_off():
    leds.fill((0, 0, 0))
    tone_off()


def play_sequence():
    # Gets quicker each round: 0.45 s per step at the start, 0.18 s at best.
    on = max(0.18, 0.45 - 0.025 * (len(sequence) - 1))
    off = max(0.07, on * 0.35)
    time.sleep(0.5)
    for i in sequence:
        signal_on(i)
        time.sleep(on)
        signal_off()
        time.sleep(off)


def resync_buttons():
    # Forget presses made while the sequence was playing.
    t = time.monotonic()
    for j, b in enumerate(buttons):
        last_pressed[j] = not b.value
        button_down_at[j] = t


def next_round():
    global step, input_deadline
    sequence.append(random.randrange(4))
    play_sequence()
    step = 0
    resync_buttons()
    input_deadline = time.monotonic() + INPUT_TIMEOUT


def start_game():
    global game_state, game_score, new_best
    print("SIGNAL: new game")
    sequence.clear()
    game_score = 0
    new_best = False
    game_state = "PLAYING"
    for i in range(4):          # quick "incoming" sweep
        leds[LED_FOR_BUTTON[i]] = SIGNALS[i][0]
        beep(600 + i * 150, 0.05)
    time.sleep(0.15)
    signal_off()
    next_round()


def round_cleared():
    global game_score
    game_score = len(sequence)
    print("SIGNAL: round", game_score, "cleared")
    time.sleep(0.15)
    leds.fill((0, 40, 0))
    beep(880, 0.06)
    beep(1175, 0.08)
    leds.fill((0, 0, 0))
    time.sleep(0.35)
    if len(sequence) >= 99:
        game_over(won=True)
    else:
        next_round()


def game_over(timeout=False, won=False):
    global game_state, game_best, new_best, lost_on
    lost_on = step + 1
    print("SIGNAL: game over, score", game_score, "(timeout)" if timeout else "")
    signal_off()
    if not won:
        leds.fill((255, 0, 0))
        for f in (220, 175, 131):
            beep(f, 0.18)
        beep(98, 0.45)
    leds.fill((0, 0, 0))
    if game_score > game_best:
        game_best = game_score
        new_best = True
        save_best(game_best)
    game_state = "OVER"
    draw()
    resync_buttons()


def game_press(i):
    # Light and sound while the button is held.
    signal_on(i)


def game_release(i, held):
    global step, input_deadline, game_state
    signal_off()
    if game_state != "PLAYING":
        start_game()
        return
    if i == sequence[step]:
        step += 1
        input_deadline = time.monotonic() + INPUT_TIMEOUT
        if step == len(sequence):
            round_cleared()
    else:
        game_over()


def enter_game():
    global game_state, game_best
    game_best = load_best()
    game_state = "READY"
    pixels_power(True)
    speaker_power(True)


def leave_game():
    global game_state
    signal_off()
    speaker_power(False)
    pixels_power(False)
    game_state = "READY"
    print("SIGNAL: exit")


def game_overlay(group):
    tile_text(group, "SIGNAL DECODER", 10, 6, "day")
    best = "Best {}".format(game_best)
    tile_text(group, best, 287 - 6 * len(best), 9, "text")
    if game_state == "OVER":
        center_text(group, str(game_score), 46, 40, "big")
        center_text(group, "round" if game_score == 1 else "rounds", 46, 86, "text")
        if game_score >= 99:
            lines = ("Every step decoded. Flawless!", "")
        elif new_best:
            lines = ("New best! M-01 is impressed.", "")
        else:
            more = game_best - game_score + 1
            lines = ("Signal lost on step {} of {}.".format(lost_on, len(sequence)),
                     "{} more round{} to beat your best.".format(more, "" if more == 1 else "s"))
        lines += ("", "Press any button to go again.", "Hold D to go back.")
        for n, line in enumerate(lines):
            tile_text(group, line, 96, 36 + n * 13, "text")
    else:
        lines = (
            "M-01 picked up a coded signal. Watch",
            "the lights and listen, then repeat",
            "the pattern with the buttons. Each",
            "round adds one more step.",
            "Press any button to start. Hold D to exit.",
        )
        for n, line in enumerate(lines):
            tile_text(group, line, 12, 31 + n * 14, "text")
    for i, name in enumerate(SIGNAL_NAMES):
        center_text(group, name, 37 + i * 74, 108, "text")


# ------------------- STARTUP -------------------------------------------
# Every wake from deep sleep starts code.py from the top. Work out why we
# woke up, restore the saved weather, and decide what to show.
wake = alarm.wake_alarm
woke_button = None
if isinstance(wake, alarm.pin.PinAlarm):
    for i, pin in enumerate(BUTTON_PINS):
        if wake.pin == pin:
            woke_button = i
woke_by_timer = isinstance(wake, alarm.time.TimeAlarm)
print("WAKE:", "button " + "ABCD"[woke_button] if woke_button is not None
      else ("timer" if woke_by_timer else "power on / reset"))
if wake is not None:
    load_state()
if not clock_ok():
    last_fetch = 0               # unknown clock: treat saved data as stale

if woke_by_timer:
    # Scheduled update: fetch quietly, redraw home, straight back to sleep.
    quiet = True
    fetch_weather()
    draw()
    go_to_sleep()

if weather is None or seconds_until_update() == 0:
    fetch_weather()
if woke_button == 3:
    screen = "GAME"
    enter_game()
elif woke_button is not None:
    screen = ("HOME", "FORECAST", "TODAY")[woke_button]
draw()
awake_until = time.monotonic() + AWAKE_SECONDS
# Don't treat the button that woke us as a fresh press.
for i, b in enumerate(buttons):
    last_pressed[i] = not b.value
    button_down_at[i] = time.monotonic()
print("READY: A Home (hold A sync) / B Forecast / C Today / D Signal game")

while True:
    now = time.monotonic()
    if screen != "GAME" and led_until and now >= led_until:
        pixels_power(False)
        led_until = 0

    dirty = False
    if screen != "GAME" and seconds_until_update() == 0:
        fetch_weather()
        dirty = True

    # Back to sleep once nobody has touched a button for a while.
    if not any(last_pressed) and now >= awake_until and not dirty:
        go_to_sleep()
    if screen == "GAME":
        # Holding D: once it has been held long enough, go quiet and dark so
        # the player knows letting go will leave the game.
        if last_pressed[3] and not ignore_entry_release and not exit_armed \
                and now - button_down_at[3] >= HOLD_TO_EXIT:
            exit_armed = True
            signal_off()
        if game_state == "PLAYING" and not any(last_pressed) and now > input_deadline:
            game_over(timeout=True)
            now = time.monotonic()

    for i, button in enumerate(buttons):
        pressed = not button.value
        if pressed != last_pressed[i]:
            idle = GAME_IDLE_SECONDS if screen == "GAME" or (i == 3 and pressed) else AWAKE_SECONDS
            awake_until = now + idle
        if pressed and not last_pressed[i]:
            button_down_at[i] = now
            if screen == "GAME":
                if not ignore_entry_release:
                    game_press(i)
            else:
                if i == 3:
                    screen = "GAME"
                    ignore_entry_release = True
                    enter_game()
                else:
                    screen = ("HOME", "FORECAST", "TODAY")[i]
                    flash((18, 18, 18), 0.25)
                dirty = True
        elif not pressed and last_pressed[i]:
            held = now - button_down_at[i]
            if screen == "GAME":
                if i == 3 and ignore_entry_release:
                    ignore_entry_release = False
                elif i == 3 and exit_armed:
                    exit_armed = False
                    leave_game()
                    screen = "HOME"
                    dirty = True
                else:
                    last_pressed[i] = pressed
                    game_release(i, held)
                    now = time.monotonic()
                    # game_release may have played a whole round and
                    # resynced the buttons; skip the rest of this scan.
                    break
            elif i == 0 and held >= 1.8:
                print("MANUAL WEATHER SYNC")
                fetch_weather()
                dirty = True
        last_pressed[i] = pressed

    if dirty:
        draw()
    time.sleep(0.035)
