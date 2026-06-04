/**
 * @file main.cpp  (Portenta H7 - SMA_Driver_PIO, M7-only)
 *
 * Phase 6 SMA drive path bring-up. Port of the Arduino Uno
 * MCP4728 + TPS7A5701 LDO controller to the Portenta H7 / Mid Carrier.
 *
 * Transfer-function strategy:
 *
 *   This firmware uses a MEASURED calibration table (ported from the
 *   working Uno code), NOT an analytical open-loop formula. A `cal`
 *   sweep drives raw DAC codes 0..4095, measures the actual LDO output
 *   at CAL_N points, auto-detects the regulating window (trims the
 *   below-regulation and dropout-clamp flat ends), and stores the
 *   table in RAM. `set <V>` then converts a target voltage to a DAC
 *   code by binary-search + linear interpolation in the measured table.
 *
 *   This is the approach that lets the Uno reach the full output range:
 *   it never trusts an idealized transfer equation, it interpolates the
 *   real curve. It is also robust to the current hardware (DAC drives
 *   the LDO feedback node directly through a 6.2k resistor - the old
 *   2k/10k DAC divider has been removed), for which no simple
 *   analytical model applies.
 *
 *   The table lives in RAM only (the H7 has no Uno-style EEPROM). Cal
 *   runs automatically at boot (load is disconnected during cal) and
 *   can be re-run any time with `cal`.
 *
 *   External power: the MCP4728 VDD and the TPS7A5701 V_IN come from an
 *   external bench supply (target 5.5 V so the LDO has headroom to reach
 *   ~5 V out). The H7 only sources control signals.
 *
 *   16-bit on-chip ADC (analogReadResolution(16)) reads the LDO output
 *   through a 10k/10k feedback divider (0..5 V LDO -> 0..2.5 V at ADC,
 *   under the 3.3 V Vref); ADC_FB_SCALE = 2.0 reverses it.
 *
 *
 * Pin map (Mid Carrier J15 / Arduino mbed core names):
 *
 *   I2C SDA   -> Wire SDA  (Portenta H7 PB_7) -> Mid Carrier J15-28 / silkscreen "I2C0 SDA"
 *   I2C SCL   -> Wire SCL  (Portenta H7 PB_6) -> Mid Carrier J15-26 / silkscreen "I2C0 SCL"
 *   MOSFET    -> D3        -> Mid Carrier J15-31 / silkscreen "PWM 3"  (PWM3 = PG7, not PA_9)
 *   FB (AIN)  -> A0        -> Mid Carrier "ANA0" pad
 *
 * (Pins chosen so this module's wiring does not overlap with
 *  SensorHub_PIO M4's PA_8 / PC_6 / PC_7. The two firmwares can
 *  share the same Portenta - only one M7 image runs at a time.)
 *
 *
 * Commands (115200 baud, line-terminated):
 *
 *   set <V>          Set LDO output to V volts (cal-table lookup)
 *   <number>         Same as `set <number>` (bare number shortcut)
 *   code <N>         Set raw DAC code 0-4095 (debug, open-loop)
 *   read             Read LDO output now (averaged)
 *   drive <V> <ms>   Apply <V> for <ms> ms then return to 0 (SMA actuation).
 *                    Logs t, V_set, V_meas every 10 ms during the hold.
 *   mosfet on|off    Load-enable MOSFET control
 *   cal              Run the calibration sweep (load disconnected)
 *   sweep [step]     Raw-code diagnostic sweep, prints code / V_meas (TSV)
 *   csv   [step]     Same as sweep, CSV format (parse-friendly)
 *   vdd <V>          Set assumed MCP4728 VDD (display only: V_dac estimate)
 *   info             Print current state
 *
 * NOTE: this firmware does not push samples into the SRAM ring buffer
 * (it has no M4). Once the SMA logic merges into SensorHub_PIO M7,
 * `drive`-time feedback samples will be pushed with src=3 per the
 * sample_ring.h reservation table.
 */

#include <Arduino.h>

