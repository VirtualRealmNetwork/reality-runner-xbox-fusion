#!/usr/bin/env python3
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pyudev
from evdev import InputDevice, ecodes, list_devices

REALITY_RUNNER_V2_UNIQS = {"aa:bb:cc:dd:ee:02"}


@dataclass(frozen=True)
class DeviceSummary:
    event_path: str
    js_path: str | None
    name: str
    uniq: str
    vendor: int
    product: int
    version: int
    bus: str
    phys: str
    sys_path: str
    dev_path: str
    kind: str

    @property
    def short_id(self) -> str:
        if self.uniq:
            return self.uniq
        return self.event_path

    @property
    def vendor_product(self) -> str:
        return f"{self.vendor:04x}:{self.product:04x}"

    def label(self) -> str:
        js_text = f", js={self.js_path}" if self.js_path else ""
        uniq_text = f", uniq={self.uniq}" if self.uniq else ""
        return (
            f"{self.name} [{self.event_path}{js_text}, "
            f"id={self.vendor_product}{uniq_text}, bus={self.bus}, kind={self.kind}]"
        )


@dataclass(frozen=True)
class SelectorSpec:
    event_path: str | None = None
    uniq: str | None = None
    name: str | None = None


def canonical_path(path: str) -> str:
    return str(Path(path).resolve())


def build_js_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    sys_class_input = Path("/sys/class/input")
    if not sys_class_input.exists():
        return mapping
    for child in sys_class_input.iterdir():
        if not child.name.startswith("js"):
            continue
        device_dir = child / "device"
        if device_dir.exists():
            mapping[canonical_path(str(device_dir))] = f"/dev/input/{child.name}"
    return mapping


def classify_device(bus: str, dev_path: str, name: str) -> str:
    if bus in {"bluetooth", "usb"}:
        return "physical"
    lowered = name.lower()
    if "controller fusion prototype" in lowered:
        return "virtual"
    if "/devices/virtual/" in dev_path:
        return "virtual"
    return "unknown"


def enumerate_gamepads() -> list[DeviceSummary]:
    context = pyudev.Context()
    js_map = build_js_map()
    devices: list[DeviceSummary] = []

    for event_path in sorted(list_devices()):
        try:
            evdev_device = InputDevice(event_path)
            udev_device = pyudev.Devices.from_device_file(context, event_path)
        except Exception:
            continue

        props = udev_device.properties
        is_joystick = props.get("ID_INPUT_JOYSTICK") == "1"
        if not is_joystick:
            continue

        sys_path = canonical_path(f"/sys/class/input/{Path(event_path).name}/device")
        bus = props.get("ID_BUS", "unknown")
        uniq = evdev_device.uniq or ""
        kind = classify_device(bus, udev_device.device_path, evdev_device.name)
        devices.append(
            DeviceSummary(
                event_path=event_path,
                js_path=js_map.get(sys_path),
                name=evdev_device.name,
                uniq=uniq,
                vendor=evdev_device.info.vendor,
                product=evdev_device.info.product,
                version=evdev_device.info.version,
                bus=bus,
                phys=evdev_device.phys or "",
                sys_path=sys_path,
                dev_path=udev_device.device_path,
                kind=kind,
            )
        )

    return devices


def _matches_name(summary: DeviceSummary, needle: str) -> bool:
    return needle.casefold() in summary.name.casefold()


def _match_by_event(devices: Iterable[DeviceSummary], event_path: str) -> list[DeviceSummary]:
    canonical = canonical_path(event_path)
    return [device for device in devices if canonical_path(device.event_path) == canonical]


def _match_by_uniq(devices: Iterable[DeviceSummary], uniq: str) -> list[DeviceSummary]:
    wanted = uniq.casefold()
    return [device for device in devices if device.uniq.casefold() == wanted]


def _match_by_name(devices: Iterable[DeviceSummary], name: str) -> list[DeviceSummary]:
    return [device for device in devices if _matches_name(device, name)]


def choose_default_device(devices: list[DeviceSummary], role: str) -> DeviceSummary:
    physical = [device for device in devices if device.kind == "physical"]
    pool = physical or devices

    if role == "a":
        for candidate in pool:
            if candidate.product == 0x0B20:
                return candidate
        for candidate in pool:
            if "xbox" in candidate.name.casefold():
                return candidate
    elif role == "b":
        for candidate in pool:
            if candidate.product == 0x0B13:
                return candidate
        for candidate in pool:
            if "realityrunner" in candidate.name.casefold():
                return candidate
        for candidate in pool:
            if "bluez-hog-device" in candidate.name.casefold():
                return candidate

    if not pool:
        raise RuntimeError("No joystick-capable input devices were found.")
    return pool[0]


