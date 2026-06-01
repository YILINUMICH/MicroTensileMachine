/**
 * @file main.cpp  (Portenta H7 — SMA_Driver_PIO, M7-only)
 *
 * Phase 6 SMA drive path bring-up. Port of the Arduino Uno
 * MCP4728 + TPS7A5701 LDO controller to the Portenta H7 / Mid Carrier.
 *
 * Architectural changes from the Uno version:
 *
 *   1. NO calibration table.  The Uno did a 33-pt sweep into EEPROM
 *      because its 10-bit ADC + small flash made open-loop precision
 *      undesirable to recompute. On the H7 we keep things open-loop —
 *      voltage → DAC code uses the analytical transfer function. The
 *      `sweep` command captures the measured curve so the operator
 *      can verify accuracy and linearity directly; if the curve is
 *      acceptable the open-loop formula is the production path. If
 *      not, a single linear refit (a + b·code) can be added later
 *      without re-introducing the full lookup table.
 *
 *   2. NO EEPROM persistence. H7 has no Uno-style EEPROM. If a runtime
 *      adjustment is needed (e.g. VDD recalibrated), apply it via the
 *      `vdd <V>` command — it's session-scoped only.
 *
 *   3. External power supply for MCP4728 + LDO. H7 only sources control
 *      signals (I2C, MOSFET gate, AIN feedback). The MCP4728's VDD is
 *      whatever the bench supply is set to (assumed 5.0 V here; change
 *      via `vdd` if different). Logic-level shifting between H7 3.3 V
 *      I2C and MCP4728 5 V I2C is a hardware concern — see README.md.
 *
 *   4. 16-bit on-chip ADC. The Portenta H7's STM32H747's ADC1/2/3 give
 *      up to 16-bit resolution; analogReadResolution(16) is used so
 *      feedback reads use the full chip capability. Reference voltage
 *      is the H7's 3.3 V analog Vref.
 *
 *   5. 2:1 feedback divider. H7 ADC max input is 3.3 V; LDO output
 *      ranges 0–5 V. Resistor divider (R_FB_TOP = R_FB_BOT = 10k → 0.5)
 *      scales 0–5 V LDO → 0–2.5 V into ADC. Scale factor 2.0 reverses
 *      it in software.
 *
 *
 * Pin map (Mid Carrier J15 / Arduino mbed core names):
 *
 *   I2C SDA   → Wire SDA  (Portenta H7 PB_7) → Mid Carrier J15-28 / silkscreen "I2C0 SDA"
 *   I2C SCL   → Wire SCL  (Portenta H7 PB_6) → Mid Carrier J15-26 / silkscreen "I2C0 SCL"
 *   MOSFET    → PA_9      → Mid Carrier J15-31 / silkscreen "PWM 3"
 *   FB (AIN)  → A0        → Mid Carrier "ANA0" pad
 *
 * (Pins chosen so this module's wiring does not overlap with
 *  SensorHub_PIO M4's PA_8 / PC_6 / PC_7. The two firmwares can
 *  share the same Portenta — only one M7 image runs at a time.)
 *
 *
 * Commands (115200 baud, line-terminated):
 *
 *   set <V>          Open-loop set LDO output to V volts
 *   <number>         Same as `set <number>` (bare number shortcut)
 *   code <N>         Set raw DAC code 0–4095 (debug)
 *   read             Read LDO output now (averaged)
 *   drive <V> <ms>   Apply <V> for <ms> milliseconds then return to 0
 *                    (SMA actuation primitive). Logs t, V_set, V_meas
 *                    every 10 ms during the hold.
 *   mosfet on|off    Load-enable MOSFET control
 *   sweep [mV]       Open-loop DAC sweep, prints code/V_DAC_nominal/V_LDO_meas
 *   csv   [mV]       Same as sweep, CSV format (parse-friendly)
 *   vdd <V>          Set assumed MCP4728 VDD (affects code↔V math)
 *   info             Print current state
 *
 * NOTE: this firmware does not push samples into the SRAM ring buffer
 * (it has no M4). Once the SMA logic merges into SensorHub_PIO M7,
 * `drive`-time feedback samples will be pushed with src=3 per the
 * sample_ring.h reservation table.
 */

