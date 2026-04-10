#include <Servo.h>

namespace {

constexpr long SERIAL_BAUD = 9600;
constexpr int SERVO_PIN = 9;
constexpr int ANGLE_A = 0;
constexpr int ANGLE_B = 180;
constexpr unsigned long SETTLE_MS = 700;

Servo servo;
bool atAngleA = true;

void moveTo(int angle) {
  servo.write(angle);
  delay(SETTLE_MS);
}

void printState() {
  Serial.print("OK angle=");
  Serial.println(atAngleA ? ANGLE_A : ANGLE_B);
}

void toggleServo() {
  atAngleA = !atAngleA;
  moveTo(atAngleA ? ANGLE_A : ANGLE_B);
  printState();
}

}  // namespace

void setup() {
  Serial.begin(SERIAL_BAUD);
  servo.attach(SERVO_PIN);
  moveTo(ANGLE_A);

  Serial.println("READY simple_toggle_servo");
  Serial.println("Send 't' to toggle between 0 and 180.");
  printState();
}

void loop() {
  while (Serial.available() > 0) {
    const char incoming = static_cast<char>(Serial.read());
    if (incoming == 't' || incoming == 'T') {
      toggleServo();
    }
  }
}
