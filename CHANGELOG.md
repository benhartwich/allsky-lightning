# Changelog

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