#include <Arduino.h>

// ══════════════════════════════════════════════════════════════════════
//  M4 IDLE STUB
//
//  Compiled by the [env:portenta_m4_idle] PIO env. Flashing this to
//  the M4 partition wipes whatever was there (e.g. leftover
//  SensorHub_PIO M4) and replaces it with a do-nothing image: empty
//  setup(), __WFI() loop. M4 boots, sleeps, never touches SPI / I2C
//  / RPC. Lets the M7 run alone without M4 fighting for resources.
// ══════════════════════════════════════════════════════════════════════
#if defined(CORE_CM4)

void setup() {
    // Intentionally empty. No RPC.begin() — we deliberately do NOT
    // initialise the OpenAMP IPC channel so any stale M7 code calling
    // RPC.begin() later can't desync with us.
}

void loop() {
    // Wait-for-interrupt → core sleeps until an IRQ fires. No IRQs
    // are configured, so M4 effectively halts. SysTick may wake it
    // briefly but no handler runs.
    __WFI();
}

#elif defined(CORE_CM7)

#include <Wire.h>
#include <Adafruit_MCP4728.h>

Adafruit_MCP4728 mcp;

// ── Pins ──────────────────────────────────────────────────────────────
// See top-of-file pin map for the carrier silkscreen names.
const int MOSFET_PIN = PA_9;
const int FB_PIN     = A0;

// ── Circuit parameters (must match the bench wiring) ─────────────────
// VDD_MCP is the external bench supply rail driving the MCP4728 and the
// LDO. Default 5.0 V; adjust at runtime with `vdd <V>` if your bench
// supply differs (the open-loop V→code math uses this).
static float VDD_MCP        = 5.0f;
const float  R_TOP_DAC      = 2000.0f;
const float  R_BOT_DAC      = 10000.0f;
const float  DAC_DIV_RATIO  = R_BOT_DAC / (R_TOP_DAC + R_BOT_DAC);   // 0.8333
// TPS7A5701 in ANY-OUT mode has unity gain from SET pin to OUT pin, so
// V_LDO ≈ V_mid = V_DAC × DAC_DIV_RATIO. This is the open-loop transfer.

// Feedback path: LDO out → R_FB_TOP → A0 node → R_FB_BOT → GND.
// 10k/10k → ratio 0.5 → 0..5 V LDO becomes 0..2.5 V at ADC (well under
// the H7's 3.3 V max). FB_SCALE inverts the divider in software.
const float  R_FB_TOP       = 10000.0f;
const float  R_FB_BOT       = 10000.0f;
const float  FB_DIV_RATIO   = R_FB_BOT / (R_FB_TOP + R_FB_BOT);      // 0.5
const float  ADC_FB_SCALE   = 1.0f / FB_DIV_RATIO;                   // 2.0

// ── ADC (H7 on-chip, 16-bit) ──────────────────────────────────────────
static const int   ADC_RES_BITS = 16;
static const int   ADC_RES_MAX  = (1 << ADC_RES_BITS) - 1;       // 65535
static const float ADC_VREF     = 3.3f;                          // H7 Vref+
static const int   ADC_SAMPLES  = 64;
static const int   SETTLE_MS    = 50;        // post-DAC-write settle
                                             //   (TPS7A5701 + RC dominates;
                                             //    150 ms on Uno was MCP4728
                                             //    I2C ack overhead too.)

// ── Drive parameters ──────────────────────────────────────────────────
// drive_v / drive_ms bounds — sanity guards for the `drive` command.
static const float   DRIVE_V_MAX     = 5.0f;     // bench-supply ceiling
static const uint32_t DRIVE_MS_MAX   = 60000;    // 60 s — SMA self-heat
                                                 //   risk above this
static const uint32_t DRIVE_LOG_MS   = 10;       // feedback sample period
                                                 //   during the hold

