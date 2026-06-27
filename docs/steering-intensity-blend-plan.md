# Steering-Mode Intensity Blend — Design & Implementation

## Status

**Implemented and validated on hardware** (dry-run traces). Off by default in the CLI
(`--steer-blend`), but **enabled by default in `run_fusion_and_steam.sh`**.

- `fusion_common.py`: `compute_geometry()` + `IntensityBlender` (Schmitt hysteresis +
  falling-edge debounce + hold-then-ramp weight).
- `controller_fusion.py`: blend path in `_write_fused_left_stick`, fixed-rate output
  tick (`_select_timeout`), blender reset on fusion toggle, CLI flags.
- `test_blend.py`: hardware-free unit tests (8, all passing).
- `run_fusion_and_steam.sh`: launches fusion with `--steer-blend` on, then Steam.
- `magnitude_logger.py`: optional latency measurement tool.

Run (manual): `python controller_fusion.py --steer-blend [--debug --dry-run]`.
Run (gameplay): `./run_fusion_and_steam.sh`.

## Problem & goal

The treadmill (Controller B) derives its joystick magnitude from a sliding-window count
of magnet/Hall pulses. The window is large, so the reported magnitude **lags** true
intent: ~100 ms to ramp up, ~300–500 ms to decay after you stop. We cannot touch the
firmware. The lag is the dominant pain in twitchy action games (e.g. Stellar Blade).

**Core idea:** Controller A's (Xbox) analog stick is a *zero-latency* intent signal.
Lead with A on transients and settle to the treadmill's *accurate but lagged* magnitude
in steady state. Direction always comes from A (when deflected); only the **intensity
(magnitude)** is blended.

## Decisions locked in

- **Mode selection: auto by stick deflection.**
  - Mode 1 (treadmill-only forward): A centered → direction forward, magnitude = treadmill.
    **Out of scope — unchanged.**
  - Mode 2 (steering): A deflected past `deadzone_a` → direction = A angle, magnitude blended.
- **Both edges fixed:** lead with A on motion *start* and *stop*.

## Canonical model — unified both-edge blend with asymmetric seed

One per-tick update covers both modes; only the *direction* depends on mode.

State: `w0` (seed weight), `since_edge` (s), `m_prev` (last output magnitude), `engaged`
(Schmitt state), `confirmed_active` (debounced), `pending_off` (s). The weight follows a
**hold-then-ramp** schedule, not an exponential:

```
weight():
    t = since_edge
    if t < hold:                 w = w0 * 1                  # hold at the seed
    elif t < hold + ramp:        w = w0 * (1 - (t-hold)/ramp)# linear seed -> 0
    else:                        w = 0

update(a, b, raw_mag_a, dt):     # a = mag_A (post-deadzone), b = mag_B, both [0,1]
    # 1) Schmitt hysteresis on the RAW stick magnitude:
    if   raw_mag_a > on_threshold:    engaged = True   # on_threshold  = deadzone_a (0.18)
    elif raw_mag_a <= off_threshold:  engaged = False  # off_threshold = steer_off  (0.10)
    # 2) Debounce engaged -> confirmed_active, detect edges:
    if engaged and not confirmed_active:       # RISING: immediate (fast start)
        w0 = 1 if m_prev < eps_move else 0     #   idle -> boost ; moving -> steer only
        since_edge = 0 ; confirmed_active = True
    elif confirmed_active and not engaged:     # FALLING: only after `debounce` of disengage
        pending_off += dt
        if pending_off >= debounce:
            w0 = 1 ; since_edge = 0 ; confirmed_active = False   # fast stop
    else:
        pending_off = 0 ; since_edge += dt
    w = weight()
    m = w*a + (1-w)*b
    m_prev = m
    return m

# direction: theta = atan2(dz_a_y, dz_a_x) if a_active else FORWARD  (RR v2 backward flip applies)
# output_left_stick = (cos theta, sin theta) * m
```

Defaults: `hold=0.40 s`, `ramp=0.10 s`, `debounce=0.06 s`, `steer_off=0.10`, `eps_move=0.08`, `deadzone_a/b=0.18`, `tick_hz=250`.

### Why it works, case by case

- **Start, push-first / simultaneous:** rising edge, you were idle so `m_prev≈0` ⇒ `w0=1` ⇒
  `m=a` (instant move). Held for `hold`, then `ramp` hands over to the now-caught-up B.
