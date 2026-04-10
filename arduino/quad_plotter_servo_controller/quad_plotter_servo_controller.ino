#include <Servo.h>

namespace {

constexpr long SERIAL_BAUD = 9600;

enum ServoId : uint8_t {
  SERVO_ID_A = 0,
  SERVO_ID_B = 1,
  SERVO_ID_C = 2,
  SERVO_ID_D = 3,
  SERVO_COUNT = 4,
};

enum ServoMode : uint8_t {
  MODE_MARKER = 0,
  MODE_ERASE = 1,
  MODE_UNKNOWN = 2,
};

// Plotter-mounted servos:
// - Plotter 1 -> Servo A
// - Plotter 2 -> Servo B
// Servos C and D are left available as auxiliary channels.
constexpr int SERVO_PINS[SERVO_COUNT] = {9, 8, 7, 6};

// Start with the older proven single/dual-servo angles.
// Fine-tune per channel if needed, but avoid 0/180 endpoint stalls.
constexpr int SERVO_MARKER_ANGLES[SERVO_COUNT] = {15, 15, 15, 15};
constexpr int SERVO_ERASE_ANGLES[SERVO_COUNT] = {105, 105, 105, 105};

constexpr unsigned long SERVO_SETTLE_MS = 700;

// Keep torque applied in erase mode so the tool does not relax back before the
// bridge starts the sweep. Marker mode can still release once it is in place.
constexpr bool DETACH_AFTER_MARKER_MOVE = true;
constexpr bool DETACH_AFTER_ERASE_MOVE = false;

Servo servos[SERVO_COUNT];
ServoMode servoModes[SERVO_COUNT] = {MODE_UNKNOWN, MODE_UNKNOWN, MODE_UNKNOWN, MODE_UNKNOWN};
String inputBuffer;

const char* servoName(ServoId servoId) {
  switch (servoId) {
    case SERVO_ID_A:
      return "A";
    case SERVO_ID_B:
      return "B";
    case SERVO_ID_C:
      return "C";
    case SERVO_ID_D:
      return "D";
    default:
      return "?";
  }
}

const char* modeName(ServoMode mode) {
  if (mode == MODE_ERASE) {
    return "erase";
  }
  if (mode == MODE_MARKER) {
    return "marker";
  }
  return "unknown";
}

void attachIfNeeded(ServoId servoId) {
  Servo& servo = servos[servoId];
  if (!servo.attached()) {
    servo.attach(SERVO_PINS[servoId]);
  }
}

void maybeDetach(ServoId servoId, bool shouldDetach) {
  if (shouldDetach && servos[servoId].attached()) {
    servos[servoId].detach();
  }
}

void moveServo(ServoId servoId, int angle, bool shouldDetach) {
  attachIfNeeded(servoId);
  servos[servoId].write(angle);
  delay(SERVO_SETTLE_MS);
  maybeDetach(servoId, shouldDetach);
}

void setServoMode(ServoId servoId, ServoMode targetMode) {
  int angle = targetMode == MODE_ERASE ? SERVO_ERASE_ANGLES[servoId] : SERVO_MARKER_ANGLES[servoId];
  bool shouldDetach = targetMode == MODE_ERASE ? DETACH_AFTER_ERASE_MOVE : DETACH_AFTER_MARKER_MOVE;
  moveServo(servoId, angle, shouldDetach);
  servoModes[servoId] = targetMode;
}

void toggleServo(ServoId servoId) {
  ServoMode targetMode = servoModes[servoId] == MODE_ERASE ? MODE_MARKER : MODE_ERASE;
  setServoMode(servoId, targetMode);
}

void setPlotterServo(uint8_t plotterIndex, ServoMode targetMode) {
  if (plotterIndex == 1) {
    setServoMode(SERVO_ID_A, targetMode);
    return;
  }
  if (plotterIndex == 2) {
    setServoMode(SERVO_ID_B, targetMode);
    return;
  }
  if (plotterIndex == 3) {
    setServoMode(SERVO_ID_C, targetMode);
    return;
  }
  if (plotterIndex == 4) {
    setServoMode(SERVO_ID_D, targetMode);
  }
}

void togglePlotterServo(uint8_t plotterIndex) {
  if (plotterIndex == 1) {
    toggleServo(SERVO_ID_A);
    return;
  }
  if (plotterIndex == 2) {
    toggleServo(SERVO_ID_B);
    return;
  }
  if (plotterIndex == 3) {
    toggleServo(SERVO_ID_C);
    return;
  }
  if (plotterIndex == 4) {
    toggleServo(SERVO_ID_D);
  }
}

void setAllServos(ServoMode targetMode) {
  for (uint8_t index = 0; index < SERVO_COUNT; ++index) {
    setServoMode(static_cast<ServoId>(index), targetMode);
  }
}

void toggleAllServos() {
  for (uint8_t index = 0; index < SERVO_COUNT; ++index) {
    toggleServo(static_cast<ServoId>(index));
  }
}

String normalized(String value) {
  value.trim();
  value.toUpperCase();
  return value;
}

void printStatus() {
  Serial.print("OK STATUS ");
  for (uint8_t index = 0; index < SERVO_COUNT; ++index) {
    if (index > 0) {
      Serial.print(' ');
    }
    Serial.print(servoName(static_cast<ServoId>(index)));
    Serial.print('=');
    Serial.print(modeName(servoModes[index]));
  }
  Serial.println();
}

void printUsage() {
  Serial.println(
    "OK COMMANDS: "
    "P1M P1E P1T P2M P2E P2T P3M P3E P3T P4M P4E P4T "
    "AM AE AT BM BE BT CM CE CT DM DE DT "
    "ABM ABE ABT ALLM ALLE ALLT R STATUS"
  );
}

void printServoReply(ServoId servoId) {
  Serial.print("OK ");
  Serial.print(servoName(servoId));
  Serial.print(' ');
  Serial.println(modeName(servoModes[servoId]));
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

  if (command == "P1M") {
    setPlotterServo(1, MODE_MARKER);
    Serial.println("OK P1 marker (servo A)");
    return;
  }
  if (command == "P1E") {
    setPlotterServo(1, MODE_ERASE);
    Serial.println("OK P1 erase (servo A)");
    return;
  }
  if (command == "P1T") {
    togglePlotterServo(1);
    Serial.print("OK P1 ");
    Serial.print(modeName(servoModes[SERVO_ID_A]));
    Serial.println(" (servo A)");
    return;
  }

  if (command == "P2M") {
    setPlotterServo(2, MODE_MARKER);
    Serial.println("OK P2 marker (servo B)");
    return;
  }
  if (command == "P2E") {
    setPlotterServo(2, MODE_ERASE);
    Serial.println("OK P2 erase (servo B)");
    return;
  }
  if (command == "P2T") {
    togglePlotterServo(2);
    Serial.print("OK P2 ");
    Serial.print(modeName(servoModes[SERVO_ID_B]));
    Serial.println(" (servo B)");
    return;
  }

  if (command == "P3M") {
    setPlotterServo(3, MODE_MARKER);
    Serial.println("OK P3 marker (servo C)");
    return;
  }
  if (command == "P3E") {
    setPlotterServo(3, MODE_ERASE);
    Serial.println("OK P3 erase (servo C)");
    return;
  }
  if (command == "P3T") {
    togglePlotterServo(3);
    Serial.print("OK P3 ");
    Serial.print(modeName(servoModes[SERVO_ID_C]));
    Serial.println(" (servo C)");
    return;
  }

  if (command == "P4M") {
    setPlotterServo(4, MODE_MARKER);
    Serial.println("OK P4 marker (servo D)");
    return;
  }
  if (command == "P4E") {
    setPlotterServo(4, MODE_ERASE);
    Serial.println("OK P4 erase (servo D)");
    return;
  }
  if (command == "P4T") {
    togglePlotterServo(4);
    Serial.print("OK P4 ");
    Serial.print(modeName(servoModes[SERVO_ID_D]));
    Serial.println(" (servo D)");
    return;
  }

  if (command == "ABM") {
    setServoMode(SERVO_ID_A, MODE_MARKER);
    setServoMode(SERVO_ID_B, MODE_MARKER);
    Serial.println("OK AB marker");
    return;
  }
  if (command == "ABE") {
    setServoMode(SERVO_ID_A, MODE_ERASE);
    setServoMode(SERVO_ID_B, MODE_ERASE);
    Serial.println("OK AB erase");
    return;
  }
  if (command == "ABT") {
    toggleServo(SERVO_ID_A);
    toggleServo(SERVO_ID_B);
    Serial.print("OK AB A=");
    Serial.print(modeName(servoModes[SERVO_ID_A]));
    Serial.print(" B=");
    Serial.println(modeName(servoModes[SERVO_ID_B]));
    return;
  }

  if (command == "AM" || command == "A MARKER") {
    setServoMode(SERVO_ID_A, MODE_MARKER);
    printServoReply(SERVO_ID_A);
    return;
  }
  if (command == "AE" || command == "A ERASE") {
    setServoMode(SERVO_ID_A, MODE_ERASE);
    printServoReply(SERVO_ID_A);
    return;
  }
  if (command == "AT" || command == "A TOGGLE") {
    toggleServo(SERVO_ID_A);
    printServoReply(SERVO_ID_A);
    return;
  }

  if (command == "BM" || command == "B MARKER") {
    setServoMode(SERVO_ID_B, MODE_MARKER);
    printServoReply(SERVO_ID_B);
    return;
  }
  if (command == "BE" || command == "B ERASE") {
    setServoMode(SERVO_ID_B, MODE_ERASE);
    printServoReply(SERVO_ID_B);
    return;
  }
  if (command == "BT" || command == "B TOGGLE") {
    toggleServo(SERVO_ID_B);
    printServoReply(SERVO_ID_B);
    return;
  }

  if (command == "CM" || command == "C MARKER") {
    setServoMode(SERVO_ID_C, MODE_MARKER);
    printServoReply(SERVO_ID_C);
    return;
  }
  if (command == "CE" || command == "C ERASE") {
    setServoMode(SERVO_ID_C, MODE_ERASE);
    printServoReply(SERVO_ID_C);
    return;
  }
  if (command == "CT" || command == "C TOGGLE") {
    toggleServo(SERVO_ID_C);
    printServoReply(SERVO_ID_C);
    return;
  }

  if (command == "DM" || command == "D MARKER") {
    setServoMode(SERVO_ID_D, MODE_MARKER);
    printServoReply(SERVO_ID_D);
    return;
  }
  if (command == "DE" || command == "D ERASE") {
    setServoMode(SERVO_ID_D, MODE_ERASE);
    printServoReply(SERVO_ID_D);
    return;
  }
  if (command == "DT" || command == "D TOGGLE") {
    toggleServo(SERVO_ID_D);
    printServoReply(SERVO_ID_D);
    return;
  }

  if (command == "ALLM" || command == "ALL MARKER") {
    setAllServos(MODE_MARKER);
    Serial.println("OK ALL marker");
    return;
  }
  if (command == "ALLE" || command == "ALL ERASE") {
    setAllServos(MODE_ERASE);
    Serial.println("OK ALL erase");
    return;
  }
  if (command == "ALLT" || command == "T" || command == "R") {
    toggleAllServos();
    Serial.println("OK ALL toggled");
    printStatus();
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
    command == "T" || command == "R" || command == "S" ||
    command == "P1M" || command == "P1E" || command == "P1T" ||
    command == "P2M" || command == "P2E" || command == "P2T" ||
    command == "P3M" || command == "P3E" || command == "P3T" ||
    command == "P4M" || command == "P4E" || command == "P4T" ||
    command == "ABM" || command == "ABE" || command == "ABT" ||
    command == "AM" || command == "AE" || command == "AT" ||
    command == "BM" || command == "BE" || command == "BT" ||
    command == "CM" || command == "CE" || command == "CT" ||
    command == "DM" || command == "DE" || command == "DT"
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

  Serial.println("READY quad_plotter_servo_controller");
  Serial.println("INFO Startup leaves servo positions unchanged until a mode command is received.");
  Serial.println("INFO Plotter 1 uses servo A. Plotter 2 uses servo B.");
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
