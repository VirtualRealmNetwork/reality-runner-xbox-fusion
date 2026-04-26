# Controller Fusion Prototype

This directory contains a Linux controller-fusion prototype that reads two physical game controllers and exposes one fused virtual controller.

Current default device mapping on this machine:

- Controller A: real Xbox controller
- Controller B: treadmill controller

Fusion rule:

- left-stick direction/angle comes from Controller A
- left-stick magnitude/intensity comes from Controller B
- every non-left-stick input is forwarded into the virtual controller
- force feedback from the virtual controller is forwarded to Controller A
- fusion starts enabled and can be toggled with `LT + RT + LS + RS` on Controller A

The implementation targets Linux input devices through `evdev` and creates the fused controller through `/dev/uinput`.

## Files

- [`controller_fusion.py`](controller_fusion.py): main runtime
- [`controller_debug.py`](controller_debug.py): enumerator and live math tester
- [`fusion_common.py`](fusion_common.py): shared device selection and fusion math
- [`run_fusion.sh`](run_fusion.sh): helper launcher that activates conda and starts the main runtime
- [`run_fusion_and_steam.sh`](run_fusion_and_steam.sh): helper that starts fusion first and launches Steam with SDL limited to the virtual controller
- [`environment.yml`](environment.yml): conda environment definition

## How It Works

The runtime opens two physical `/dev/input/event*` devices:

- A supplies the left-stick angle
- B supplies the left-stick magnitude

For each update:

1. Normalize A and B left-stick values into `[-1, 1]`
2. Apply independent radial deadzones
3. Compute angle from A
4. Compute magnitude from B
5. Reconstruct fused `(x, y)` for the output left stick
6. Forward all other buttons and axes into one virtual controller

Current fallback behavior:

- if B is centered, fused output is centered
- if A is centered but B is nonzero, fused output points forward

Runtime toggle:

- default startup state is fusion on
- press `LT + RT + LS + RS` on Controller A to switch between fusion and Controller A only
- when fusion is off, the virtual controller outputs Controller A's controls only
- each switch prints `Fusion mode: ON` or `Fusion mode: OFF` in the console
- `LS` and `RS` mean pressing the left and right stick buttons

The current binding is intentionally explicit, but it is not especially comfortable. A practical alternative for later would be `Back/View + Start/Menu + LS`, which is rarer during gameplay and easier to press without holding both triggers.

The virtual device is created with a distinct name:

- `Controller Fusion Prototype`

The helper scripts default the virtual controller to a custom VID/PID:

- vendor `0xF155`
- product `0x0001`

That makes it possible to tell Steam/SDL to expose only the fused controller and ignore the two physical ones.

## Environment Setup

The project uses a dedicated conda environment so it does not depend on the system Python.

Create the environment from the checked-in file:

```bash
source "$(conda info --base)/etc/profile.d/conda.sh"
conda env create -f environment.yml
conda activate controller-fusion
```

If the environment already exists:

```bash
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate controller-fusion
```

Installed Python packages:

- `evdev`
- `pyudev`

## Device Enumeration

List all joystick-capable devices:

```bash
./controller_debug.py
```

This prints, for each device:

- event path
- js path, if present
- name
- unique ID / Bluetooth MAC when available
- vendor/product IDs
- bus type
- physical vs virtual classification

List devices through the main tool:

```bash
./controller_fusion.py --print-devices
```

## Selecting Controller A And B

You can choose devices in several ways.

By unique ID / Bluetooth MAC:

```bash
./controller_fusion.py \
  --a-uniq AA:BB:CC:DD:EE:FF \
  --b-uniq 11:22:33:44:55:66
```

By event path:

```bash
./controller_fusion.py \
  --a-event /dev/input/event29 \
  --b-event /dev/input/event28
```

By name substring:

```bash
./controller_fusion.py \
  --a-name "Xbox Wireless Controller" \
  --b-name "bluez-hog-device"
```

Selection precedence:

1. `--*-event`
2. `--*-uniq`
3. `--*-name`
4. otherwise use the default Linux device-selection heuristics

## Running The Fusion Tool

Dry-run mode prints fused values without creating a virtual controller:

```bash
./controller_fusion.py --dry-run --debug
```

Run the full virtual controller without grabbing the source devices:

