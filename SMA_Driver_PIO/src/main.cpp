/**
 * @file main.cpp  (Portenta H7 - SMA_Driver_PIO, M7-only)
 *
 * Phase 6 SMA drive path bring-up. Port of the Arduino Uno
 * MCP4728 + TPS7A57 LDO controller to the Portenta H7 / Mid Carrier.
 *
 * Transfer-function strategy: ANALYTICAL (datasheet-derived).
 *
 *   The TPS7A57 sets its output by an internal precision current source
 *   (IREF, 50 uA nominal) flowing through the REF resistor, with SNS tied
 *   to OUT so the error amp runs unity-gain:  VOUT = IREF x RREF
 *   (datasheet SBVS395, Eq.5; IREF from Table 7-4: 0.5 V/10k, 1.0 V/20k).
 *
 *   Our board is TI's "DAC margining" topology: the MCP4728 drives the
 *   REF pin through a 6.2 k series resistor (the old 2k/10k DAC divider
 *   was removed). IREF flows out the REF pin through that resistor into
 *   the DAC node, so the REF node - and therefore VOUT (unity gain) - is:
 *
 *       V_LDO = V_DAC + IREF * R_SERIES
 *       V_DAC = (code / 4095) * VDD_MCP
 *
 *   => V_LDO = V_OFFSET + (VDD_MCP / 4095) * code      (a + b*code)
 *      with V_OFFSET = IREF * R_SERIES ~ 0.31 V (the measured floor).
 *
 *   This is exact-form linear, so it inverts in closed form (no table,
 *   no binary search):  code = (V_target - V_OFFSET) / VDD_MCP * 4095.
 *   VDD_MCP (slope) and V_OFFSET (intercept) are runtime-tunable (`vdd`,
 *   `offset`) so the formula can be trimmed to the meter if the real
 *   IREF / R_SERIES / VDD differ from nominal.
 *
 *   External power: MCP4728 VDD and TPS7A57 V_IN come from an external
 *   bench supply (>= ~5.5 V so the LDO has headroom). The H7 only sources
 *   control signals. 16-bit on-chip ADC reads the LDO output through a
 *   10k/10k feedback divider (ADC_FB_SCALE = 2.0); ADC_VREF_V is a 1-point
 *   reference cal (`aref`).
 *
 *
 * Pin map (Mid Carrier J15 / Arduino mbed core names):
 *
 *   I2C SDA   -> Wire SDA  (Portenta H7 PB_7) -> Mid Carrier J15-28 / silkscreen "I2C0 SDA"
 *   I2C SCL   -> Wire SCL  (Portenta H7 PB_6) -> Mid Carrier J15-26 / silkscreen "I2C0 SCL"
 *   MOSFET    -> D3        -> Mid Carrier J15-31 / silkscreen "PWM 3"  (PWM3 = PG7, not PA_9)
 *   FB (AIN)  -> A0        -> Mid Carrier "ANA0" pad  (LDO out, BEFORE shunt)
 *   I-SENSE   -> A1        -> Mid Carrier "ANA1" pad  (INA296A OUT; 100 mOhm shunt)
 *   TRIG OUT  -> PJ_11     -> Mid Carrier "PWM4" (J2-67) -> scope CH1 (edge = t0)
 *
 * (Pins chosen so this module's wiring does not overlap with
 *  SensorHub_PIO M4's PA_8 / PC_6 / PC_7. The two firmwares can
 *  share the same Portenta - only one M7 image runs at a time.)
 *
 *
 * Commands (115200 baud, line-terminated):
 *
 *   set <V>          Set LDO output to V volts (analytical inverse)
 *   <number>         Same as `set <number>` (bare number shortcut)
 *   code <N>         Set raw DAC code 0-4095; shows predicted + measured V
 *   read             Read LDO output, SMA current (INA296A), V_sma, R_sma
 *   gain <V/V>       Set INA296A gain (default 10 = A1 variant)
 *   shunt <ohm>      Set shunt resistance (default 0.1 = 100 mOhm)
 *   ioffset <V>      Set INA296A 0 A output offset (default 0, REF=GND)
 *   drive <V> <ms>   Apply <V> for <ms> ms then return to 0 (SMA actuation).
 *                    Logs t, V_set, V_meas every 10 ms during the hold.
 *   mosfet on|off    Load-enable MOSFET control
 *   sweep [step]     Raw-code diagnostic sweep, prints code / V_meas (TSV)
 *   csv   [step]     Same as sweep, CSV format (parse-friendly)
 *   step <code>[ms]  Log the LDO settle transient (10 ms cadence)
 *   fire <code>[ms][from]  Scope-triggered step: pulse TRIG_PIN at the DAC
 *                    write (= scope t0), hold <ms>, then DAC->0. Optional
 *                    <from> code sets the pre-step baseline (default 0).
 *                    MOSFET state left as-is (set `mosfet on|off` first).
 *   vdd <V>          Set MCP4728 VDD (the slope of V_LDO vs code)
 *   offset <V>       Set V_OFFSET = IREF*R_series (the intercept)
 *   aref <V>         Set ADC Vref+ (1-pt reference cal; default 3.145 V)
 *   info             Print current state
 *
 * NOTE: this firmware does not push samples into the SRAM ring buffer
 * (it has no M4). Once the SMA logic merges into SensorHub_PIO M7,
 * `drive`-time feedback samples will be pushed with src=3 per the
 * sample_ring.h reservation table.
 */

