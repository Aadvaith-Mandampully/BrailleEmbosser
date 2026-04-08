# ⠃⠗⠁⠊⠇⠇⠑ DIY Braille Plotter

A fully open-source desktop braille embosser built from off-the-shelf hardware, an Arduino Mega, and a Python GUI application. Translates plain text into Grade 1 or Grade 2 UEB braille and drives a solenoid-tipped gantry to emboss raised dots onto paper.

---

## Contents

- [How it works](#how-it-works)
- [Hardware](#hardware)
- [Wiring](#wiring)
- [Repository files](#repository-files)
- [Software setup](#software-setup)
- [Firmware setup](#firmware-setup)
- [Calibration](#calibration)
- [Using the application](#using-the-application)
- [Braille geometry](#braille-geometry)
- [Serial protocol](#serial-protocol)
- [Troubleshooting](#troubleshooting)

---

## How it works

The machine has two motion axes and one embossing actuator:

- **X axis** — a NEMA17 stepper drives a T8 4-start lead screw (8 mm lead) that moves a gantry carriage across the paper width. The carriage holds the solenoid and stylus.
- **Y axis** — a second NEMA17 drives a TPU-coated rubber roller that advances the paper through the machine one dot-row at a time.
- **Solenoid** — a 12 V 42 N push-type solenoid fires the stylus downward onto a divot strip below the paper, embossing a single raised dot.

The Python application translates text to Unicode braille, computes the physical position of every raised dot in millimetres, sorts them into an efficient raster (boustrophedon snake-scan) order, and streams MOVE/DOT commands to the Arduino over USB serial. The Arduino moves both axes and fires the solenoid in response.

---

## Hardware
| Metric | M3 Hardware|
| Metric | M3 Heat set inserts, 4mm|
### Motion and structure

| Part | Specification |
|---|---|
| Frame | 3D printed PLA side plates, 4 mm wall |
| X stepper | NEMA17, 1.7 A, 200 steps/rev |
| Y stepper | NEMA17, 1.7 A, 200 steps/rev |
| X lead screw | T8 4-start, 8 mm lead, 300 mm travel |
| X linear rod | 8 mm steel, LM8UU bearing in carriage |
| Y roller | PLA core + TPU 95A skin, 20 mm OD, 300 mm long |
| Y roller bearings | 608ZZ (×2), press-fit into roller ends |
| Pinch roller | PLA body, O-ring contact, 608ZZ bearings, spring-loaded pivot arms |
| Divot strip | Aluminium or PETG, 54 divots (27 cells × 2 columns) |
| Divot size | Ø 2.1 mm × 0.7 mm deep, hemispheric |
| Platen foam | 3 mm EVA foam backing between paper and divot strip |

### Electronics

| Part | Specification |
|---|---|
| Microcontroller | Arduino Mega 2560 |
| CNC shield | CNC Shield v3 (fits Mega header rows) |
| Stepper drivers | DRV8825 × 2, 1/16 microstepping |
| Solenoid | DC 12 V, 42 N, 10 mm stroke (JF-1253B,)|
| MOSFET driver | IRLZ44N, 100 Ω gate resistor, 1N4007 flyback diode |
| Power supply | 12 V 5 A DC |
| Endstop | Mechanical microswitch, normally-open, pin D9 |
| Paper sensor | IR break-beam or lever microswitch, pin D11 |

---

## Wiring

### CNC Shield v3 pinout (on Arduino Mega)

```
X STEP  → D2       X DIR   → D5
Y STEP  → D3       Y DIR   → D6
ENABLE  → D8       (shared, active LOW)
X-MIN   → D9       (endstop, INPUT_PULLUP, NO switch to GND)
Z-MIN   → D11      (paper sensor, INPUT_PULLUP, LOW when paper present)
D14     → 100 Ω → IRLZ44N gate   (solenoid MOSFET)
```

### DRV8825 microstepping (1/16)

Set MS1, MS2, MS3 jumpers on the CNC shield:

```
MS1 = LOW
MS2 = LOW
MS3 = HIGH
→ 1/16 microstepping
```

### MOSFET solenoid driver circuit

```
D14 ──── 100 Ω ──── IRLZ44N gate
                     IRLZ44N source → GND
                     IRLZ44N drain  → solenoid −
solenoid + → 12 V rail

10 kΩ between gate and GND  (prevents firing on boot)
1N4007 cathode → 12 V, anode → drain  (flyback protection — MANDATORY)
100 µF across 12 V/GND rails near solenoid
```

### VREF setting (do before first power-on)

With USB only (no 12 V), set VREF on each DRV8825:

```
VREF = I_rated × 0.5

For a 1.7 A motor: VREF = 0.85 V
For a 1.5 A motor: VREF = 0.75 V
For a 1.0 A motor: VREF = 0.50 V
```

Probe the trimmer pot (brass centre) with the + probe, GND with the − probe.

---

## Repository files

```
braille_plotter.ino        Arduino firmware (AccelStepper, CNC Shield pinout)
diode_test.ino             Solenoid circuit verification sketch
braille_plotter.py         Core logic: braille translator + Arduino serial interface
braille_plotter_app.py     Desktop GUI application (tkinter)
README.md                  This file
```
---

## Software setup

### Requirements

- Python 3.10 or later
- `pyserial` — the only required package

```bash
pip install pyserial
```

No other dependencies.

### Running the GUI

```bash
python braille_plotter_app.py
```

### Running the CLI terminal

```bash
python braille_terminal.py --port /dev/ttyUSB0
# Windows:
python braille_terminal.py --port COM3
```

### Running from the command line (headless)

```bash
python braille_plotter.py --port /dev/ttyUSB0 --text "Hello World"
python braille_plotter.py --port COM3 --file myfile.txt --grade 2
python braille_plotter.py --port /dev/ttyUSB0 --text "Hi" --dry-run
```

---

## Firmware setup

### Arduino IDE

1. Install [Arduino IDE 2.x](https://www.arduino.cc/en/software)
2. Install the **AccelStepper** library:
   `Sketch → Include Library → Manage Libraries → search "AccelStepper" → Install`
3. Select board: `Tools → Board → Arduino Mega or Mega 2560`
4. Select port: `Tools → Port → (your COM or /dev/tty port)`
5. Open `braille_plotter.ino` and flash

### Configuration flags in firmware

```cpp
// At the top of braille_plotter.ino:
#define USE_PAPER_SENSOR 1   // 1 = IR sensor on D11 (recommended)
                             // 0 = manual paper load (send PAPERREADY command)
```

### Steps/mm (update after calibration)

```cpp
float X_STEPS_PER_MM = 400.0;   // T8 4-start, 1/16 microstep, 200-step motor
float Y_STEPS_PER_MM = 50.93;   // 20 mm roller, 1/16 microstep, 200-step motor
```

These starting values are correct for the hardware above. Adjust after running the calibration procedure.

---

## Calibration

Calibration must be done in this exact order — each stage depends on the previous one being correct.

### Stage 1 — VREF (before 12 V power)

USB only. Set VREF on both DRV8825 drivers as described in the Wiring section. Then connect the 12 V PSU.

### Stage 2 — Solenoid circuit test

Flash `diode_test.ino`. Open serial monitor at 115200 baud. The sketch runs three tests automatically:

- **Test 1** — drain voltage at rest (~12 V) — verifies flyback diode is not shorted
- **Test 2** — single fire — verifies MOSFET switches correctly
- **Test 3** — 20-pulse burst — verifies flyback diode is present under stress

All three must print `PASS`. Then connect the solenoid and flash `braille_plotter.ino`.

### Stage 3 — Homing

Open the app, connect, go to the **Calibrate** tab, click **HOME**. The X carriage should drive left, hit the endstop on D9, back off ~0.2 mm, and stop. The console shows `Homed ✓`.

If X times out: check the endstop is wired NO (normally open) to D9 and GND. Press the switch by hand — the LED on the CNC shield should light.

### Stage 4 — X steps/mm

In the Calibrate tab, click **HOME**, then click **X +100 mm**. Measure the actual travel with calipers.

```
new_X_STEPS_PER_MM = current × (100.0 / measured_mm)
```

Update `X_STEPS_PER_MM` in `braille_plotter.ino`, reflash, repeat until within 0.1 mm over 100 mm.

Alternatively, use the **Auto-calibration** section: home, jog the carriage to a measured reference point using the jog buttons, enter the distance in mm, click **CAPTURE X**. The app reads step count from the firmware and calculates the value for you.

### Stage 5 — Y steps/mm

Insert paper. Mark the leading edge. In the Calibrate tab click **Feed 100 mm**. Measure actual paper advance with a ruler.

```
new_Y_STEPS_PER_MM = current × (100.0 / measured_mm)
```

Update `Y_STEPS_PER_MM`, reflash, repeat until within 0.2 mm over 100 mm. Always calibrate with paper loaded and the pinch roller engaged.

### Stage 6 — Paper origin

Home the machine. Jog the carriage until the stylus tip is directly above the top-left corner of the paper. Use 0.1 mm jog steps for precision. Click **SET ORIGIN**. The displayed values update in amber — copy them into `braille_plotter.py`:

```python
PAPER_ORIGIN_X = 36.000   # your measured value
PAPER_ORIGIN_Y = 0.000    # your measured value
```

### Stage 7 — Dot geometry and solenoid

**Dot spacing** — click **Dot test** in the Calibrate tab. Two dots are fired 2.5 mm apart. Measure with calipers — target 2.5 mm ± 0.05 mm.

**Cell spacing** — click **Row test**. Five cells are printed. Measure left-dot to left-dot of adjacent cells — target 6.25 mm ± 0.05 mm.

**Dot height** — fire 3 dots in a column (use the Fire Dot button + Feed 2.5 mm between each). Flip the paper and measure dot height with a feeler gauge. Target: 0.48–0.7 mm. Adjust `SOLENOID_MS` in the firmware:
- Too shallow → increase by 5 ms
- Paper tearing → decrease by 5 ms
- Start at 20 ms — the 42 N solenoid is very strong

**Alignment check** — in the Page Preview tab, click **⊕ Alignment check**. Four dots are fired at the corners of the printable area. Measure each and compare to the expected coordinates shown in the dialog.

---

## Using the application

### Print tab

1. Paste or load a `.txt` file
2. Choose Grade 1 (uncontracted) or Grade 2 (contracted)
3. Enable **Mirror** for back-face embossing (default — paper is embossed from behind, then flipped to read)
4. Click **TRANSLATE** — the braille preview and dot count update
5. Use the page navigation buttons to step through pages
6. Click **▶ PRINT ALL** to print all pages, or **▶ THIS PAGE** for the current page
7. Set X and Y speed with the sliders — adjust until motors run reliably without skipping steps

### Page Preview tab

- Shows all raised dot positions at accurate physical scale (mm)
- Rulers along top and left edges in mm, matching machine coordinates exactly
- Hover anywhere to see the cursor position and nearest dot position to 0.01 mm
- Toggle **Grid**, **Margins**, **Coords** overlays independently
- Zoom from 50% to 200%
- Orange cursor tracks the head position during printing
- **⊕ Alignment check** — fires 4 corner dots for physical verification
- **Export coords…** — saves all dot positions for the current page as CSV

### Calibrate tab

- **HOME** — homes X endstop and paper sensor, then parks head at (0.5, 0.0)
- **MOTORS OFF** — releases stepper hold current
- **FIRE DOT** — single solenoid pulse at current position
- **SET ORIGIN** — records current position as paper corner
- Jog pad — directional movement in configurable step sizes (0.1 to 10 mm)
- Quick-jog buttons — move exactly one dot pitch, cell spacing, or row spacing
- Auto-calibration — jog to reference, enter distance, click CAPTURE to compute steps/mm

### Settings tab

- Left/top margins and page width (applied on next translation)
- Rows per page (default 24 for A4 at 10.5 mm row spacing)
- Solenoid dwell time in ms

---

## Braille geometry

This machine uses non-standard large-format dimensions for enhanced tactile clarity:

| Parameter | This machine | Standard UEB |
|---|---|---|
| Dot diameter | 2.0 mm | 1.44 mm |
| Dot pitch (H & V) | 2.5 mm | 2.34 mm |
| Cell spacing | 6.25 mm | 6.2 mm |
| Row spacing | 10.5 mm | 10.0 mm |
| Cells per line (A4) | 27 | 40 |
| Rows per page (A4) | 24 | 25 |

Output is not compatible with standard braille displays or embossers. It is intended for learners or users who benefit from larger dots.

---

## Serial protocol

All commands are sent as ASCII lines at 115200 baud. The firmware replies `OK` on success or `ERR <reason>` on failure.

| Command | Description |
|---|---|
| `HOME` | Home X to endstop, establish Y=0 via paper sensor (or return immediately if USE_PAPER_SENSOR=0) |
| `PAPERREADY` | Manual mode: declare current Y position as Y=0, advance TOP_MARGIN_MM |
| `MOVE x y` | Move to absolute position in mm (Y first, then X) |
| `FEED mm` | Advance paper by mm (relative, positive = forward) |
| `DOT` | Fire solenoid once for SOLENOID_MS milliseconds |
| `SETSPEED xs ys` | Set X max speed (steps/sec) and Y max speed (steps/sec) |
| `DONE` | Disable stepper motors (releases hold current) |
| `POS` | Reply with `POS <x_steps> <y_steps>` — used for auto-calibration |

---


## Troubleshooting

**X carriage doesn't home / times out**
Check the endstop is wired normally-open between D9 and GND. Press it by hand — the X-MIN LED on the CNC shield should light. Timeout is 60 seconds.

**Motor skips steps**
Lower the X or Y speed slider. Increase VREF slightly. Check motor cable connections. Reduce solenoid firing dwell time if the solenoid and motor fire simultaneously.

**First dot in each line is missing**
The app parks the head at (0.5, 0.0) after homing to prevent this. If it still occurs, check the firmware version — the first-dot fix requires the park move in `home()`.

**Dots too shallow**
Increase `SOLENOID_MS` in the firmware by 5 ms, reflash, retest. Check foam backer is even. Ensure paper weight is 120–160 gsm.

**Dots too deep / paper tearing**
Decrease `SOLENOID_MS`. Try heavier paper. Check the stylus tip diameter — it should be 1.5 mm with a rounded profile.

**Paper skews across the page**
Check the drive roller and pinch roller are parallel. Verify both ends of the roller shaft are seated in identical Y positions in the frame. Ensure paper enters square against the registration guide.

**Columns shift right across the page**
X steps/mm is slightly low — recalibrate. Alternatively X_MAX_SPEED is too high causing missed steps — lower it.

**Rows shift down the page**
Y steps/mm is slightly low — recalibrate. Check pinch roller pressure is consistent.

**App freezes during print**
All serial communication is on a background thread — the UI should never freeze. If it does, check for an `ERR` in the console log. The most common cause is a serial timeout from a missed `OK` reply.

**`ModuleNotFoundError: No module named 'braille_plotter'`**
Both `braille_plotter.py` and `braille_plotter_app.py` must be in the same folder.

---

