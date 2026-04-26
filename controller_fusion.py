#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import selectors
import signal
import sys
import time
from dataclasses import dataclass, field

from evdev import AbsInfo, InputDevice, UInput, ecodes

from fusion_common import (
    SelectorSpec,
    center_for,
    choose_default_device,
    denormalize_axis,
    enumerate_gamepads,
    fuse_left_stick,
    resolve_device,
)


LEFT_STICK_CODES = {ecodes.ABS_X, ecodes.ABS_Y}
ONE_SIDED_ABS_CODES = {ecodes.ABS_Z, ecodes.ABS_RZ, ecodes.ABS_GAS, ecodes.ABS_BRAKE}
DEFAULT_DEADZONE = 0.18
DEFAULT_VIRTUAL_VENDOR = 0xF155
DEFAULT_VIRTUAL_PRODUCT = 0x0001
TOGGLE_TRIGGER_THRESHOLD = 0.75
TOGGLE_LEFT_TRIGGER_AXES = (ecodes.ABS_Z,)
TOGGLE_RIGHT_TRIGGER_AXES = (ecodes.ABS_RZ,)
TOGGLE_LEFT_TRIGGER_KEYS = (ecodes.BTN_TL2,)
TOGGLE_RIGHT_TRIGGER_KEYS = (ecodes.BTN_TR2,)
TOGGLE_STICK_KEYS = (ecodes.BTN_THUMBL, ecodes.BTN_THUMBR)


@dataclass
class SourceState:
    device: InputDevice
    abs_values: dict[int, int] = field(default_factory=dict)
    abs_seq: dict[int, int] = field(default_factory=dict)
    key_values: dict[int, int] = field(default_factory=dict)