#include <Arduino.h>

// ======================================================================
//  M4 IDLE STUB  (compiled by [env:portenta_m4_idle])
// ======================================================================
#if defined(CORE_CM4)

void setup() {
    // Intentionally empty. No RPC.begin() - deliberately do NOT init the
    // OpenAMP IPC channel so a stale M7 calling RPC.begin() can't desync.
}

void loop() {
    __WFI();   // core sleeps; no IRQs configured -> effectively halted
}

#elif defined(CORE_CM7)

#include <Wire.h>
#include <Adafruit_MCP4728.h>

Adafruit_MCP4728 mcp;

// -- Pins --------------------------------------------------------------
const int MOSFET_PIN = D3;   // PWM3 = D3 = PG7 (Mid Carrier J15-31). Arduino alias, not raw PinName.
const int FB_PIN     = A0;
// scope trigger OUT: rising edge = DAC-step t0 (plain GPIO via digitalWrite).
// Mid Carrier silkscreen "PWM4" = J2-67 = STM32 PJ_11 (next to PWM3/PG_7 = MOSFET).
// Explicit PinName, not the D-alias: the PWM_n macros are unreliable on this core.
const PinName TRIG_PIN = PJ_11;

// -- Analytical LDO transfer (TPS7A57: V_LDO = V_DAC + IREF*R_SERIES) ---
// VDD_MCP is the MCP4728 supply rail = DAC full-scale; it is the SLOPE of
// V_LDO vs code. V_OFFSET = IREF*R_SERIES is the INTERCEPT. Both are
// runtime-tunable (`vdd`, `offset`) to trim the formula to the meter.
static float VDD_MCP     = 5.5f;       // DAC full-scale rail (slope term)
static const float IREF_A    = 50e-6f; // TPS7A57 ref current, datasheet nominal 50 uA
static const float R_SERIES  = 6200.0f;// DAC -> REF pin series resistor (6.2 k)
static float V_OFFSET    = IREF_A * R_SERIES;  // ~0.31 V intercept (IREF*R); tunable via `offset`

// -- Feedback readback divider (LDO out -> 10k/10k -> A0) ---------------
const float  R_FB_TOP     = 10000.0f;
const float  R_FB_BOT     = 10000.0f;
const float  FB_DIV_RATIO = R_FB_BOT / (R_FB_TOP + R_FB_BOT);   // 0.5
const float  ADC_FB_SCALE = 1.0f / FB_DIV_RATIO;                // 2.0

