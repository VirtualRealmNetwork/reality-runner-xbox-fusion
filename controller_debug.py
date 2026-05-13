#!/usr/bin/env python3
from __future__ import annotations

import argparse
import selectors
import signal
import sys
import time

from evdev import InputDevice, ecodes

from fusion_common import (
    SelectorSpec,
    enumerate_gamepads,
    fuse_left_stick,
    is_reality_runner_v2,
    normalize_axis,
    resolve_device,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enumerate controllers and inspect the fusion math live.")
    parser.add_argument("--a-event", help="Use this /dev/input/event path for Controller A.")
    parser.add_argument("--b-event", help="Use this /dev/input/event path for Controller B.")
    parser.add_argument("--a-uniq", help="Match Controller A by unique identifier.")
    parser.add_argument("--b-uniq", help="Match Controller B by unique identifier.")
    parser.add_argument("--a-name", help="Match Controller A by a case-insensitive name substring.")
    parser.add_argument("--b-name", help="Match Controller B by a case-insensitive name substring.")
    parser.add_argument("--deadzone-a", type=float, default=0.18, help="Radial deadzone for Controller A.")
    parser.add_argument("--deadzone-b", type=float, default=0.18, help="Radial deadzone for Controller B.")
    parser.add_argument("--live", action="store_true", help="Continuously print raw and fused left-stick values.")
    parser.add_argument("--hz", type=float, default=10.0, help="Maximum live print frequency.")
    return parser.parse_args()


def list_devices() -> int:
    devices = enumerate_gamepads()
    if not devices:
        print("No joystick-capable input devices were found.")
        return 1
    for device in devices:
        print(device.label())
        selector_parts = [f"--{role}-event {device.event_path}" for role in ("a", "b")]
        if device.uniq:
            selector_parts = [f"--{role}-uniq {device.uniq}" for role in ("a", "b")]
        print(f"  selectors: {' | '.join(selector_parts)}")
    return 0


def live_debug(args: argparse.Namespace) -> int:
    devices = enumerate_gamepads()
    device_a = resolve_device(
        devices,
        SelectorSpec(event_path=args.a_event, uniq=args.a_uniq, name=args.a_name),
        "a",
    )
    device_b = resolve_device(
        devices,
        SelectorSpec(event_path=args.b_event, uniq=args.b_uniq, name=args.b_name),
        "b",
    )
    if device_a.event_path == device_b.event_path:
        print("error: Controller A and Controller B resolved to the same device.", file=sys.stderr)
        return 1

    source_a = InputDevice(device_a.event_path)
    source_b = InputDevice(device_b.event_path)
    b_supports_backward = is_reality_runner_v2(device_b)
    selector = selectors.DefaultSelector()
    selector.register(source_a, selectors.EVENT_READ, "a")
    selector.register(source_b, selectors.EVENT_READ, "b")
    abs_values = {
        "a": {
            code: absinfo.value
            for code, absinfo in source_a.capabilities(absinfo=True).get(ecodes.EV_ABS, [])
        },
        "b": {
            code: absinfo.value
            for code, absinfo in source_b.capabilities(absinfo=True).get(ecodes.EV_ABS, [])
        },
    }
    should_stop = False
    interval = 1.0 / max(args.hz, 1.0)
    last_print = 0.0

    def _handler(signum, frame):  # noqa: ARG001
        nonlocal should_stop
        should_stop = True

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)

    print(f"Controller A: {device_a.label()}")
    print(f"Controller B: {device_b.label()}")
    print(f"Controller B backward support: {'ON' if b_supports_backward else 'OFF'}")
    print("Live debug started. Press Ctrl-C to stop.", flush=True)

    try:
        while not should_stop:
            for key, _ in selector.select(timeout=0.1):
                source_name = key.data
                device = source_a if source_name == "a" else source_b
                for event in device.read():
                    if event.type == ecodes.EV_ABS:
                        abs_values[source_name][event.code] = int(event.value)

            now = time.monotonic()
            if now - last_print < interval:
                continue
            last_print = now

            a_info_x = source_a.absinfo(ecodes.ABS_X)
            a_info_y = source_a.absinfo(ecodes.ABS_Y)
            b_info_x = source_b.absinfo(ecodes.ABS_X)
            b_info_y = source_b.absinfo(ecodes.ABS_Y)

            a_x = normalize_axis(abs_values["a"].get(ecodes.ABS_X, a_info_x.value), a_info_x)
            a_y = normalize_axis(abs_values["a"].get(ecodes.ABS_Y, a_info_y.value), a_info_y)
            b_x = normalize_axis(abs_values["b"].get(ecodes.ABS_X, b_info_x.value), b_info_x)
            b_y = normalize_axis(abs_values["b"].get(ecodes.ABS_Y, b_info_y.value), b_info_y)
            fused_x, fused_y, debug = fuse_left_stick(
                a_x,
                a_y,
                b_x,
                b_y,
                args.deadzone_a,
                args.deadzone_b,
                b_supports_backward,
            )
            print(
                (
                    f"A raw=({a_x:+.3f},{a_y:+.3f}) "
                    f"B raw=({b_x:+.3f},{b_y:+.3f}) "
                    f"fused=({fused_x:+.3f},{fused_y:+.3f}) "
                    f"a_active={debug['a_active']} "
                    f"a_mag={debug['a_magnitude']:.3f} "
                    f"b_mag={debug['b_magnitude']:.3f} "
                    f"b_backward={debug['b_backward']} "
                    f"angle_source={debug['angle_source']}"
                ),
                flush=True,
            )
    finally:
        source_a.close()
        source_b.close()

    return 0


def main() -> int:
    args = parse_args()
    if not args.live:
        return list_devices()
    return live_debug(args)


if __name__ == "__main__":
    raise SystemExit(main())
