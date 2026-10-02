""" allsky_lightning.py

Thunderstorm / lightning capture module for Allsky.
https://github.com/AllskyTeam/allsky

Problem: a lightning bolt lasts ~10-200 ms and strikes at a RANDOM instant, so you
cannot react to a bolt and then shorten the exposure - it is long gone before any
detection finishes. Two independent quantities matter:

    * DUTY CYCLE  (fraction of time the sensor is open)  -> catch PROBABILITY
    * EXPOSURE LENGTH                                     -> image QUALITY

At night Allsky's auto-exposure happily picks 50-90 s. A bolt inside a 50 s frame is
completely blown out and the sky is washed white. This module watches the incoming
frames for the brief brightening a storm produces (pure software trigger, no extra
hardware), and when a storm is detected it switches NIGHT capture into LIGHTNING MODE:

    auto-exposure OFF, fixed short exposure (default 2 s), fixed moderate gain,
    minimal inter-frame delay (max duty cycle).

so a captured bolt stays crisp and the background does not clip. Every armed frame is
scanned for an actual bolt (bright transient vs. the previous frame); frames that
contain one are saved to the website 'lightning' gallery (+ thumbnail + a lightning.json
index) and optionally uploaded, exactly like the meteor module. When the storm has been
quiet for a cooldown period the ORIGINAL exposure settings are restored automatically.

Detection is DIFFERENCE based, not absolute-brightness based: a lightning flash is a
sudden POSITIVE change over a chunk of sky, which cleanly separates it from steady
moonlight / light pollution (those do not change frame-to-frame).

Daytime: optionally the module can also run on the day flow (day_enabled). A daytime
bolt against a bright sky is genuinely hard for an allsky - the sky is already bright
and daytime exposures are already short - so the day path is CAPTURE-ONLY: it detects
and saves bolt frames but does NOT touch the (already short) day exposure. Treat it as
best-effort; the strong dark storm-cell contrast is where it can still work.

The exposure change is pushed to the RUNNING camera (see _serviceCaptureReload): writing
settings.json alone is not enough, because the capture program only reads it at start.

Safety: the original night-exposure settings are saved on the FIRST override and always
restored on exit / after the cooldown / on cleanup, so the camera can never get stuck in
short-exposure mode - even across a service restart, a reboot, or into the next night.
"""
import allsky_shared as s
import os
import json
import time
import math
import subprocess
import signal
import urllib.request
import cv2
import numpy as np

