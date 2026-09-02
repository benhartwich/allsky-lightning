# Changelog

## v0.9.0

- **The storm exposure now actually reaches the camera.** Until now the module only
  rewrote `settings.json` via `s.updateSetting()`, but the capture program never re-reads
  that file: `allsky.sh` converts it into `tmp/capture_args.txt` once at start and passes
  that snapshot with `-config`. Measured in the field on 2026-08-22: a storm armed at
  22:00:21 and the exposure stayed at 90 s for more than 20 frames, changing to 2 s only
  at 00:00:40 when the Pi rebooted. In other words, the module's central feature -
  switching to short exposures during a storm - had never once taken effect while a storm
  was running. Every lightning frame captured so far was a long exposure.
- **New setting "Apply Exposure To Running Camera" (`reload_capture`, default on).** Uses
  Allsky's own supported path, the one the service declares as `ExecReload`: send SIGHUP
  to the capture program, whose handler exits with `EXIT_RESTARTING` (98), so `allsky.sh`
  exits 0 and systemd's `Restart=on-success` starts it again and regenerates
  `capture_args.txt` from `settings.json`. This is a full capture restart, not an in-place
  reload - `allsky_common.cpp` carries an explicit
  `TODO: Re-read configuration instead of restarting.` Measured end-to-end on a Pi 4 with
  an ASI678MC: 9 s from the signal to the first new exposure, 16 s to the first saved
  image, plus the exposure in flight (up to 90 s at night). No elevated privileges:
  capture runs as the same user as this module.
- **New setting "Minimum Reload Interval" (`reload_min_interval_sec`, default 300).**
  Every arm/disarm would otherwise restart the capture program, so a storm state flapping
  near its threshold could restart the camera every few minutes. A reload blocked by the
  limit is not dropped: it stays pending and fires on a later frame, so the camera can
  never be left on an exposure that no longer matches `settings.json`.
- The reload also fires from the **day** flow when the night exposure is restored there.
  `capture_args.txt` holds both the day and the night settings and is fixed for the whole
  service run, so a night exposure restored during the day would otherwise still not apply
  at dusk.
- The state is written to disk **before** the signal is sent. This module runs as a
  descendant of the capture program (capture -> `saveImage.sh` -> `flow-runner.py` -> here),
  so the SIGHUP tears down our parent while we are still running; persisting first means a
  lost tail cannot drop the rate limit and turn this into a restart loop.

## v0.8.1

- **Fixed: the camera could get stuck on the short storm exposure permanently.** The state
  file lived in `ALLSKY_TMP`, which is a **tmpfs**. A reboot while a storm was active wiped
  `state["saved"]` while `settings.json` — on real disk — still held the short exposure. The
  next arm then saved that override as the "original", so every later restore was a no-op and
  the camera kept taking 2 s night frames indefinitely. Those frames are near-black, so
  `removeBadImages.sh` deleted them once their mean fell below the low threshold: no night
  images at all. Observed in the field from 2026-08-23 to 2026-09-01 (10 nights).
  The guard in `_enterLightningMode` was correct in intent but rested on the state surviving a
  restart, which on a tmpfs it does not.
- **State moved off the tmpfs.** New `_stateDir()` puts `allsky_lightning_state.json` under
  `ALLSKY_HOME/config/allsky_lightning/`, falling back to `ALLSKY_TMP` if that cannot be
  created. `PREV_FRAME`, `WEATHER_FILE` and `STATS_FILE` stay in tmp — they are caches and
  losing them is harmless.
- **Added a second line of defence: `_isOverride()`.** If the night settings already look
  exactly like this module's own override while no originals are saved, the genuine values are
  unrecoverable — so the module now refuses to save them, refuses to enter the mode, and logs
  an `ERROR` naming the settings to restore by hand. Previously it silently saved the override
  and cemented the broken state. The `override_unknown` flag keeps this from spamming the log
  on every frame and is cleared when the storm ends, so a later storm arms normally once the
  settings are fixed. `_enterLightningMode()` now returns a bool.

## v0.8.0

- **Sun-elevation guard default raised from -6° to -12° (nautical twilight).** A night of
  observation showed the false optical "flashes" cluster at sun elevations of -6° to -8° —
  i.e. *below* the old -6° guard, which therefore never fired on them; only the weather
  gate (fail-open) was catching them. -12° covers the whole still-brightening twilight band
  with margin, at the cost of ~25-35 min of storm coverage at each end of the night.
- **Nightly detector statistics (`allsky_lightning_stats.json`) + a dawn summary line.**
  The detector runs ~1000×/night, so the per-frame `debug` log is useless for tuning.
  Instead the module now keeps a small rolling stats file (flash count, peak flashes-in-
  window vs the arm threshold, max flash area, near-arms blocked and *by which gate*, plus
  the last 60 individual flash events with time/area/sun/weather/block-reason) and emits
  one human-readable `lightning night report - …` line to the Allsky log at dawn. Counters
  reset automatically at dusk, so each report describes a single night. This is what makes
  the thresholds tunable from real data instead of guesswork.

