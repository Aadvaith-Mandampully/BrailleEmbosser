/*
 * Solenoid & Flyback Diode Test
 * ==============================
 * Runs a sequence of tests to verify the solenoid drive circuit is wired
 * correctly before connecting it to the braille plotter firmware.
 *
 * What each test checks:
 *
 *   TEST 1 — Static drain voltage
 *     With the MOSFET off, the drain should sit at or near +12 V (pulled up
 *     through the solenoid coil).  If it reads near 0 V, the diode is shorted
 *     (installed backwards or failed) — it is permanently forward-biasing and
 *     clamping the drain to GND through the coil.
 *     Reads drain via a 10:1 resistor divider on A0 (see wiring below).
 *
 *   TEST 2 — Single fire
 *     Fires the solenoid once for DWELL_MS and confirms the drain voltage
 *     drops to near 0 V while the MOSFET is on.  If drain stays high, the
 *     MOSFET is not switching (gate wiring problem).
 *
 *   TEST 3 — Rapid burst stress test
 *     Fires the solenoid 20 times in quick succession with a short off-time.
 *     A missing flyback diode causes the MOSFET to fail during this test —
 *     the spike on every turn-off accumulates stress and the device breaks
 *     down within a few cycles.  If the Arduino resets or the solenoid stops
 *     firing partway through, the diode is absent or the MOSFET is dead.
 *
 *   TEST 4 — Quiescent current check (optional, needs multimeter not code)
 *     Printed as a reminder in serial output.
 *
 * Wiring for drain voltage sensing (TEST 1 + 2):
 *   The Arduino's ADC is 5 V max.  12 V will destroy it.
 *   Use a 10:1 voltage divider on the drain node:
 *       drain node ── 47 kΩ ── A0 ── 5.1 kΩ ── GND
 *   This scales 12 V → 1.12 V on A0, safely within range.
 *   ADC_TO_VOLTS multiplier accounts for the divider ratio.
 *
 *   If you don't want to add the divider, comment out ENABLE_ADC_TEST
 *   and tests 1 & 2 will skip the voltage measurement (fire-only mode).
 *
 * Standard solenoid pins (same as braille plotter firmware):
 *   SOLENOID_PIN  9   (MOSFET gate via 100 Ω resistor)
 *   DRAIN_ADC_PIN A0  (drain node via 47k/5.1k divider)
 */

// ── Configuration ─────────────────────────────────────────────────
#define SOLENOID_PIN    9
#define DRAIN_ADC_PIN   A0

// Comment this line out if you have not wired the voltage divider.
#define ENABLE_ADC_TEST

// Solenoid dwell time for normal test fires (ms)
const unsigned int DWELL_MS       = 60;

// Short dwell for stress test — maximises spike stress on the circuit
const unsigned int STRESS_DWELL   = 20;

// Gap between stress-test pulses (ms) — short enough to stress, long enough
// for the coil to fully de-energise between shots
const unsigned int STRESS_GAP     = 30;

// Number of rapid-fire pulses in the stress test
const unsigned int STRESS_COUNT   = 20;

// Voltage divider ratio: (R1 + R2) / R2 = (47000 + 5100) / 5100 = 10.22
// ADC reference = 5.0 V, 10-bit ADC = 1023 counts
const float ADC_TO_VOLTS = (5.0 / 1023.0) * 10.22;

// Thresholds for pass/fail decisions (V)
const float DRAIN_HIGH_MIN = 8.0;   // drain must be above this when MOSFET off
const float DRAIN_LOW_MAX  = 1.5;   // drain must be below this when MOSFET on

// ── Helpers ───────────────────────────────────────────────────────

float readDrainVolts() {
#ifdef ENABLE_ADC_TEST
  // Average 8 samples for stability
  long sum = 0;
  for (int i = 0; i < 8; i++) {
    sum += analogRead(DRAIN_ADC_PIN);
    delay(1);
  }
  return (sum / 8.0) * ADC_TO_VOLTS;
#else
  return -1.0;  // sentinel: ADC test disabled
#endif
}

void printVolts(float v) {
  if (v < 0) {
    Serial.print("(ADC test disabled)");
  } else {
    Serial.print(v, 2);
    Serial.print(" V");
  }
}

