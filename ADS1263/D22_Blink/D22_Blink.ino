/**
 * @file D22_Blink.ino
 * @brief Blink D22 to verify it drives Pi header pin 15
 * Probe Pi header pin 15 with multimeter — should toggle 0V/3.3V at 1Hz
 * Also probe ADS1263 CS pin directly — should see same toggling
 */
void setup() {
    Serial.begin(115200);
    delay(2000);
    pinMode(PE_6, OUTPUT);
    Serial.println("Blinking PE_6 (SAI D0, J2-53, Pi pin 15) at 1Hz");
    Serial.println("Probe Pi header pin 15 AND ADS1263 CS pin");
}

void loop() {
    digitalWrite(PE_6, LOW);
    Serial.println("PE_6 LOW  (CS asserted)");
    delay(500);
    digitalWrite(PE_6, HIGH);
    Serial.println("PE_6 HIGH (CS deasserted)");
    delay(500);
}
