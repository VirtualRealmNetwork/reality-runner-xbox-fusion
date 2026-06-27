#!/usr/bin/env python3
"""Hardware-free unit tests for the steering intensity blend.

Run: python test_blend.py   (needs the project env so fusion_common imports evdev)
"""
from __future__ import annotations

from fusion_common import IntensityBlender, compute_geometry


def approx(x: float, y: float, tol: float = 1e-6) -> None:
    assert abs(x - y) <= tol, f"{x} != {y} (tol {tol})"


def test_start_from_idle() -> None:
    bl = IntensityBlender(hold=0.4, ramp=0.1, eps_move=0.08)
    approx(bl.update(0.0, 0.0, 0.0, 0.0), 0.0)    # idle (raw=0, disengaged)
    approx(bl.update(0.9, 0.0, 0.9, 0.01), 0.9)   # raw>on -> engage, rising from idle -> A
    approx(bl.update(0.9, 0.5, 0.9, 0.2), 0.9)    # within hold -> still A
    approx(bl.update(0.9, 0.5, 0.9, 0.25), 0.7)   # since=0.45 -> w=0.5 -> 0.5*0.9+0.5*0.5
    approx(bl.update(0.9, 0.5, 0.9, 0.1), 0.5)    # past ramp -> w=0 -> B


def test_steer_during_walk_no_glitch() -> None:
    bl = IntensityBlender(hold=0.4, ramp=0.1, eps_move=0.08)
    approx(bl.update(0.0, 0.6, 0.0, 0.1), 0.6)    # mode 1 walking forward (disengaged)
    approx(bl.update(0.9, 0.6, 0.9, 0.01), 0.6)   # engage while moving -> w=0 -> stays B


def test_fast_stop_then_resume() -> None:
    bl = IntensityBlender(hold=0.4, ramp=0.1, eps_move=0.08, debounce=0.06)
    bl.update(0.9, 0.6, 0.9, 0.0)                 # start steering
    approx(bl.update(0.9, 0.6, 0.9, 0.6), 0.6)    # settle to B
    approx(bl.update(0.0, 0.6, 0.0, 0.04), 0.6)   # released, within debounce -> still B
    approx(bl.update(0.0, 0.6, 0.0, 0.04), 0.0)   # disengaged 0.08 > 0.06 -> fast stop
    approx(bl.update(0.0, 0.6, 0.0, 0.6), 0.6)    # kept walking -> resume B (side-effect #2)


def test_reversal_through_center_no_flash() -> None:
    # Left->right sweep: raw dips through center (< debounce) then back up.
    bl = IntensityBlender(hold=0.4, ramp=0.1, eps_move=0.08, debounce=0.06)
    bl.update(0.9, 0.6, 0.7, 0.0)
    approx(bl.update(0.9, 0.6, 0.7, 0.6), 0.6)    # steady steering, w=0, m=B
    approx(bl.update(0.0, 0.6, 0.0, 0.03), 0.6)   # 30ms through center -> NOT a stop, stays B
    approx(bl.update(0.8, 0.6, 0.7, 0.01), 0.6)   # back on other side -> no flash, stays B


def test_hysteresis_partial_release_stays_engaged() -> None:
    # Easing into the off..on band (0.05-0.18) must NOT disengage (no false stop).
    bl = IntensityBlender(hold=0.4, ramp=0.1, eps_move=0.08, debounce=0.06)
    bl.update(0.9, 0.6, 0.7, 0.0)
    approx(bl.update(0.9, 0.6, 0.7, 0.6), 0.6)    # steady steering
    approx(bl.update(0.0, 0.6, 0.14, 0.2), 0.6)   # raw 0.14 in hysteresis band (0.10-0.18) -> engaged, B
    approx(bl.update(0.0, 0.6, 0.14, 0.2), 0.6)   # still engaged, no debounce countdown -> B


def test_belt_first_no_boost() -> None:
    bl = IntensityBlender(hold=0.4, ramp=0.1, eps_move=0.08)
    approx(bl.update(0.0, 0.4, 0.0, 0.2), 0.4)    # already moving via mode 1 (m_prev>eps)
    approx(bl.update(1.0, 0.4, 1.0, 0.01), 0.4)   # engage while moving -> w=0 -> B's laggy value


def test_standing_flick_self_corrects() -> None:
    bl = IntensityBlender(hold=0.4, ramp=0.1, eps_move=0.08)
    bl.update(0.0, 0.0, 0.0, 0.0)
    approx(bl.update(0.9, 0.0, 0.9, 0.01), 0.9)   # flick while standing -> brief lurch
    approx(bl.update(0.9, 0.0, 0.9, 0.6), 0.0)    # B stays 0 -> decays back to 0


def test_geometry_forward_and_steer() -> None:
    ux, uy, _ma, _mb, act, _bw, _ang, src = compute_geometry(0.0, 0.0, 0.0, 0.5, 0.18, 0.18, False)
    assert act is False and ux == 0.0 and uy == -1.0 and src == "forward"
    ux, uy, _ma, _mb, act, _bw, _ang, src = compute_geometry(0.5, 0.0, 0.0, 0.5, 0.18, 0.18, False)
    assert act is True and src == "controller_a"
    approx(ux, 1.0)
    approx(uy, 0.0)


def main() -> int:
    tests = sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f))
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"ALL PASS ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
