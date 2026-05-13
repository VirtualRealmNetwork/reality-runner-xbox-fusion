#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import json
import os
import selectors
import signal
import sys
import time
import traceback
from dataclasses import dataclass, field

import pyudev
from evdev import AbsInfo, InputDevice, UInput, ecodes

from fusion_common import (
    SelectorSpec,
    center_for,
    choose_default_device,
    denormalize_axis,
    enumerate_gamepads,
    fuse_left_stick,
    is_reality_runner_v2,
    resolve_device,
)


LEFT_STICK_CODES = {ecodes.ABS_X, ecodes.ABS_Y}
ONE_SIDED_ABS_CODES = {ecodes.ABS_GAS, ecodes.ABS_BRAKE}
DEFAULT_DEADZONE = 0.18
DEFAULT_VIRTUAL_VENDOR = 0xF155
DEFAULT_VIRTUAL_PRODUCT = 0x0001
DEFAULT_STARTUP_WAIT_SECONDS = 30.0
TOGGLE_TRIGGER_THRESHOLD = 0.75
TOGGLE_LEFT_TRIGGER_AXES = (ecodes.ABS_BRAKE, ecodes.ABS_Z)
TOGGLE_RIGHT_TRIGGER_AXES = (ecodes.ABS_GAS, ecodes.ABS_RZ)
TOGGLE_LEFT_TRIGGER_KEYS = (ecodes.BTN_TL2,)
TOGGLE_RIGHT_TRIGGER_KEYS = (ecodes.BTN_TR2,)
TOGGLE_STICK_KEYS = (ecodes.BTN_THUMBL, ecodes.BTN_THUMBR)