metaData = {
    "name": "Lightning Capture",
    "description": "Detects thunderstorms from brightness transients and switches to short exposures to capture crisp lightning bolts",
    "version": "v0.11.1",
    "events": [
        "day",
        "night"
    ],
    "experimental": "true",
    "module": "allsky_lightning",
    "arguments": {
        "mask": "meteor_mask.png",
        "edge_feather": "35",
        "flash_delta": "18",
        "flash_min_area": "400",
        "flashes_to_arm": "2",
        "window_sec": "300",
        "cooldown_sec": "600",
        "lightning_exposure_ms": "2000",
        "lightning_gain": "150",
        "lightning_delay_ms": "0",
        "day_enabled": "false",
        "save_captures": "true",
        "bolt_delta": "40",
        "bolt_min_area": "60",
        "upload_remote": "true",
        "weather_gate": "false",
        "weather_cache_sec": "600",
        "weather_clear_cooldown_sec": "120",
        "min_sun_elevation": "-12.0",
        "reload_capture": "true",
        "reload_min_interval_sec": "300",
        "rearm_holdoff_sec": "1800",
        "outputdir": "",
        "save_debug": "false",
        "debug": "false"
    },
    "argumentdetails": {
        "mask": {
            "required": "false",
            "description": "Detection Mask",
            "help": "Image mask in the overlay images folder. White = sky to analyse, black = ignore (trees/horizon). You can reuse the meteor mask.",
            "type": {"fieldtype": "image"}
        },
        "edge_feather": {
            "required": "false",
            "description": "Mask Edge Feather (px)",
            "help": "Soft fade of the mask edge so the mask boundary itself is never mistaken for a bolt.",
            "type": {"fieldtype": "spinner", "min": 0, "max": 151, "step": 2}
        },
        "flash_delta": {
            "required": "true",
            "description": "Flash Threshold (gray levels)",
            "help": "How much brighter a pixel must be than in the PREVIOUS frame (0-255) to count as flash-lit. A lightning flash brightens a wide area at once; steady moonlight/light pollution does not change frame-to-frame, so it is ignored. Lower = more sensitive.",
            "type": {"fieldtype": "spinner", "min": 5, "max": 120, "step": 1}
        },
        "flash_min_area": {
            "required": "true",
            "description": "Flash Min Area (px)",
            "help": "How many pixels must brighten together for it to count as a flash (not a satellite glint or noise). A real flash lights up a large patch of sky. Raise if passing clouds arm the module.",
            "type": {"fieldtype": "spinner", "min": 50, "max": 20000, "step": 50}
        },
        "flashes_to_arm": {
            "required": "true",
            "description": "Flashes To Arm",
            "help": "Number of flashes within the window below before lightning mode switches on. 2 avoids arming on a single glint or a car headlight sweep.",
            "type": {"fieldtype": "spinner", "min": 1, "max": 10, "step": 1}
        },
        "window_sec": {
            "required": "false",
            "description": "Flash Window (s)",
            "help": "Rolling time window over which flashes are counted for arming.",
            "type": {"fieldtype": "spinner", "min": 30, "max": 1800, "step": 30}
        },
        "cooldown_sec": {
            "required": "false",
            "description": "Cooldown (s)",
            "help": "How long the sky must stay flash-free before lightning mode turns off and the original exposure settings are restored.",
            "type": {"fieldtype": "spinner", "min": 60, "max": 3600, "step": 30}
        },
        "lightning_exposure_ms": {
            "required": "true",
            "description": "Lightning Exposure (ms)",
            "help": "Fixed NIGHT exposure used in lightning mode. Short enough that a bolt is not blown out and the background stays dark; long enough to keep the duty cycle high. 2000 ms (2 s) is a good start; lower it under a bright/light-polluted sky.",
            "type": {"fieldtype": "spinner", "min": 100, "max": 15000, "step": 100}
        },
        "lightning_gain": {
            "required": "true",
            "description": "Lightning Gain",
            "help": "Fixed gain used in night lightning mode. Bolts are very bright, so a moderate gain is plenty and keeps noise/background down.",
            "type": {"fieldtype": "spinner", "min": 0, "max": 400, "step": 5}
        },
        "lightning_delay_ms": {
            "required": "false",
            "description": "Lightning Delay (ms)",
            "help": "Delay between frames in night lightning mode. 0 = maximum duty cycle (fewest bolts lost in the gap between frames). Raise only if the camera/Pi cannot keep up.",
            "type": {"fieldtype": "spinner", "min": 0, "max": 5000, "step": 50}
        },
        "day_enabled": {
            "required": "false",
            "description": "Also Capture In Daytime",
            "help": "Run the detector on daytime frames too. Best-effort: a daytime bolt against a bright sky is hard for an allsky, so the day path only DETECTS and SAVES bolts - it does not change the (already short) day exposure.",
            "type": {"fieldtype": "checkbox"}
        },
        "save_captures": {
            "required": "false",
            "description": "Save Bolt Frames",
            "help": "While armed, save every frame that actually contains a bolt to the 'lightning' gallery (+ thumbnail + index).",
            "type": {"fieldtype": "checkbox"}
        },
        "bolt_delta": {
            "required": "false",
            "description": "Bolt Threshold (gray levels)",
            "help": "Brightness increase over the PREVIOUS frame for a pixel to count as part of a saved bolt. Higher = only the brightest strikes are kept.",
            "type": {"fieldtype": "spinner", "min": 10, "max": 150, "step": 1}
        },
        "bolt_min_area": {
            "required": "false",
            "description": "Bolt Min Area (px)",
            "help": "Minimum number of newly-bright pixels for a frame to be saved as a bolt capture. Rejects sensor noise and faint scintillation.",
            "type": {"fieldtype": "spinner", "min": 10, "max": 5000, "step": 10}
        },
        "upload_remote": {
            "required": "false",
            "description": "Upload To Remote Website",
            "help": "If the remote website is enabled, upload each saved bolt image + thumbnail + the lightning.json index to it (folder 'lightning').",
            "type": {"fieldtype": "checkbox"}
        },
        "weather_gate": {
            "required": "false",
            "description": "Weather Gate (Open-Meteo)",
            "help": "Cross-check a free weather service (Open-Meteo, no API key, worldwide) using the camera's latitude/longitude. Two effects: (1) DAYTIME arming is blocked while the service reports a confidently calm/clear sky, so drifting daytime clouds can't arm the detector; (2) the cooldown is shortened to 'Weather Clear Cooldown' once the sky is confidently calm, so the camera resets sooner after a storm has clearly moved on. Night stays pure-optical. Fail-open: if the lookup fails the module behaves exactly as if this were off.",
            "type": {"fieldtype": "checkbox"}
        },
        "weather_cache_sec": {
            "required": "false",
            "description": "Weather Cache (s)",
            "help": "How long a weather reading is reused before the service is queried again. The API is hit at most this often (default 600 s = 10 min), so it never slows the capture loop.",
            "type": {"fieldtype": "spinner", "min": 120, "max": 3600, "step": 60}
        },
        "weather_clear_cooldown_sec": {
            "required": "false",
            "description": "Weather Clear Cooldown (s)",
            "help": "Shortened cooldown used while the weather service reports a confidently calm/clear sky (only when the Weather Gate is on). Lets the camera reset much sooner than the full Cooldown once a storm has clearly passed.",
            "type": {"fieldtype": "spinner", "min": 30, "max": 1800, "step": 30}
        },
        "min_sun_elevation": {
            "required": "false",
            "description": "Min Sun Elevation (deg)",
            "help": "The storm mode will not arm while the sun is higher than this elevation (degrees; -12 = end of nautical twilight, -6 = end of civil twilight). A brightness trigger cannot work against a bright, fast-changing twilight sky, so this blocks false arming at dusk/dawn even if the weather lookup is unavailable. Observed false flashes at this site sat at -6 to -8 deg, so the default is -12 to cover the whole ramp band with margin. Uses the camera latitude/longitude; if those are missing it never blocks.",
            "type": {"fieldtype": "spinner", "min": -18, "max": 10, "step": 1}
        },
        "reload_capture": {
            "required": "false",
            "description": "Apply Exposure To Running Camera",
            "help": "Push the storm exposure to the CAMERA, not just to settings.json. Allsky's capture program reads settings.json only once, at start (allsky.sh converts it into tmp/capture_args.txt and passes that snapshot), so without this the short exposure reaches the camera only at the next Allsky restart - the switch effectively never happens during a storm. With this on, the module uses Allsky's own reload path: SIGHUP to the capture program, which exits with EXIT_RESTARTING so the service restarts it and regenerates capture_args.txt. Costs a full capture restart - measured on a Pi 4 / ASI678MC: 9 s from the signal to the first new exposure, 16 s to the first saved image - and the exposure in flight is lost (up to 90 s at night). No elevated privileges are involved - capture runs as the same user as this module. SAFETY: the switch INTO the storm exposure only happens when allsky.service would also restart after a shutdown that times out (Restart=always / on-failure / on-abnormal). Allsky's default Restart=on-success does not, and an upload hanging on an unreachable remote website can make the shutdown time out - the camera then stays stopped. On such a service the storm is still detected and bolts are still saved, just at the normal exposure; see 'Service hardening' in the README. The switch BACK always happens when the camera needs it, and is skipped when the running camera already has the restored values.",
            "type": {"fieldtype": "checkbox"}
        },
        "reload_min_interval_sec": {
            "required": "false",
            "description": "Minimum Reload Interval (s)",
            "help": "Shortest time between two camera reloads. Every arm/disarm would otherwise restart the capture program, so a storm state that flaps near its threshold could restart the camera every few minutes. A reload blocked by this limit is NOT dropped - it stays pending and fires on a later frame, so the camera can never be left on an exposure that no longer matches settings.json.",
            "type": {"fieldtype": "spinner", "min": 60, "max": 3600, "step": 30}
        },
        "rearm_holdoff_sec": {
            "required": "false",
            "description": "Re-arm Hold-off (s)",
            "help": "After the normal exposure has been restored, do not switch back to the storm exposure for this long. At the edge of a storm the flash rate drifts around the arming threshold, and every switch in either direction is a full camera restart. The storm itself still re-arms and bolts are still captured - only the exposure switch waits. If the storm is still going when the hold-off ends, the switch happens then.",
            "type": {"fieldtype": "spinner", "min": 0, "max": 7200, "step": 60}
        },
        "outputdir": {
            "required": "false",
            "description": "Output Folder",
            "help": "Where bolt captures are stored (with a thumbnails/ subfolder). Empty = website 'lightning' folder so the gallery page finds them.",
            "type": {"fieldtype": "text"}
        },
        "save_debug": {
            "required": "false",
            "description": "Save Debug Images",
            "help": "Write the difference / mask debug images for tuning.",
            "type": {"fieldtype": "checkbox"}
        },
        "debug": {
            "required": "false",
            "description": "Verbose Logging",
            "help": "Log the flash area, state and bolt area on every frame.",
            "type": {"fieldtype": "checkbox"}
        }
    }
}

