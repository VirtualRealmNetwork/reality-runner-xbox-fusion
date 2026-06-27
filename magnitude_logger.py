#!/usr/bin/env python3
"""Log Controller B (treadmill) left-stick magnitude with timestamps.

Captures the treadmill's evdev joystick output so we can measure ramp-up and
decay latency (deadtime vs. filter lag) in NORMAL play mode. Turn the Reality
Runner dashboard's Pulse Detector OFF before running -- it warns that game
output is unstable while the detector is active, so it is not representative.

Reuses fusion_common so Controller B is auto-selected exactly like the main
runtime. Does not grab the device or create a virtual controller.

CSV columns (stdout and/or --out):
    t_rel    seconds since first event
    t_event  raw evdev event timestamp (seconds)
    abs_x    raw ABS_X value
    abs_y    raw ABS_Y value
    nx, ny   normalized axes in [-1, 1]
    raw_mag  hypot(nx, ny), no deadzone
    dz_mag   magnitude after radial deadzone (matches fusion's magnitude_b)

Usage:
    python magnitude_logger.py --out walk_stop.csv
    # optionally: --b-uniq <mac> / --b-name <substr> / --deadzone-b 0.18
    # walk -> hold steady -> stop, then Ctrl-C.
"""
from __future__ import annotations

import argparse
import math
import sys
import time

from evdev import InputDevice, ecodes

from fusion_common import (
    SelectorSpec,
    enumerate_gamepads,
    normalize_axis,
    radial_deadzone,
    resolve_device,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Log treadmill (Controller B) left-stick magnitude over time.")
    p.add_argument("--b-event", help="Use this /dev/input/event path for Controller B.")
    p.add_argument("--b-uniq", help="Match Controller B by unique id (Bluetooth MAC).")
    p.add_argument("--b-name", help="Match Controller B by case-insensitive name substring.")
    p.add_argument("--deadzone-b", type=float, default=0.0, help="Radial deadzone used for dz_mag (default 0 = raw).")
    p.add_argument("--out", help="Write CSV to this file (in addition to stdout).")
    p.add_argument("--quiet", action="store_true", help="Do not echo rows to stdout (file only).")
    p.add_argument("--print-hz", type=float, default=20.0, help="Max stdout echo rate; all rows still go to --out.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    devices = enumerate_gamepads()
    if not devices:
        print("No joystick-capable input devices found.", file=sys.stderr)
        return 1
    summary = resolve_device(devices, SelectorSpec(args.b_event, args.b_uniq, args.b_name), "b")
    dev = InputDevice(summary.event_path)
    print(f"# device: {summary.label()}", file=sys.stderr)
    print(f"# deadzone_b={args.deadzone_b} -- turn the dashboard Pulse Detector OFF for a representative capture", file=sys.stderr)

    ax = dev.absinfo(ecodes.ABS_X)
    ay = dev.absinfo(ecodes.ABS_Y)
    x_val = ax.value
    y_val = ay.value

    out = open(args.out, "w") if args.out else None
    header = "t_rel,t_event,abs_x,abs_y,nx,ny,raw_mag,dz_mag"
    if out:
        out.write(header + "\n")
    if not args.quiet:
        print(header, flush=True)

    t0: float | None = None
    last_print = 0.0
    min_print_dt = 1.0 / max(args.print_hz, 0.001)

    try:
        for ev in dev.read_loop():
            if ev.type != ecodes.EV_ABS:
                continue
            if ev.code == ecodes.ABS_X:
                x_val = ev.value
            elif ev.code == ecodes.ABS_Y:
                y_val = ev.value
            else:
                continue

            te = ev.timestamp()
            if t0 is None:
                t0 = te
            nx = normalize_axis(x_val, ax)
            ny = normalize_axis(y_val, ay)
            raw_mag = math.hypot(nx, ny)
            _, _, dz_mag = radial_deadzone(nx, ny, args.deadzone_b)
            row = f"{te - t0:.4f},{te:.4f},{x_val},{y_val},{nx:+.4f},{ny:+.4f},{raw_mag:.4f},{dz_mag:.4f}"

            if out:
                out.write(row + "\n")
            if not args.quiet:
                now = time.monotonic()
                if now - last_print >= min_print_dt:
                    last_print = now
                    print(row, flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        if out:
            out.flush()
            out.close()
        try:
            dev.close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