// -- INA296A current sense (LDO out -> 100 mOhm shunt -> SMA) -----------
// Topology:  LDO_OUT --[A0 taps here]--[ 100 mOhm shunt ]--> SMA --> GND
// The INA296A sits across the shunt; its OUT goes to A1. A1 variant = 10 V/V.
//   V_ina = I * R_SHUNT * INA_GAIN   ->   I = (V_ina - offset) / (INA_GAIN*R_SHUNT)
// With 10 V/V and 0.1 ohm the scale is 1.0 V/A (1 A -> 1.0 V), ~3.3 A full-scale.
// Unidirectional (REF=GND): OUT = 0 V at 0 A, so ISENSE_OFFSET_V = 0 nominal.
// V_sma = V_ldo - I*R_SHUNT (A0 is BEFORE the shunt); R_sma = V_sma / I.
// All three are runtime-tunable (`gain`, `shunt`, `ioffset`) to trim to the meter.
const int    ISENSE_PIN      = A1;     // INA296A OUT
static float INA_GAIN        = 10.0f;  // INA296A1 = 10 V/V
static float R_SHUNT_OHM     = 0.1f;   // 100 mOhm
static float ISENSE_OFFSET_V = 0.0f;   // 0 A output (REF=GND)
static const float I_FLOOR_A = 1e-3f;  // below this, R is undefined (open/no-drive)

// -- ADC (H7 on-chip, 16-bit) ------------------------------------------
static const int   ADC_RES_BITS = 16;
static const int   ADC_RES_MAX  = (1 << ADC_RES_BITS) - 1;     // 65535
static float       ADC_VREF_V   = 3.145f;  // H7 Vref+. 1-pt cal (A0 meter 2.89 V vs fw 3.032 V @code4095).
static const int   ADC_SAMPLES  = 64;
// Settle timing is poll-based (settleWait): the TPS7A57 REF node is slow
// (~100 ms, set by the CNR/SS soft-start cap), so a fixed delay either
// under-waits (reads mid-slew) or wastes time. settleWait polls until quiet.

// -- Drive parameters --------------------------------------------------
static const float    DRIVE_V_MAX  = 5.0f;     // SMA-side ceiling for set/drive
static const uint32_t DRIVE_MS_MAX = 60000;    // 60 s - SMA self-heat risk above
static const uint32_t DRIVE_LOG_MS = 10;       // feedback sample period during hold

// -- DAC state ---------------------------------------------------------
static uint16_t currentCode = 0;

// ======================================================================
//  ADC / DAC helpers
// ======================================================================

// Averaged read at the A0 pin (volts at the ADC input).
static float readADC(int pin) {
    analogRead(pin);                 // throw-away (prime the input stage)
    delay(1);
    uint32_t sum = 0;
    for (int i = 0; i < ADC_SAMPLES; i++) sum += (uint32_t)analogRead(pin);
    float code = (float)sum / (float)ADC_SAMPLES;
    return (code / (float)ADC_RES_MAX) * ADC_VREF_V;
}

// LDO output voltage (un-divided).
static float readLDO() {
    return readADC(FB_PIN) * ADC_FB_SCALE;
}

// One coherent electrical read of the SMA drive path.
struct SmaRead { float v_ldo; float i; float v_sma; float r; };
static SmaRead readSma() {
    SmaRead s;
    s.v_ldo = readLDO();                                  // A0, before the shunt
    float v_ina = readADC(ISENSE_PIN);                    // A1, INA296A OUT
    float scale = INA_GAIN * R_SHUNT_OHM;                 // V/A
    s.i     = (scale > 0.0f) ? (v_ina - ISENSE_OFFSET_V) / scale : 0.0f;
    s.v_sma = s.v_ldo - s.i * R_SHUNT_OHM;                // subtract shunt drop
    s.r     = (fabs(s.i) >= I_FLOOR_A) ? s.v_sma / s.i : NAN;
    return s;
}

// Raw DAC write - updates the code + lets the I2C/DAC update, does NOT
// wait for the slow LDO output to settle. Use for transient logging.
static void setDACraw(uint16_t code) {
    if (code > 4095) code = 4095;
    currentCode = code;
    mcp.setChannelValue(MCP4728_CHANNEL_A, code, MCP4728_VREF_VDD, MCP4728_GAIN_1X);
    delay(2);
}

// Poll the LDO output until quiet (SETTLE_QUIET_N consecutive ~20 ms reads
// within SETTLE_TOL_V) or a hard timeout. Honors the LDO's ~100 ms settle.
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