// ======================================================================
//  M4 IDLE STUB
//
//  Compiled by the [env:portenta_m4_idle] PIO env. Flashing this to
//  the M4 partition wipes whatever was there (e.g. leftover
//  SensorHub_PIO M4) and replaces it with a do-nothing image: empty
//  setup(), __WFI() loop. M4 boots, sleeps, never touches SPI / I2C
//  / RPC. Lets the M7 run alone without M4 fighting for resources.
// ======================================================================
#if defined(CORE_CM4)

void setup() {
    // Intentionally empty. No RPC.begin() - we deliberately do NOT
    // initialise the OpenAMP IPC channel so any stale M7 code calling
    // RPC.begin() later can't desync with us.
}

void loop() {
    // Wait-for-interrupt -> core sleeps until an IRQ fires. No IRQs
    // are configured, so M4 effectively halts.
    __WFI();
}

#elif defined(CORE_CM7)

#include <Wire.h>
#include <Adafruit_MCP4728.h>

Adafruit_MCP4728 mcp;

// -- Pins --------------------------------------------------------------
// See top-of-file pin map for the carrier silkscreen names.
const int MOSFET_PIN = D3;   // PWM3 = D3 = PG7 (Mid Carrier J15-31). Arduino alias, not raw PinName.
const int FB_PIN     = A0;

// -- Circuit parameters ------------------------------------------------
// VDD_MCP is the external bench supply rail driving the MCP4728. It is
// used ONLY to estimate V_dac for display (info / code commands). The
// actual code->V_LDO relationship comes from the measured cal table, so
// VDD_MCP no longer affects set-point accuracy. Target rail: 5.5 V.
static float VDD_MCP = 5.5f;

// Feedback path: LDO out -> R_FB_TOP -> A0 node -> R_FB_BOT -> GND.
// 10k/10k -> ratio 0.5 -> 0..5 V LDO becomes 0..2.5 V at ADC (under the
// H7's 3.3 V max). FB_SCALE inverts the divider in software.
const float  R_FB_TOP     = 10000.0f;
const float  R_FB_BOT     = 10000.0f;
const float  FB_DIV_RATIO = R_FB_BOT / (R_FB_TOP + R_FB_BOT);   // 0.5
const float  ADC_FB_SCALE = 1.0f / FB_DIV_RATIO;                // 2.0

// -- ADC (H7 on-chip, 16-bit) ------------------------------------------
static const int   ADC_RES_BITS = 16;
static const int   ADC_RES_MAX  = (1 << ADC_RES_BITS) - 1;     // 65535
static float       ADC_VREF_V   = 3.145f;  // H7 Vref+. 1-pt cal: A0 meter 2.89V vs fw 3.032V @code4095
                                          //   (3.3 was ~5% high). Tune live with `aref <V>`, then re-`cal`.
static const int   ADC_SAMPLES  = 64;
// Settle timing is now poll-based (see settleWait): the TPS7A5701 output is
// slow (~100 ms time constant measured), so a fixed delay either under-waits
// (big steps read mid-slew) or wastes time. settleWait polls until quiet.

// -- Drive parameters --------------------------------------------------
static const float    DRIVE_V_MAX  = 5.0f;     // bench-supply ceiling
static const uint32_t DRIVE_MS_MAX = 60000;    // 60 s - SMA self-heat risk above
static const uint32_t DRIVE_LOG_MS = 10;       // feedback sample period during hold

// -- DAC state ---------------------------------------------------------
static uint16_t currentCode = 0;

// -- Calibration table -------------------------------------------------
// Measured code->V_LDO curve. CAL_N points evenly spaced over 0..4095
// (step 128). detectRegulatingWindow() trims the flat ends so set-point
// interpolation only runs over the linear, regulating region.
static const uint8_t CAL_N = 33;
struct CalPoint {
    uint16_t code;
    float    vldo;
};
static CalPoint calTable[CAL_N];
static bool     calValid = false;
static uint8_t  calStart = 0;            // first idx of regulating window
static uint8_t  calEnd   = CAL_N - 1;    // last  idx of regulating window
static float    vldoMin  = 0.0f;
static float    vldoMax  = 0.0f;

// ======================================================================
//  ADC / DAC helpers
// ======================================================================

