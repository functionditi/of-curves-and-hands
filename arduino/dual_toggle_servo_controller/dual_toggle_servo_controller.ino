#include <Servo.h>

namespace {

constexpr long SERIAL_BAUD = 9600;

// Change these pins only if your wiring differs.
constexpr int SERVO_1_PIN = 9;
constexpr int SERVO_2_PIN = 10;

// Change these if your mechanism needs different endpoints.
constexpr int MARKER_ANGLE = 0;
constexpr int ERASE_ANGLE = 180;
constexpr unsigned long SETTLE_MS = 700;

Servo servo1;
Servo servo2;

bool servo1AtMarker = true;
bool servo2AtMarker = true;

String inputBuffer;

String normalized(String value) {
  value.trim();
  value.toLowerCase();
  return value;
}

void moveServo(Servo& servo, int angle) {
  servo.write(angle);
  delay(SETTLE_MS);
}

void printStatus() {
  Serial.print("OK STATUS P1=");
  Serial.print(servo1AtMarker ? "marker" : "erase");
  Serial.print(" P2=");
  Serial.println(servo2AtMarker ? "marker" : "erase");
}

void toggleServo1() {
  servo1AtMarker = !servo1AtMarker;
  moveServo(servo1, servo1AtMarker ? MARKER_ANGLE : ERASE_ANGLE);
  Serial.print("OK T1 angle=");
  Serial.println(servo1AtMarker ? MARKER_ANGLE : ERASE_ANGLE);
}

void toggleServo2() {
  servo2AtMarker = !servo2AtMarker;
  moveServo(servo2, servo2AtMarker ? MARKER_ANGLE : ERASE_ANGLE);
  Serial.print("OK T2 angle=");
  Serial.println(servo2AtMarker ? MARKER_ANGLE : ERASE_ANGLE);
}

void handleCommand(const String& rawCommand) {
  const String command = normalized(rawCommand);
  if (command.length() == 0) {
    return;
  }

  if (command == "t1") {
    toggleServo1();
    return;
  }

  if (command == "t2") {
    toggleServo2();
    return;
  }

  if (command == "s" || command == "status") {
    printStatus();
    return;
  }

  Serial.print("ERR UNKNOWN ");
  Serial.println(command);
  Serial.println("OK COMMANDS: t1 t2 s");
}

bool tryHandleCompactCommand(String* buffer) {
  if (buffer == nullptr || buffer->length() == 0) {
    return false;
  }

  const String command = normalized(*buffer);
  if (command == "t1" || command == "t2" || command == "s") {
    handleCommand(command);
    *buffer = "";
    return true;
  }

  return false;
}

}  // namespace

void setup() {
  Serial.begin(SERIAL_BAUD);
  inputBuffer.reserve(16);

  // It is fine if only one physical servo/plotter is connected; the unused pin
  // will simply have no load attached.
  servo1.attach(SERVO_1_PIN);
  servo2.attach(SERVO_2_PIN);

  moveServo(servo1, MARKER_ANGLE);
  moveServo(servo2, MARKER_ANGLE);

  Serial.println("READY dual_toggle_servo_controller");
  Serial.println("Send t1 to toggle plotter 1, t2 to toggle plotter 2.");
  printStatus();
}

void loop() {
  while (Serial.available() > 0) {
    const char incoming = static_cast<char>(Serial.read());
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