// DAC write that returns only after the LDO output has settled.
static void setDAC(uint16_t code) {
    setDACraw(code);
    settleWait();
}

// Estimated DAC pin voltage for a code (display only).
static inline float codeToVdac(uint16_t code) {
    return ((float)code / 4095.0f) * VDD_MCP;
}

// ======================================================================
//  Analytical transfer:  V_LDO = V_OFFSET + (VDD_MCP/4095) * code
// ======================================================================

// Forward: predicted LDO output for a DAC code.
static inline float codeToVldo(uint16_t code) {
    return V_OFFSET + ((float)code / 4095.0f) * VDD_MCP;
}

// Inverse (closed form): DAC code for a target LDO voltage, clamped 0..4095.
static uint16_t vldoToCode(float vtarget) {
    float vdac = vtarget - V_OFFSET;
    if (vdac < 0.0f) vdac = 0.0f;
    float code = (vdac / VDD_MCP) * 4095.0f;
    if (code < 0.0f)    code = 0.0f;
    if (code > 4095.0f) code = 4095.0f;
    return (uint16_t)(code + 0.5f);
}

// Output range achievable by the formula.
static inline float vldoMin() { return codeToVldo(0); }       // = V_OFFSET
static inline float vldoMax() { return codeToVldo(4095); }    // = V_OFFSET + VDD_MCP

// ======================================================================
//  Commands
// ======================================================================

static void cmdSetVoltage(float vtarget) {
    if (vtarget < 0 || vtarget > DRIVE_V_MAX) {
        Serial.print(F("ERR: V out of range [0, "));
        Serial.print(DRIVE_V_MAX, 2); Serial.println(F("]"));
        return;
    }
    float lo = vldoMin(), hi = vldoMax();
    float vc = vtarget;
    bool clamped = false;
    if (vc < lo) { vc = lo; clamped = true; }
    if (vc > hi) { vc = hi; clamped = true; }
    if (clamped) {
        Serial.print(F("WARN: target ")); Serial.print(vtarget, 3);
        Serial.print(F("V out of achievable range [")); Serial.print(lo, 3);
        Serial.print(F(", ")); Serial.print(hi, 3);
        Serial.print(F("] -> clamped to ")); Serial.print(vc, 3); Serial.println('V');
    }

    uint16_t code = vldoToCode(vc);
    setDAC(code);
    float vmeas = readLDO();
    float err   = vmeas - vtarget;
    Serial.print(F("Target=")); Serial.print(vtarget, 3);
    Serial.print(F("V  Code=")); Serial.print(code);
    Serial.print(F("  V_pred=")); Serial.print(codeToVldo(code), 3);
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
    Serial.print(F("  V_pred=")); Serial.print(codeToVldo(code), 3);
    Serial.print(F("  V_LDO_meas=")); Serial.print(vldo, 3);
    Serial.println('V');
}

static void cmdRead() {
    SmaRead s = readSma();
    Serial.print(F("V_LDO=")); Serial.print(s.v_ldo, 4);
    Serial.print(F("V  I="));  Serial.print(s.i * 1000.0f, 2);
    Serial.print(F("mA  V_sma=")); Serial.print(s.v_sma, 4);
    Serial.print(F("V  R="));
    if (isnan(s.r)) Serial.print(F("--"));
    else            Serial.print(s.r, 3);
    Serial.print(F("ohm  code=")); Serial.println(currentCode);
}

static void cmdMosfet(bool on) {
    digitalWrite(MOSFET_PIN, on ? HIGH : LOW);
    Serial.print(F("MOSFET=")); Serial.println(on ? F("HIGH (load on)") : F("LOW (load off)"));
}

