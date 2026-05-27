/**
 * @file PinScanner.ino
 * @brief Find correct pin names for Pi header pins 15 and 12
 *
 * From the H7 core, D0-D14 map to known STM32 ports.
 * Pi pin 15 (SAI_D0, J2-53) and Pi pin 12 (I2S_CK, J1-56)
 * are NOT in D0-D14 — use STM32 port names directly.
 *
 * Probe Pi header pin 15 with multimeter.
 * When it toggles, note which name is printed.
 */

// Candidate STM32 port names for Pi pin 15 (SAI_D0, J2-53)
// and Pi pin 12 (I2S_CK, J1-56)
const PinName candidates[] = {
    // Likely candidates for J2-53 (SAI_D0 / Pi pin 15)
    PE_6, PB_9, PC_1, PD_6, PD_4, PF_6, PG_9,
    // Likely candidates for J1-56 (I2S_CK / Pi pin 12)
    PE_2, PB_3, PC_10, PI_5, PD_3,
    // Extra candidates
    PH_3, PH_4, PH_6, PJ_0, PJ_1, PJ_2, PJ_3,
    PK_0, PK_2, PK_3, PG_10, PG_11, PG_12,
};
const char* names[] = {
    "PE_6", "PB_9", "PC_1", "PD_6", "PD_4", "PF_6", "PG_9",
    "PE_2", "PB_3", "PC_10", "PI_5", "PD_3",
    "PH_3", "PH_4", "PH_6", "PJ_0", "PJ_1", "PJ_2", "PJ_3",
    "PK_0", "PK_2", "PK_3", "PG_10", "PG_11", "PG_12",
};
const int N = sizeof(candidates) / sizeof(candidates[0]);

void setup() {
    Serial.begin(115200);
    delay(2000);
    Serial.println(F("Pin Scanner — probe Pi header pin 15 (or 12)"));
    Serial.println(F("Note which name causes the pin to toggle"));
    Serial.println();
}

int idx = 0;

void loop() {
    if (idx >= N) {
        idx = 0;
        Serial.println(F("--- Scan complete, restarting ---"));
        delay(1000);
        return;
    }

    Serial.print(F("Toggling ")); Serial.print(names[idx]); Serial.println(F("..."));

    pinMode(candidates[idx], OUTPUT);
    for (int i = 0; i < 6; i++) {
        digitalWrite(candidates[idx], (i % 2 == 0) ? LOW : HIGH);
        delay(200);
    }
    digitalWrite(candidates[idx], HIGH);
    pinMode(candidates[idx], INPUT);

    idx++;
    delay(100);
}