// Raw averaged read at the A0 pin (volts at the ADC input).
static float readADC(int pin) {
    analogRead(pin);                 // throw-away (prime the input stage)
    delay(1);
    uint32_t sum = 0;
    for (int i = 0; i < ADC_SAMPLES; i++) sum += (uint32_t)analogRead(pin);
    float code = (float)sum / (float)ADC_SAMPLES;
    return (code / (float)ADC_RES_MAX) * ADC_VREF_V;
}

// Read the LDO output voltage (un-divided).
static float readLDO() {
    return readADC(FB_PIN) * ADC_FB_SCALE;
}

// Issue an MCP4728 channel-A write. VDD ref + 1x gain explicit on every
// write - without it the chip can run on EEPROM defaults (internal
// 2.048 V ref + 2x gain) which clips the DAC at ~4.04 V regardless of VDD.
// Raw DAC write - updates the code and lets the I2C/DAC settle, but does NOT
// wait for the (slow ~100 ms) LDO output to track. Use for transient logging.
static void setDACraw(uint16_t code) {
    if (code > 4095) code = 4095;
    currentCode = code;
    mcp.setChannelValue(MCP4728_CHANNEL_A, code,
                        MCP4728_VREF_VDD, MCP4728_GAIN_1X);
    delay(2);
}

// Poll the LDO output until it is quiet (SETTLE_QUIET_N consecutive ~20 ms
// reads agree within SETTLE_TOL_V) or a hard timeout. The LDO settles with a
// ~100 ms time constant, so a full-scale step needs ~0.8 s, while a small
// step or an already-settled output returns much sooner.
static void settleWait() {
    const float    SETTLE_TOL_V      = 0.002f;   // 2 mV quiet band
    const int      SETTLE_QUIET_N    = 5;        // ~100 ms of quiet
    const uint32_t SETTLE_TIMEOUT_MS = 2000;     // hard cap
    float prev = readLDO();
    int quiet = 0;
    uint32_t t0 = millis();
    while (millis() - t0 < SETTLE_TIMEOUT_MS) {
        delay(18);
        float v = readLDO();
        if (fabs(v - prev) < SETTLE_TOL_V) { if (++quiet >= SETTLE_QUIET_N) break; }
        else quiet = 0;
        prev = v;
    }
}

// DAC write that returns only after the LDO output has settled, so a
// subsequent readLDO() reflects the final value. Used by set/code/sweep/cal.
static void setDAC(uint16_t code) {
    setDACraw(code);
    settleWait();
}

// Estimated DAC output voltage for a code (display only - assumes VDD ref,
// 1x gain). The real LDO output comes from the cal table, not this.
static inline float codeToVdac(uint16_t code) {
    return ((float)code / 4095.0f) * VDD_MCP;
}

// ======================================================================
//  Calibration
// ======================================================================

// Identify the regulating window of the cal table. Trims leading
// "below regulation" and trailing "dropout clamp" flat regions by
// walking inward until local slope >= 30% of the peak slope.
static void detectRegulatingWindow() {
    float maxSlope = 0.0f;
    for (uint8_t i = 0; i < CAL_N - 1; i++) {
        float dv = calTable[i + 1].vldo - calTable[i].vldo;
        float dc = (float)(calTable[i + 1].code - calTable[i].code);
        if (dc > 0) {
            float s = dv / dc;
            if (s > maxSlope) maxSlope = s;
        }
    }
    float threshold = 0.3f * maxSlope;

    calStart = 0;
    for (uint8_t i = 0; i < CAL_N - 1; i++) {
        float s = (calTable[i + 1].vldo - calTable[i].vldo) /
                  (float)(calTable[i + 1].code - calTable[i].code);
        if (s >= threshold) { calStart = i; break; }
    }
    calEnd = CAL_N - 1;
    for (int i = CAL_N - 2; i >= 0; i--) {
        float s = (calTable[i + 1].vldo - calTable[i].vldo) /
                  (float)(calTable[i + 1].code - calTable[i].code);
        if (s >= threshold) { calEnd = (uint8_t)(i + 1); break; }
    }

    if (calEnd <= calStart) {     // degenerate (flat curve) -> full range
        calStart = 0;
        calEnd   = CAL_N - 1;
    }

    vldoMin = calTable[calStart].vldo;
    vldoMax = calTable[calEnd].vldo;
}