// SMA actuation primitive: MOSFET on, apply V (analytical code), log
// feedback every DRIVE_LOG_MS for the hold, then DAC->0 and MOSFET off.
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

    float vc = vtarget;
    if (vc < vldoMin()) vc = vldoMin();
    if (vc > vldoMax()) vc = vldoMax();
    uint16_t code = vldoToCode(vc);

    digitalWrite(MOSFET_PIN, HIGH);
    delay(2);

    Serial.print(F("[DRIVE] start V=")); Serial.print(vtarget, 3);
    Serial.print(F(" t_ms="));            Serial.println(hold_ms);
    Serial.println(F("t_rel_ms\tV_set\tV_meas\tI_mA\tR_ohm"));

    uint32_t t_start = millis();
    setDACraw(code);               // raw: log the LDO rise transient below
    SmaRead s0 = readSma();
    Serial.print(0);            Serial.print('\t');
    Serial.print(vtarget, 4);   Serial.print('\t');
    Serial.print(s0.v_ldo, 4);  Serial.print('\t');
    Serial.print(s0.i * 1000.0f, 2); Serial.print('\t');
    if (isnan(s0.r)) Serial.println(F("--")); else Serial.println(s0.r, 3);

    float max_err = fabs(s0.v_ldo - vtarget);
    uint32_t next_sample_ms = DRIVE_LOG_MS;
    while (true) {
        uint32_t t_rel = millis() - t_start;
        if (t_rel >= hold_ms) break;
        if (t_rel >= next_sample_ms) {
            SmaRead s = readSma();
            float e  = fabs(s.v_ldo - vtarget);
            if (e > max_err) max_err = e;
            Serial.print(t_rel);   Serial.print('\t');
            Serial.print(vtarget, 4); Serial.print('\t');
            Serial.print(s.v_ldo, 4); Serial.print('\t');
            Serial.print(s.i * 1000.0f, 2); Serial.print('\t');
            if (isnan(s.r)) Serial.println(F("--")); else Serial.println(s.r, 3);
            next_sample_ms += DRIVE_LOG_MS;
        }
    }

    SmaRead sf = readSma();          // final electrical state before release
    setDACraw(0);
    digitalWrite(MOSFET_PIN, LOW);   // release load-enable - return to safe state
    uint32_t t_done = millis();

    Serial.print(F("[DRIVE] done V_final=")); Serial.print(sf.v_ldo, 4);
    Serial.print(F(" I_final=")); Serial.print(sf.i * 1000.0f, 2); Serial.print(F("mA"));
    Serial.print(F(" R_final="));
    if (isnan(sf.r)) Serial.print(F("--")); else Serial.print(sf.r, 3);
    Serial.print(F("ohm max_err="));
    Serial.print(max_err * 1000.0f, 1);       Serial.print(F("mV"));
    Serial.print(F(" elapsed_ms="));          Serial.println(t_done - t_start);
}

// Raw-code diagnostic sweep: walks codes 0..4095 in codeStep increments,
// printing predicted and measured LDO output (handy to validate the model).
static void cmdSweep(int codeStep, bool csv) {
    if (codeStep < 16)   codeStep = 16;
    if (codeStep > 2048) codeStep = 2048;

    if (csv) {
        Serial.println(F("dac_code,v_pred,v_ldo_meas"));
    } else {
        Serial.println(F("\nCode  V_pred  V_meas"));
        Serial.println(F("----  ------  ------"));
    }

    for (int c = 0; c <= 4095; c += codeStep) {
        uint16_t code = (uint16_t)(c > 4095 ? 4095 : c);
        setDAC(code);
        float vmeas = readLDO();
        float vpred = codeToVldo(code);
        if (csv) {
            Serial.print(code);    Serial.print(',');
            Serial.print(vpred, 4); Serial.print(',');
            Serial.println(vmeas, 4);
        } else {
            if (code < 1000) Serial.print(' ');
            if (code < 100)  Serial.print(' ');
            if (code < 10)   Serial.print(' ');
            Serial.print(code);    Serial.print(F("  "));
            Serial.print(vpred, 3); Serial.print(F("  "));
            Serial.println(vmeas, 3);
        }
    }
    setDAC(0);
    if (!csv) Serial.println();
}

// Step-response logger: raw-write a code and log V_meas every 10 ms for
// `ms` (default 1200). MOSFET untouched. Use to measure the settle time.
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