def resolve_device(devices: list[DeviceSummary], selector: SelectorSpec, role: str) -> DeviceSummary:
    matches = devices
    if selector.event_path:
        matches = _match_by_event(matches, selector.event_path)
    if selector.uniq:
        matches = _match_by_uniq(matches, selector.uniq)
    if selector.name:
        matches = _match_by_name(matches, selector.name)

    if not selector.event_path and not selector.uniq and not selector.name:
        return choose_default_device(devices, role)
    if not matches:
        raise RuntimeError(f"No device matched the selector for controller {role.upper()}.")
    if len(matches) > 1:
        labels = "; ".join(device.label() for device in matches)
        raise RuntimeError(
            f"Selector for controller {role.upper()} is ambiguous. Matches: {labels}"
        )
    return matches[0]


def is_reality_runner_v2(summary: DeviceSummary) -> bool:
    return summary.uniq.casefold() in REALITY_RUNNER_V2_UNIQS


def center_for(absinfo) -> float:
    return (absinfo.min + absinfo.max) / 2.0


def half_range_for(absinfo) -> float:
    return max((absinfo.max - absinfo.min) / 2.0, 1.0)


def normalize_axis(value: int, absinfo) -> float:
    normalized = (float(value) - center_for(absinfo)) / half_range_for(absinfo)
    return max(-1.0, min(1.0, normalized))


def denormalize_axis(value: float, absinfo) -> int:
    raw = center_for(absinfo) + max(-1.0, min(1.0, value)) * half_range_for(absinfo)
    return int(round(max(absinfo.min, min(absinfo.max, raw))))


def radial_deadzone(x: float, y: float, deadzone: float) -> tuple[float, float, float]:
    magnitude = math.hypot(x, y)
    if magnitude <= deadzone:
        return 0.0, 0.0, 0.0
    scaled = (magnitude - deadzone) / max(1.0 - deadzone, 1e-9)
    scaled = max(0.0, min(1.0, scaled))
    if magnitude == 0:
        return 0.0, 0.0, 0.0
    ratio = scaled / magnitude
    return x * ratio, y * ratio, scaled


def fuse_left_stick(
    a_x: float,
    a_y: float,
    b_x: float,
    b_y: float,
    deadzone_a: float,
    deadzone_b: float,
    b_supports_backward: bool = False,
) -> tuple[float, float, dict[str, float | bool | str]]:
    dz_a_x, dz_a_y, magnitude_a = radial_deadzone(a_x, a_y, deadzone_a)
    _, dz_b_y, magnitude_b = radial_deadzone(b_x, b_y, deadzone_b)

    angle_active = magnitude_a > 0.0
    b_backward = b_supports_backward and dz_b_y > 0.0
    if magnitude_b <= 0.0:
        fused_x = 0.0
        fused_y = 0.0
        angle_source = "centered"
        angle_radians = 0.0
    elif angle_active:
        angle_radians = math.atan2(dz_a_y, dz_a_x)
        if b_backward:
            angle_radians += math.pi
        fused_x = math.cos(angle_radians) * magnitude_b
        fused_y = math.sin(angle_radians) * magnitude_b
        angle_source = "controller_a_opposite" if b_backward else "controller_a"
    else:
        fused_x = 0.0
        fused_y = magnitude_b if b_backward else -magnitude_b
        angle_radians = math.pi / 2.0 if b_backward else -math.pi / 2.0
        angle_source = "backward" if b_backward else "forward"

    debug = {
        "a_active": angle_active,
        "a_magnitude": magnitude_a,
        "b_magnitude": magnitude_b,
        "b_backward": b_backward,
        "angle_source": angle_source,
        "angle_radians": angle_radians,
    }
    return fused_x, fused_y, debug


def summarize_devices(devices: list[DeviceSummary]) -> str:
    lines = []
    for device in devices:
        lines.append(device.label())
    return "\n".join(lines)