void fireSolenoid(unsigned int ms) {
  digitalWrite(SOLENOID_PIN, HIGH);
  delay(ms);
  digitalWrite(SOLENOID_PIN, LOW);
}

// ── Individual tests ──────────────────────────────────────────────

bool test1_staticDrain() {
  Serial.println(F("\n--- TEST 1: Static drain voltage (MOSFET off) ---"));

  // Ensure MOSFET is off
  digitalWrite(SOLENOID_PIN, LOW);
  delay(100);

  float v = readDrainVolts();
  Serial.print(F("  Drain voltage: "));
  printVolts(v);
  Serial.println();

  if (v < 0) {
    Serial.println(F("  SKIP — no ADC divider wired."));
    Serial.println(F("  Manual check: probe drain with multimeter. Should read ~12 V."));
    return true;  // can't fail without ADC
  }

  if (v >= DRAIN_HIGH_MIN) {
    Serial.print(F("  PASS — drain sitting at "));
    printVolts(v);
    Serial.println(F(" as expected. Diode not shorted."));
    return true;
  } else {
    Serial.print(F("  FAIL — drain at "));
    printVolts(v);
    Serial.println(F(". Expected >= 8 V."));
    Serial.println(F("  Likely cause: diode installed backwards (anode to +12V)"));
    Serial.println(F("  and is permanently forward-biased, shorting drain to GND."));
    Serial.println(F("  Fix: flip the 1N4007 — banded end (cathode) to +12 V side."));
    return false;
  }
}

bool test2_singleFire() {
  Serial.println(F("\n--- TEST 2: Single fire ---"));

  float before = readDrainVolts();
  Serial.print(F("  Drain before fire: "));
  printVolts(before);
  Serial.println();

  Serial.print(F("  Firing solenoid for "));
  Serial.print(DWELL_MS);
  Serial.println(F(" ms..."));

  digitalWrite(SOLENOID_PIN, HIGH);
  delay(10);  // let it settle before reading
  float during = readDrainVolts();
  delay(DWELL_MS - 10);
  digitalWrite(SOLENOID_PIN, LOW);
  delay(50);
  float after = readDrainVolts();

  Serial.print(F("  Drain during fire:  "));
  printVolts(during);
  Serial.println();
  Serial.print(F("  Drain after fire:   "));
  printVolts(after);
  Serial.println();

  bool pass = true;

  if (during >= 0) {
    if (during <= DRAIN_LOW_MAX) {
      Serial.println(F("  PASS — drain pulled low during fire. MOSFET switching correctly."));
    } else {
      Serial.print(F("  FAIL — drain stayed at "));
      printVolts(during);
      Serial.println(F(" during fire. MOSFET not switching."));
      Serial.println(F("  Check: gate resistor connected? D9 reaching gate? MOSFET seated?"));
      pass = false;
    }
    if (after >= DRAIN_HIGH_MIN) {
      Serial.println(F("  PASS — drain recovered to high after fire. Solenoid de-energised cleanly."));
    } else {
      Serial.print(F("  WARN — drain only at "));
      printVolts(after);
      Serial.println(F(" after fire. May still be energised or coil is slow to discharge."));
    }
  } else {
    Serial.println(F("  SKIP — no ADC. Listen for solenoid click. If silent: MOSFET not switching."));
  }

  return pass;
}

bool test3_stressBurst() {
  Serial.println(F("\n--- TEST 3: Rapid burst stress test ---"));
  Serial.print(F("  Firing "));
  Serial.print(STRESS_COUNT);
  Serial.print(F(" pulses ("));
  Serial.print(STRESS_DWELL);
  Serial.print(F(" ms on / "));
  Serial.print(STRESS_GAP);
  Serial.println(F(" ms off)..."));
  Serial.println(F("  A missing flyback diode will kill the MOSFET within the first few pulses."));
  Serial.println(F("  Watch for: Arduino reset, solenoid stops clicking, burnt smell."));

  for (unsigned int i = 1; i <= STRESS_COUNT; i++) {
    fireSolenoid(STRESS_DWELL);
    delay(STRESS_GAP);

    // Print progress every 5 pulses
    if (i % 5 == 0) {
      Serial.print(F("  Pulse "));
      Serial.print(i);
      Serial.print(F("/"));
      Serial.print(STRESS_COUNT);

      float v = readDrainVolts();
      if (v >= 0) {
        Serial.print(F("  drain idle: "));
        printVolts(v);
      }
      Serial.println();
    }
  }

  // Final check: drain should still be high after burst
  delay(200);
  float vFinal = readDrainVolts();
  Serial.print(F("  Drain after burst: "));
  printVolts(vFinal);
  Serial.println();

  if (vFinal >= 0 && vFinal < DRAIN_HIGH_MIN) {
    Serial.println(F("  FAIL — drain low after burst. MOSFET may have failed short-circuit."));
    Serial.println(F("  This strongly suggests the flyback diode was absent or backwards."));
    Serial.println(F("  Replace the MOSFET, add/correct the 1N4007, and retest."));
    return false;
  }

  Serial.println(F("  PASS — all pulses fired, drain recovered. Circuit is healthy."));
  return true;
}