# --- persistent state between frames (module stays loaded in the postprocess service) ---
_maskCache = {"name": None, "soft": None, "hard": None}


def _stateDir():
    """Directory for state that must survive a REBOOT, not just a frame.

    ALLSKY_TMP is a tmpfs. Keeping the storm state there meant a restart while a storm
    was active wiped state["saved"] while settings.json still held the short exposure -
    the next arm then saved the override as the 'original' and every later restore was
    a no-op, leaving the camera on 2 s night frames for good. Falls back to ALLSKY_TMP
    if the real-disk directory cannot be created. Never raises."""
    try:
        base = s.getEnvironmentVariable("ALLSKY_HOME") or os.path.expanduser("~/allsky")
        path = os.path.join(base, "config", "allsky_lightning")
        os.makedirs(path, exist_ok=True)
        return path
    except Exception:
        return s.ALLSKY_TMP


STATE_FILE = os.path.join(_stateDir(), "allsky_lightning_state.json")
PREV_FRAME = os.path.join(s.ALLSKY_TMP, "allsky_lightning_prev.png")
WEATHER_FILE = os.path.join(s.ALLSKY_TMP, "allsky_lightning_weather.json")
STATS_FILE = os.path.join(s.ALLSKY_TMP, "allsky_lightning_stats.json")

# Open-Meteo: free, no API key, worldwide (the site is in Austria, so a Germany-only
# source like DWD/Bright Sky has no nearby station). We read the current WMO weather
# code and coarsely classify it. A "confidently calm" sky (used to gate daytime arming
# and to shorten the cooldown) means no precipitation at all.
_OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"
_CALM_CONDITIONS = {"dry", "fog"}


def _wmoCondition(code):
    """Map an Open-Meteo WMO weather code to a coarse condition string."""
    if code in (95, 96, 99):
        return "thunderstorm"
    if code in (0, 1, 2, 3):
        return "dry"           # clear .. overcast, no precipitation
    if code in (45, 48):
        return "fog"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    return "rain"              # drizzle / rain / showers (51..82) and anything else

# the exact settings.json keys the NIGHT lightning mode overrides / restores
_EXPOSURE_KEYS = ["nightautoexposure", "nightexposure",
                  "nightautogain", "nightgain", "nightdelay"]


def _truthy(v):
    """Checkbox args arrive from the flow config as the STRING 'true'/'false';
    'false' is truthy in Python, so parse booleans explicitly."""
    return v is True or (not isinstance(v, bool) and str(v).strip().lower() in ("true", "1", "yes", "on"))


def _loadState():
    """Persisted, restart-safe storm state. Never raises."""
    try:
        if os.path.exists(STATE_FILE):
            return json.load(open(STATE_FILE))
    except Exception:
        pass
    return {"active": False, "saved": None, "last_flash": 0.0, "flash_times": [],
            "override_unknown": False, "last_reload": 0.0, "reload_pending": False}


def _saveState(state):
    try:
        json.dump(state, open(STATE_FILE, "w"), default=float)
    except Exception as ex:
        s.log(1, f"WARNING: lightning could not write state: {ex}")


# --- lightweight nightly observability -----------------------------------------
# The detector runs ~1000x/night; verbose per-frame logging (debug=true) is unusable
# for tuning. Instead we keep a small rolling stats file that records only the rare,
# interesting events (actual flashes and blocked near-arms) plus running maxima, and
# emit ONE human-readable summary line to the log at dawn. Counters reset at dusk so
# each entry describes a single night. Never raises.
_STATS_RECENT_CAP = 60


def _newStats(period):
    return {"prev_period": period, "night_start": 0.0, "flashes": 0,
            "near_arms": 0, "blocked_sun": 0, "blocked_weather": 0, "arms": 0,
            "peak_in_window": 0, "max_flash_area": 0, "recent": []}


def _loadStats():
    try:
        if os.path.exists(STATS_FILE):
            return json.load(open(STATS_FILE))
    except Exception:
        pass
    return _newStats(None)


def _saveStats(stats):
    try:
        json.dump(stats, open(STATS_FILE, "w"), default=float)
    except Exception:
        pass


def _updateStats(now, period, is_flash, flash_area, flashes_in_window,
                 flashes_to_arm, arm_blocked, too_bright, wx_calm, weather_gate,
                 sun_elev, wx_condition, just_armed):
    """Accumulate per-night detector statistics and log a summary at day/night flips."""
    stats = _loadStats()
    prev = stats.get("prev_period")

    # dusk (day -> night): a fresh observation night begins - reset the counters.
    if prev == "day" and period == "night":
        stats = _newStats(period)
        stats["night_start"] = now
    # dawn (night -> day): emit the nightly report before the counters go stale.
    elif prev == "night" and period == "day":
        s.log(1, f"INFO: lightning night report - {stats.get('flashes', 0)} flashes, "
                 f"peak {stats.get('peak_in_window', 0)}/{flashes_to_arm} in window, "
                 f"max flash area {stats.get('max_flash_area', 0)}px; near-arms blocked "
                 f"{stats.get('near_arms', 0)} (sun {stats.get('blocked_sun', 0)}, "
                 f"weather {stats.get('blocked_weather', 0)}); storms armed "
                 f"{stats.get('arms', 0)}")
    stats["prev_period"] = period

    if is_flash:
        stats["flashes"] = stats.get("flashes", 0) + 1
        stats["max_flash_area"] = max(stats.get("max_flash_area", 0), flash_area)
        stats["peak_in_window"] = max(stats.get("peak_in_window", 0), flashes_in_window)
        reason = "+".join([r for r, on in
                           (("sun", too_bright), ("weather", weather_gate and wx_calm)) if on])
        if just_armed:
            stats["arms"] = stats.get("arms", 0) + 1
        elif flashes_in_window >= flashes_to_arm and arm_blocked:
            stats["near_arms"] = stats.get("near_arms", 0) + 1
            if too_bright:
                stats["blocked_sun"] = stats.get("blocked_sun", 0) + 1
            if weather_gate and wx_calm:
                stats["blocked_weather"] = stats.get("blocked_weather", 0) + 1
        rec = {"t": round(now), "area": int(flash_area), "inWin": int(flashes_in_window),
               "sun": (round(sun_elev, 1) if sun_elev is not None else None),
               "wx": (wx_condition if weather_gate else None),
               "blk": (reason or ("armed" if just_armed else "none"))}
        stats["recent"] = (stats.get("recent", []) + [rec])[-_STATS_RECENT_CAP:]

    _saveStats(stats)