def compute_geometry(
    a_x: float,
    a_y: float,
    b_x: float,
    b_y: float,
    deadzone_a: float,
    deadzone_b: float,
    b_supports_backward: bool = False,
) -> tuple[float, float, float, float, bool, bool, float, str]:
    """Direction + per-source magnitudes for the steering blend.

    Returns (ux, uy, magnitude_a, magnitude_b, a_active, b_backward, angle_radians,
    angle_source). (ux, uy) is the unit output direction; the caller multiplies it by a
    blended magnitude. Direction matches fuse_left_stick (Controller A angle when A is
    active, else forward, with the Reality Runner v2 backward flip).
    """
    dz_a_x, dz_a_y, magnitude_a = radial_deadzone(a_x, a_y, deadzone_a)
    _, dz_b_y, magnitude_b = radial_deadzone(b_x, b_y, deadzone_b)

    a_active = magnitude_a > 0.0
    b_backward = b_supports_backward and dz_b_y > 0.0
    if a_active:
        angle_radians = math.atan2(dz_a_y, dz_a_x)
        if b_backward:
            angle_radians += math.pi
        ux = math.cos(angle_radians)
        uy = math.sin(angle_radians)
        angle_source = "controller_a_opposite" if b_backward else "controller_a"
    else:
        ux = 0.0
        uy = 1.0 if b_backward else -1.0
        angle_radians = math.pi / 2.0 if b_backward else -math.pi / 2.0
        angle_source = "backward" if b_backward else "forward"
    return ux, uy, magnitude_a, magnitude_b, a_active, b_backward, angle_radians, angle_source


@dataclass
class IntensityBlender:
    """Blend Controller A's instant intensity with the treadmill's lagged intensity.

    A is "engaged" via a Schmitt trigger on the raw stick magnitude: engage above
    `on_threshold`, disengage below `off_threshold` (hysteresis). The engaged state is
    then debounced: the *rising* edge fires immediately (fast start; seed 1 only if we
    were idle, m_prev < eps_move, else 0 for steer-only), while the *falling* edge fires
    only after the stick stays disengaged for `debounce` seconds. The low off-threshold
    keeps the disengaged window tiny during a direction reversal, so the short debounce
    bridges it without chopping output to 0; a real release (settling near center longer
    than the debounce) still snaps output toward mag_A (== 0) for a fast stop.

    Weight w holds at its seed for `hold` seconds then ramps linearly to 0 over `ramp`.
    Output magnitude m = w * a + (1 - w) * b.
    """

    hold: float
    ramp: float
    eps_move: float
    debounce: float = 0.06
    on_threshold: float = 0.18
    off_threshold: float = 0.10
    w0: float = 0.0
    since_edge: float = 1.0e9
    m_prev: float = 0.0
    engaged: bool = False
    confirmed_active: bool = False
    pending_off: float = 0.0

    def reset(self) -> None:
        self.w0 = 0.0
        self.since_edge = 1.0e9
        self.m_prev = 0.0
        self.engaged = False
        self.confirmed_active = False
        self.pending_off = 0.0

    def weight(self) -> float:
        t = self.since_edge
        if t < self.hold:
            profile = 1.0
        elif self.ramp > 0.0 and t < self.hold + self.ramp:
            profile = 1.0 - (t - self.hold) / self.ramp
        else:
            profile = 0.0
        return self.w0 * profile

    def is_active(self) -> bool:
        return (self.w0 > 0.0 and self.since_edge < self.hold + self.ramp) or self.pending_off > 0.0

    def update(self, a: float, b: float, raw_mag_a: float, dt: float) -> float:
        dt = dt if dt > 0.0 else 0.0

        # Schmitt-trigger engagement on the raw stick magnitude (hysteresis).
        if not self.engaged and raw_mag_a > self.on_threshold:
            self.engaged = True
        elif self.engaged and raw_mag_a <= self.off_threshold:
            self.engaged = False

        # Debounce the engaged state into confirmed_active and detect edges.
        rising = False
        falling = False
        if self.engaged != self.confirmed_active:
            if self.engaged:
                self.confirmed_active = True   # rising: confirm immediately (fast start)
                self.pending_off = 0.0
                rising = True
            else:
                self.pending_off += dt         # falling: require `debounce` of disengagement
                if self.pending_off >= self.debounce:
                    self.confirmed_active = False
                    self.pending_off = 0.0
                    falling = True
        else:
            self.pending_off = 0.0

        if rising:
            self.w0 = 1.0 if self.m_prev < self.eps_move else 0.0
            self.since_edge = 0.0
        elif falling:
            self.w0 = 1.0
            self.since_edge = 0.0
        else:
            self.since_edge += dt

        w = self.weight()
        m = w * a + (1.0 - w) * b
        self.m_prev = m
        return m