// Run the calibration sweep. Load is disconnected (MOSFET LOW) for the
// whole sweep so the LDO is characterized unloaded; the MOSFET is LEFT
// LOW afterwards (safe default - the operator enables the load
// explicitly with `mosfet on` or via `drive`).
static void runCalibration() {
    Serial.println(F("\n== Calibration =="));
    Serial.println(F(">> Load OFF (MOSFET LOW) for sweep"));

    // Park DAC at 0 before anything, then ensure load off.
    mcp.setChannelValue(MCP4728_CHANNEL_A, 0, MCP4728_VREF_VDD, MCP4728_GAIN_1X);
    currentCode = 0;
    delay(50);
    digitalWrite(MOSFET_PIN, LOW);
    delay(200);

    Serial.println(F("idx  code   V_LDO"));
    Serial.println(F("---  ----   ------"));

    for (uint8_t i = 0; i < CAL_N; i++) {
        uint16_t code = (uint16_t)i * 128;      // 0,128,...,3968,(4096->clamp)
        if (code > 4095) code = 4095;
        setDAC(code);                  // raw write + poll-settle (LDO is slow)
        float v = readLDO();
        calTable[i].code = code;
        calTable[i].vldo = v;

        if (i < 10) Serial.print(' ');
        Serial.print(' '); Serial.print(i); Serial.print(F("   "));
        if (code < 1000) Serial.print(' ');
        if (code < 100)  Serial.print(' ');
        if (code < 10)   Serial.print(' ');
        Serial.print(code); Serial.print(F("   "));
        Serial.println(v, 4);
    }

    // Park DAC at 0 again; leave MOSFET LOW (safe default).
    mcp.setChannelValue(MCP4728_CHANNEL_A, 0, MCP4728_VREF_VDD, MCP4728_GAIN_1X);
    currentCode = 0;
    delay(50);
    Serial.println(F(">> Done. Load still OFF (MOSFET LOW)."));

    // Monotonicity check (5 mV slack for noise).
    bool mono = true;
    for (uint8_t i = 1; i < CAL_N; i++) {
        if (calTable[i].vldo < calTable[i - 1].vldo - 0.005f) {
            Serial.print(F("WARN: non-monotonic at i=")); Serial.print(i);
            Serial.print(F(" ("));   Serial.print(calTable[i - 1].vldo, 4);
            Serial.print(F(" -> ")); Serial.print(calTable[i].vldo, 4);
            Serial.println(')');
            mono = false;
        }
    }

    detectRegulatingWindow();

    float dv = calTable[calEnd].vldo - calTable[calStart].vldo;
    uint16_t dc = calTable[calEnd].code - calTable[calStart].code;
    float slope_mV = (dc > 0) ? (dv * 1000.0f / (float)dc) : 0.0f;

    Serial.println();
    Serial.print(F("Cal: ")); Serial.print(CAL_N); Serial.print(F(" pts in, "));
    Serial.print(calEnd - calStart + 1); Serial.print(F(" kept ["));
    Serial.print(calStart); Serial.print('-'); Serial.print(calEnd);
    Serial.print(F("], V_range = "));
    Serial.print(vldoMin, 3); Serial.print(F(" - "));
    Serial.print(vldoMax, 3); Serial.print(F(" V, slope = "));
    Serial.print(slope_mV, 3); Serial.print(F(" mV/code, "));
    Serial.println(mono ? F("monotonic OK") : F("WARN nonmono"));

    calValid = true;
}

// Voltage -> DAC code via binary search + linear interpolation inside the
// regulating window of the measured table.
static uint16_t voltageToCode(float vtarget) {
    uint8_t lo = calStart, hi = calEnd;
    while (hi - lo > 1) {
        uint8_t mid = (lo + hi) / 2;
        if (calTable[mid].vldo <= vtarget) lo = mid;
        else                               hi = mid;
    }
    float    v0 = calTable[lo].vldo, v1 = calTable[hi].vldo;
    uint16_t c0 = calTable[lo].code, c1 = calTable[hi].code;
    if (v1 <= v0) return c0;
    float frac = (vtarget - v0) / (v1 - v0);
    float code = (float)c0 + frac * (float)(c1 - c0);
    if (code < 0)    code = 0;
    if (code > 4095) code = 4095;
    return (uint16_t)(code + 0.5f);
}