// ── DAC state ─────────────────────────────────────────────────────────
static uint16_t currentCode = 0;

// ══════════════════════════════════════════════════════════════════════
//  Helpers
// ══════════════════════════════════════════════════════════════════════

// Raw averaged read at the A0 pin (volts at the ADC input).
static float readADC(int pin) {
    analogRead(pin);                 // throw-away (prime the input stage)
    delay(1);
    uint32_t sum = 0;
    for (int i = 0; i < ADC_SAMPLES; i++) sum += (uint32_t)analogRead(pin);
    float code = (float)sum / (float)ADC_SAMPLES;
    return (code / (float)ADC_RES_MAX) * ADC_VREF;
}

// Read the LDO output voltage (un-divided).
static float readLDO() {
    return readADC(FB_PIN) * ADC_FB_SCALE;
}

// Issue an MCP4728 channel-A write. VDD ref + 1x gain explicit on every
// write — see the Uno code's rationale.
static void setDAC(uint16_t code) {
    if (code > 4095) code = 4095;
    currentCode = code;
    mcp.setChannelValue(MCP4728_CHANNEL_A, code,
                        MCP4728_VREF_VDD, MCP4728_GAIN_1X);
    delay(SETTLE_MS);
}

// Open-loop forward model:  V_LDO ≈ (code / 4095) × VDD_MCP × DAC_DIV_RATIO.
static inline float codeToVldo(uint16_t code) {
    return ((float)code / 4095.0f) * VDD_MCP * DAC_DIV_RATIO;
}

// Open-loop inverse model: V_target → DAC code.
// Clamped to [0, 4095]. No cal table — accuracy depends on VDD_MCP being
// set correctly. `sweep` empirically validates this.
static uint16_t vldoToCode(float vtarget) {
    if (vtarget <= 0.0f) return 0;
    float vldo_max = codeToVldo(4095);
    if (vtarget > vldo_max) vtarget = vldo_max;
    float code = (vtarget / (VDD_MCP * DAC_DIV_RATIO)) * 4095.0f;
    if (code < 0)    code = 0;
    if (code > 4095) code = 4095;
    return (uint16_t)(code + 0.5f);
}

// ══════════════════════════════════════════════════════════════════════
//  Commands
// ══════════════════════════════════════════════════════════════════════

static void cmdSetVoltage(float vtarget) {
    if (vtarget < 0 || vtarget > DRIVE_V_MAX) {
        Serial.print(F("ERR: V out of range [0, "));
        Serial.print(DRIVE_V_MAX, 2); Serial.println(F("]"));
        return;
    }
    uint16_t code = vldoToCode(vtarget);
    setDAC(code);
    float vmeas = readLDO();
    float err   = vmeas - vtarget;
    Serial.print(F("Target=")); Serial.print(vtarget, 3);
    Serial.print(F("V  Code=")); Serial.print(code);
    Serial.print(F("  V_LDO=")); Serial.print(vmeas, 3);
    Serial.print(F("V  err="));
    if (err >= 0) Serial.print('+');
    Serial.print(err * 1000.0f, 1); Serial.println(F("mV"));
}

static void cmdCode(uint16_t code) {
    setDAC(code);
    float vldo = readLDO();
    Serial.print(F("Code=")); Serial.print(code);
    Serial.print(F("  V_LDO_nominal=")); Serial.print(codeToVldo(code), 3);
    Serial.print(F("  V_LDO_meas="));    Serial.print(vldo, 3);
    Serial.println('V');
}

static void cmdRead() {
    float v_adc = readADC(FB_PIN);
    float v_ldo = v_adc * ADC_FB_SCALE;
    Serial.print(F("A0=")); Serial.print(v_adc, 4);
    Serial.print(F("V  V_LDO=")); Serial.print(v_ldo, 4);
    Serial.print(F("V  code=")); Serial.println(currentCode);
}

static void cmdMosfet(bool on) {
    digitalWrite(MOSFET_PIN, on ? HIGH : LOW);
    Serial.print(F("MOSFET=")); Serial.println(on ? F("HIGH (load on)") : F("LOW (load off)"));
}