## v0.7.0

- **Sun-elevation guard (`min_sun_elevation`, default -6°).** An independent backstop to
  the weather gate: the storm mode will not arm while the sun is higher than this
  elevation. A brightness trigger cannot work against a bright, fast-changing twilight
  sky, so this blocks false arming at dusk/dawn even when the weather lookup is
  unavailable or stale (which fails open). Uses the camera latitude/longitude with NOAA
  solar-position math (no extra library); if the coordinates are missing it never blocks.
  The current sun elevation is exposed to the overlay as `AS_LIGHTNING_SUN`.

## v0.6.0

- **Fix: the weather gate now blocks false arming at night, not just during the day.**
  Previously the calm-sky arm block only applied to daytime frames, so a clear, dry night
  stayed purely optical. On such a night the fast frame-to-frame brightness changes of
  twilight and drifting moonlit clouds could brighten a large patch of sky, register as
  two "flashes", and false-arm the storm mode — forcing the camera into the short 2 s
  lightning exposure even though no storm was present, which left the night images dark.
  The calm-sky block (`arm_blocked = weather_gate and wx_calm`) now applies day and night.
  A real storm never reads as calm (it reports rain/thunderstorm), so genuine night storms
  still arm optically, and the gate stays fail-open on any lookup error.

## v0.5.0

- **Fix: day/night was read from the wrong place, so daytime frames were captured as
  false "bolts".** Allsky calls the flow with `event="postcapture"` (not `"day"`/
  `"night"`), so the old `event`-based check made every frame look like night. That meant
  `day_enabled` never took effect and the bright daytime sky and drifting clouds were
  saved as bolts, and the storm mode stayed armed around the clock (moving clouds keep
  producing "flashes"). The period now comes from the `DAY_OR_NIGHT` environment variable.
- **Daytime capture off by default and truly off.** With `day_enabled` unset the module
  no longer runs detection or saves anything during the day; it only keeps managing the
  storm state so a storm ending around dawn still disarms and restores the night exposure.
- Note: a brightness trigger cannot make pretty bolt images from a fully overcast storm -
  it detects the flash (the whole cloud deck lights up) but there is no visible bolt.

## v0.4.0

- **Optional weather gate (Open-Meteo, free, no API key, worldwide).** Off by default.
  The pure-software optical trigger stays primary; the weather service only refines it:
  - **Daytime arming** is blocked while the service reports a confidently calm/clear sky,
    so drifting daytime clouds can no longer arm the detector. Night stays pure-optical.
  - **The cooldown is shortened** (to `weather_clear_cooldown_sec`, default 120 s) once the
    sky is confidently calm, so the camera resets much sooner after a storm has moved on.
  - **Fail-open:** any lookup error / missing coordinates → the module behaves exactly as
    if the gate were off, so the network can never leave the camera stuck. Readings are
    cached (`weather_cache_sec`, default 600 s) so the API is never hit in the hot path.
  - Uses Open-Meteo rather than a Germany-only source because the camera site is in
    Austria; the current WMO weather code is mapped to dry/fog/rain/snow/thunderstorm.

## v0.3.0

- **Exposure is now restored from any flow, not just the night flow.** Previously the
  short-exposure override was only undone on a night frame, so a storm that kept going
  past dawn left the night exposure overridden until the *next* real night — hours later.
  Entering lightning mode stays night-only (only the night exposure is ever touched), but
  the restore now fires from the day flow too, so the camera resets as soon as the
  cooldown elapses regardless of the time of day.

## v0.2.0

- **Difference-based trigger** instead of absolute brightness: a flash is a sudden
  positive frame-to-frame brightening over a chunk of sky, so steady moonlight and
  light pollution no longer arm the detector.
- **Storm state decoupled from the exposure override** (`saved` flag): a storm that
  begins before dusk now switches the night exposure correctly once night starts,
  instead of being stuck because it was already "active".
- **Previous frame is dropped on every exposure transition**, so the brightness jump
  when switching exposure can no longer fire a false flash.
- **Per-capture peak brightness** recorded in `lightning.json` and shown in the gallery.
- **Optional daytime path** (`day_enabled`): runs on the day flow as capture-only —
  detects/saves bolts without touching the (already short) day exposure.
- Output goes to the website `lightning/` folder with a `lightning.json` index and
  remote upload (image + thumbnail + index), matching the meteor module.

## v0.1.0

- Initial thunderstorm capture module: software brightness trigger, night switch to a
  fixed short exposure with maximum duty cycle, bolt detection + gallery save, automatic
  restore of the original exposure after a cooldown.
