/**
 * @file SPI_Loopback_H7.ino
 * @brief SPI loopback test for Portenta H7 + Hat Carrier
 *
 * WIRING: Physically connect Pi header pin 19 (MOSI) to pin 21 (MISO).
 * That's a single jumper wire on the HAT's Pi header.
 * No ADS1263 needed — just tests if H7 SPI is working at all.
 *
 * Expected result: every byte sent should come back identical.
 * If loopback fails → SPI object is mapped to wrong pins.
 *
 * Serial Monitor: 115200 baud
 */

#include <SPI.h>

// CS just needs to be any output pin — not critical for loopback
#define CS_PIN PI_0

SPISettings spiSettings(500000, MSBFIRST, SPI_MODE1);

void setup() {
    Serial.begin(115200);
    delay(3000);

    pinMode(CS_PIN, OUTPUT);
    digitalWrite(CS_PIN, HIGH);

    SPI.begin();

    Serial.println(F("╔══════════════════════════════════════════╗"));
    Serial.println(F("║  Portenta H7 SPI Loopback Test           ║"));
    Serial.println(F("║  Wire Pi pin 19 (MOSI) → pin 21 (MISO)  ║"));
    Serial.println(F("╚══════════════════════════════════════════╝"));
    Serial.println();
    Serial.println(F("Sending 0xAA, 0x55, 0x12, 0x34, 0xFF, 0x00"));
    Serial.println();

    uint8_t testBytes[] = {0xAA, 0x55, 0x12, 0x34, 0xFF, 0x00};
    bool allPass = true;

    SPI.beginTransaction(spiSettings);
    digitalWrite(CS_PIN, LOW);
    delayMicroseconds(5);

    for (int i = 0; i < 6; i++) {
        uint8_t sent = testBytes[i];
        uint8_t recv = SPI.transfer(sent);
        bool pass = (recv == sent);
        if (!pass) allPass = false;

        Serial.print(F("  Sent: 0x"));
        if (sent < 0x10) Serial.print('0');
        Serial.print(sent, HEX);
        Serial.print(F("  Recv: 0x"));
        if (recv < 0x10) Serial.print('0');
        Serial.print(recv, HEX);
        Serial.println(pass ? F("  ✓") : F("  ✗ MISMATCH"));
    }

    delayMicroseconds(5);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();

    Serial.println();
    if (allPass) {
        Serial.println(F("PASS — SPI loopback working. Default SPI maps correctly."));
    } else {
        Serial.println(F("FAIL — SPI not working. Try wiring pin 19→21 more carefully,"));
        Serial.println(F("       or the default SPI object maps to different pins."));
        Serial.println();
        Serial.println(F("Next step: try SPI_MODE3 (type '3') or check pin assignments."));
    }
}

void loop() {
    if (!Serial.available()) return;
    char c = Serial.read();
    while (Serial.available()) Serial.read();

    if (c == '1' || c == '3') {
        uint8_t mode = (c == '1') ? SPI_MODE1 : SPI_MODE3;
        Serial.print(F("Retrying with SPI_MODE")); Serial.println(c);
        spiSettings = SPISettings(500000, MSBFIRST, mode);

        uint8_t testBytes[] = {0xAA, 0x55, 0x12, 0x34, 0xFF, 0x00};
        bool allPass = true;

        SPI.beginTransaction(spiSettings);
        digitalWrite(CS_PIN, LOW);
        delayMicroseconds(5);

        for (int i = 0; i < 6; i++) {
            uint8_t sent = testBytes[i];
            uint8_t recv = SPI.transfer(sent);
            bool pass = (recv == sent);
            if (!pass) allPass = false;
            Serial.print(F("  Sent: 0x"));
            if (sent < 0x10) Serial.print('0');
            Serial.print(sent, HEX);
            Serial.print(F("  Recv: 0x"));
            if (recv < 0x10) Serial.print('0');
            Serial.print(recv, HEX);
            Serial.println(pass ? F("  ✓") : F("  ✗"));
        }

        delayMicroseconds(5);
        digitalWrite(CS_PIN, HIGH);
        SPI.endTransaction();
        Serial.println(allPass ? F("PASS") : F("FAIL"));
    }
}