// SMA actuation primitive:
//   1. ensure MOSFET on
//   2. set DAC for V_target
//   3. log feedback every DRIVE_LOG_MS for the hold
//   4. set DAC to 0 (drive removed; SMA cools)
//
// Format (TSV, parseable):
//   [DRIVE] start V=2.500 t_ms=4000
//   <t_rel_ms>\t<V_set>\t<V_meas>
//   ...
//   [DRIVE] done V_final=0.012 max_err=+3.2mV elapsed_ms=4007
static void cmdDrive(float vtarget, uint32_t hold_ms) {
    if (vtarget < 0 || vtarget > DRIVE_V_MAX) {
        Serial.print(F("ERR: V out of range [0, "));
        Serial.print(DRIVE_V_MAX, 2); Serial.println(F("]"));
        return;
    }
    if (hold_ms == 0 || hold_ms > DRIVE_MS_MAX) {
        Serial.print(F("ERR: hold_ms out of range (1, "));
        Serial.print(DRIVE_MS_MAX); Serial.println(F("]"));
        return;
    }

    digitalWrite(MOSFET_PIN, HIGH);
    delay(2);

    uint16_t code = vldoToCode(vtarget);

    Serial.print(F("[DRIVE] start V=")); Serial.print(vtarget, 3);
    Serial.print(F(" t_ms="));            Serial.println(hold_ms);
    Serial.println(F("t_rel_ms\tV_set\tV_meas"));

    uint32_t t_start = millis();
    setDAC(code);
    float v_at_t0 = readLDO();
    Serial.print(0);            Serial.print('\t');
    Serial.print(vtarget, 4);   Serial.print('\t');
    Serial.println(v_at_t0, 4);

    float max_err = fabs(v_at_t0 - vtarget);
    uint32_t next_sample_ms = DRIVE_LOG_MS;
    while (true) {
        uint32_t t_now = millis();
        uint32_t t_rel = t_now - t_start;
        if (t_rel >= hold_ms) break;
        if (t_rel >= next_sample_ms) {
            float vm = readLDO();
            float e  = fabs(vm - vtarget);
            if (e > max_err) max_err = e;
            Serial.print(t_rel);   Serial.print('\t');
            Serial.print(vtarget, 4); Serial.print('\t');
            Serial.println(vm, 4);
            next_sample_ms += DRIVE_LOG_MS;
        }
        // Tight wait — no delay() so the sampling cadence stays close
        // to DRIVE_LOG_MS even with the ADC averaging cost (~64 reads).
    }

    setDAC(0);
    uint32_t t_done = millis();
    float v_final = readLDO();

    Serial.print(F("[DRIVE] done V_final=")); Serial.print(v_final, 4);
    Serial.print(F(" max_err="));
    Serial.print(max_err * 1000.0f, 1);       Serial.print(F("mV"));
    Serial.print(F(" elapsed_ms="));          Serial.println(t_done - t_start);
}

// Open-loop sweep: walks the DAC across [0.5 V, 5.0 V] of the LDO
// output (nominal) in stepMV steps. Prints each step's measured
// feedback so the operator can plot V_LDO_meas vs code and decide
// whether the open-loop formula is accurate enough.
static void cmdSweep(float stepMV, bool csv) {
    const float V_MIN = 0.5f;
    const float V_MAX = 5.0f;
    if (stepMV < 10.0f)   stepMV = 10.0f;
    if (stepMV > 2000.0f) stepMV = 2000.0f;
    float stepV = stepMV / 1000.0f;
    int n = (int)((V_MAX - V_MIN) / stepV) + 1;

    if (csv) {
        Serial.println(F("dac_code,v_ldo_nominal,v_ldo_meas"));
    } else {
        Serial.println(F("\nCode  V_nom   V_meas"));
        Serial.println(F("----  ------  ------"));
    }

    for (int i = 0; i < n; i++) {
        float vt = V_MIN + i * stepV;
        if (vt > V_MAX) vt = V_MAX;
        uint16_t code = vldoToCode(vt);
        setDAC(code);
        float vmeas = readLDO();
        float vnom  = codeToVldo(code);

        if (csv) {
            Serial.print(code);     Serial.print(',');
            Serial.print(vnom, 4);  Serial.print(',');
            Serial.println(vmeas, 4);
        } else {
            if (code < 1000) Serial.print(' ');
            if (code < 100)  Serial.print(' ');
            if (code < 10)   Serial.print(' ');
            Serial.print(code);     Serial.print(F("  "));
            Serial.print(vnom, 3);  Serial.print(F("   "));
            Serial.println(vmeas, 3);
        }
    }
    setDAC(0);
    if (!csv) Serial.println();
}

