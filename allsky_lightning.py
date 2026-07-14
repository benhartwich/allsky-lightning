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

Safety: the original night-exposure settings are saved on the FIRST override and always
restored on exit / after the cooldown / on cleanup, so the camera can never get stuck in
short-exposure mode - even across a service restart or into the next night.
"""
import allsky_shared as s
import os
import json
import time
import subprocess
import cv2
import numpy as np

metaData = {
    "name": "Lightning Capture",
    "description": "Detects thunderstorms from brightness transients and switches to short exposures to capture crisp lightning bolts",
    "version": "v0.2.0",
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
STATE_FILE = os.path.join(s.ALLSKY_TMP, "allsky_lightning_state.json")
PREV_FRAME = os.path.join(s.ALLSKY_TMP, "allsky_lightning_prev.png")

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
    return {"active": False, "saved": None, "last_flash": 0.0, "flash_times": []}


def _saveState(state):
    try:
        json.dump(state, open(STATE_FILE, "w"), default=float)
    except Exception as ex:
        s.log(1, f"WARNING: lightning could not write state: {ex}")


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


def _enterLightningMode(state, expo_ms, gain, delay_ms):
    """Save the current night-exposure settings ONCE, then switch to short exposures."""
    if not state.get("saved"):
        # only capture originals when the exposure is NOT already overridden, so a
        # restart mid-storm can never save the short exposure as the 'original'.
        state["saved"] = {k: s.getSetting(k) for k in _EXPOSURE_KEYS}
    s.updateSetting([
        {"nightautoexposure": False},
        {"nightexposure": s.int(expo_ms)},
        {"nightautogain": False},
        {"nightgain": s.int(gain)},
        {"nightdelay": s.int(delay_ms)},
    ])
    s.log(1, f"INFO: lightning mode ON - exposure {expo_ms} ms, gain {gain}, "
             f"delay {delay_ms} ms (was {state['saved']})")


def _exitLightningMode(state):
    """Restore whatever the night-exposure settings were before the storm."""
    saved = state.get("saved")
    if saved:
        s.updateSetting([{k: saved[k]} for k in _EXPOSURE_KEYS if saved.get(k) is not None])
        s.log(1, f"INFO: lightning mode OFF - restored {saved}")
    state["saved"] = None


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

    period = "day" if str(event).lower() == "day" else "night"
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

    if period == "day" and not day_enabled:
        return "day disabled"

    outdir, thumbdir = _resolveOutputDir(params)

    gray = cv2.cvtColor(s.image, cv2.COLOR_BGR2GRAY)
    soft, hard = _loadMask(mask_name, feather, gray.shape)

    # --- difference vs previous frame: only NEW light (a flash/bolt appears) --------
    prev = cv2.imread(PREV_FRAME, cv2.IMREAD_GRAYSCALE)
    have_prev = prev is not None and prev.shape == gray.shape
    flash_area = 0
    bolt_area = 0
    peak = 0
    if have_prev:
        diff = cv2.subtract(gray, prev)                    # clamps at 0: darker -> 0
        diff = (diff.astype(np.float32) * soft).astype(np.uint8)
        flash_area = int(np.count_nonzero(diff >= flash_delta))
        bolt_area = int(np.count_nonzero(diff >= bolt_delta))
        peak = int(diff.max())
    is_flash = have_prev and flash_area >= flash_min_area

    state = _loadState()
    if is_flash:
        state["last_flash"] = now
        state.setdefault("flash_times", []).append(now)
    state["flash_times"] = [t for t in state.get("flash_times", []) if now - t <= window_sec]
    flashes_in_window = len(state["flash_times"])

    # --- arming state machine (period-independent) ---------------------------
    if not state["active"] and flashes_in_window >= flashes_to_arm:
        state["active"] = True
        s.log(1, f"INFO: lightning STORM detected ({flashes_in_window} flashes / {int(window_sec)}s)")
    elif state["active"] and (now - state.get("last_flash", 0)) > cooldown_sec:
        state["active"] = False
        s.log(1, "INFO: lightning storm ended (cooldown elapsed)")

    # --- NIGHT: apply / restore the short exposure to match the storm state ---
    transitioned = False
    if period == "night":
        if state["active"] and not state.get("saved"):
            _enterLightningMode(state, expo_ms, gain, delay_ms)
            transitioned = True
        elif not state["active"] and state.get("saved"):
            _exitLightningMode(state)
            transitioned = True

    # --- bolt capture (only while armed) -------------------------------------
    result = "quiet"
    if state["active"]:
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

    if debug:
        s.log(1, f"INFO: lightning [{period}] flashArea={flash_area} boltArea={bolt_area} "
                 f"peak={peak} flash={is_flash} inWin={flashes_in_window} "
                 f"active={state['active']} override={bool(state.get('saved'))}")

    # expose a couple of variables for the overlay
    try:
        s.saveExtraData("allsky_lightning.json", {
            "AS_LIGHTNING_MODE": "ON" if state["active"] else "OFF",
            "AS_LIGHTNING_FLASHES": flashes_in_window,
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
            "files": {STATE_FILE, PREV_FRAME},
            "env": {}
        }
    }
    s.cleanupModule(moduleData)