class FusionRuntime:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.sequence = 0
        self.should_stop = False
        self.debug_interval = 1.0 / max(args.debug_hz, 1.0)
        self.last_debug_at = 0.0
        self.output_absinfo: dict[int, AbsInfo] = {}
        self.output_values: dict[tuple[int, int], int] = {}
        self.ui: UInput | None = None
        self.ff_target: InputDevice | None = None
        self.ff_effect_ids: set[int] = set()
        self.selector = selectors.DefaultSelector()
        self.sources: dict[str, SourceState] = {}
        self.selected: dict[str, object] = {}
        self.fusion_enabled = True
        self.toggle_chord_was_down = False
        self._setup()

    def _setup(self) -> None:
        devices = enumerate_gamepads()
        if not devices:
            raise RuntimeError("No joystick-capable input devices were found.")

        selector_a = SelectorSpec(
            event_path=self.args.a_event,
            uniq=self.args.a_uniq,
            name=self.args.a_name,
        )
        selector_b = SelectorSpec(
            event_path=self.args.b_event,
            uniq=self.args.b_uniq,
            name=self.args.b_name,
        )
        summary_a = resolve_device(devices, selector_a, "a")
        summary_b = resolve_device(devices, selector_b, "b")
        if summary_a.event_path == summary_b.event_path:
            raise RuntimeError("Controller A and Controller B resolved to the same device.")

        self.selected = {"a": summary_a, "b": summary_b}
        self.sources = {
            "a": self._open_source(summary_a.event_path),
            "b": self._open_source(summary_b.event_path),
        }
        self.toggle_chord_was_down = self._toggle_chord_down()
        self.output_absinfo = self._build_output_absinfo()

        if not self.args.dry_run:
            capabilities = self._build_uinput_capabilities()
            max_effects = self.sources["a"].device.ff_effects_count if ecodes.EV_FF in self.sources["a"].device.capabilities() else 0
            self.ui = UInput(
                capabilities,
                name=self.args.virtual_name,
                vendor=self.args.virtual_vendor,
                product=self.args.virtual_product,
                version=1,
                max_effects=max_effects,
            )
            self.ff_target = self.sources["a"].device if ecodes.EV_FF in self.sources["a"].device.capabilities() else None
            self.selector.register(self.ui, selectors.EVENT_READ, "ui")
            self._initialize_output()

        self._install_signal_handlers()

    def _install_signal_handlers(self) -> None:
        def _handler(signum, frame):  # noqa: ARG001
            self.should_stop = True

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

    def _open_source(self, path: str) -> SourceState:
        device = InputDevice(path)
        state = SourceState(device=device)

        for code, absinfo in device.capabilities(absinfo=True).get(ecodes.EV_ABS, []):
            state.abs_values[code] = absinfo.value
            state.abs_seq[code] = 0

        for key_code in device.capabilities().get(ecodes.EV_KEY, []):
            state.key_values[key_code] = 1 if key_code in device.active_keys() else 0

        if self._should_grab(path):
            device.grab()

        self.selector.register(device, selectors.EVENT_READ, device.path)
        return state

    def _should_grab(self, path: str) -> bool:
        if self.args.grab == "none":
            return False
        if self.args.grab == "both":
            return True
        if self.args.grab == "a":
            return path == self.selected["a"].event_path
        if self.args.grab == "b":
            return path == self.selected["b"].event_path
        return False

    def _build_output_absinfo(self) -> dict[int, AbsInfo]:
        output: dict[int, AbsInfo] = {}
        left_x = self.sources["a"].device.absinfo(ecodes.ABS_X)
        left_y = self.sources["a"].device.absinfo(ecodes.ABS_Y)
        output[ecodes.ABS_X] = AbsInfo(
            value=int(center_for(left_x)),
            min=left_x.min,
            max=left_x.max,
            fuzz=left_x.fuzz,
            flat=left_x.flat,
            resolution=left_x.resolution,
        )
        output[ecodes.ABS_Y] = AbsInfo(
            value=int(center_for(left_y)),
            min=left_y.min,
            max=left_y.max,
            fuzz=left_y.fuzz,
            flat=left_y.flat,
            resolution=left_y.resolution,
        )

        for source in self.sources.values():
            for code, absinfo in source.device.capabilities(absinfo=True).get(ecodes.EV_ABS, []):
                if code in LEFT_STICK_CODES:
                    continue
                current = output.get(code)
                if current is None:
                    output[code] = AbsInfo(
                        value=absinfo.value,
                        min=absinfo.min,
                        max=absinfo.max,
                        fuzz=absinfo.fuzz,
                        flat=absinfo.flat,
                        resolution=absinfo.resolution,
                    )
                else:
                    output[code] = AbsInfo(
                        value=current.value,
                        min=min(current.min, absinfo.min),
                        max=max(current.max, absinfo.max),
                        fuzz=max(current.fuzz, absinfo.fuzz),
                        flat=max(current.flat, absinfo.flat),
                        resolution=max(current.resolution, absinfo.resolution),
                    )
        return output

    def _build_uinput_capabilities(self) -> dict[int, object]:
        key_codes: set[int] = set()
        for source in self.sources.values():
            key_codes.update(source.device.capabilities().get(ecodes.EV_KEY, []))

        abs_caps = sorted(self.output_absinfo.items(), key=lambda item: item[0])
        capabilities = {
            ecodes.EV_KEY: sorted(key_codes),
            ecodes.EV_ABS: abs_caps,
        }
        ff_caps = self.sources["a"].device.capabilities().get(ecodes.EV_FF, [])
        if ff_caps:
            capabilities[ecodes.EV_FF] = ff_caps
        return capabilities

    def _initialize_output(self) -> None:
        for key_code in self._all_key_codes():
            merged = self._merged_key_value(key_code)
            self._write_output(ecodes.EV_KEY, key_code, merged)

        for abs_code in self.output_absinfo:
            if abs_code in LEFT_STICK_CODES:
                continue
            merged = self._merged_abs_value(abs_code)
            if merged is not None:
                self._write_output(ecodes.EV_ABS, abs_code, merged)

        self._write_left_stick()
        if self.ui:
            self.ui.syn()

    def _all_key_codes(self) -> set[int]:
        codes: set[int] = set()
        for source in self.sources.values():
            codes.update(source.key_values)
        return codes

    def _output_sources(self) -> tuple[SourceState, ...]:
        if self.fusion_enabled:
            return tuple(self.sources.values())
        return (self.sources["a"],)

    def _merged_key_value(self, code: int) -> int:
        return 1 if any(source.key_values.get(code, 0) for source in self._output_sources()) else 0

    def _merged_abs_value(self, code: int) -> int | None:
        candidates: list[tuple[int, int]] = []
        for source in self._output_sources():
            if code in source.abs_values:
                candidates.append((source.abs_values[code], source.abs_seq.get(code, 0)))
        if not candidates:
            return None

        info = self.output_absinfo[code]
        center = center_for(info)
        if code in ONE_SIDED_ABS_CODES:
            return max(candidates, key=lambda item: (item[0], item[1]))[0]
        return max(candidates, key=lambda item: (abs(item[0] - center), item[1]))[0]

    def _neutral_abs_value(self, code: int) -> int:
        info = self.output_absinfo[code]
        if code in ONE_SIDED_ABS_CODES:
            return info.min
        return int(center_for(info))

    def _normalized_left(self, which: str) -> tuple[float, float]:
        source = self.sources[which]
        abs_x = source.device.absinfo(ecodes.ABS_X)
        abs_y = source.device.absinfo(ecodes.ABS_Y)
        value_x = source.abs_values.get(ecodes.ABS_X, abs_x.value)
        value_y = source.abs_values.get(ecodes.ABS_Y, abs_y.value)
        from fusion_common import normalize_axis

        return normalize_axis(value_x, abs_x), normalize_axis(value_y, abs_y)

    def _write_left_stick(self) -> bool:
        if not self.fusion_enabled:
            return self._write_controller_a_left_stick()
        return self._write_fused_left_stick()

    def _write_controller_a_left_stick(self) -> bool:
        a_x, a_y = self._normalized_left("a")
        raw_x = denormalize_axis(a_x, self.output_absinfo[ecodes.ABS_X])
        raw_y = denormalize_axis(a_y, self.output_absinfo[ecodes.ABS_Y])
        changed = False
        changed |= self._write_output(ecodes.EV_ABS, ecodes.ABS_X, raw_x)
        changed |= self._write_output(ecodes.EV_ABS, ecodes.ABS_Y, raw_y)

        if self.args.debug and (changed or time.monotonic() - self.last_debug_at >= self.debug_interval):
            self.last_debug_at = time.monotonic()
            print(f"A=({a_x:+.3f},{a_y:+.3f}) fusion=off", flush=True)
        return changed

    def _write_fused_left_stick(self) -> bool:
        a_x, a_y = self._normalized_left("a")
        b_x, b_y = self._normalized_left("b")
        fused_x, fused_y, debug = fuse_left_stick(
            a_x,
            a_y,
            b_x,
            b_y,
            self.args.deadzone_a,
            self.args.deadzone_b,
        )
        raw_x = denormalize_axis(fused_x, self.output_absinfo[ecodes.ABS_X])
        raw_y = denormalize_axis(fused_y, self.output_absinfo[ecodes.ABS_Y])
        changed = False
        changed |= self._write_output(ecodes.EV_ABS, ecodes.ABS_X, raw_x)
        changed |= self._write_output(ecodes.EV_ABS, ecodes.ABS_Y, raw_y)

        if self.args.debug and (changed or time.monotonic() - self.last_debug_at >= self.debug_interval):
            self.last_debug_at = time.monotonic()
            print(
                (
                    f"A=({a_x:+.3f},{a_y:+.3f}) "
                    f"B=({b_x:+.3f},{b_y:+.3f}) "
                    f"fused=({fused_x:+.3f},{fused_y:+.3f}) "
                    f"a_active={debug['a_active']} "
                    f"b_mag={debug['b_magnitude']:.3f} "
                    f"angle_source={debug['angle_source']}"
                ),
                flush=True,
            )
        return changed

    def _trigger_axis_active(self, source: SourceState, codes: tuple[int, ...]) -> bool:
        for code in codes:
            if code not in source.abs_values:
                continue
            info = source.device.absinfo(code)
            span = info.max - info.min
            if span <= 0:
                continue
            normalized = (source.abs_values[code] - info.min) / span
            if normalized >= TOGGLE_TRIGGER_THRESHOLD:
                return True
        return False

    def _trigger_key_active(self, source: SourceState, codes: tuple[int, ...]) -> bool:
        return any(source.key_values.get(code, 0) for code in codes)

    def _toggle_chord_down(self) -> bool:
        source = self.sources.get("a")
        if not source:
            return False

        left_trigger = self._trigger_axis_active(source, TOGGLE_LEFT_TRIGGER_AXES) or self._trigger_key_active(
            source, TOGGLE_LEFT_TRIGGER_KEYS
        )
        right_trigger = self._trigger_axis_active(source, TOGGLE_RIGHT_TRIGGER_AXES) or self._trigger_key_active(
            source, TOGGLE_RIGHT_TRIGGER_KEYS
        )
        sticks = all(source.key_values.get(code, 0) for code in TOGGLE_STICK_KEYS)
        return left_trigger and right_trigger and sticks

    def _refresh_toggle_state(self) -> bool:
        chord_down = self._toggle_chord_down()
        changed = False
        if chord_down and not self.toggle_chord_was_down:
            self.fusion_enabled = not self.fusion_enabled
            print(f"Fusion mode: {'ON' if self.fusion_enabled else 'OFF'}", flush=True)
            changed = self._rerender_output()
        self.toggle_chord_was_down = chord_down
        return changed

    def _rerender_output(self) -> bool:
        changed = False
        for key_code in self._all_key_codes():
            changed |= self._write_output(ecodes.EV_KEY, key_code, self._merged_key_value(key_code))

        for abs_code in self.output_absinfo:
            if abs_code in LEFT_STICK_CODES:
                continue
            merged = self._merged_abs_value(abs_code)
            if merged is not None:
                changed |= self._write_output(ecodes.EV_ABS, abs_code, merged)
            elif not self.fusion_enabled:
                changed |= self._write_output(ecodes.EV_ABS, abs_code, self._neutral_abs_value(abs_code))

        changed |= self._write_left_stick()
        return changed

    def _write_output(self, event_type: int, code: int, value: int) -> bool:
        key = (event_type, code)
        if self.output_values.get(key) == value:
            return False
        self.output_values[key] = value
        if self.ui:
            self.ui.write(event_type, code, value)
        return True

    def _process_event(self, source_name: str, event) -> bool:
        source = self.sources[source_name]
        self.sequence += 1

        if event.type == ecodes.EV_KEY:
            source.key_values[event.code] = int(event.value)
            toggle_changed = self._refresh_toggle_state() if source_name == "a" else False
            merged = self._merged_key_value(event.code)
            return toggle_changed | self._write_output(ecodes.EV_KEY, event.code, merged)

        if event.type == ecodes.EV_ABS:
            source.abs_values[event.code] = int(event.value)
            source.abs_seq[event.code] = self.sequence
            toggle_changed = self._refresh_toggle_state() if source_name == "a" else False
            if event.code in LEFT_STICK_CODES:
                return toggle_changed | self._write_left_stick()
            merged = self._merged_abs_value(event.code)
            if merged is None:
                return toggle_changed
            return toggle_changed | self._write_output(ecodes.EV_ABS, event.code, merged)

        return False

    def _process_ui_event(self, event) -> bool:
        if not self.ui or not self.ff_target:
            return False

        if event.type == ecodes.EV_UINPUT:
            if event.code == ecodes.UI_FF_UPLOAD:
                upload = self.ui.begin_upload(event.value)
                if upload.effect.id not in self.ff_effect_ids:
                    self.ff_effect_ids.add(upload.effect.id)
                    upload.effect.id = -1
                self.ff_target.upload_effect(upload.effect)
                upload.retval = 0
                self.ui.end_upload(upload)
                return False

            if event.code == ecodes.UI_FF_ERASE:
                erase = self.ui.begin_erase(event.value)
                erase.retval = 0
                try:
                    self.ff_target.erase_effect(erase.effect_id)
                except OSError:
                    pass
                self.ff_effect_ids.discard(erase.effect_id)
                self.ui.end_erase(erase)
                return False

        if event.type == ecodes.EV_FF:
            self.ff_target.write(event.type, event.code, event.value)
            return False

        return False

    def run(self) -> None:
        self._print_startup_banner()
        while not self.should_stop:
            dirty = False
            for key, _ in self.selector.select(timeout=0.1):
                source_key = key.data
                if source_key == "ui":
                    if not self.ui:
                        continue
                    for event in self.ui.read():
                        if event.type == ecodes.EV_SYN:
                            continue
                        self._process_ui_event(event)
                    continue

                source_name = "a" if source_key == self.selected["a"].event_path else "b"
                for event in self.sources[source_name].device.read():
                    if event.type == ecodes.EV_SYN:
                        continue
                    dirty |= self._process_event(source_name, event)
            if dirty and self.ui:
                self.ui.syn()

    def _print_startup_banner(self) -> None:
        print(f"Controller A: {self.selected['a'].label()}", flush=True)
        print(f"Controller B: {self.selected['b'].label()}", flush=True)
        if self.args.dry_run:
            print("Mode: dry-run (no virtual controller created)", flush=True)
        else:
            print(f"Virtual controller: {self.args.virtual_name}", flush=True)
            if self.ff_target:
                print(f"Force feedback target: {self.selected['a'].label()}", flush=True)
            else:
                print("Force feedback target: unavailable on Controller A", flush=True)
        print(f"Grab mode: {self.args.grab}", flush=True)
        print("Fusion mode: ON", flush=True)
        print("Toggle binding: LT + RT + LS + RS on Controller A", flush=True)

    def close(self) -> None:
        if self.ui:
            self._write_output(ecodes.EV_ABS, ecodes.ABS_X, denormalize_axis(0.0, self.output_absinfo[ecodes.ABS_X]))
            self._write_output(ecodes.EV_ABS, ecodes.ABS_Y, denormalize_axis(0.0, self.output_absinfo[ecodes.ABS_Y]))
            self.ui.syn()

        for source in self.sources.values():
            try:
                source.device.ungrab()
            except OSError:
                pass
            try:
                source.device.close()
            except OSError:
                pass

        if self.ui:
            try:
                self.selector.unregister(self.ui)
            except Exception:
                pass
            try:
                self.ui.close()
            except OSError:
                pass