// ======================================================================
//  Commands
// ======================================================================

static void cmdSetVoltage(float vtarget) {
    if (!calValid) {
        Serial.println(F("ERR: no cal. Run 'cal' first."));
        return;
    }
    if (vtarget < 0 || vtarget > DRIVE_V_MAX) {
        Serial.print(F("ERR: V out of range [0, "));
        Serial.print(DRIVE_V_MAX, 2); Serial.println(F("]"));
        return;
    }

    float vc = vtarget;
    bool clamped = false;
    if (vc < vldoMin) { vc = vldoMin; clamped = true; }
    if (vc > vldoMax) { vc = vldoMax; clamped = true; }
    if (clamped) {
        Serial.print(F("WARN: target ")); Serial.print(vtarget, 3);
        Serial.print(F("V out of cal range [")); Serial.print(vldoMin, 3);
        Serial.print(F(", ")); Serial.print(vldoMax, 3);
        Serial.print(F("] -> clamped to ")); Serial.print(vc, 3); Serial.println('V');
    }

    uint16_t code = voltageToCode(vc);
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
    Serial.print(F("  V_dac~")); Serial.print(codeToVdac(code), 3);
    Serial.print(F("  V_LDO_meas=")); Serial.print(vldo, 3);
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
//   2. set DAC (via cal table) for V_target
//   3. log feedback every DRIVE_LOG_MS for the hold
//   4. set DAC to 0 AND release MOSFET (drive removed; SMA cools)
static void cmdDrive(float vtarget, uint32_t hold_ms) {
    if (!calValid) {
        Serial.println(F("ERR: no cal. Run 'cal' first."));
        return;
    }
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

    float vc = vtarget;
    if (vc < vldoMin) vc = vldoMin;
    if (vc > vldoMax) vc = vldoMax;
    uint16_t code = voltageToCode(vc);

    digitalWrite(MOSFET_PIN, HIGH);
    delay(2);

    Serial.print(F("[DRIVE] start V=")); Serial.print(vtarget, 3);
    Serial.print(F(" t_ms="));            Serial.println(hold_ms);
    Serial.println(F("t_rel_ms\tV_set\tV_meas"));

    uint32_t t_start = millis();
    setDACraw(code);               // raw: log the LDO rise transient below
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
    }

    setDACraw(0);
    digitalWrite(MOSFET_PIN, LOW);   // release load-enable - return to safe state
    uint32_t t_done = millis();
    float v_final = readLDO();

    Serial.print(F("[DRIVE] done V_final=")); Serial.print(v_final, 4);
    Serial.print(F(" max_err="));
    Serial.print(max_err * 1000.0f, 1);       Serial.print(F("mV"));
    Serial.print(F(" elapsed_ms="));          Serial.println(t_done - t_start);
}

// Raw-code diagnostic sweep: walks DAC codes 0..4095 in `codeStep`
// increments and prints the measured LDO output. Unlike `cal`, this does
// NOT touch the MOSFET (sweep whatever load state you're in) and does not
// update the cal table - it's for inspecting the curve (e.g. under load).
static void cmdSweep(int codeStep, bool csv) {
    if (codeStep < 16)   codeStep = 16;
    if (codeStep > 2048) codeStep = 2048;

    if (csv) {
        Serial.println(F("dac_code,v_ldo_meas"));
    } else {
        Serial.println(F("\nCode  V_LDO_meas"));
        Serial.println(F("----  ----------"));
    }

    for (int c = 0; c <= 4095; c += codeStep) {
        uint16_t code = (uint16_t)(c > 4095 ? 4095 : c);
        setDAC(code);
        float vmeas = readLDO();
        if (csv) {
            Serial.print(code); Serial.print(',');
            Serial.println(vmeas, 4);
        } else {
            if (code < 1000) Serial.print(' ');
            if (code < 100)  Serial.print(' ');
            if (code < 10)   Serial.print(' ');
            Serial.print(code); Serial.print(F("  "));
            Serial.println(vmeas, 4);
        }
    }
    setDAC(0);
    if (!csv) Serial.println();
}