@dataclass
class SourceState:
    device: InputDevice
    event_path: str
    abs_info: dict[int, AbsInfo] = field(default_factory=dict)
    abs_values: dict[int, int] = field(default_factory=dict)
    abs_seq: dict[int, int] = field(default_factory=dict)
    key_values: dict[int, int] = field(default_factory=dict)
    connected: bool = True


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
        self.source_selectors: dict[str, SelectorSpec] = {}
        self.reconnect_selectors: dict[str, SelectorSpec] = {}
        self.reconnect_after: dict[str, float] = {}
        self.next_presence_check = 0.0
        self.udev_context: pyudev.Context | None = None
        self.udev_monitor: pyudev.Monitor | None = None
        self.fusion_enabled = True
        self.toggle_chord_was_down = False
        self.toggle_debug_last = ""
        self.toggle_left_trigger_axes: tuple[int, ...] = ()
        self.toggle_right_trigger_axes: tuple[int, ...] = ()
        self.b_supports_backward = False
        self._setup()

    def _setup(self) -> None:
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
        self.source_selectors = {"a": selector_a, "b": selector_b}
        summary_a, summary_b = self._resolve_startup_devices(selector_a, selector_b)

        self.selected = {"a": summary_a, "b": summary_b}
        self.b_supports_backward = is_reality_runner_v2(summary_b)
        self.reconnect_selectors = {
            "a": self._build_reconnect_selector(selector_a, summary_a),
            "b": self._build_reconnect_selector(selector_b, summary_b),
        }
        self.sources = {
            "a": self._open_source(summary_a.event_path, "a"),
            "b": self._open_source(summary_b.event_path, "b"),
        }
        self._select_toggle_trigger_axes()
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

        self._setup_udev_monitor()
        self._install_signal_handlers()

    def _resolve_startup_devices(self, selector_a: SelectorSpec, selector_b: SelectorSpec):
        deadline = time.monotonic() + self.args.startup_wait
        last_error: Exception | None = None

        while True:
            try:
                devices = enumerate_gamepads()
                if not devices:
                    raise RuntimeError("No joystick-capable input devices were found.")

                summary_a = resolve_device(devices, selector_a, "a")
                summary_b = resolve_device(devices, selector_b, "b")
                if summary_a.event_path == summary_b.event_path:
                    raise RuntimeError("Controller A and Controller B resolved to the same device.")
                return summary_a, summary_b
            except Exception as exc:
                last_error = exc
                if self.args.startup_wait <= 0 or time.monotonic() >= deadline:
                    raise
                print(f"Waiting for distinct Controller A/B devices: {exc}", flush=True)
                time.sleep(min(self.args.reconnect_interval, 1.0))

    def _build_reconnect_selector(self, requested: SelectorSpec, summary) -> SelectorSpec:
        if requested.uniq or summary.uniq:
            return SelectorSpec(uniq=requested.uniq or summary.uniq)
        if requested.name or summary.name:
            return SelectorSpec(name=requested.name or summary.name)
        return requested

    def _install_signal_handlers(self) -> None:
        def _handler(signum, frame):  # noqa: ARG001
            self.should_stop = True

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

    def _open_source(self, path: str, source_name: str) -> SourceState:
        device = InputDevice(path)
        state = SourceState(device=device, event_path=device.path)

        for code, absinfo in device.capabilities(absinfo=True).get(ecodes.EV_ABS, []):
            state.abs_info[code] = absinfo
            state.abs_values[code] = absinfo.value
            state.abs_seq[code] = 0

        for key_code in device.capabilities().get(ecodes.EV_KEY, []):
            state.key_values[key_code] = 1 if key_code in device.active_keys() else 0

        if self._should_grab(path):
            device.grab()

        self.selector.register(device, selectors.EVENT_READ, source_name)
        return state

    def _setup_udev_monitor(self) -> None:
        try:
            self.udev_context = pyudev.Context()
            self.udev_monitor = pyudev.Monitor.from_netlink(self.udev_context)
            self.udev_monitor.filter_by(subsystem="input")
            self.udev_monitor.start()
            self.selector.register(self.udev_monitor, selectors.EVENT_READ, "udev")
        except Exception as exc:
            self.udev_monitor = None
            print(f"Udev monitor unavailable; falling back to polling only: {exc}", flush=True)

    def _close_source(self, source_name: str, *, close_device: bool = True) -> None:
        source = self.sources.get(source_name)
        if not source:
            return
        try:
            self.selector.unregister(source.device)
        except Exception:
            pass
        if not close_device:
            return
        try:
            source.device.ungrab()
        except OSError:
            pass
        try:
            source.device.close()
        except OSError:
            pass

    def _mark_source_disconnected(self, source_name: str, reason: str) -> None:
        source = self.sources.get(source_name)
        if not source or not source.connected:
            return

        old_label = self.selected[source_name].label()
        if source_name == "a":
            self.ff_target = None
        # Closing a vanished Bluetooth evdev fd can block in evdev_cleanup.
        # Detach it from the selector and let process shutdown reclaim it.
        self._close_source(source_name, close_device=False)
        source.connected = False
        self.reconnect_after[source_name] = time.monotonic() + self.args.reconnect_interval
        self._neutralize_source(source_name)
        self.toggle_chord_was_down = False
        print(f"Controller {source_name.upper()} disconnected: {old_label} ({reason})", flush=True)

    def _neutralize_source(self, source_name: str) -> None:
        source = self.sources[source_name]
        for code in list(source.key_values):
            source.key_values[code] = 0
        for code in list(source.abs_values):
            if code in self.output_absinfo:
                source.abs_values[code] = self._neutral_abs_value(code)
        if self.ui:
            if self._rerender_output():
                self.ui.syn()

    def _reconnect_sources(self) -> None:
        now = time.monotonic()
        for source_name, source in list(self.sources.items()):
            if source.connected:
                continue
            if now < self.reconnect_after.get(source_name, 0.0):
                continue
            self.reconnect_after[source_name] = now + self.args.reconnect_interval
            try:
                self._reconnect_source(source_name)
            except Exception as exc:
                if self.args.debug_reconnect:
                    print(f"Controller {source_name.upper()} reconnect pending: {exc}", flush=True)

    def _reconnect_source(self, source_name: str) -> None:
        devices = enumerate_gamepads()
        summary = resolve_device(devices, self.reconnect_selectors[source_name], source_name)
        other_name = "b" if source_name == "a" else "a"
        other = self.selected.get(other_name)
        other_path = other.event_path if other and self.sources.get(other_name, None) and self.sources[other_name].connected else None
        if other_path and summary.event_path == other_path:
            raise RuntimeError("resolved to the other connected controller")

        self.selected[source_name] = summary
        if source_name == "b":
            self.b_supports_backward = is_reality_runner_v2(summary)
        self.sources[source_name] = self._open_source(summary.event_path, source_name)
        if source_name == "a":
            self._select_toggle_trigger_axes()
            self.toggle_chord_was_down = self._toggle_chord_down()
            if ecodes.EV_FF in self.sources["a"].device.capabilities():
                self.ff_target = self.sources["a"].device
            else:
                self.ff_target = None

        if self.ui and self._rerender_output():
            self.ui.syn()
        print(f"Controller {source_name.upper()} reconnected: {summary.label()}", flush=True)

    def _check_source_presence(self) -> None:
        now = time.monotonic()
        if now < self.next_presence_check:
            return
        self.next_presence_check = now + self.args.reconnect_interval
        for source_name, source in list(self.sources.items()):
            if not source.connected:
                continue
            if not os.path.exists(source.event_path):
                self._mark_source_disconnected(source_name, "device node disappeared")

    def _process_udev_events(self) -> None:
        if not self.udev_monitor:
            return
        while True:
            try:
                device = self.udev_monitor.poll(timeout=0)
            except Exception as exc:
                if self.args.debug_reconnect:
                    print(f"Udev monitor read failed: {exc}", flush=True)
                return
            if device is None:
                return

            devname = device.device_node or ""
            action = device.action or ""
            if self.args.debug_reconnect and action in {"add", "remove", "change"} and devname.startswith("/dev/input/"):
                print(f"udev {action}: {devname}", flush=True)

            if action == "remove":
                for source_name, source in list(self.sources.items()):
                    if source.connected and devname == source.event_path:
                        self._mark_source_disconnected(source_name, "udev remove")

            if action in {"add", "change"}:
                for source_name, source in list(self.sources.items()):
                    if not source.connected:
                        self.reconnect_after[source_name] = 0.0

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

    def _select_toggle_trigger_axes(self) -> None:
        source = self.sources["a"]
        self.toggle_left_trigger_axes = self._preferred_present_axis(source, (ecodes.ABS_BRAKE, ecodes.ABS_Z))
        self.toggle_right_trigger_axes = self._preferred_present_axis(source, (ecodes.ABS_GAS, ecodes.ABS_RZ))

    def _preferred_present_axis(self, source: SourceState, candidates: tuple[int, ...]) -> tuple[int, ...]:
        for code in candidates:
            if code in source.abs_values:
                return (code,)
        return ()

    def _is_toggle_event(self, event) -> bool:
        if event.type == ecodes.EV_KEY:
            return event.code in TOGGLE_LEFT_TRIGGER_KEYS + TOGGLE_RIGHT_TRIGGER_KEYS + TOGGLE_STICK_KEYS
        if event.type == ecodes.EV_ABS:
            return event.code in self.toggle_left_trigger_axes + self.toggle_right_trigger_axes
        return False

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
        abs_x = source.abs_info[ecodes.ABS_X]
        abs_y = source.abs_info[ecodes.ABS_Y]
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
            self.b_supports_backward,
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
                    f"b_backward={debug['b_backward']} "
                    f"angle_source={debug['angle_source']}"
                ),
                flush=True,
            )
        return changed

    def _trigger_axis_active(self, source: SourceState, codes: tuple[int, ...]) -> bool:
        for code in codes:
            if code not in source.abs_values:
                continue
            info = source.abs_info[code]
            span = info.max - info.min
            if span <= 0:
                continue
            normalized = (source.abs_values[code] - info.min) / span
            if normalized >= TOGGLE_TRIGGER_THRESHOLD:
                return True
        return False

    def _describe_trigger_axes(self, source: SourceState, codes: tuple[int, ...]) -> str:
        parts = []
        for code in codes:
            if code not in source.abs_values:
                continue
            info = source.abs_info[code]
            span = info.max - info.min
            normalized = 0.0 if span <= 0 else (source.abs_values[code] - info.min) / span
            parts.append(f"{ecodes.ABS.get(code, code)}={source.abs_values[code]}({normalized:.2f})")
        return ",".join(parts) if parts else "missing"

    def _trigger_key_active(self, source: SourceState, codes: tuple[int, ...]) -> bool:
        return any(source.key_values.get(code, 0) for code in codes)

    def _toggle_chord_down(self) -> bool:
        return all(self._toggle_chord_parts().values())

    def _toggle_chord_parts(self) -> dict[str, bool]:
        source = self.sources.get("a")
        if not source:
            return {"lt": False, "rt": False, "ls": False, "rs": False}

        left_trigger = self._trigger_axis_active(source, self.toggle_left_trigger_axes) or self._trigger_key_active(
            source, TOGGLE_LEFT_TRIGGER_KEYS
        )
        right_trigger = self._trigger_axis_active(source, self.toggle_right_trigger_axes) or self._trigger_key_active(
            source, TOGGLE_RIGHT_TRIGGER_KEYS
        )
        return {
            "lt": left_trigger,
            "rt": right_trigger,
            "ls": bool(source.key_values.get(ecodes.BTN_THUMBL, 0)),
            "rs": bool(source.key_values.get(ecodes.BTN_THUMBR, 0)),
        }

    def _debug_toggle_state(self, event) -> None:
        if not self.args.debug_toggle:
            return
        if not self._is_toggle_event(event):
            return

        source = self.sources["a"]
        parts = self._toggle_chord_parts()
        line = (
            "Toggle chord: "
            f"LT={parts['lt']} "
            f"RT={parts['rt']} "
            f"LS={parts['ls']} "
            f"RS={parts['rs']} "
            f"left_axes={self._describe_trigger_axes(source, self.toggle_left_trigger_axes)} "
            f"right_axes={self._describe_trigger_axes(source, self.toggle_right_trigger_axes)} "
            f"left_trigger_key={self._trigger_key_active(source, TOGGLE_LEFT_TRIGGER_KEYS)} "
            f"right_trigger_key={self._trigger_key_active(source, TOGGLE_RIGHT_TRIGGER_KEYS)} "
            f"last_event={ecodes.bytype[event.type].get(event.code, event.code)}:{event.value}"
        )
        if line != self.toggle_debug_last:
            self.toggle_debug_last = line
            print(line, flush=True)

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
            toggle_event = source_name == "a" and self._is_toggle_event(event)
            if toggle_event:
                self._debug_toggle_state(event)
            toggle_changed = self._refresh_toggle_state() if toggle_event else False
            merged = self._merged_key_value(event.code)
            return toggle_changed | self._write_output(ecodes.EV_KEY, event.code, merged)

        if event.type == ecodes.EV_ABS:
            source.abs_values[event.code] = int(event.value)
            source.abs_seq[event.code] = self.sequence
            toggle_event = source_name == "a" and self._is_toggle_event(event)
            if toggle_event:
                self._debug_toggle_state(event)
            toggle_changed = self._refresh_toggle_state() if toggle_event else False
            if event.code in LEFT_STICK_CODES:
                return toggle_changed | self._write_left_stick()
            merged = self._merged_abs_value(event.code)
            if merged is None:
                return toggle_changed
            return toggle_changed | self._write_output(ecodes.EV_ABS, event.code, merged)

        return False

    def _process_ui_event(self, event) -> bool:
        if not self.ui or not self.ff_target:
            if self.ui and event.type == ecodes.EV_UINPUT and event.code == ecodes.UI_FF_UPLOAD:
                upload = self.ui.begin_upload(event.value)
                upload.retval = -errno.ENODEV
                self.ui.end_upload(upload)
            elif self.ui and event.type == ecodes.EV_UINPUT and event.code == ecodes.UI_FF_ERASE:
                erase = self.ui.begin_erase(event.value)
                erase.retval = -errno.ENODEV
                self.ui.end_erase(erase)
            return False

        if event.type == ecodes.EV_UINPUT:
            if event.code == ecodes.UI_FF_UPLOAD:
                upload = self.ui.begin_upload(event.value)
                if upload.effect.id not in self.ff_effect_ids:
                    self.ff_effect_ids.add(upload.effect.id)
                    upload.effect.id = -1
                try:
                    self.ff_target.upload_effect(upload.effect)
                    upload.retval = 0
                except OSError as exc:
                    upload.retval = -abs(getattr(exc, "errno", errno.ENODEV) or errno.ENODEV)
                    self.ff_target = None
                self.ui.end_upload(upload)
                return False

            if event.code == ecodes.UI_FF_ERASE:
                erase = self.ui.begin_erase(event.value)
                try:
                    erase.retval = 0
                    self.ff_target.erase_effect(erase.effect_id)
                except OSError as exc:
                    erase.retval = -abs(getattr(exc, "errno", errno.ENODEV) or errno.ENODEV)
                    self.ff_target = None
                self.ff_effect_ids.discard(erase.effect_id)
                self.ui.end_erase(erase)
                return False

        if event.type == ecodes.EV_FF:
            try:
                self.ff_target.write(event.type, event.code, event.value)
            except OSError:
                self.ff_target = None
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
                    try:
                        ui_events = list(self.ui.read())
                    except OSError as exc:
                        if getattr(exc, "errno", None) in {errno.EAGAIN, errno.EWOULDBLOCK}:
                            continue
                        print(f"Virtual controller event read failed: {exc}", flush=True)
                        continue
                    for event in ui_events:
                        if event.type == ecodes.EV_SYN:
                            continue
                        self._process_ui_event(event)
                    continue

                if source_key == "udev":
                    self._process_udev_events()
                    continue

                source_name = source_key
                source = self.sources.get(source_name)
                if not source or not source.connected:
                    continue
                try:
                    events = list(source.device.read())
                except OSError as exc:
                    if getattr(exc, "errno", None) in {errno.EAGAIN, errno.EWOULDBLOCK}:
                        continue
                    self._mark_source_disconnected(source_name, str(exc))
                    continue
                for event in events:
                    if event.type == ecodes.EV_SYN:
                        continue
                    dirty |= self._process_event(source_name, event)
            if dirty and self.ui:
                self.ui.syn()
            self._check_source_presence()
            self._reconnect_sources()

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
        print(f"Controller B backward support: {'ON' if self.b_supports_backward else 'OFF'}", flush=True)
        print("Fusion mode: ON", flush=True)
        print("Toggle binding: LT + RT + LS + RS on Controller A", flush=True)
        if self.args.debug_toggle:
            print("Toggle debug: enabled", flush=True)

    def close(self) -> None:
        if self.ui:
            self._write_output(ecodes.EV_ABS, ecodes.ABS_X, denormalize_axis(0.0, self.output_absinfo[ecodes.ABS_X]))
            self._write_output(ecodes.EV_ABS, ecodes.ABS_Y, denormalize_axis(0.0, self.output_absinfo[ecodes.ABS_Y]))
            self.ui.syn()

        for source in self.sources.values():
            if not source.connected:
                continue
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
        if self.udev_monitor:
            try:
                self.selector.unregister(self.udev_monitor)
            except Exception:
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
    parser.add_argument("--debug-toggle", action="store_true", help="Print LT/RT/LS/RS toggle chord state while Controller A inputs change.")
    parser.add_argument("--debug-reconnect", action="store_true", help="Print failed reconnect attempts.")
    parser.add_argument("--debug-hz", type=float, default=10.0, help="Maximum debug print frequency.")
    parser.add_argument("--reconnect-interval", type=float, default=1.0, help="Seconds between disconnected controller reconnect attempts.")
    parser.add_argument("--startup-wait", type=float, default=DEFAULT_STARTUP_WAIT_SECONDS, help="Seconds to wait for two distinct controllers at startup.")
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
        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        if runtime:
            runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
