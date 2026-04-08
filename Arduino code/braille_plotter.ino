

#include <AccelStepper.h>

// ── Configuration ─────────────────────────────────────────────────
#define USE_PAPER_SENSOR 0

// ── Pins ──────────────────────────────────────────────────────────
#define X_STEP_PIN       2
#define X_DIR_PIN        5
#define Y_STEP_PIN       3
#define Y_DIR_PIN        6
#define ENABLE_PIN       8
#define SOLENOID_PIN     14
#define X_ENDSTOP_PIN    9
#define PAPER_SENSOR_PIN 11

// ── Motion constants ( FOR 1/16) ───────────────────────────

const float X_STEPS_PER_MM  = 400.0;
const float Y_STEPS_PER_MM  = 48;
const float X_MAX_MM = 250.0;
const float X_MAX_SPEED      = 12000.0;
const float X_ACCELERATION   = 25000.0;
const float X_HOMING_SPEED   = 5000.0;

// Y axis
const unsigned int STEP_PULSE_US  = 2;
const unsigned int Y_STEP_DELAY   = 100;
const unsigned int HOME_DELAY_US  = 600;

// Solenoid
const float        TOP_MARGIN_MM  = 10.0;
const unsigned int SOLENOID_MS    = 60;
const long         BACKOFF_STEPS  = 50;

// ── Stepper ───────────────────────────────────────────────────────
AccelStepper stepperX(AccelStepper::DRIVER, X_STEP_PIN, X_DIR_PIN);

// ── State ─────────────────────────────────────────────────────────
long currentY = 0;

// ── Motor control ─────────────────────────────────────────────────
void enableMotors() {
  digitalWrite(ENABLE_PIN, LOW);
}

void disableMotors() {
  digitalWrite(ENABLE_PIN, HIGH);
}

// ── X axis ────────────────────────────────────────────────────────
void xMoveToMM(float xMM) {
  long targetSteps = lround(xMM * X_STEPS_PER_MM);
  stepperX.moveTo(targetSteps);
  stepperX.runToPosition();
}

bool homeX() {
  stepperX.setMaxSpeed(X_HOMING_SPEED);
  stepperX.setAcceleration(X_ACCELERATION);

  stepperX.move(-1000000L);
  unsigned long startMs = millis();

  while (digitalRead(X_ENDSTOP_PIN) == HIGH) {
    if (millis() - startMs > 30000UL) return false;
    stepperX.run();
  }

  stepperX.stop();
  stepperX.setCurrentPosition(0);

  stepperX.moveTo(BACKOFF_STEPS);
  stepperX.runToPosition();
  stepperX.setCurrentPosition(0);

  stepperX.setMaxSpeed(X_MAX_SPEED);
  stepperX.setAcceleration(X_ACCELERATION);

  return true;
}

// ── Y axis ────────────────────────────────────────────────────────
void yStep(bool dir) {
  digitalWrite(Y_DIR_PIN, dir ? HIGH : LOW);

  digitalWrite(Y_STEP_PIN, HIGH);
  delayMicroseconds(STEP_PULSE_US);
  digitalWrite(Y_STEP_PIN, LOW);

  delayMicroseconds(Y_STEP_DELAY);
}

void feedPaper(float mm) {
  bool dir = (mm >= 0);
  long steps = lround(abs(mm) * Y_STEPS_PER_MM);

  for (long i = 0; i < steps; i++) {
    yStep(dir);
  }

  currentY += lround(mm * Y_STEPS_PER_MM);
}

bool homePaper() {
#if USE_PAPER_SENSOR
  unsigned long startMs = millis();

  while (digitalRead(PAPER_SENSOR_PIN) == HIGH) {
    if (millis() - startMs > 30000UL) return false;

    digitalWrite(Y_DIR_PIN, HIGH);
    digitalWrite(Y_STEP_PIN, HIGH);
    delayMicroseconds(STEP_PULSE_US);
    digitalWrite(Y_STEP_PIN, LOW);
    delayMicroseconds(HOME_DELAY_US);
  }

  feedPaper(TOP_MARGIN_MM);
  currentY = 0;
  return true;
#else
  return true;
#endif
}

// ── Solenoid ──────────────────────────────────────────────────────
void fireSolenoid() {
  digitalWrite(SOLENOID_PIN, HIGH);
  delay(SOLENOID_MS);
  digitalWrite(SOLENOID_PIN, LOW);
  delay(20);
}

// ── Move ──────────────────────────────────────────────────────────
void moveTo(float xMM, float yMM) {
  if (xMM > X_MAX_MM || xMM < 0.0) {
    Serial.println("ERR X");
    return;
  }

  long targetY = lround(yMM * Y_STEPS_PER_MM);
  long dY = targetY - currentY;

  if (abs(dY) > 0) {
    bool dir = (dY > 0);
    for (long i = 0; i < abs(dY); i++) {
      yStep(dir);
    }
    currentY = targetY;
  }

  xMoveToMM(xMM);
}

// ── Commands ──────────────────────────────────────────────────────
String inputBuffer = "";

void processCommand(String cmd) {
  cmd.trim();

  if (cmd == "HOME") {
    enableMotors();
    if (!homeX()) { Serial.println("ERR X"); return; }
    if (!homePaper()) { Serial.println("ERR Y"); return; }
    Serial.println("OK");

  } else if (cmd == "PAPERREADY") {
    enableMotors();
    feedPaper(TOP_MARGIN_MM);
    currentY = 0;
    Serial.println("OK");

  } else if (cmd.startsWith("FEED ")) {
    float mm = cmd.substring(5).toFloat();
    enableMotors();
    feedPaper(mm);
    Serial.println("OK");

  } else if (cmd.startsWith("MOVE ")) {
    int sp = cmd.indexOf(' ', 5);
    if (sp < 0) { Serial.println("ERR"); return; }

    float tx = cmd.substring(5, sp).toFloat();
    float ty = cmd.substring(sp + 1).toFloat();

    enableMotors();
    moveTo(tx, ty);
    Serial.println("OK");

  } else if (cmd == "DOT") {
    fireSolenoid();
    Serial.println("OK");

  } else if (cmd.startsWith("DWELL ")) {
    delay(cmd.substring(6).toInt());
    Serial.println("OK");

  } else if (cmd == "DONE") {
    disableMotors();
    Serial.println("OK");

  } else {
    Serial.println("ERR");
  }
}

// ── Setup ─────────────────────────────────────────────────────────
void setup() {
  pinMode(X_STEP_PIN, OUTPUT);
  pinMode(X_DIR_PIN, OUTPUT);
  pinMode(Y_STEP_PIN, OUTPUT);
  pinMode(Y_DIR_PIN, OUTPUT);
  pinMode(ENABLE_PIN, OUTPUT);
  pinMode(SOLENOID_PIN, OUTPUT);
  pinMode(X_ENDSTOP_PIN, INPUT_PULLUP);
  pinMode(PAPER_SENSOR_PIN, INPUT_PULLUP);

  disableMotors();

  stepperX.setMaxSpeed(X_MAX_SPEED);
  stepperX.setAcceleration(X_ACCELERATION);

  Serial.begin(115200);
  Serial.println("OK READY");
}

// ── Loop ──────────────────────────────────────────────────────────
void loop() {
  while (Serial.available()) {
    char c = Serial.read();

    if (c == '\n') {
      processCommand(inputBuffer);
      inputBuffer = "";
    } else if (c != '\r') {
      inputBuffer += c;
    }
  }
}