// Step-response logger: raw-write a DAC code and log V_meas every 10 ms for
// `ms` (default 1200). MOSFET untouched, cal table unchanged. Use to measure
// the LDO settle time directly.
static void cmdStep(uint16_t code, uint32_t ms) {
    Serial.print(F("[STEP] code=")); Serial.print(code);
    Serial.print(F(" ms="));          Serial.println(ms);
    Serial.println(F("t_rel_ms\tV_meas"));
    uint32_t t_start = millis();
    setDACraw(code);
    uint32_t next = 0;
    while (true) {
        uint32_t t_rel = millis() - t_start;
        if (t_rel >= ms) break;
        if (t_rel >= next) {
            Serial.print(t_rel); Serial.print('\t');
            Serial.println(readLDO(), 4);
            next += 10;
        }
    }
    Serial.print(F("[STEP] done V_final=")); Serial.println(readLDO(), 4);
}

static void cmdInfo() {
    Serial.println(F("\n== SMA_Driver_PIO state =="));
    if (calValid) {
        float dv = calTable[calEnd].vldo - calTable[calStart].vldo;
        uint16_t dc = calTable[calEnd].code - calTable[calStart].code;
        float slope_mV = (dc > 0) ? (dv * 1000.0f / (float)dc) : 0.0f;
        Serial.print(F("Cal               : VALID, "));
        Serial.print(calEnd - calStart + 1); Serial.print(F(" pts ["));
        Serial.print(calStart); Serial.print('-'); Serial.print(calEnd);
        Serial.print(F("], V_range = ")); Serial.print(vldoMin, 3);
        Serial.print(F(" - ")); Serial.print(vldoMax, 3);
        Serial.print(F(" V, slope ")); Serial.print(slope_mV, 3);
        Serial.println(F(" mV/code"));
    } else {
        Serial.println(F("Cal               : NOT VALID - run 'cal'"));
    }
    Serial.print(F("VDD_MCP (display) : ")); Serial.print(VDD_MCP, 3); Serial.println(F(" V"));
    Serial.print(F("ADC res / Vref    : ")); Serial.print(ADC_RES_BITS); Serial.print(F("-bit / "));
                                              Serial.print(ADC_VREF_V, 2); Serial.println(F(" V"));
    Serial.print(F("FB_DIV_RATIO      : ")); Serial.println(FB_DIV_RATIO, 4);
    Serial.print(F("DAC code          : ")); Serial.print(currentCode);
                                              Serial.print(F("  (V_dac~"));
                                              Serial.print(codeToVdac(currentCode), 3);
                                              Serial.println(F(" V)"));
    Serial.print(F("V_LDO measured    : ")); Serial.print(readLDO(), 3); Serial.println(F(" V"));
    Serial.print(F("MOSFET            : ")); Serial.println(digitalRead(MOSFET_PIN) ? F("HIGH (load on)") : F("LOW (load off)"));
}