def parse_args() -> argparse.Namespace:
    def _parse_int(value: str) -> int:
        return int(value, 0)

    parser = argparse.ArgumentParser(description="Fuse two physical controllers into one virtual gamepad.")
    parser.add_argument("--a-event", help="Use this /dev/input/event path for Controller A.")
    parser.add_argument("--b-event", help="Use this /dev/input/event path for Controller B.")
    parser.add_argument("--a-uniq", help="Match Controller A by unique identifier (for Bluetooth devices this is typically the MAC).")
    parser.add_argument("--b-uniq", help="Match Controller B by unique identifier.")
    parser.add_argument("--a-name", help="Match Controller A by a case-insensitive name substring.")
    parser.add_argument("--b-name", help="Match Controller B by a case-insensitive name substring.")
    parser.add_argument("--deadzone-a", type=float, default=DEFAULT_DEADZONE, help="Radial deadzone for Controller A's left stick.")
    parser.add_argument("--deadzone-b", type=float, default=DEFAULT_DEADZONE, help="Radial deadzone for Controller B's left stick.")
    parser.add_argument("--grab", choices=["none", "a", "b", "both"], default="none", help="Optionally grab source devices to hide them while the tool is running.")
    parser.add_argument("--dry-run", action="store_true", help="Print fused values without creating a virtual controller.")
    parser.add_argument("--debug", action="store_true", help="Print normalized source and fused left-stick values.")
    parser.add_argument("--debug-hz", type=float, default=10.0, help="Maximum debug print frequency.")
    parser.add_argument("--print-devices", action="store_true", help="List joystick-capable devices and exit.")
    parser.add_argument("--print-selection-json", action="store_true", help="Resolve Controller A and B and print the selected device metadata as JSON.")
    parser.add_argument("--virtual-name", default="Controller Fusion Prototype", help="Name of the virtual controller.")
    parser.add_argument("--virtual-vendor", type=_parse_int, default=DEFAULT_VIRTUAL_VENDOR, help="Virtual controller vendor ID, for example 0xF155.")
    parser.add_argument("--virtual-product", type=_parse_int, default=DEFAULT_VIRTUAL_PRODUCT, help="Virtual controller product ID, for example 0x0001.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    devices = enumerate_gamepads()
    selector_a = SelectorSpec(
        event_path=args.a_event,
        uniq=args.a_uniq,
        name=args.a_name,
    )
    selector_b = SelectorSpec(
        event_path=args.b_event,
        uniq=args.b_uniq,
        name=args.b_name,
    )
    if args.print_devices:
        if not devices:
            print("No joystick-capable input devices were found.")
            return 1
        default_a = choose_default_device(devices, "a")
        default_b = choose_default_device(devices, "b")
        for device in devices:
            labels = []
            if device.event_path == default_a.event_path:
                labels.append("default-A")
            if device.event_path == default_b.event_path:
                labels.append("default-B")
            suffix = f" [{', '.join(labels)}]" if labels else ""
            print(f"{device.label()}{suffix}")
        return 0
    if args.print_selection_json:
        if not devices:
            print("No joystick-capable input devices were found.", file=sys.stderr)
            return 1
        summary_a = resolve_device(devices, selector_a, "a")
        summary_b = resolve_device(devices, selector_b, "b")
        if summary_a.event_path == summary_b.event_path:
            print("Controller A and Controller B resolved to the same device.", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "a": {
                        "event_path": summary_a.event_path,
                        "js_path": summary_a.js_path,
                        "name": summary_a.name,
                        "uniq": summary_a.uniq,
                    },
                    "b": {
                        "event_path": summary_b.event_path,
                        "js_path": summary_b.js_path,
                        "name": summary_b.name,
                        "uniq": summary_b.uniq,
                    },
                }
            )
        )
        return 0

    runtime: FusionRuntime | None = None
    try:
        runtime = FusionRuntime(args)
        runtime.run()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if runtime:
            runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