// Hardware-triggered step for scope capture. Settles the DAC at `code_from`
// (the pre-step baseline, default 0), then at t0 raises TRIG_PIN and *immediately*
// writes `code_to` raw. A scope armed single-shot on the C1 rising edge captures
// the V_LDO transient. After `ms`, DAC -> 0 and TRIG_PIN -> LOW so the next shot
// can re-arm. MOSFET state is left untouched: the caller picks loaded vs unloaded
// with `mosfet on|off` beforehand.
static void cmdFire(uint16_t code_to, uint32_t ms, uint16_t code_from) {
    // Establish + settle the pre-step baseline.
    setDACraw(code_from);
    settleWait();                       // honor the ~100 ms LDO soft-start
    digitalWrite(TRIG_PIN, LOW);
    delay(20);                          // quiet pre-trigger baseline on the scope

    Serial.print(F("[FIRE] from="));  Serial.print(code_from);
    Serial.print(F(" to="));          Serial.print(code_to);
    Serial.print(F(" ms="));          Serial.print(ms);
    Serial.print(F(" mosfet="));      Serial.println(digitalRead(MOSFET_PIN) ? F("on") : F("off"));

    // t0: trigger edge, then the DAC step. The I2C write lands a few hundred us
    // later -> the C2 (DAC node) trace shows the move just right of the C1 edge.
    digitalWrite(TRIG_PIN, HIGH);
    delayMicroseconds(5);
    setDACraw(code_to);

    uint32_t t0 = millis();
    while (millis() - t0 < ms) { /* hold; scope is acquiring */ }
    SmaRead sf = readSma();             // settled electrical state before release

    setDACraw(0);
    digitalWrite(TRIG_PIN, LOW);        // re-arm for next shot

    Serial.print(F("[FIRE] done V_final=")); Serial.print(sf.v_ldo, 4);
    Serial.print(F("V V_pred="));            Serial.print(codeToVldo(code_to), 4);
    Serial.print(F("V I_final="));           Serial.print(sf.i * 1000.0f, 2);
    Serial.print(F("mA R_final="));
    if (isnan(sf.r)) Serial.print(F("--")); else Serial.print(sf.r, 3);
    Serial.println(F("ohm"));
}

static void cmdInfo() {
    Serial.println(F("\n== SMA_Driver_PIO state (analytical model) =="));
    Serial.print(F("V_LDO = V_OFFSET + (VDD/4095)*code"));
    Serial.println();
    Serial.print(F("VDD_MCP (slope)   : ")); Serial.print(VDD_MCP, 3); Serial.println(F(" V"));
    Serial.print(F("V_OFFSET (intcpt) : ")); Serial.print(V_OFFSET, 4);
        Serial.print(F(" V  (IREF*R = ")); Serial.print(IREF_A * 1e6f, 1);
        Serial.print(F("uA x ")); Serial.print(R_SERIES, 0); Serial.println(F(" ohm)"));
    Serial.print(F("V_LDO range       : ")); Serial.print(vldoMin(), 3);
        Serial.print(F(" - ")); Serial.print(vldoMax(), 3); Serial.println(F(" V"));
    Serial.print(F("ADC res / Vref    : ")); Serial.print(ADC_RES_BITS); Serial.print(F("-bit / "));
        Serial.print(ADC_VREF_V, 3); Serial.println(F(" V"));
    Serial.print(F("DAC code          : ")); Serial.print(currentCode);
        Serial.print(F("  (V_pred=")); Serial.print(codeToVldo(currentCode), 3); Serial.println(F(" V)"));
    SmaRead s = readSma();
    Serial.print(F("V_LDO measured    : ")); Serial.print(s.v_ldo, 3); Serial.println(F(" V"));
    Serial.print(F("I sense (INA296A) : ")); Serial.print(s.i * 1000.0f, 2);
        Serial.print(F(" mA  (gain=")); Serial.print(INA_GAIN, 1);
        Serial.print(F(" V/V, shunt=")); Serial.print(R_SHUNT_OHM * 1000.0f, 1);
        Serial.print(F(" mOhm, scale=")); Serial.print(INA_GAIN * R_SHUNT_OHM, 3); Serial.println(F(" V/A)"));
    Serial.print(F("V_sma / R_sma     : ")); Serial.print(s.v_sma, 3); Serial.print(F(" V  /  "));
        if (isnan(s.r)) Serial.println(F("-- ohm (I < floor)")); else { Serial.print(s.r, 3); Serial.println(F(" ohm")); }
    Serial.print(F("MOSFET            : ")); Serial.println(digitalRead(MOSFET_PIN) ? F("HIGH (load on)") : F("LOW (load off)"));
}