// ======================================================================
//  Setup
// ======================================================================
void setup() {
    Serial.begin(115200);
    uint32_t t0 = millis();
    while (!Serial && (millis() - t0) < 2000) {}

    // MOSFET safe state - load OFF until the operator explicitly enables.
    pinMode(MOSFET_PIN, OUTPUT);
    digitalWrite(MOSFET_PIN, LOW);

    // 16-bit ADC; prime the input with throwaways.
    analogReadResolution(ADC_RES_BITS);
    for (int i = 0; i < 10; i++) analogRead(FB_PIN);

    // I2C bring-up + MCP4728 detect.
    Wire.begin();

    Serial.println();
    Serial.println(F("== SMA_Driver_PIO  (Portenta H7 M7, Phase 6 bring-up) =="));
    Serial.print  (F("Build: ")); Serial.print(__DATE__); Serial.print(' '); Serial.println(__TIME__);

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
        digitalWrite(MOSFET_PIN, LOW);   // keep load-enable released while halted
        while (1) { delay(1000); }
    }

    // Park DAC at 0 (safe). Explicit VDD ref + 1x gain.
    mcp.setChannelValue(MCP4728_CHANNEL_A, 0, MCP4728_VREF_VDD, MCP4728_GAIN_1X);
    currentCode = 0;
    delay(50);

    // Auto-calibrate at boot. Load is OFF (MOSFET LOW), so this only
    // exercises the LDO unloaded - no SMA actuation. RAM-only; re-runs
    // each boot (no EEPROM on the H7).
    Serial.println(F("\nAuto-cal at boot (load disconnected)..."));
    runCalibration();

    Serial.println();
    Serial.println(F("Commands:"));
    Serial.println(F("  <voltage>          Set LDO output (cal-table lookup)"));
    Serial.println(F("  set <V>            Same as above"));
    Serial.println(F("  code <N>           Set raw DAC code 0..4095 (debug)"));
    Serial.println(F("  read               Read LDO output (averaged)"));
    Serial.println(F("  drive <V> <ms>     Apply V for ms, then return to 0  (SMA actuation)"));
    Serial.println(F("  mosfet on|off      Load-enable MOSFET"));
    Serial.println(F("  cal                Re-run calibration sweep (load off)"));
    Serial.println(F("  sweep [codestep]   Raw-code diagnostic sweep (TSV)"));
    Serial.println(F("  csv   [codestep]   Same as sweep (CSV)"));
    Serial.println(F("  step <code> [ms]   Log LDO settle transient (10 ms cadence)"));
    Serial.println(F("  vdd <V>            Set assumed MCP4728 VDD (display only)"));
    Serial.println(F("  aref <V>           Set ADC Vref+ (1-pt cal; default 3.145 V)"));
    Serial.println(F("  info               Print state"));
    Serial.println();
}

// ======================================================================
//  Command loop
// ======================================================================
void loop() {
    if (!Serial.available()) return;

    String in = Serial.readStringUntil('\n');
    in.trim();
    if (in.length() == 0) return;

    String low = in;
    low.toLowerCase();

    if (low == "info")  { cmdInfo();              return; }
    if (low == "read")  { cmdRead();              return; }
    if (low == "cal")   { runCalibration();       return; }
    if (low == "sweep") { cmdSweep(128, false);   return; }
    if (low == "csv")   { cmdSweep(128, true);    return; }

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
        Serial.print(F("VDD_MCP=")); Serial.print(VDD_MCP, 3); Serial.println(F(" V (display only)"));
        return;
    }
    if (low.startsWith("aref ")) {
        // One-point ADC-reference cal: meter A0, then `aref <metered_A0_over_raw>`.
        // Simpler in practice: set the real H7 Vref+ here (default 3.145 V).
        float r = in.substring(5).toFloat();
        if (r < 2.8f || r > 3.4f) { Serial.println(F("Range: 2.8-3.4 V")); return; }
        ADC_VREF_V = r;
        Serial.print(F("ADC_VREF_V=")); Serial.print(ADC_VREF_V, 4);
        Serial.println(F(" V  (re-run 'cal' to refresh the table)"));
        return;
    }
    if (low.startsWith("sweep ")) {
        int s = in.substring(6).toInt();
        cmdSweep(s, false);
        return;
    }
    if (low.startsWith("csv ")) {
        int s = in.substring(4).toInt();
        cmdSweep(s, true);
        return;
    }
    if (low.startsWith("step ")) {
        String rest = in.substring(5); rest.trim();
        int sp = rest.indexOf(' ');
        uint16_t c; uint32_t ms = 1200;
        if (sp > 0) { c = (uint16_t)rest.substring(0, sp).toInt();
                      ms = (uint32_t)rest.substring(sp + 1).toInt(); }
        else        { c = (uint16_t)rest.toInt(); }
        if (c > 4095) c = 4095;
        if (ms == 0 || ms > 10000) ms = 1200;
        cmdStep(c, ms);
        return;
    }
    if (low.startsWith("drive ")) {
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
  #error "Unknown core - build with CORE_CM7 or CORE_CM4"
#endif