```bash
./controller_fusion.py
```

Run the full tool and exclusively grab both source devices:

```bash
./controller_fusion.py --grab both
```

Helper launcher using the default device-selection heuristics:

```bash
./run_fusion.sh --grab both
```

Helper launcher for the Steam workflow:

```bash
./run_fusion_and_steam.sh
```

This is the recommended way to use the prototype with Steam on this machine.

Useful options:

```bash
./controller_fusion.py --deadzone-a 0.18 --deadzone-b 0.18
./controller_fusion.py --debug
./controller_fusion.py --virtual-name "My Fusion Pad"
```

Grab modes:

- `--grab none`: leave source devices visible
- `--grab a`: grab only A
- `--grab b`: grab only B
- `--grab both`: grab both source devices

`--grab both` is the safest option when testing outside Steam, because it reduces duplicate input from the real controllers.

The helper forwards extra flags directly to the main runtime, so these work too:

```bash
./run_fusion.sh --dry-run --debug
./run_fusion.sh --grab both --deadzone-a 0.12 --deadzone-b 0.20
```

The Steam helper starts the fusion runtime with `--grab both`, waits briefly for the virtual controller to appear, then launches Steam with SDL restricted to the fused controller via `SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT`.

Why this helper exists:

- plain `--grab both` is not enough to hide the original Bluetooth controllers from Steam
- Steam can still enumerate the physical pads through other interfaces
- that can leave the fused controller in a later slot instead of the first one
- `./run_fusion_and_steam.sh` fixes that by making Steam see only the virtual controller

## Live Validation

The debug tool can show the raw source sticks and fused output continuously:

```bash
./controller_debug.py --live
```

This prints:

- raw normalized A left stick
- raw normalized B left stick
- fused output vector
- whether A is outside deadzone
- A magnitude after deadzone
- B magnitude after deadzone
- which angle source was used

## Testing With Steam Or A Game

Recommended test order:

1. Start the fusion tool first
2. For Steam, prefer `./run_fusion_and_steam.sh`
3. Confirm the virtual device appears
4. Then launch the target game

Suggested validation flow:

1. Run `./controller_debug.py --live`
2. Confirm the fusion math looks correct
3. Run `./run_fusion_and_steam.sh`
4. Use Steam input test to verify only the virtual pad is exposed and it is the first visible controller
5. Launch the target game and verify the fused left stick behaves as expected

## Duplicate Input Safety

Without grabbing, games may see:

- Controller A
- Controller B
- the virtual fused controller

That can cause duplicate or conflicting input.

The runtime supports optional exclusive grabs using Linux `EVIOCGRAB`.

Important behavior:

- grabs only last while the process is running
- grabs are released on clean exit
- grabs are also released if the process is terminated
- if Steam or a game already opened the real devices before the grab happened, it may still have seen them

For best results:

- start the fusion tool before the game
- outside Steam, use `--grab both`
- for Steam specifically, prefer [`run_fusion_and_steam.sh`](run_fusion_and_steam.sh)

## Forwarding Behavior

The tool fuses only the output left stick.

Everything else is forwarded from the two source devices into the virtual controller:

- buttons are treated as pressed if either controller is pressing them
- one-way trigger-style axes use the larger value
- centered axes and hats use the larger deflection

Reserved behavior:

- `ABS_X` and `ABS_Y` belong to the fused left stick and are not forwarded directly from either source

Force feedback behavior:

- the virtual controller accepts Linux force-feedback uploads
- uploaded effects and play commands are forwarded to Controller A
- this is intended for rumble support in Steam or games that address the fused virtual controller

## Limitations

- Linux-only prototype
- virtual output is a Linux `uinput` gamepad, not a native XInput device
- current fallback when A is centered and B is nonzero is fixed forward
- force feedback is forwarded only to Controller A
- if both controllers drive the same non-left-stick control at once, the merge rules above apply

## Troubleshooting

If the virtual controller does not appear:

```bash
ls -l /dev/uinput
lsmod | rg 'uinput|uhid'
```

If Python dependencies are missing, recreate the environment:

```bash
conda env remove -n controller-fusion
conda env create -f environment.yml
```

If you want to inspect currently visible physical controllers again:

```bash
./controller_debug.py
```