def _parseLatLon(v):
    """settings.json stores coordinates like '48.136010N' / '14.389510E'.
    Return a signed decimal float, or None if it can't be parsed."""
    try:
        v = str(v).strip()
        sign = -1 if v[-1:].upper() in ("S", "W") else 1
        return sign * float(v.rstrip("NSEWnsew ").strip())
    except Exception:
        return None


def _sunElevation(lat, lon, t_epoch):
    """Solar elevation in degrees at (lat, lon) for a Unix timestamp, using the NOAA
    solar-position formulas (no external library). lon is east-positive.

    Returns the elevation in degrees, or None if the location is missing - callers
    treat None as 'unknown' and never block on it (fail-open, like the weather gate)."""
    if lat is None or lon is None:
        return None
    try:
        jd = t_epoch / 86400.0 + 2440587.5
        T = (jd - 2451545.0) / 36525.0
        L0 = (280.46646 + T * (36000.76983 + T * 0.0003032)) % 360.0
        M = 357.52911 + T * (35999.05029 - 0.0001537 * T)
        e = 0.016708634 - T * (0.000042037 + 0.0000001267 * T)
        Mr = math.radians(M)
        C = (math.sin(Mr) * (1.914602 - T * (0.004817 + 0.000014 * T))
             + math.sin(2 * Mr) * (0.019993 - 0.000101 * T)
             + math.sin(3 * Mr) * 0.000289)
        true_long = L0 + C
        omega = 125.04 - 1934.136 * T
        lam = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
        eps0 = 23.0 + (26.0 + ((21.448 - T * (46.815 + T * (0.00059 - T * 0.001813)))) / 60.0) / 60.0
        eps = eps0 + 0.00256 * math.cos(math.radians(omega))
        epsr = math.radians(eps)
        decl = math.asin(math.sin(epsr) * math.sin(math.radians(lam)))
        y = math.tan(epsr / 2.0) ** 2
        L0r = math.radians(L0)
        eot = 4.0 * math.degrees(
            y * math.sin(2 * L0r) - 2 * e * math.sin(Mr)
            + 4 * e * y * math.sin(Mr) * math.cos(2 * L0r)
            - 0.5 * y * y * math.sin(4 * L0r)
            - 1.25 * e * e * math.sin(2 * Mr))
        tst = ((t_epoch % 86400.0) / 60.0 + eot + 4.0 * lon) % 1440.0
        ha = math.radians(tst / 4.0 - 180.0)
        latr = math.radians(lat)
        cosz = math.sin(latr) * math.sin(decl) + math.cos(latr) * math.cos(decl) * math.cos(ha)
        cosz = max(-1.0, min(1.0, cosz))
        return 90.0 - math.degrees(math.acos(cosz))
    except Exception:
        return None


WEATHER_STALE_OK_SEC = 3600   # a failed lookup keeps using the last good answer this long


def _getWeatherCondition(lat, lon, cache_sec):
    """Current sky 'condition' at the site from Open-Meteo (free, no key, worldwide),
    cached in ALLSKY_TMP so the API is hit at most every cache_sec.

    A failed lookup (Open-Meteo answers 503 or times out a few times a week) keeps
    using the last good answer for up to WEATHER_STALE_OK_SEC: one server hiccup must
    not open the gate. On 2026-09-26 a single 503 at 21:07 did exactly that, and the
    moonlit clouds armed the storm mode for 5 hours on a clear night.

    FAIL-OPEN beyond that: with no good answer for an hour, or no location, it returns
    None, and every caller treats None as 'no weather info' (never blocks arming,
    never shortens the cooldown). The weather gate can therefore never leave the
    camera stuck.

    Returns a coarse condition string (dry/fog/rain/snow/thunderstorm) or None."""
    cache = {}
    try:
        if os.path.exists(WEATHER_FILE):
            cache = json.load(open(WEATHER_FILE))
            if time.time() - cache.get("ts", 0) <= cache_sec:
                return cache.get("condition")
    except Exception:
        cache = {}
    if lat is None or lon is None:
        return None
    condition = None
    try:
        url = f"{_OPENMETEO_URL}?latitude={lat:.4f}&longitude={lon:.4f}&current=weather_code"
        with urllib.request.urlopen(url, timeout=4) as r:
            data = json.load(r)
        code = (data.get("current") or {}).get("weather_code")
        condition = _wmoCondition(code) if code is not None else None
    except Exception as ex:
        s.log(1, f"WARNING: lightning weather lookup failed: {ex}")
        condition = None
    good = cache.get("good")
    if condition is not None:
        good = {"ts": time.time(), "condition": condition}
    elif good and time.time() - good.get("ts", 0) <= WEATHER_STALE_OK_SEC:
        condition = good.get("condition")          # bridge the outage with the last answer
    try:  # cache even a None so a flapping network doesn't hammer the API
        json.dump({"ts": time.time(), "condition": condition, "good": good}, open(WEATHER_FILE, "w"))
    except Exception:
        pass
    return condition


def _resolveOutputDir(params):
    outdir = (params.get("outputdir", "") or "").strip()
    if not outdir:
        website = s.getEnvironmentVariable("ALLSKY_WEBSITE") or \
            os.path.join(s.getEnvironmentVariable("ALLSKY_HOME") or os.path.expanduser("~/allsky"),
                         "html", "allsky")
        outdir = os.path.join(website, "lightning")
    return outdir, os.path.join(outdir, "thumbnails")


