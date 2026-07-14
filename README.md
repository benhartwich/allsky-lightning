# allsky_lightning

A **thunderstorm / lightning capture** module for [Allsky](https://github.com/AllskyTeam/allsky).

When a storm is detected it switches night capture to a **short fixed exposure** so a
lightning bolt stays crisp instead of being blown out by Allsky's 50–90 s auto-exposure —
then saves every frame that actually contains a bolt to a gallery. The trigger is pure
software (brightness transients in the incoming frames); no extra hardware is required.

## Why exposure length is only half the problem

A bolt lasts ~10–200 ms and strikes at a **random** instant, so you cannot react to a
bolt and *then* shorten the exposure — it is long gone before any detection finishes.
Two independent quantities matter:

- **Duty cycle** (fraction of time the sensor is open) → *catch probability*
- **Exposure length** → *image quality*

At night Allsky's auto-exposure happily picks 50–90 s. A bolt inside a 50 s frame is
completely washed out. So the module does two things at once: it keeps the **duty cycle**
high (short exposures, near-zero inter-frame delay) *and* keeps each exposure short enough
that the bolt is not clipped and the background stays dark.

> **Merksatz:** short exposure makes the bolt *look good*; high duty cycle is what makes
> you *catch it at all*. You need both.

## How it works

```
frame N-1, frame N ─► difference ─► threshold ─► soft mask ─► brightened area
      │
      ├─ area ≥ flash_min_area  →  a FLASH (a storm brightens a wide patch at once;
      │                            steady moonlight/light-pollution does not change
      │                            frame-to-frame, so it is ignored)
      │
      └─ ≥ flashes_to_arm flashes within window_sec  →  STORM ARMED
             │
             ├─ NIGHT: auto-exposure OFF, fixed short exposure (default 2 s),
             │         fixed gain, delay 0  →  crisp bolt, dark sky, max duty cycle
             │
             └─ every armed frame with a bolt (area ≥ bolt_min_area)  →  saved to the
                'lightning' gallery (+ thumbnail + lightning.json index) and optionally
                uploaded to the remote website

  quiet for cooldown_sec  →  original exposure settings restored automatically
```

Detection is **difference based**, not absolute-brightness based — that is what separates
a lightning flash (a sudden positive change over a chunk of sky) from steady moonlight.

## Safety

The original night-exposure settings are saved on the **first** override and always
restored on exit / after the cooldown / on module cleanup, so the camera can **never** get
stuck in short-exposure mode — even across a service restart or into the next night. The
save-once / restore logic is guarded so a restart mid-storm cannot mistake the short
exposure for the "original".

## Daytime

The module can also run on the day flow (`day_enabled`). A daytime bolt against a bright
sky is genuinely hard for an allsky, so the day path is **capture-only**: it detects and
saves bolt frames but does **not** touch the day exposure — and it shouldn't, because
daytime exposures are already a few milliseconds (the bolt is already frozen; there is no
long-exposure wash-out to fix). Treat it as best-effort. Because the default day cadence
between frames is large, the daytime duty cycle is low — raise `flash_min_area` there to
keep drifting clouds from arming the detector.

## Installation

1. Copy `allsky_lightning.py` into `~/allsky/scripts/modules/`.
2. In the WebUI **Module Manager**, add **Lightning Capture** to the **night** flow
   (and optionally the day flow, with *Also Capture In Daytime* enabled).
3. Copy `web/lightning.html` into your website folder (`~/allsky/html/allsky/`) for the
   gallery page; captures are written to `html/allsky/lightning/`.

Reuse the meteor detection mask (`meteor_mask.png`) or build a dedicated one — white =
sky to analyse, black = trees/horizon.

## Key settings

| Setting | Meaning |
| --- | --- |
| `flash_delta` / `flash_min_area` | how bright and how large a frame-to-frame brightening must be to count as a flash |
| `flashes_to_arm` / `window_sec` | how many flashes within the window arm the storm mode |
| `cooldown_sec` | quiet time before the original exposure is restored |
| `lightning_exposure_ms` / `lightning_gain` / `lightning_delay_ms` | the fixed **night** capture in lightning mode (all exposures in **milliseconds**) |
| `bolt_delta` / `bolt_min_area` | threshold for a frame to be *saved* as a bolt |
| `day_enabled` | also detect/save on the day flow (capture-only) |

## Honest limitations

- **Dead time** between frames remains — a bolt in the gap between two exposures is lost.
  That is why the night mode drives the delay to zero (maximum duty cycle).
- The **first** switch into lightning mode has up to one long auto-frame of latency (the
  in-progress exposure finishes first).
- `lightning_exposure_ms` wants tuning per site — lower it under a bright / light-polluted
  sky, raise it under a very dark one.
- Daytime capture is best-effort (low duty cycle, low bolt-vs-sky contrast).

## License

MIT © Benjamin Hartwich