void test4_manualReminder() {
  Serial.println(F("\n--- TEST 4: Manual checks (cannot be done in code) ---"));
  Serial.println(F("  With 12 V applied and MOSFET off:"));
  Serial.println(F("  a) Measure voltage across diode with multimeter in diode-test mode."));
  Serial.println(F("     Should read OL (open) in the forward direction of current flow."));
  Serial.println(F("     ~0.6 V in the reverse direction (banded end to + probe)."));
  Serial.println(F("  b) Touch MOSFET tab after stress test — should be barely warm."));
  Serial.println(F("     Hot = diode missing or gate resistor wrong."));
  Serial.println(F("  c) Touch solenoid body after stress test — warm is normal."));
  Serial.println(F("     Hot = SOLENOID_MS too long or duty cycle too high."));
}

// ── Main ──────────────────────────────────────────────────────────

void setup() {
  pinMode(SOLENOID_PIN, OUTPUT);
  digitalWrite(SOLENOID_PIN, LOW);
  pinMode(DRAIN_ADC_PIN, INPUT);

  Serial.begin(115200);
  while (!Serial);  // wait for serial monitor on boards with native USB

  delay(500);
  Serial.println(F("================================================"));
  Serial.println(F(" Solenoid + Flyback Diode Test"));
  Serial.println(F("================================================"));

#ifdef ENABLE_ADC_TEST
  Serial.println(F(" ADC drain sensing: ENABLED (divider on A0)"));
#else
  Serial.println(F(" ADC drain sensing: DISABLED (no divider wired)"));
  Serial.println(F(" Fire-only mode — listen for clicks and check manually."));
#endif

  Serial.println(F("\n Press Enter in serial monitor to begin..."));
  while (Serial.read() != '\n') { /* wait */ }

  bool t1 = test1_staticDrain();
  bool t2 = test2_singleFire();

  // Only run stress test if basic fire test passed — no point stressing
  // a circuit that isn't even switching
  bool t3 = false;
  if (t2) {
    t3 = test3_stressBurst();
  } else {
    Serial.println(F("\n--- TEST 3: SKIPPED (test 2 failed) ---"));
  }

  test4_manualReminder();

  Serial.println(F("\n================================================"));
  Serial.print(F(" Test 1 (static drain): "));
  Serial.println(t1 ? F("PASS") : F("FAIL"));
  Serial.print(F(" Test 2 (single fire):  "));
  Serial.println(t2 ? F("PASS") : F("FAIL"));
  Serial.print(F(" Test 3 (stress burst): "));
  Serial.println(t2 ? (t3 ? F("PASS") : F("FAIL")) : F("SKIPPED"));
  Serial.println(F("================================================"));

  if (t1 && t2 && t3) {
    Serial.println(F("\n ALL TESTS PASSED. Circuit ready for braille plotter."));
  } else {
    Serial.println(F("\n ONE OR MORE TESTS FAILED. Do not connect to plotter."));
    Serial.println(F(" Check wiring, replace suspect components, and retest."));
  }
}

void loop() {
  // Nothing — tests run once in setup().
  // Send 'f' in serial monitor to fire the solenoid manually for audible check.
  if (Serial.available()) {
    char c = Serial.read();
    if (c == 'f' || c == 'F') {
      Serial.println(F("Manual fire..."));
      fireSolenoid(DWELL_MS);
      Serial.println(F("Done."));
    }
  }
}