static void cmdInfo() {
    Serial.println(F("\n== SMA_Driver_PIO state =="));
    Serial.print(F("VDD_MCP (assumed) : ")); Serial.print(VDD_MCP, 3);     Serial.println(F(" V"));
    Serial.print(F("DAC_DIV_RATIO     : ")); Serial.println(DAC_DIV_RATIO, 4);
    Serial.print(F("V_LDO_max nominal : ")); Serial.print(codeToVldo(4095), 3); Serial.println(F(" V"));
    Serial.print(F("FB_DIV_RATIO      : ")); Serial.println(FB_DIV_RATIO, 4);
    Serial.print(F("ADC res / Vref    : ")); Serial.print(ADC_RES_BITS);   Serial.print(F("-bit / "));
                                              Serial.print(ADC_VREF, 2);     Serial.println(F(" V"));
    Serial.print(F("DAC code          : ")); Serial.print(currentCode);
                                              Serial.print(F("  (nominal V_LDO="));
                                              Serial.print(codeToVldo(currentCode), 3);
                                              Serial.println(F(" V)"));
    Serial.print(F("V_LDO measured    : ")); Serial.print(readLDO(), 3);    Serial.println(F(" V"));
    Serial.print(F("MOSFET            : ")); Serial.println(digitalRead(MOSFET_PIN) ? F("HIGH (load on)") : F("LOW (load off)"));
}

// ══════════════════════════════════════════════════════════════════════
//  Setup
// ══════════════════════════════════════════════════════════════════════
void setup() {
    Serial.begin(115200);
    uint32_t t0 = millis();
    while (!Serial && (millis() - t0) < 2000) {}

    // MOSFET safe state — load OFF until the operator explicitly enables.
    pinMode(MOSFET_PIN, OUTPUT);
    digitalWrite(MOSFET_PIN, LOW);

    // 16-bit ADC; the analogReference call is a no-op on the H7 (Vref
    // comes from the chip's internal Vref+ pin, ~3.3 V on Mid Carrier)
    // but kept for portability/intent. Prime the input with throwaways.
    analogReadResolution(ADC_RES_BITS);
    for (int i = 0; i < 10; i++) analogRead(FB_PIN);

    // I2C bring-up + MCP4728 detect.
    Wire.begin();

    Serial.println();
    Serial.println(F("== SMA_Driver_PIO  (Portenta H7 M7, Phase 6 bring-up) =="));
    Serial.print  (F("Build: ")); Serial.print(__DATE__); Serial.print(' '); Serial.println(__TIME__);

    // Quick I2C bus scan — useful when the level-shifter wiring or
    // pullups are wrong (the chip won't even ack).
    Serial.println(F("\nI2C scan..."));
    byte cnt = 0;
    for (byte a = 1; a < 127; a++) {
        Wire.beginTransmission(a);
        if (Wire.endTransmission() == 0) {
            Serial.print(F("  0x"));
            if (a < 16) Serial.print('0');
            Serial.println(a, HEX);
            cnt++;
        }
    }
    Serial.print(cnt); Serial.println(F(" device(s)"));

    if (!mcp.begin(0x60)) {
        Serial.println(F("MCP4728 not found at 0x60. Check VDD + I2C level shifter + pullups."));
        while (1) { delay(1000); }
    }

    // Park DAC at 0 (safe). Explicit VDD ref + 1x gain — defense against
    // a chip whose EEPROM defaults have drifted.
    mcp.setChannelValue(MCP4728_CHANNEL_A, 0,
                        MCP4728_VREF_VDD, MCP4728_GAIN_1X);
    currentCode = 0;
    delay(SETTLE_MS);

    Serial.println();
    Serial.println(F("Commands:"));
    Serial.println(F("  <voltage>          Set LDO output (open-loop)"));
    Serial.println(F("  set <V>            Same as above"));
    Serial.println(F("  code <N>           Set raw DAC code 0..4095"));
    Serial.println(F("  read               Read LDO output (averaged)"));
    Serial.println(F("  drive <V> <ms>     Apply V for ms, then return to 0  (SMA actuation)"));
    Serial.println(F("  mosfet on|off      Load-enable MOSFET"));
    Serial.println(F("  sweep [mV]         Open-loop DAC sweep (TSV)"));
    Serial.println(F("  csv   [mV]         Same as sweep (CSV)"));
    Serial.println(F("  vdd <V>            Override assumed MCP4728 VDD"));
    Serial.println(F("  info               Print state"));
    Serial.println();
}