// ======================================================================
//  Setup
// ======================================================================
void setup() {
    Serial.begin(115200);
    uint32_t t0 = millis();
    while (!Serial && (millis() - t0) < 2000) {}

    pinMode(MOSFET_PIN, OUTPUT);
    digitalWrite(MOSFET_PIN, LOW);     // load OFF until operator enables

    pinMode(TRIG_PIN, OUTPUT);
    digitalWrite(TRIG_PIN, LOW);       // scope trigger idle LOW (re-armed each shot)

    analogReadResolution(ADC_RES_BITS);
    for (int i = 0; i < 10; i++) analogRead(FB_PIN);
    for (int i = 0; i < 10; i++) analogRead(ISENSE_PIN);   // prime INA296A input (A1)

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
        digitalWrite(MOSFET_PIN, LOW);
        while (1) { delay(1000); }
    }

    setDACraw(0);                      // park DAC at 0 (safe)

    Serial.println();
    Serial.print(F("Model: V_LDO = ")); Serial.print(V_OFFSET, 3);
    Serial.print(F(" + (")); Serial.print(VDD_MCP, 3);
    Serial.print(F("/4095)*code   range ")); Serial.print(vldoMin(), 3);
    Serial.print(F(" - ")); Serial.print(vldoMax(), 3); Serial.println(F(" V"));
    Serial.println();
    Serial.println(F("Commands:"));
    Serial.println(F("  <voltage>          Set LDO output (analytical inverse)"));
    Serial.println(F("  set <V>            Same as above"));
    Serial.println(F("  code <N>           Set raw DAC code 0..4095 (pred + meas)"));
    Serial.println(F("  read               Read LDO output (averaged)"));
    Serial.println(F("  drive <V> <ms>     Apply V for ms, then return to 0  (SMA actuation)"));
    Serial.println(F("  mosfet on|off      Load-enable MOSFET"));
    Serial.println(F("  sweep [codestep]   Raw-code sweep, pred vs meas (TSV)"));
    Serial.println(F("  csv   [codestep]   Same as sweep (CSV)"));
    Serial.println(F("  step <code> [ms]   Log LDO settle transient (10 ms cadence)"));
    Serial.println(F("  fire <code> [ms] [from]  Scope-triggered step: TRIG_PIN edge at DAC write"));
    Serial.println(F("  vdd <V>            Set MCP4728 VDD (slope)"));
    Serial.println(F("  offset <V>         Set V_OFFSET = IREF*R_series (intercept)"));
    Serial.println(F("  aref <V>           Set ADC Vref+ (1-pt cal; default 3.145 V)"));
    Serial.println(F("  gain <V/V>         Set INA296A gain (default 10 = A1 variant)"));
    Serial.println(F("  shunt <ohm>        Set shunt resistance (default 0.1 = 100 mOhm)"));
    Serial.println(F("  ioffset <V>        Set INA296A 0 A output offset (default 0, REF=GND)"));
    Serial.println(F("  info               Print state (incl. I / V_sma / R)"));
    Serial.println(F("  reset | reboot     Safe-state + soft-reboot the MCU (port re-enumerates)"));
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
    if (low == "sweep") { cmdSweep(128, false);   return; }
    if (low == "csv")   { cmdSweep(128, true);    return; }
    if (low == "reset" || low == "reboot") {
        // Software reset: return the rig to a safe state, then restart the MCU.
        // Recovers from a wedged logical state without the physical button. (A
        // truly hung firmware can't reach this — pulse DTR from the host then,
        // or press RST.) The USB-CDC port drops on reset; the host must reopen.
        digitalWrite(MOSFET_PIN, LOW);   // load OFF first
        setDACraw(0);                    // DAC to 0 V
        digitalWrite(TRIG_PIN, LOW);
        Serial.println(F("[RESET] rebooting MCU ..."));
        Serial.flush();
        delay(50);
        NVIC_SystemReset();              // CMSIS: never returns
    }

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
        Serial.print(F("VDD_MCP=")); Serial.print(VDD_MCP, 3);
        Serial.print(F(" V  -> V_LDO range ")); Serial.print(vldoMin(), 3);
        Serial.print(F(" - ")); Serial.print(vldoMax(), 3); Serial.println(F(" V"));
        return;
    }
    if (low.startsWith("offset ")) {
        float v = in.substring(7).toFloat();
        if (v < 0.0f || v > 1.0f) { Serial.println(F("Range: 0.0-1.0 V")); return; }
        V_OFFSET = v;
        Serial.print(F("V_OFFSET=")); Serial.print(V_OFFSET, 4);
        Serial.print(F(" V  -> V_LDO range ")); Serial.print(vldoMin(), 3);
        Serial.print(F(" - ")); Serial.print(vldoMax(), 3); Serial.println(F(" V"));
        return;
    }
    if (low.startsWith("aref ")) {
        float r = in.substring(5).toFloat();
        if (r < 2.8f || r > 3.4f) { Serial.println(F("Range: 2.8-3.4 V")); return; }
        ADC_VREF_V = r;
        Serial.print(F("ADC_VREF_V=")); Serial.print(ADC_VREF_V, 4); Serial.println(F(" V"));
        return;
    }
    if (low.startsWith("gain ")) {                 // INA296A gain (V/V)
        float g = in.substring(5).toFloat();
        if (g < 1.0f || g > 1000.0f) { Serial.println(F("Range: 1-1000 V/V")); return; }
        INA_GAIN = g;
        Serial.print(F("INA_GAIN=")); Serial.print(INA_GAIN, 1);
        Serial.print(F(" V/V  -> I scale ")); Serial.print(INA_GAIN * R_SHUNT_OHM, 3); Serial.println(F(" V/A"));
        return;
    }
    if (low.startsWith("shunt ")) {                // shunt resistance (ohm)
        float r = in.substring(6).toFloat();
        if (r <= 0.0f || r > 10.0f) { Serial.println(F("Range: >0 - 10 ohm")); return; }
        R_SHUNT_OHM = r;
        Serial.print(F("R_SHUNT=")); Serial.print(R_SHUNT_OHM * 1000.0f, 1);
        Serial.print(F(" mOhm -> I scale ")); Serial.print(INA_GAIN * R_SHUNT_OHM, 3); Serial.println(F(" V/A"));
        return;
    }
    if (low.startsWith("ioffset ")) {              // INA296A 0 A output (V)
        float v = in.substring(8).toFloat();
        if (v < -1.0f || v > 3.3f) { Serial.println(F("Range: -1.0 - 3.3 V")); return; }
        ISENSE_OFFSET_V = v;
        Serial.print(F("ISENSE_OFFSET=")); Serial.print(ISENSE_OFFSET_V, 4); Serial.println(F(" V"));
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
    if (low.startsWith("fire ")) {
        // fire <code_to> [ms] [code_from]   (scope-triggered step)
        String rest = in.substring(5); rest.trim();
        int s1 = rest.indexOf(' ');
        uint16_t code_to, code_from = 0; uint32_t ms = 500;
        if (s1 < 0) { code_to = (uint16_t)rest.toInt(); }
        else {
            code_to = (uint16_t)rest.substring(0, s1).toInt();
            String r2 = rest.substring(s1 + 1); r2.trim();
            int s2 = r2.indexOf(' ');
            if (s2 < 0) { ms = (uint32_t)r2.toInt(); }
            else { ms = (uint32_t)r2.substring(0, s2).toInt();
                   code_from = (uint16_t)r2.substring(s2 + 1).toInt(); }
        }
        if (code_to   > 4095) code_to   = 4095;
        if (code_from > 4095) code_from = 4095;
        if (ms == 0 || ms > 10000) ms = 500;
        cmdFire(code_to, ms, code_from);
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