def _loadMask(maskName, feather, shape):
    """Return (soft float 0..1 mask, hard uint8 mask) matching the frame, cached."""
    if _maskCache["name"] == (maskName, feather) and _maskCache["soft"] is not None \
            and _maskCache["soft"].shape == shape:
        return _maskCache["soft"], _maskCache["hard"]
    hard = None
    if maskName:
        p = os.path.join(s.ALLSKY_OVERLAY, "images", maskName)
        hard = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
    if hard is None:
        hard = np.full(shape, 255, np.uint8)
    if hard.shape != shape:
        hard = cv2.resize(hard, (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST)
    f = s.int(feather)
    if f > 0:
        k = f + (1 - f % 2)  # odd
        soft = cv2.GaussianBlur(hard, (k, k), 0).astype(np.float32) / 255.0
    else:
        soft = hard.astype(np.float32) / 255.0
    _maskCache.update(name=(maskName, feather), soft=soft, hard=hard)
    return soft, hard


# --- pushing new settings to the RUNNING capture program ------------------------
# s.updateSetting() only rewrites settings.json, and the capture program never re-reads
# it: allsky.sh converts settings.json into tmp/capture_args.txt ONCE at start and passes
# that snapshot via -config. Measured in the field on 2026-08-22: a storm armed at
# 22:00:21 and the exposure stayed at 90 s for 20+ frames, only changing at 00:00:40 when
# the Pi rebooted. In other words the storm switch never actually reached the camera.
#
# Allsky's supported way to apply new settings is scripts/utilities/reload.sh, which the
# service runs as ExecReload: it sends SIGHUP to the capture program, whose handler exits
# with EXIT_RESTARTING (98); allsky.sh then exits 0 and systemd's Restart=on-success
# starts it again, regenerating capture_args.txt from settings.json. Note this is a full
# capture restart, not an in-place reload - allsky_common.cpp carries an explicit
# "TODO: Re-read configuration instead of restarting." Measured cost on a Pi 4 / ASI678MC:
# 9 s from the signal to the first new exposure, 16 s to the first saved image, plus the
# exposure in flight. capture runs as the same user as this module, so no privileges.

def _capturePid():
    """PID of the running capture program, or None. Mirrors reload.sh: capture is the
    child of allsky.sh, which is the service MainPID. Never raises."""
    try:
        main = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", "allsky"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
        if main.isdigit() and int(main) > 0:
            kids = subprocess.run(["pgrep", "--parent", main],
                                  capture_output=True, text=True, timeout=10).stdout.split()
            if kids:
                return int(kids[0])
    except Exception:
        pass
    for name in ("capture_ZWO", "capture_RPi"):     # fallback if systemd is unavailable
        try:
            out = subprocess.run(["pgrep", "-x", name],
                                 capture_output=True, text=True, timeout=10).stdout.split()
            if out:
                return int(out[0])
        except Exception:
            pass
    return None


def _serviceCaptureReload(state, min_interval):
    """Send a pending SIGHUP if the rate limit allows. A throttled request STAYS pending
    and is retried on later frames, so the camera can never be left running an exposure
    that no longer matches settings.json. Never raises."""
    if not state.get("reload_pending"):
        return False
    now = time.time()
    if now - state.get("last_reload", 0.0) < min_interval:
        return False
    pid = _capturePid()
    if pid is None:
        state["reload_pending"] = False
        s.log(1, "WARNING: lightning found no capture process to reload - the new "
                 "exposure will apply at the next Allsky restart")
        return False
    # Persist BEFORE signalling. This module runs as a DESCENDANT of the capture program
    # (capture -> saveImage.sh -> flow-runner.py -> here), so the SIGHUP tears down our
    # parent while we are still running. Writing the state first means a lost tail can
    # never drop the rate limit and turn this into a restart loop.
    state["last_reload"] = now
    state["reload_pending"] = False
    _saveState(state)
    try:
        os.kill(pid, signal.SIGHUP)
    except Exception as ex:
        s.log(1, f"WARNING: lightning could not signal capture (pid {pid}): {ex}")
        return False
    s.log(1, f"INFO: lightning reloading capture (SIGHUP to pid {pid}) - Allsky restarts "
             "it in ~9 s with the new exposure")
    return True


def _requestCaptureReload(state, min_interval):
    """Mark that capture must pick up settings.json, and do it as soon as allowed."""
    state["reload_pending"] = True
    return _serviceCaptureReload(state, min_interval)


# --- is a capture restart safe on this install? ---------------------------------------
# A reload is a full service restart, and whether the service comes back depends on how
# its SHUTDOWN goes. When capture exits, systemd sends SIGTERM to everything left in the
# service and waits TimeoutStopSec (90 s by default). Some of what is left ignores that on
# purpose: upload.sh runs `trap "" SIGTERM` so a transfer is never cut off (lftp inherits
# the ignore), and capture itself starts a 'Restarting' notification upload the moment it
# gets the SIGHUP. With the remote website unreachable those uploads sit in lftp retries,
# the stop runs past 90 s, and systemd records the result as 'timeout'. Allsky's unit has
# Restart=on-success, which restarts only on a clean result - so the camera stays down
# until someone restarts it by hand. Seen on 2026-09-20: three reloads in 20 minutes,
# the first two came back, the third stopped the camera for the rest of the night.

def _restartSurvivesStopTimeout():
    """True when systemd restarts allsky.service even after a stop that timed out:
    Restart=always, on-failure or on-abnormal. Allsky's default on-success does not.
    Readable without privileges. Unknown counts as unsafe."""
    try:
        policy = subprocess.run(["systemctl", "show", "-p", "Restart", "--value", "allsky"],
                                capture_output=True, text=True, timeout=10).stdout.strip()
        return policy in ("always", "on-failure", "on-abnormal")
    except Exception:
        return False


def _liveNightArgs():
    """The night-exposure values the RUNNING capture was started with. allsky.sh writes
    them to ALLSKY_TMP/capture_args.txt at every start and capture never re-reads
    settings.json, so this - not settings.json - is what the camera is doing.
    None when unreadable."""
    try:
        live = {}
        with open(os.path.join(s.ALLSKY_TMP, "capture_args.txt")) as fh:
            for line in fh:
                key, sep, value = line.strip().partition("=")
                if sep and key in _EXPOSURE_KEYS:
                    live[key] = value
        return live if len(live) == len(_EXPOSURE_KEYS) else None
    except Exception:
        return None


def _liveMatches(values):
    """True when the running capture already uses exactly these night-exposure values,
    so restarting it would change nothing. Unreadable counts as a mismatch, so a needed
    reload is never skipped on a guess."""
    live = _liveNightArgs()
    if not live or not values:
        return False
    try:
        for key in _EXPOSURE_KEYS:
            want, have = values.get(key), live.get(key)
            if key in ("nightautoexposure", "nightautogain"):
                if _truthy(want) != _truthy(have):
                    return False
            elif s.int(want) != s.int(have):
                return False
        return True
    except Exception:
        return False


def _mayOverride(state, now, reload_capture, rearm_holdoff):
    """Whether this frame may switch the camera to the storm exposure. Two reasons not
    to, each logged once per storm:
      * the service would not come back from a restart whose shutdown times out;
      * the exposure was restored only moments ago, i.e. the storm is flapping at its
        edge, and every switch is another restart.
    Only the exposure switch is skipped: the storm stays active and bolts are still
    captured, at the normal exposure."""
    reason = None
    if reload_capture and not _restartSurvivesStopTimeout():
        reason = "unsafe"
    elif now - state.get("restored_at", 0.0) < rearm_holdoff:
        reason = "holdoff"
    if reason is None:
        state.pop("override_skipped", None)
        return True
    if state.get("override_skipped") != reason:
        state["override_skipped"] = reason
        if reason == "unsafe":
            s.log(1, "WARNING: lightning storm active but NOT switching the camera to the "
                     "storm exposure: allsky.service has Restart=on-success, so a restart "
                     "whose shutdown times out - e.g. an upload stuck on an unreachable "
                     "remote website - leaves the camera stopped until restarted by hand. "
                     "Bolts are still captured at the normal exposure. To enable the "
                     "switch, see 'Service hardening' in the module README.")
        else:
            since = int(now - state.get("restored_at", 0.0))
            s.log(1, f"INFO: lightning storm active again {since} s after the exposure "
                     f"was restored - keeping the normal exposure for another "
                     f"{int(rearm_holdoff) - since} s rather than restarting the camera "
                     "on a flapping storm edge")
    return False


def _isOverride(current, expo_ms, gain, delay_ms):
    """True when the night settings already ARE this module's short-exposure override.
    Such values can never be genuine 'originals'."""
    try:
        return (not _truthy(current.get("nightautoexposure"))
                and not _truthy(current.get("nightautogain"))
                and s.int(current.get("nightexposure")) == s.int(expo_ms)
                and s.int(current.get("nightgain")) == s.int(gain))
    except Exception:
        return False


def _enterLightningMode(state, expo_ms, gain, delay_ms):
    """Save the current night-exposure settings ONCE, then switch to short exposures.
    Returns True when the mode was actually entered."""
    if not state.get("saved"):
        # only capture originals when the exposure is NOT already overridden, so a
        # restart mid-storm can never save the short exposure as the 'original'.
        current = {k: s.getSetting(k) for k in _EXPOSURE_KEYS}
        if _isOverride(current, expo_ms, gain, delay_ms):
            # Belt-and-braces behind the persistent STATE_FILE: the night settings are
            # already our override while we hold no saved originals, so the real values
            # are unrecoverable from here. Saving these would make every later restore a
            # no-op and keep the camera on short night frames for good - black images
            # that removeBadImages.sh then deletes. Refuse and say so loudly instead.
            state["override_unknown"] = True
            s.log(0, "ERROR: lightning NOT entering mode - night settings already look "
                     f"like the short-exposure override ({current}) but no originals "
                     "are saved. Restore nightautoexposure/nightexposure/nightautogain/"
                     "nightgain/nightdelay by hand; the mode stays off until then.")
            return False
        state["saved"] = current
    s.updateSetting([
        {"nightautoexposure": False},
        {"nightexposure": s.int(expo_ms)},
        {"nightautogain": False},
        {"nightgain": s.int(gain)},
        {"nightdelay": s.int(delay_ms)},
    ])
    s.log(1, f"INFO: lightning mode ON - exposure {expo_ms} ms, gain {gain}, "
             f"delay {delay_ms} ms (was {state['saved']})")
    return True


def _exitLightningMode(state):
    """Restore whatever the night-exposure settings were before the storm."""
    saved = state.get("saved")
    if saved:
        s.updateSetting([{k: saved[k]} for k in _EXPOSURE_KEYS if saved.get(k) is not None])
        s.log(1, f"INFO: lightning mode OFF - restored {saved}")
    state["saved"] = None
    # clear the refusal too, so a later storm can arm again once the settings are fixed
    state["override_unknown"] = False


def _saveBolt(outdir, thumbdir, fname, rec):
    """Write the true-colour bolt frame + thumbnail + append the lightning.json index.
    Returns 1/0. Never raises."""
    try:
        os.makedirs(thumbdir, exist_ok=True)
        cv2.imwrite(os.path.join(outdir, fname), s.image)          # GALLERY: untouched colours
        h, w = s.image.shape[:2]
        tw = 300
        thumb = cv2.resize(s.image, (tw, max(1, int(h * tw / w))), interpolation=cv2.INTER_AREA)
        cv2.imwrite(os.path.join(thumbdir, fname), thumb)
    except Exception as ex:
        s.log(1, f"WARNING: lightning could not save capture {fname}: {ex}")
        return 0
    logpath = os.path.join(outdir, "lightning.json")
    try:
        log = json.load(open(logpath)) if os.path.exists(logpath) else []
    except Exception:
        log = []
    log.append(rec)
    try:
        json.dump(log[-2000:], open(logpath, "w"), default=float)
    except Exception as ex:
        s.log(1, f"WARNING: lightning could not write index: {ex}")
    return 1


def _uploadRemote(outdir, thumbdir, fname):
    """Upload a saved bolt image + thumbnail + the index to the remote website via
    Allsky's upload.sh. Mirrors how the meteor module uploads. Never raises."""
    try:
        if str(s.getSetting("useremotewebsite")).lower() not in ("true", "1", "yes", "on"):
            return
        scripts = s.getEnvironmentVariable("ALLSKY_SCRIPTS") or \
            os.path.join(s.getEnvironmentVariable("ALLSKY_HOME") or os.path.expanduser("~/allsky"), "scripts")
        uploader = os.path.join(scripts, "upload.sh")
        if not os.path.isfile(uploader):
            return
        base = (s.getSetting("remotewebsiteimagedir") or "").rstrip("/")
        remote_dir = f"{base}/lightning" if base else "lightning"
        for local, rdir, tag in (
            (os.path.join(outdir, fname), remote_dir, "Lightning"),
            (os.path.join(thumbdir, fname), remote_dir + "/thumbnails", "LightningThumb"),
            # the index that drives the chart + gallery - without it the remote
            # page has the images but no data, so both stay empty
            (os.path.join(outdir, "lightning.json"), remote_dir, "LightningLog"),
        ):
            if os.path.isfile(local):
                subprocess.Popen([uploader, "--silent", "--wait", "--remote-web", local, rdir, fname, tag],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as ex:
        s.log(1, f"WARNING: lightning remote upload failed: {ex}")


def lightning(params, event):
    if s.image is None:
        s.log(0, "ERROR: lightning module received no image")
        return "no image"

    # Allsky calls the flow with event="postcapture" for per-image runs - NOT "day"/
    # "night". The real time of day is in the DAY_OR_NIGHT environment variable. Reading
    # `event` here made EVERY frame look like "night", so day_enabled never took effect
    # and the bright daytime sky / drifting clouds were processed as if at night and
    # saved as false "bolts". Fall back to the event arg only if the env var is missing.
    tod = (s.getEnvironmentVariable("DAY_OR_NIGHT") or "").strip().lower()
    if tod not in ("day", "night"):
        tod = "day" if str(event).lower() == "day" else "night"
    period = tod
    debug = _truthy(params.get("debug", False))
    now = time.time()

    # --- tunables ---
    mask_name = params.get("mask", "") or ""
    feather = s.int(params.get("edge_feather", 35))
    flash_delta = s.int(params.get("flash_delta", 18))
    flash_min_area = s.int(params.get("flash_min_area", 400))
    flashes_to_arm = s.int(params.get("flashes_to_arm", 2))
    window_sec = s.asfloat(params.get("window_sec", 300))
    cooldown_sec = s.asfloat(params.get("cooldown_sec", 600))
    expo_ms = s.int(params.get("lightning_exposure_ms", 2000))
    gain = s.int(params.get("lightning_gain", 150))
    delay_ms = s.int(params.get("lightning_delay_ms", 0))
    day_enabled = _truthy(params.get("day_enabled", False))
    save_captures = _truthy(params.get("save_captures", True))
    bolt_delta = s.int(params.get("bolt_delta", 40))
    bolt_min_area = s.int(params.get("bolt_min_area", 60))
    upload_remote = _truthy(params.get("upload_remote", True))
    weather_gate = _truthy(params.get("weather_gate", False))
    weather_cache_sec = s.asfloat(params.get("weather_cache_sec", 600))
    weather_clear_cooldown_sec = s.asfloat(params.get("weather_clear_cooldown_sec", 120))
    min_sun_elevation = s.asfloat(params.get("min_sun_elevation", -12.0))
    reload_capture = _truthy(params.get("reload_capture", True))
    reload_min_interval = s.asfloat(params.get("reload_min_interval_sec", 300))
    rearm_holdoff = s.asfloat(params.get("rearm_holdoff_sec", 1800))

    # Daytime capture with a brightness trigger is hopeless - the bright sky and drifting
    # clouds swamp any bolt (they get saved as false "sunshine bolts"). So unless
    # day_enabled is set we do NOT detect or capture during the day. We still fall through
    # to the state machine below, so a storm that ends around dawn is disarmed and the
    # night exposure restored.
    detect = not (period == "day" and not day_enabled)

    outdir, thumbdir = _resolveOutputDir(params)

    # --- difference vs previous frame: only NEW light (a flash/bolt appears) --------
    gray = None
    diff = None
    have_prev = False
    flash_area = 0
    bolt_area = 0
    peak = 0
    if detect:
        gray = cv2.cvtColor(s.image, cv2.COLOR_BGR2GRAY)
        soft, hard = _loadMask(mask_name, feather, gray.shape)
        prev = cv2.imread(PREV_FRAME, cv2.IMREAD_GRAYSCALE)
        have_prev = prev is not None and prev.shape == gray.shape
        if have_prev:
            diff = cv2.subtract(gray, prev)                # clamps at 0: darker -> 0
            diff = (diff.astype(np.float32) * soft).astype(np.uint8)
            flash_area = int(np.count_nonzero(diff >= flash_delta))
            bolt_area = int(np.count_nonzero(diff >= bolt_delta))
            peak = int(diff.max())
    is_flash = detect and have_prev and flash_area >= flash_min_area

    state = _loadState()
    if is_flash:
        state["last_flash"] = now
        state.setdefault("flash_times", []).append(now)
    state["flash_times"] = [t for t in state.get("flash_times", []) if now - t <= window_sec]
    flashes_in_window = len(state["flash_times"])

    # --- optional weather gate (Bright Sky / DWD, opt-in, fail-open) ----------
    # Only look up the weather when it can actually change a decision: a flash just
    # happened or the window holds enough flashes (possible arming), or we are armed
    # (possible cooldown). The result is cached so the API is hit at most every
    # weather_cache_sec.
    wx_condition = None
    wx_calm = False   # sky is confidently calm/clear per the weather service
    # The gates must also be checked on a quiet frame that follows enough flashes:
    # the window still holds them, so that frame can arm too.
    may_arm = not state["active"] and flashes_in_window >= flashes_to_arm
    if weather_gate and (is_flash or state["active"] or may_arm):
        lat = _parseLatLon(s.getSetting("latitude"))
        lon = _parseLatLon(s.getSetting("longitude"))
        wx_condition = _getWeatherCondition(lat, lon, weather_cache_sec)
        wx_calm = wx_condition in _CALM_CONDITIONS

    # --- sun-elevation guard (independent backstop, fail-open) ---------------
    # A brightness-transient trigger cannot work while the sky itself is bright and
    # changing fast: the twilight ramp and sunlit clouds swamp any real bolt, and you
    # cannot get a crisp bolt against a bright sky anyway. So refuse to arm until the sun
    # is safely below the horizon (min_sun_elevation, default -12 deg = end of nautical
    # twilight). This backstops the weather gate for the case where the weather lookup is
    # unavailable/stale (which fails open). Unknown location -> None -> never blocks.
    sun_elev = None
    if is_flash or state["active"] or may_arm:
        sun_elev = _sunElevation(_parseLatLon(s.getSetting("latitude")),
                                 _parseLatLon(s.getSetting("longitude")), now)
    too_bright = sun_elev is not None and sun_elev > min_sun_elevation

    # --- arming state machine (period-independent) ---------------------------
    # Weather gate, effect 1: don't let optical flashes arm the storm mode while the
    # weather service reports a confidently calm/clear sky (dry/fog) - day OR night.
    # This stops drifting daytime clouds AND the fast brightness changes of twilight /
    # moonlit clouds from false-arming on a night when there is no storm. A real storm
    # never reads as calm (it reports rain/thunderstorm), so genuine night storms still
    # arm optically. Unknown weather (None) never blocks (fail-open).
    # Plus the sun-elevation guard above: never arm while the sun is above the threshold.
    arm_blocked = (weather_gate and wx_calm) or too_bright
    # Weather gate, effect 2: a confidently calm sky shortens the cooldown, so the
    # camera resets much sooner once a storm has clearly moved on.
    eff_cooldown = weather_clear_cooldown_sec if (weather_gate and wx_calm) else cooldown_sec

    if not (state["active"] and weather_gate and wx_calm):
        state.pop("calm_since", None)
    just_armed = False
    if not state["active"] and flashes_in_window >= flashes_to_arm and not arm_blocked:
        state["active"] = True
        just_armed = True
        s.log(1, f"INFO: lightning STORM detected ({flashes_in_window} flashes / {int(window_sec)}s)")
    elif state["active"] and (now - state.get("last_flash", 0)) > eff_cooldown:
        state["active"] = False
        state.pop("override_skipped", None)     # next storm reports its own reason
        state.pop("calm_since", None)
        s.log(1, f"INFO: lightning storm ended (cooldown {int(eff_cooldown)}s elapsed"
                 + (f", weather={wx_condition}" if weather_gate else "") + ")")
    elif state["active"] and weather_gate and wx_calm \
            and now - state.setdefault("calm_since", now) > weather_clear_cooldown_sec:
        # Weather gate, effect 3: the weather service has reported a calm sky (dry/fog)
        # for longer than the clear cooldown, so end the storm even though "flashes"
        # keep coming. Drifting moonlit clouds produce a flash on nearly every short
        # exposure, so waiting for a flash-free cooldown can take all night (5 h on
        # 2026-09-26). A real storm never reads as calm.
        state["active"] = False
        state.pop("override_skipped", None)
        state.pop("calm_since", None)
        s.log(1, f"INFO: lightning storm ended (weather {wx_condition} for "
                 f"{int(weather_clear_cooldown_sec)}s although flashes continue)")

    # --- apply / restore the short exposure to match the storm state ---------
    transitioned = False
    # ENTER lightning mode: NIGHT only - we only ever override the night exposure.
    # _mayOverride can veto the switch (unsafe restart, or flapping); the storm itself
    # stays active either way, and a vetoed frame falls through to the pending-reload
    # flush below instead of starving it.
    if period == "night" and state["active"] and not state.get("saved") \
            and not state.get("override_unknown") \
            and _mayOverride(state, now, reload_capture, rearm_holdoff):
        transitioned = _enterLightningMode(state, expo_ms, gain, delay_ms)
        if transitioned and reload_capture:
            _requestCaptureReload(state, reload_min_interval)
    # EXIT / restore: from ANY flow (day or night). If a storm ends after the
    # day/night boundary (e.g. it keeps going past dawn) the night exposure would
    # otherwise stay overridden until the next real night frame - hours later.
    # Restoring the night settings from the day flow is harmless (day uses the day
    # exposure) and resets the camera as soon as the cooldown elapses.
    elif not state["active"] and (state.get("saved") or state.get("override_unknown")):
        saved = state.get("saved")
        _exitLightningMode(state)
        transitioned = True
        if saved:
            state["restored_at"] = now
        # Reload from the day flow too: capture_args.txt holds BOTH the day and the night
        # settings and is fixed for the whole service run, so a night exposure restored
        # during the day would otherwise still not apply at dusk. The restore always
        # reloads when needed, even on an unhardened service: leaving the camera on the
        # storm exposure means dark frames all night. But a restart that would change
        # nothing is skipped - every restart is a chance for the service not to return.
        if saved and reload_capture:
            if _liveMatches(saved):
                s.log(1, "INFO: lightning restored settings already match the running "
                         "camera - no restart needed")
            else:
                _requestCaptureReload(state, reload_min_interval)
    elif reload_capture:
        # steady state - flush a request an earlier frame had to throttle
        _serviceCaptureReload(state, reload_min_interval)

    # --- bolt capture (only while armed AND actually detecting) --------------
    result = "quiet"
    if detect and state["active"]:
        result = "armed"
        # skip the frame straight after an exposure change: its diff vs the
        # differently-scaled previous frame is meaningless.
        if have_prev and not transitioned and bolt_area >= bolt_min_area:
            stamp = time.strftime("%Y%m%d%H%M%S", time.localtime(now))
            fname = f"lightning-{stamp}.jpg"
            expo_used = expo_ms if (period == "night") else s.getSetting("dayexposure")
            rec = {"time": stamp, "file": fname, "area": bolt_area, "peak": peak,
                   "period": period, "exposure_ms": expo_used}
            if save_captures and _saveBolt(outdir, thumbdir, fname, rec):
                result = f"BOLT area={bolt_area} peak={peak}"
                s.log(1, f"INFO: lightning bolt captured {stamp} ({period}, area {bolt_area}px, peak +{peak})")
                if _truthy(params.get("save_debug", False)):
                    s.startModuleDebug("allsky_lightning")
                    s.writeDebugImage("allsky_lightning", f"diff-{stamp}.png", diff)
                if upload_remote:
                    _uploadRemote(outdir, thumbdir, fname)
            else:
                result = f"bolt area={bolt_area}"

    # --- roll the previous frame ---------------------------------------------
    if detect:
        if transitioned:
            # exposure scale just changed: drop the stale reference so the next frame
            # starts clean instead of firing a false flash on the brightness jump.
            try:
                if os.path.exists(PREV_FRAME):
                    os.remove(PREV_FRAME)
            except Exception:
                pass
        else:
            try:
                cv2.imwrite(PREV_FRAME, gray)
            except Exception:
                pass

    _saveState(state)

    # nightly stats + dawn summary (records flashes/blocked near-arms, resets at dusk)
    _updateStats(now, period, is_flash, flash_area, flashes_in_window, flashes_to_arm,
                 arm_blocked, too_bright, wx_calm, weather_gate, sun_elev, wx_condition,
                 just_armed)

    if debug:
        s.log(1, f"INFO: lightning [{period}] flashArea={flash_area} boltArea={bolt_area} "
                 f"peak={peak} flash={is_flash} inWin={flashes_in_window} "
                 f"active={state['active']} override={bool(state.get('saved'))}")

    # expose a couple of variables for the overlay
    try:
        s.saveExtraData("allsky_lightning.json", {
            "AS_LIGHTNING_MODE": "ON" if state["active"] else "OFF",
            "AS_LIGHTNING_FLASHES": flashes_in_window,
            "AS_LIGHTNING_WX": (wx_condition or "n/a") if weather_gate else "off",
            "AS_LIGHTNING_SUN": (round(sun_elev, 1) if sun_elev is not None else "n/a"),
        })
    except Exception:
        pass

    return result


def lightning_cleanup():
    """Called when the module is removed from the flow: make sure we never leave the
    camera stuck in short-exposure mode."""
    state = _loadState()
    if state.get("saved"):
        _exitLightningMode(state)
        state["active"] = False
        _saveState(state)
    moduleData = {
        "metaData": metaData,
        "cleanup": {
            "files": {STATE_FILE, PREV_FRAME, WEATHER_FILE, STATS_FILE},
            "env": {}
        }
    }
    s.cleanupModule(moduleData)
