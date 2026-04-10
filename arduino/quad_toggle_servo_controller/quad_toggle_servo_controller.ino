#include <Servo.h>

namespace {

constexpr long SERIAL_BAUD = 9600;
constexpr uint8_t PLOTTER_COUNT = 4;

// Change these pins only if your wiring differs.
constexpr int SERVO_PINS[PLOTTER_COUNT] = {8, 9, 10, 11};

// Change these if your mechanism needs different endpoints.
constexpr int MARKER_ANGLE = 0;
constexpr int ERASE_ANGLE = 180;
constexpr unsigned long SETTLE_MS = 700;

Servo servos[PLOTTER_COUNT];
bool atMarker[PLOTTER_COUNT] = {true, true, true, true};

String inputBuffer;

String normalized(String value) {
  value.trim();
  value.toLowerCase();
  return value;
}

bool isValidPlotter(uint8_t plotterIndex) {
  return plotterIndex >= 1 && plotterIndex <= PLOTTER_COUNT;
}

bool isToggleCommand(const String& command) {
  return command.length() == 2 && command[0] == 't' && isDigit(command[1]) &&
         isValidPlotter(static_cast<uint8_t>(command[1] - '0'));
}

void printUsage() {
  Serial.println("OK COMMANDS: t1 t2 t3 t4 s");
}

void moveServo(uint8_t plotterIndex, int angle) {
  const uint8_t arrayIndex = plotterIndex - 1;
  servos[arrayIndex].write(angle);
  delay(SETTLE_MS);
}

void printStatus() {
  Serial.print("OK STATUS");
  for (uint8_t plotterIndex = 1; plotterIndex <= PLOTTER_COUNT; ++plotterIndex) {
    Serial.print(" P");
    Serial.print(plotterIndex);
    Serial.print('=');
    Serial.print(atMarker[plotterIndex - 1] ? "marker" : "erase");
  }
  Serial.println();
}

void printToggleAck(uint8_t plotterIndex) {
  Serial.print("OK T");
  Serial.print(plotterIndex);
  Serial.print(" angle=");
  Serial.println(atMarker[plotterIndex - 1] ? MARKER_ANGLE : ERASE_ANGLE);
}

void togglePlotter(uint8_t plotterIndex) {
  if (!isValidPlotter(plotterIndex)) {
    return;
  }

  const uint8_t arrayIndex = plotterIndex - 1;
  atMarker[arrayIndex] = !atMarker[arrayIndex];
  moveServo(plotterIndex, atMarker[arrayIndex] ? MARKER_ANGLE : ERASE_ANGLE);
  printToggleAck(plotterIndex);
}

void handleCommand(const String& rawCommand) {
  const String command = normalized(rawCommand);
  if (command.length() == 0) {
    return;
  }

  if (command == "s" || command == "status") {
    printStatus();
    return;
  }

  if (isToggleCommand(command)) {
    const uint8_t plotterIndex = static_cast<uint8_t>(command[1] - '0');
    togglePlotter(plotterIndex);
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

  const String command = normalized(*buffer);
  if (command == "s" || isToggleCommand(command)) {
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

  for (uint8_t plotterIndex = 1; plotterIndex <= PLOTTER_COUNT; ++plotterIndex) {
    servos[plotterIndex - 1].attach(SERVO_PINS[plotterIndex - 1]);
    moveServo(plotterIndex, MARKER_ANGLE);
  }

  Serial.println("READY quad_toggle_servo_controller");
  Serial.println("Send t1, t2, t3, or t4 to toggle plotter 1, 2, 3, or 4.");
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