// ══════════════════════════════════════════════════════════════════════
//  Command loop
// ══════════════════════════════════════════════════════════════════════
void loop() {
    if (!Serial.available()) return;

    String in = Serial.readStringUntil('\n');
    in.trim();
    if (in.length() == 0) return;

    String low = in;
    low.toLowerCase();

    if (low == "info")  { cmdInfo();       return; }
    if (low == "read")  { cmdRead();       return; }
    if (low == "sweep") { cmdSweep(250.0f, false); return; }
    if (low == "csv")   { cmdSweep(250.0f, true);  return; }

    if (low.startsWith("mosfet ")) {
        String arg = low.substring(7); arg.trim();
        if      (arg == "on")  cmdMosfet(true);
        else if (arg == "off") cmdMosfet(false);
        else Serial.println(F("ERR: mosfet on|off"));
        return;
    }
    if (low.startsWith("code ")) {
        int c = in.substring(5).toInt();
        if (c < 0 || c > 4095) { Serial.println(F("Range: 0-4095")); return; }
        cmdCode((uint16_t)c);
        return;
    }
    if (low.startsWith("vdd ")) {
        float v = in.substring(4).toFloat();
        if (v < 2.7f || v > 5.5f) { Serial.println(F("Range: 2.7-5.5 V")); return; }
        VDD_MCP = v;
        Serial.print(F("VDD_MCP=")); Serial.print(VDD_MCP, 3); Serial.println(F(" V"));
        return;
    }
    if (low.startsWith("sweep ")) {
        float s = in.substring(6).toFloat();
        cmdSweep(s, false);
        return;
    }
    if (low.startsWith("csv ")) {
        float s = in.substring(4).toFloat();
        cmdSweep(s, true);
        return;
    }
    if (low.startsWith("drive ")) {
        // drive <V> <ms>
        String rest = in.substring(6); rest.trim();
        int sp = rest.indexOf(' ');
        if (sp <= 0) { Serial.println(F("Usage: drive <V> <ms>")); return; }
        float vt = rest.substring(0, sp).toFloat();
        long ms = rest.substring(sp + 1).toInt();
        if (ms <= 0) { Serial.println(F("Usage: drive <V> <ms>")); return; }
        cmdDrive(vt, (uint32_t)ms);
        return;
    }

    // `set <V>` or bare-number shortcut
    float target;
    if (low.startsWith("set ")) {
        target = in.substring(4).toFloat();
    } else {
        target = in.toFloat();
        if (target == 0.0f && in[0] != '0') {
            Serial.print(F("? ")); Serial.println(in);
            return;
        }
    }
    cmdSetVoltage(target);
}

#else
  #error "Unknown core — build with CORE_CM7 or CORE_CM4"
#endif