- **Steer during steady walk (old side-effect #1):** rising edge, already moving so
  `m_prev>eps_move` ⇒ `w0=0` ⇒ `m=b` unchanged. Only direction changes — **#1 removed.**
- **Stop in mode 2:** falling edge ⇒ `w=1` and `a=0` ⇒ `m=0` immediately, held hard at 0 for
  the full `hold` (no bump), masking the 300–500 ms tail.
- **Release but keep walking (side-effect #2, accepted):** `m=0` for `hold`, then ramps back
  to `b` ⇒ character stops briefly then resumes forward.
- **Steady state (either mode):** `w→0` ⇒ `m=b` (accurate treadmill pace).
- **Direction reversal (stick sweeps through center):** the low `off_threshold` keeps the
  disengaged window tiny and the `debounce` bridges it, so `confirmed_active` stays true and
  output holds at `b` — no chop to 0. (At 60 ms debounce *without* hysteresis this flashed.)

### The discriminator is `m_prev`, never B

"Start-while-walking vs. steady-walking" cannot be told apart from B's reading — a value
*rising due to latency* is identical to a *steady slow walk* at any instant, and B is too
noisy (measured: swings 0↔12 step-to-step) to trust its slope. So the decision uses our own
clean, lag-free output state `m_prev`: already-moving ⇒ steer only; was-idle ⇒ fast start.

## Schedule timing vs. the treadmill decay

`hold` should ≈ the treadmill decay time so that, after a real stop, B has decayed to ~0 by
the time the `ramp` lets B back in — otherwise the ramp re-admits a still-high B and the
character drifts forward briefly. With `hold=0.40 s` (≈ measured decay) the stop is a hard 0
for 400 ms then a 100 ms ramp; any residual bump is small and brief. `hold` erring *large* is
safe (it only lengthens how long the start blend rides A). `ramp` keeps the hand-off smooth.

## Known limitations (by design)

- **Belt-first start is not boosted.** Start the belt then deflect the stick later → you were
  already moving via mode 1 (`m_prev>eps_move`) → push is steer-only → B's normal laggy ramp.
  Consistent (that start lived in mode 1, out of scope) and unavoidable (rising-vs-steady
  ambiguity). The **stop** edge is still fast regardless of how you started.
- **Flicking the stick while standing still** gives a brief `mag_A` lurch that self-corrects
  within `hold+ramp` (B stays 0). The price of the fast start.
- **Mid-walk speed changes** while steering (`w≈0`) follow B's lagged value. Out of scope.

## Architecture

`w` advances on a clock, so output is no longer purely event-driven:

- **Fixed-rate tick:** `run()` calls `_write_left_stick()` each loop when the blender is on;
  `_select_timeout()` returns `1/tick_hz` while `blender.is_active()` (schedule live), else
  0.1 s — so we only spin fast during the ~`hold+ramp` window.
- **`IntensityBlender`** (in `fusion_common.py`, unit-testable): holds `w0`, `since_edge`,
  `m_prev`, `engaged`, `confirmed_active`, `pending_off`; `update(a, b, raw_mag_a, dt) -> m`
  (Schmitt hysteresis + falling-edge debounce). `is_active()` stays true while a debounce is
  `pending_off`, so the fast tick keeps timing it.
- **`compute_geometry()`** returns the unit direction + both magnitudes + `a_active`; the
  runtime blends the magnitude and composes `(cos θ, sin θ)·m`. `dt` from `time.monotonic()`.
- Blender is **reset on fusion toggle** to avoid a stale edge.

## Parameters (CLI flags, all gated behind `--steer-blend`)

| Flag | Default | Meaning |
|------|---------|---------|
| `--steer-blend` | off | master enable |
| `--blend-hold` | 0.40 s | seconds `w` holds at the seed after an edge (≈ treadmill decay) |
| `--blend-ramp` | 0.10 s | seconds `w` decays linearly seed→0 after the hold |
| `--blend-debounce` | 0.06 s | a release counts as a stop only after the stick stays disengaged this long (bridges reversals) |
| `--steer-off` | 0.10 | hysteresis off-threshold: A disengages only below this raw stick magnitude (engages at `--deadzone-a`) |
| `--eps-move` | 0.08 | `m_prev` threshold: idle vs already-moving (fixed; curve-robust, above idle floor) |
| `--tick-hz` | 250 | output tick rate while the schedule is active |
| `--deadzone-a/-b` | 0.18 | existing; `deadzone_a` = hysteresis on-threshold |

## Speed Curve interaction — what to measure, what not to

The dashboard's **Speed Curve** maps belt *pulses → joystick value* — a **memoryless** map:
it reshapes output *amplitude*, not *timing*. The lag lives upstream in the pulse window.

- **`hold` (timing) is mostly curve-independent.** The decay *time* is a property of the pulse
  window. The one coupling: the curve's low-pulse **zero-knee** sets when output reaches 0 on
  deceleration; raising it cuts to 0 sooner. Re-tune `hold` only if you change that knee or the
  magnet count.
- **`eps_move` is curve-robust.** The curve always maps 0 pulses → 0, so idle output ≈ 0 under
  any curve; a small fixed value (≈0.05–0.10) works universally.

**Do we still need to measure?** Not a blocker — `hold` erring large is safe, so ship the
defaults and tune by feel. `magnitude_logger.py` (Pulse Detector OFF) gives the real decay
time to pin `hold` and confirm slope-vs-deadtime if desired. Only `hold` is
measurement-sensitive.

**Bonus — the Speed Curve is itself a firmware-side stop-latency lever:** raising the
low-pulse zero-knee makes the treadmill drop to 0 sooner on decel (cost: low-speed
resolution). Complementary to the A-blend.

## Tuning procedure

1. `--steer-blend --debug --dry-run`; watch `A, B, fused, a_active, w, m`.
2. Set `blend-hold` ≈ treadmill decay (default 0.40); verify the stop drops to a hard 0 with
   at most mild drift on the ramp.
3. Verify steering mid-walk doesn't glitch speed (no #1) and a true start-from-rest boosts.
4. In-game A/B in Stellar Blade: start crispness, stop crispness, no false stops.

## Test plan

`test_blend.py` (no hardware, 8 tests) covers: idle→push (jump to A, relax to B), walk→push
(no glitch), release→fast-stop-then-resume, reversal-through-center (no flash), hysteresis
partial-release (stays engaged), belt-first (no boost), standing flick (self-corrects), and
`compute_geometry` direction. **In-game A/B** in Stellar Blade is the remaining manual check.

## Milestones

1. ~~Measure~~ — optional; `magnitude_logger.py` ready, run if pinning `hold`.
2. ~~Implement~~ — **done**: fixed-rate tick + `IntensityBlender` (asymmetric seed,
   hold-then-ramp, Schmitt hysteresis, falling-edge debounce), flag-gated, 8 unit tests.
3. ~~Dry-run hardware validation~~ — **done**: fast start, fast stop (masks B's tail), and
   stop-then-resume confirmed; 60 ms debounce + hysteresis bridges direction reversals.
4. **In-game tuning** in Stellar Blade (via `run_fusion_and_steam.sh`) — pending.
