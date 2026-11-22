# OUTPOST // 01 v0.6 | Sharper weather icons & a plain-language Today screen
# A: Home (hold ~2 sec to retry weather) | B: Forecast | C: Today | D: Reactor
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
print("OUTPOST // 01 v0.6 booting - LIGHT INTERFACE")
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
    "TODAY": "/today.bmp",
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
    # Mixed-case 6x13 text (ASCII 32-126 plus a degree sign) for the Today screen.
    "text": ("/text.bmp", "".join(chr(c) for c in range(32, 127)) + "\u00b0", 6, 13),
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
ICON_INDEX = {
    "CLEAR": 0, "PARTLY": 1, "CLOUDY": 2, "FOG": 3, "DRIZZLE": 4,
    "RAIN": 5, "SNOW": 6, "STORM": 7, "CLEAR_NIGHT": 8, "PARTLY_NIGHT": 9,
}

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
    # One TileGrid per string (not per character) keeps memory use low.
    bmp, chars, w, h = fonts[style]
    message = str(message)
    if style != "text":
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
        finally:
            response.close()
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
print("READY: A Home (hold A sync) / B Forecast / C Today / D Reactor")

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
                    screen = ("HOME", "FORECAST", "TODAY")[i]
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
