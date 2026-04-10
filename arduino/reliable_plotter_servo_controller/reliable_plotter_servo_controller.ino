#include <Servo.h>

namespace {

constexpr long SERIAL_BAUD = 9600;
constexpr uint8_t SERVO_COUNT = 4;

// Primary servo pins. Plotter 1 is on pin 9.
// Plotter 2 defaults to pin 8 in the current quad wiring.
constexpr int SERVO_PINS[SERVO_COUNT] = {9, 8, 7, 6};

// Optional mirror pins. This is useful if older hardware still has plotter 2
// wired to pin 10 instead of pin 8; the sketch will drive both.
constexpr int SERVO_MIRROR_PINS[SERVO_COUNT] = {-1, 10, -1, -1};

// Adjust these angles for the actual marker / eraser geometry on each plotter.
constexpr int SERVO_MARKER_ANGLES[SERVO_COUNT] = {15, 15, 15, 15};
constexpr int SERVO_ERASE_ANGLES[SERVO_COUNT] = {105, 105, 105, 105};

constexpr uint8_t SERVO_STEP_DEGREES = 2;
constexpr unsigned long SERVO_STEP_DELAY_MS = 18;
constexpr unsigned long SERVO_FINAL_SETTLE_MS = 300;

// Keep the servos attached so they hold position between draw / erase phases.
constexpr bool KEEP_SERVOS_ATTACHED = true;

enum ServoMode : uint8_t {
  MODE_MARKER = 0,
  MODE_ERASE = 1,
};

Servo servos[SERVO_COUNT];
Servo mirrorServos[SERVO_COUNT];

ServoMode servoModes[SERVO_COUNT] = {
  MODE_MARKER,
  MODE_MARKER,
  MODE_MARKER,
  MODE_MARKER,
};
int currentAngles[SERVO_COUNT] = {0, 0, 0, 0};
bool angleKnown[SERVO_COUNT] = {false, false, false, false};

String inputBuffer;

const char* servoName(uint8_t channel) {
  switch (channel) {
    case 0:
      return "A";
    case 1:
      return "B";
    case 2:
      return "C";
    case 3:
      return "D";
    default:
      return "?";
  }
}

const char* modeName(ServoMode mode) {
  return mode == MODE_ERASE ? "erase" : "marker";
}

String normalized(String value) {
  value.trim();
  value.toUpperCase();
  return value;
}

void attachChannel(uint8_t channel) {
  if (channel >= SERVO_COUNT) {
    return;
  }

  if (!servos[channel].attached()) {
    servos[channel].attach(SERVO_PINS[channel]);
  }

  const int mirrorPin = SERVO_MIRROR_PINS[channel];
  if (mirrorPin >= 0 && !mirrorServos[channel].attached()) {
    mirrorServos[channel].attach(mirrorPin);
  }
}

void detachChannel(uint8_t channel) {
  if (channel >= SERVO_COUNT || KEEP_SERVOS_ATTACHED) {
    return;
  }

  if (servos[channel].attached()) {
    servos[channel].detach();
  }
  if (mirrorServos[channel].attached()) {
    mirrorServos[channel].detach();
  }
}

void writeChannelAngle(uint8_t channel, int angle) {
  const int constrainedAngle = constrain(angle, 0, 180);
  servos[channel].write(constrainedAngle);
  if (mirrorServos[channel].attached()) {
    mirrorServos[channel].write(constrainedAngle);
  }
}

void moveChannelToAngle(uint8_t channel, int targetAngle) {
  if (channel >= SERVO_COUNT) {
    return;
  }

  attachChannel(channel);
  const int constrainedTarget = constrain(targetAngle, 0, 180);

  if (!angleKnown[channel]) {
    writeChannelAngle(channel, constrainedTarget);
    delay(SERVO_FINAL_SETTLE_MS);
    currentAngles[channel] = constrainedTarget;
    angleKnown[channel] = true;
    detachChannel(channel);
    return;
  }

  int current = currentAngles[channel];
  if (current == constrainedTarget) {
    writeChannelAngle(channel, constrainedTarget);
    delay(SERVO_FINAL_SETTLE_MS);
    detachChannel(channel);
    return;
  }

  const int step = constrainedTarget > current ? SERVO_STEP_DEGREES : -static_cast<int>(SERVO_STEP_DEGREES);
  while (current != constrainedTarget) {
    current += step;
    if (step > 0 && current > constrainedTarget) {
      current = constrainedTarget;
    }
    if (step < 0 && current < constrainedTarget) {
      current = constrainedTarget;
    }
    writeChannelAngle(channel, current);
    delay(SERVO_STEP_DELAY_MS);
  }

  writeChannelAngle(channel, constrainedTarget);
  delay(SERVO_FINAL_SETTLE_MS);
  currentAngles[channel] = constrainedTarget;
  detachChannel(channel);
}

void setChannelMode(uint8_t channel, ServoMode targetMode) {
  const int angle = targetMode == MODE_ERASE ? SERVO_ERASE_ANGLES[channel] : SERVO_MARKER_ANGLES[channel];
  moveChannelToAngle(channel, angle);
  servoModes[channel] = targetMode;
}

void toggleChannelMode(uint8_t channel) {
  setChannelMode(channel, servoModes[channel] == MODE_ERASE ? MODE_MARKER : MODE_ERASE);
}

bool isValidPlotterIndex(int plotterIndex) {
  return plotterIndex >= 1 && plotterIndex <= static_cast<int>(SERVO_COUNT);
}

void setPlotterMode(int plotterIndex, ServoMode targetMode) {
  if (!isValidPlotterIndex(plotterIndex)) {
    return;
  }
  setChannelMode(static_cast<uint8_t>(plotterIndex - 1), targetMode);
}

void togglePlotterMode(int plotterIndex) {
  if (!isValidPlotterIndex(plotterIndex)) {
    return;
  }
  toggleChannelMode(static_cast<uint8_t>(plotterIndex - 1));
}

void setAllModes(ServoMode targetMode) {
  for (uint8_t channel = 0; channel < SERVO_COUNT; ++channel) {
    setChannelMode(channel, targetMode);
  }
}

void toggleAllModes() {
  for (uint8_t channel = 0; channel < SERVO_COUNT; ++channel) {
    toggleChannelMode(channel);
  }
}

void printStatusLine(const char* prefix) {
  Serial.print(prefix);
  for (uint8_t channel = 0; channel < SERVO_COUNT; ++channel) {
    Serial.print(channel == 0 ? " " : " ");
    Serial.print(servoName(channel));
    Serial.print('=');
    Serial.print(modeName(servoModes[channel]));
  }
  Serial.println();
}

void printAck(const String& command) {
  Serial.print("OK ");
  Serial.print(command);
  for (uint8_t channel = 0; channel < SERVO_COUNT; ++channel) {
    Serial.print(' ');
    Serial.print(servoName(channel));
    Serial.print('=');
    Serial.print(modeName(servoModes[channel]));
  }
  Serial.println();
}

void printUsage() {
  Serial.println(
    "OK COMMANDS: "
    "P1M P1E P1T P2M P2E P2T P3M P3E P3T P4M P4E P4T "
    "AM AE AT BM BE BT CM CE CT DM DE DT "
    "ABM ABE ABT BOTHM BOTHE BOTHT ALLM ALLE ALLT R S STATUS"
  );
}

bool handlePlotterCommand(const String& command) {
  if (command.length() != 3 || command[0] != 'P' || !isDigit(command[1])) {
    return false;
  }

  const int plotterIndex = command[1] - '0';
  if (!isValidPlotterIndex(plotterIndex)) {
    return false;
  }

  if (command[2] == 'M') {
    setPlotterMode(plotterIndex, MODE_MARKER);
    printAck(command);
    return true;
  }
  if (command[2] == 'E') {
    setPlotterMode(plotterIndex, MODE_ERASE);
    printAck(command);
    return true;
  }
  if (command[2] == 'T') {
    togglePlotterMode(plotterIndex);
    printAck(command);
    return true;
  }

  return false;
}

bool handleSingleServoCommand(const String& command) {
  if (command.length() != 2) {
    return false;
  }

  const char servoId = command[0];
  const char action = command[1];
  int channel = -1;
  if (servoId == 'A') {
    channel = 0;
  } else if (servoId == 'B') {
    channel = 1;
  } else if (servoId == 'C') {
    channel = 2;
  } else if (servoId == 'D') {
    channel = 3;
  } else {
    return false;
  }

  if (action == 'M') {
    setChannelMode(static_cast<uint8_t>(channel), MODE_MARKER);
    printAck(command);
    return true;
  }
  if (action == 'E') {
    setChannelMode(static_cast<uint8_t>(channel), MODE_ERASE);
    printAck(command);
    return true;
  }
  if (action == 'T') {
    toggleChannelMode(static_cast<uint8_t>(channel));
    printAck(command);
    return true;
  }

  return false;
}

void handleCommand(const String& rawCommand) {
  String command = normalized(rawCommand);
  if (command.length() == 0) {
    return;
  }

  if (command == "STATUS" || command == "S") {
    printStatusLine("OK STATUS");
    return;
  }

  if (handlePlotterCommand(command) || handleSingleServoCommand(command)) {
    return;
  }

  if (command == "ABM" || command == "BOTHM" || command == "BOTH MARKER") {
    setPlotterMode(1, MODE_MARKER);
    setPlotterMode(2, MODE_MARKER);
    printAck(command);
    return;
  }
  if (command == "ABE" || command == "BOTHE" || command == "BOTH ERASE") {
    setPlotterMode(1, MODE_ERASE);
    setPlotterMode(2, MODE_ERASE);
    printAck(command);
    return;
  }
  if (command == "ABT" || command == "BOTHT") {
    togglePlotterMode(1);
    togglePlotterMode(2);
    printAck(command);
    return;
  }

  if (command == "ALLM" || command == "ALL MARKER") {
    setAllModes(MODE_MARKER);
    printAck(command);
    return;
  }
  if (command == "ALLE" || command == "ALL ERASE") {
    setAllModes(MODE_ERASE);
    printAck(command);
    return;
  }
  if (command == "ALLT" || command == "T" || command == "R") {
    toggleAllModes();
    printAck(command);
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
  if (
    command == "S" || command == "T" || command == "R" ||
    command == "AM" || command == "AE" || command == "AT" ||
    command == "BM" || command == "BE" || command == "BT" ||
    command == "CM" || command == "CE" || command == "CT" ||
    command == "DM" || command == "DE" || command == "DT" ||
    command == "P1M" || command == "P1E" || command == "P1T" ||
    command == "P2M" || command == "P2E" || command == "P2T" ||
    command == "P3M" || command == "P3E" || command == "P3T" ||
    command == "P4M" || command == "P4E" || command == "P4T" ||
    command == "ABM" || command == "ABE" || command == "ABT"
  ) {
    handleCommand(command);
    *buffer = "";
    return true;
  }

  return false;
}

void initializeChannelsToMarker() {
  for (uint8_t channel = 0; channel < SERVO_COUNT; ++channel) {
    setChannelMode(channel, MODE_MARKER);
  }
}

}  // namespace

void setup() {
  Serial.begin(SERIAL_BAUD);
  inputBuffer.reserve(32);

  initializeChannelsToMarker();

  Serial.println("READY reliable_plotter_servo_controller");
  printStatusLine("OK STATUS");
  printUsage();
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
