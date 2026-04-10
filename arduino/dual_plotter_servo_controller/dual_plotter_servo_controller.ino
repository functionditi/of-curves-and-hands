#include <Servo.h>

namespace {

constexpr long SERIAL_BAUD = 9600;

constexpr int SERVO_A_PIN = 9;
constexpr int SERVO_B_PIN = 10;

// Calibrate these four values for your hardware.
constexpr int SERVO_A_MARKER_ANGLE = 15;
constexpr int SERVO_A_ERASE_ANGLE = 105;
constexpr int SERVO_B_MARKER_ANGLE = 15;
constexpr int SERVO_B_ERASE_ANGLE = 105;

constexpr unsigned long SERVO_SETTLE_MS = 700;
constexpr bool DETACH_AFTER_MOVE = true;

Servo servoA;
Servo servoB;

enum ServoMode {
  MODE_MARKER,
  MODE_ERASE,
};

ServoMode servoAMode = MODE_MARKER;
ServoMode servoBMode = MODE_MARKER;

String inputBuffer;

const char* modeName(ServoMode mode) {
  return mode == MODE_ERASE ? "erase" : "marker";
}

void attachIfNeeded(Servo& servo, int pin) {
  if (!servo.attached()) {
    servo.attach(pin);
  }
}

void maybeDetach(Servo& servo) {
  if (DETACH_AFTER_MOVE && servo.attached()) {
    servo.detach();
  }
}

void moveServo(Servo& servo, int pin, int angle) {
  attachIfNeeded(servo, pin);
  servo.write(angle);
  delay(SERVO_SETTLE_MS);
  maybeDetach(servo);
}

void setServoA(ServoMode targetMode) {
  int angle = targetMode == MODE_ERASE ? SERVO_A_ERASE_ANGLE : SERVO_A_MARKER_ANGLE;
  moveServo(servoA, SERVO_A_PIN, angle);
  servoAMode = targetMode;
}

void setServoB(ServoMode targetMode) {
  int angle = targetMode == MODE_ERASE ? SERVO_B_ERASE_ANGLE : SERVO_B_MARKER_ANGLE;
  moveServo(servoB, SERVO_B_PIN, angle);
  servoBMode = targetMode;
}

String normalized(String value) {
  value.trim();
  value.toUpperCase();
  return value;
}

void printStatus() {
  Serial.print("OK STATUS A=");
  Serial.print(modeName(servoAMode));
  Serial.print(" B=");
  Serial.println(modeName(servoBMode));
}

void printUsage() {
  Serial.println("OK COMMANDS: AM AE AT BM BE BT BOTHM BOTHE BOTHT STATUS");
}

void handleCommand(const String& rawCommand) {
  String command = normalized(rawCommand);
  if (command.length() == 0) {
    return;
  }

  if (command == "STATUS" || command == "S") {
    printStatus();
    return;
  }

  if (command == "AM" || command == "A MARKER") {
    setServoA(MODE_MARKER);
    Serial.println("OK A marker");
    return;
  }
  if (command == "AE" || command == "A ERASE") {
    setServoA(MODE_ERASE);
    Serial.println("OK A erase");
    return;
  }
  if (command == "AT" || command == "A TOGGLE") {
    setServoA(servoAMode == MODE_ERASE ? MODE_MARKER : MODE_ERASE);
    Serial.print("OK A ");
    Serial.println(modeName(servoAMode));
    return;
  }

  if (command == "BM" || command == "B MARKER") {
    setServoB(MODE_MARKER);
    Serial.println("OK B marker");
    return;
  }
  if (command == "BE" || command == "B ERASE") {
    setServoB(MODE_ERASE);
    Serial.println("OK B erase");
    return;
  }
  if (command == "BT" || command == "B TOGGLE") {
    setServoB(servoBMode == MODE_ERASE ? MODE_MARKER : MODE_ERASE);
    Serial.print("OK B ");
    Serial.println(modeName(servoBMode));
    return;
  }

  if (command == "BOTHM" || command == "BOTH MARKER") {
    setServoA(MODE_MARKER);
    setServoB(MODE_MARKER);
    Serial.println("OK BOTH marker");
    return;
  }
  if (command == "BOTHE" || command == "BOTH ERASE") {
    setServoA(MODE_ERASE);
    setServoB(MODE_ERASE);
    Serial.println("OK BOTH erase");
    return;
  }
  if (command == "BOTHT" || command == "T") {
    setServoA(servoAMode == MODE_ERASE ? MODE_MARKER : MODE_ERASE);
    setServoB(servoBMode == MODE_ERASE ? MODE_MARKER : MODE_ERASE);
    Serial.print("OK BOTH A=");
    Serial.print(modeName(servoAMode));
    Serial.print(" B=");
    Serial.println(modeName(servoBMode));
    return;
  }

  Serial.print("ERR UNKNOWN ");
  Serial.println(command);
  printUsage();
}

bool tryHandleCompactCommand(String* buffer) {
  if (buffer == nullptr || buffer->length() == 0) {
    return false;
  }

  String command = normalized(*buffer);
  if (
    command == "T" || command == "S" ||
    command == "AM" || command == "AE" || command == "AT" ||
    command == "BM" || command == "BE" || command == "BT"
  ) {
    handleCommand(command);
    *buffer = "";
    return true;
  }

  return false;
}

}  // namespace

void setup() {
  Serial.begin(SERIAL_BAUD);
  inputBuffer.reserve(32);

  setServoA(MODE_MARKER);
  setServoB(MODE_MARKER);

  Serial.println("READY dual_plotter_servo_controller");
  printStatus();
  printUsage();
}

void loop() {
  while (Serial.available() > 0) {
    char incoming = static_cast<char>(Serial.read());
    if (incoming == '\r') {
      continue;
    }

    if (incoming == '\n') {
      handleCommand(inputBuffer);
      inputBuffer = "";
      continue;
    }

    inputBuffer += incoming;
    tryHandleCompactCommand(&inputBuffer);
  }
}
