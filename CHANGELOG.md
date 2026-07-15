# Changelog

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
