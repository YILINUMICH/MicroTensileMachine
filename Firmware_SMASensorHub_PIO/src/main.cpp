/**
 * @file main.cpp  (Portenta H7 dual-core — Firmware_SMASensorHub_PIO)
 *
 * COMBINED firmware: dual-ADC sensing on M4 + SMA drive path on M7, one
 * image pair. Merge of Firmware_SensorHub_PIO (sensing) and
 * Firmware_SMADriver_PIO (SMA controller). The two subsystems were
 * bench-verified separately; this runs them together on the same H7.
 *
 *   M4 (UNCHANGED from SensorHub):
 *     - ADC1 (32-bit, 400 SPS, Sinc3, PGA=1) → AIN4(+)/AIN5(-)  [IL-030 laser]
 *     - ADC2 (24-bit, 400 SPS, Sinc3, gain=1) → AIN2(+)/AIN3(-) [LCA-9PC load]
 *     - External REF7050 (+5 V) on AIN0(+)/AIN1(-), shared by both ADCs.
 *     - DRDY-ISR sampling → SRAM4 ring buffer (sample_ring.h).
 *
 *   M7 (bridge + SMA controller):
 *     - Drains the SRAM4 ring → USB-CDC (sensor TSV stream, untagged).
 *     - Emits [STATUS] telemetry once per second.
 *     - Runs the SMA driver as a NON-BLOCKING STATE MACHINE:
 *         I2C → MCP4728 DAC → TPS7A57 LDO → MOSFET-gated SMA load;
 *         INA296A current sense on A1; scope TRIG on PJ_11.
 *       Long ops (drive/fire/step/sweep) are serviced one step per loop
 *       pass so the sensor stream NEVER freezes during SMA actuation and
 *       an `abort` command can interrupt a live drive.
 *
 * ── Shared USB serial: three line classes ────────────────────────────
 *   <untagged TSV>   sample stream   : t_ms\tsrc\traw\tV\thw_us\tseq
 *                    src=1 laser, 2 load  (from M4 via the ring);
 *                    src=3 SMA V, 4 SMA I, 5 SMA R  (from M7 during
 *                    drive/fire — emitted directly, NOT via the ring).
 *   [STATUS] ...     pipeline telemetry (1 Hz)
 *   [SMA] ...        SMA driver banners / responses (human-readable)
 *   The host sensor parser already drops any line containing '[', so the
 *   [STATUS] and [SMA] classes are cleanly demultiplexed from samples.
 *
 * ── Why the merge is safe (no pin overlap) ───────────────────────────
 *   M4 sensing : PA_8 (CS), PC_6 (DRDY), PC_7 (RESET) + SPI bus
 *   M7 SMA     : Wire/I2C (PB_6/PB_7), A0 (FB), A1 (INA296A I-sense),
 *                PG_7/D3 (MOSFET), PJ_11 (scope TRIG)
 *
 * Flash order (first time):
 *   pio run -e portenta_m7 -t upload
 *   pio run -e portenta_m4 -t upload
 *   pio device monitor          (115200)
 * → Power-cycle the rig (USB + EVM supply) after every upload.
 */

#include <Arduino.h>
#include "RPC.h"
#include "sample_ring.h"

// ── Enable/disable each ADC sensing path at build time ────────────────
// Shared by M7 (output formatting) and M4 (ADC control).
#define ENABLE_ADC1   1
#define ENABLE_ADC2   1


// ══════════════════════════════════════════════════════════════════════
//  M7 CORE — ring→USB bridge + [STATUS] telemetry + SMA state machine
// ══════════════════════════════════════════════════════════════════════
#if defined(CORE_CM7)

#include <Wire.h>
#include <Adafruit_MCP4728.h>

// ──────────────────────────────────────────────────────────────────────
//  SMA drive-path hardware (ported verbatim from Firmware_SMADriver_PIO)
// ──────────────────────────────────────────────────────────────────────
Adafruit_MCP4728 mcp;
static bool sma_ok = false;          // false if MCP4728 absent → SMA cmds no-op
                                     //   (sensor bridge still runs — non-fatal)

// -- Pins --------------------------------------------------------------
const int     MOSFET_PIN = D3;       // PWM3 = D3 = PG_7 (Mid Carrier J15-31)
const int     FB_PIN     = A0;       // LDO out via 10k/10k divider (BEFORE shunt)
const int     ISENSE_PIN = A1;       // INA296A OUT (current sense) — ENABLED below
const PinName TRIG_PIN   = PJ_11;    // scope trigger; rising edge = DAC-step t0

// -- Analytical LDO transfer: V_LDO = V_OFFSET + (VDD_MCP/4095)*code ----
static float       VDD_MCP   = 5.5f;          // DAC full-scale rail (slope)
static const float IREF_A    = 50e-6f;        // TPS7A57 ref current (nominal)
static const float R_SERIES  = 6200.0f;       // DAC → REF pin series resistor
static float       V_OFFSET  = IREF_A * R_SERIES;  // ~0.31 V intercept (tunable)

// -- Feedback readback divider (LDO out → 10k/10k → A0) ----------------
const float  FB_DIV_RATIO = 0.5f;
const float  ADC_FB_SCALE = 1.0f / FB_DIV_RATIO;   // 2.0

// -- INA296A current sense (LDO out → 100 mOhm shunt → SMA) ------------
//   V_ina = I * R_SHUNT * INA_GAIN  →  I = (V_ina - offset)/(INA_GAIN*R_SHUNT)
//   A1 variant = 10 V/V; 0.1 ohm → 1.0 V/A; unidirectional (REF=GND).
//   V_sma = V_ldo - I*R_SHUNT (A0 is BEFORE the shunt); R_sma = V_sma/I.
static float       INA_GAIN        = 10.0f;   // INA296A1 = 10 V/V
static float       R_SHUNT_OHM     = 0.1f;    // 100 mOhm
static float       ISENSE_OFFSET_V = 0.0f;    // 0 A output (REF=GND)
static const float I_FLOOR_A       = 1e-3f;   // below this, R is undefined

// -- On-chip ADC (H7, 16-bit) ------------------------------------------
static const int   ADC_RES_BITS = 16;
static const int   ADC_RES_MAX  = (1 << ADC_RES_BITS) - 1;   // 65535
static float       ADC_VREF_V   = 3.145f;     // H7 Vref+ (1-pt cal)
static const int   ADC_SAMPLES  = 64;

// -- Drive parameters --------------------------------------------------
static const float    DRIVE_V_MAX  = 5.0f;
static const uint32_t DRIVE_MS_MAX  = 60000;  // 60 s — SMA self-heat risk above
static const uint32_t DRIVE_LOG_MS  = 10;     // feedback sample period during hold

static uint16_t currentCode = 0;

// ──────────────────────────────────────────────────────────────────────
//  ADC / DAC helpers  (electrical primitives — kept byte-for-byte; only
//  the BLOCKING control flow above them was restructured into a machine)
// ──────────────────────────────────────────────────────────────────────

// Averaged read at an analog pin (volts at the ADC input).
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

// One coherent electrical read of the SMA drive path (INA296A current sense).
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

// Raw DAC write — updates code + lets the DAC update; does NOT wait for
// the slow LDO output to settle. No-op (records code only) if MCP absent.
static void setDACraw(uint16_t code) {
    if (code > 4095) code = 4095;
    currentCode = code;
    if (sma_ok) {
        mcp.setChannelValue(MCP4728_CHANNEL_A, code, MCP4728_VREF_VDD, MCP4728_GAIN_1X);
        delay(2);
    }
}

static inline float codeToVdac(uint16_t code) { return ((float)code / 4095.0f) * VDD_MCP; }
static inline float codeToVldo(uint16_t code) { return V_OFFSET + ((float)code / 4095.0f) * VDD_MCP; }
static uint16_t vldoToCode(float vtarget) {
    float vdac = vtarget - V_OFFSET;
    if (vdac < 0.0f) vdac = 0.0f;
    float code = (vdac / VDD_MCP) * 4095.0f;
    if (code < 0.0f)    code = 0.0f;
    if (code > 4095.0f) code = 4095.0f;
    return (uint16_t)(code + 0.5f);
}
static inline float vldoMin() { return codeToVldo(0); }
static inline float vldoMax() { return codeToVldo(4095); }

// Line tag — every SMA-subsystem line starts with this so the host sensor
// parser (drops '['-containing lines) cleanly ignores SMA output.
static inline void smaTag() { Serial.print(F("[SMA] ")); }


// ══════════════════════════════════════════════════════════════════════
//  SMA NON-BLOCKING STATE MACHINE
//
//  loop() services ONE step of the active op per pass; nothing blocks for
//  more than a single readADC (~1-2 ms). The sensor ring is drained every
//  pass by pumpSensors(), so sensor data keeps flowing to USB DURING an
//  SMA drive, and `abort` can interrupt a live op.
// ══════════════════════════════════════════════════════════════════════

enum SmaState {
    SMA_IDLE,
    SMA_SET_SETTLE,    // set <V> / bare number : settle then report
    SMA_CODE_SETTLE,   // code <N>              : settle then report
    SMA_DRIVING,       // drive <V> <ms>        : MOSFET on, log, return to 0
    SMA_STEPPING,      // step <code> <ms>      : log settle transient
    SMA_SWEEP_SETTLE,  // sweep / csv           : settle each code, print
    SMA_FIRE_SETTLE,   // fire : settle baseline code_from
    SMA_FIRE_QUIET,    // fire : 20 ms quiet pre-trigger
    SMA_FIRE_HOLD,     // fire : hold code_to after the trigger edge
    SMA_CYCLE_HIGH,    // cycle : heating phase  (V_high for fire_ms)
    SMA_CYCLE_LOW      // cycle : cooling phase  (V_low for cool_ms)
};
static SmaState smaState = SMA_IDLE;

// Generic op context (reused across states; only one op runs at a time).
static uint32_t op_t0;          // phase start (millis)
static uint32_t op_hold_ms;     // hold duration
static uint16_t op_code;        // target code
static float    op_vtarget;     // for drive/set logging
static uint32_t op_next_log;    // next feedback-log time (rel ms)
static float    op_max_err;

// Non-blocking settle detector (replaces the blocking settleWait()).
static float    st_prev;
static int      st_quiet;
static uint32_t st_t0;
static uint32_t st_next;        // next LDO read time (abs ms)

// Sweep context.
static long     sw_c;
static int      sw_step;
static bool     sw_csv;

// Fire context.
static uint16_t fire_to, fire_from;
static uint32_t fire_ms;

// ── Cyclic actuation context (the ON-M7 experiment state machine) ─────
// A cycle = heat at V_high for fire_ms, then cool at V_low for cool_ms.
// Timing is M7-local (millis()) → deterministic, independent of host /
// USB latency. The host only sets params (`cycle`) and heartbeats (`ping`).
static float    cyc_v_high   = 0.0f;
static float    cyc_v_low    = 0.0f;
static uint32_t cyc_fire_ms  = 0;
static uint32_t cyc_cool_ms  = 0;
static uint32_t cyc_n_target = 0;   // 0 = continuous until `stop`/`abort`
static uint32_t cyc_n_done   = 0;   // completed cycles so far
static uint32_t cyc_phase_t0 = 0;   // current phase start (millis)
static uint32_t cyc_next_log = 0;   // next src=3/4/5 stream time (rel ms)
static const uint32_t CYCLE_LOG_MS = 10;
static const uint32_t CYCLE_MS_MAX = 600000;   // 10 min per phase ceiling

// ── Host heartbeat watchdog (safety for unattended SMA heating) ───────
// While cycling, if no `ping` arrives within WDT_MS the machine aborts to
// a safe state. `wdt <ms>` tunes it; `wdt 0` disables (manual bench use).
static uint32_t wdt_timeout_ms = 5000;   // 0 = disabled
static uint32_t wdt_last_ping  = 0;

// Begin a settle measurement (mirrors settleWait timing: first compare at
// +18 ms, 2 mV quiet band, 5 consecutive quiet reads, 2 s hard timeout).
static void settleBegin() {
    st_prev  = sma_ok ? readLDO() : 0.0f;
    st_quiet = 0;
    st_t0    = millis();
    st_next  = st_t0 + 18;
}
// Service the settle; returns true when settled OR timed out.
static bool settleService() {
    const float    SETTLE_TOL_V      = 0.002f;
    const int      SETTLE_QUIET_N    = 5;
    const uint32_t SETTLE_TIMEOUT_MS = 2000;
    uint32_t now = millis();
    if (now - st_t0 >= SETTLE_TIMEOUT_MS) return true;
    if ((int32_t)(now - st_next) >= 0) {
        float v = readLDO();
        if (fabs(v - st_prev) < SETTLE_TOL_V) { if (++st_quiet >= SETTLE_QUIET_N) return true; }
        else st_quiet = 0;
        st_prev = v;
        st_next = now + 18;
    }
    return false;
}

static bool smaBusy() { return smaState != SMA_IDLE; }

// Drop any active op to a safe state.
static void abortSma() {
    setDACraw(0);
    digitalWrite(MOSFET_PIN, LOW);
    digitalWrite(TRIG_PIN, LOW);
    smaState = SMA_IDLE;
    smaTag(); Serial.println(F("[ABORT] safe state (DAC=0, MOSFET off)"));
}

// ── Unified-stream SMA feedback (src=3 V, src=4 I, src=5 R) ────────────
// Emitted as UNTAGGED sensor-TSV lines (same 6-column format as the M4
// laser/load samples) so the host logs SMA feedback time-aligned with the
// sensor streams. M7 is the sole USB writer, so this needs NO ring
// producer — the M4-owned SPSC ring is untouched.
//
// NOTE: t_ms / hw_us on these lines are M7's clock, distinct from the M4
// sensor lines' clock (the two cores boot at different times). That is
// fine — the host joins all streams on its own arrival clock; the
// embedded stamps are for per-stream jitter / drop detection only.
//
//   src=3 (SMA drive V) : raw = DAC code (currentCode), voltage = V_ldo
//   src=4 (SMA current)  : raw = 0,                      voltage = I [A]
//   src=5 (SMA R = V/I)   : raw = 0,                      voltage = R [ohm]
//                           (omitted when R is NaN, i.e. I below the floor)
static uint32_t sma_seq[6] = {0, 0, 0, 0, 0, 0};   // per-src seq (idx 3,4,5)
static void emitSmaSample(uint8_t src, int32_t raw, float volts,
                          uint32_t hw, uint32_t ms) {
    Serial.print(ms);        Serial.print('\t');
    Serial.print((int)src);  Serial.print('\t');
    Serial.print(raw);       Serial.print('\t');
    Serial.print(volts, 6);  Serial.print('\t');
    Serial.print(hw);        Serial.print('\t');
    Serial.println(sma_seq[src]++);
}
static void streamSma(const SmaRead& s) {
    uint32_t hw = micros();
    uint32_t ms = millis();
    emitSmaSample(SAMPLE_SRC_SMA_V, (int32_t)currentCode, s.v_ldo, hw, ms);
    emitSmaSample(SAMPLE_SRC_SMA_I, 0,                    s.i,     hw, ms);
    if (!isnan(s.r)) emitSmaSample(SAMPLE_SRC_SMA_R, 0,   s.r,     hw, ms);
}

// ── Instant (non-state) commands ──────────────────────────────────────
static void cmdRead() {
    SmaRead s = readSma();
    smaTag();
    Serial.print(F("V_LDO=")); Serial.print(s.v_ldo, 4);
    Serial.print(F("V  I="));  Serial.print(s.i * 1000.0f, 2);
    Serial.print(F("mA  V_sma=")); Serial.print(s.v_sma, 4);
    Serial.print(F("V  R="));
    if (isnan(s.r)) Serial.print(F("--")); else Serial.print(s.r, 3);
    Serial.print(F("ohm  code=")); Serial.println(currentCode);
}

static void cmdInfo() {
    smaTag(); Serial.println(F("== SMA state (analytical model) =="));
    smaTag(); Serial.print(F("VDD_MCP (slope)   : ")); Serial.print(VDD_MCP, 3); Serial.println(F(" V"));
    smaTag(); Serial.print(F("V_OFFSET (intcpt) : ")); Serial.print(V_OFFSET, 4); Serial.println(F(" V"));
    smaTag(); Serial.print(F("V_LDO range       : ")); Serial.print(vldoMin(), 3);
        Serial.print(F(" - ")); Serial.print(vldoMax(), 3); Serial.println(F(" V"));
    smaTag(); Serial.print(F("DAC code          : ")); Serial.print(currentCode);
        Serial.print(F("  (V_pred=")); Serial.print(codeToVldo(currentCode), 3); Serial.println(F(" V)"));
    SmaRead s = readSma();
    smaTag(); Serial.print(F("I sense (INA296A) : ")); Serial.print(s.i * 1000.0f, 2);
        Serial.print(F(" mA  (gain=")); Serial.print(INA_GAIN, 1);
        Serial.print(F(" V/V, shunt=")); Serial.print(R_SHUNT_OHM * 1000.0f, 1);
        Serial.print(F(" mOhm, scale=")); Serial.print(INA_GAIN * R_SHUNT_OHM, 3); Serial.println(F(" V/A)"));
    smaTag(); Serial.print(F("V_sma / R_sma     : ")); Serial.print(s.v_sma, 3); Serial.print(F(" V  /  "));
        if (isnan(s.r)) Serial.println(F("-- ohm")); else { Serial.print(s.r, 3); Serial.println(F(" ohm")); }
    smaTag(); Serial.print(F("MOSFET            : ")); Serial.println(digitalRead(MOSFET_PIN) ? F("HIGH (load on)") : F("LOW (load off)"));
    smaTag(); Serial.print(F("state             : ")); Serial.println(smaBusy() ? F("BUSY") : F("IDLE"));
    bool cycling = (smaState == SMA_CYCLE_HIGH || smaState == SMA_CYCLE_LOW);
    smaTag(); Serial.print(F("cycle             : "));
    if (cycling) {
        Serial.print(smaState == SMA_CYCLE_HIGH ? F("HEAT") : F("COOL"));
        Serial.print(F(" n=")); Serial.print(cyc_n_done + 1);
        if (cyc_n_target) { Serial.print('/'); Serial.print(cyc_n_target); }
        Serial.print(F("  vh=")); Serial.print(cyc_v_high, 2);
        Serial.print(F(" vl=")); Serial.print(cyc_v_low, 2);
        Serial.print(F(" fire=")); Serial.print(cyc_fire_ms);
        Serial.print(F(" cool=")); Serial.println(cyc_cool_ms);
    } else {
        Serial.println(F("idle"));
    }
    smaTag(); Serial.print(F("watchdog          : "));
    if (wdt_timeout_ms) { Serial.print(wdt_timeout_ms); Serial.println(F(" ms (send 'ping')")); }
    else                  Serial.println(F("disabled"));
}

// ── State entry helpers (called from dispatch) ────────────────────────
static void startSet(float vtarget) {
    if (vtarget < 0 || vtarget > DRIVE_V_MAX) {
        smaTag(); Serial.print(F("ERR: V out of range [0, ")); Serial.print(DRIVE_V_MAX, 2); Serial.println(F("]"));
        return;
    }
    float lo = vldoMin(), hi = vldoMax(), vc = vtarget;
    if (vc < lo) vc = lo;
    if (vc > hi) vc = hi;
    if (vc != vtarget) {
        smaTag(); Serial.print(F("WARN: ")); Serial.print(vtarget, 3);
        Serial.print(F("V clamped to ")); Serial.print(vc, 3); Serial.println('V');
    }
    op_vtarget = vtarget;
    op_code    = vldoToCode(vc);
    setDACraw(op_code);
    settleBegin();
    smaState = SMA_SET_SETTLE;
}

static void startCode(uint16_t code) {
    op_code = code;
    setDACraw(code);
    settleBegin();
    smaState = SMA_CODE_SETTLE;
}

static void startDrive(float vtarget, uint32_t hold_ms) {
    if (vtarget < 0 || vtarget > DRIVE_V_MAX) {
        smaTag(); Serial.print(F("ERR: V out of range [0, ")); Serial.print(DRIVE_V_MAX, 2); Serial.println(F("]"));
        return;
    }
    if (hold_ms == 0 || hold_ms > DRIVE_MS_MAX) {
        smaTag(); Serial.print(F("ERR: hold_ms out of range (1, ")); Serial.print(DRIVE_MS_MAX); Serial.println(F("]"));
        return;
    }
    float vc = vtarget;
    if (vc < vldoMin()) vc = vldoMin();
    if (vc > vldoMax()) vc = vldoMax();
    op_code    = vldoToCode(vc);
    op_vtarget = vtarget;
    op_hold_ms = hold_ms;

    digitalWrite(MOSFET_PIN, HIGH);
    delay(2);
    op_t0 = millis();
    setDACraw(op_code);                 // raw write — log the rise transient

    smaTag(); Serial.print(F("[DRIVE] start V=")); Serial.print(vtarget, 3);
    Serial.print(F(" t_ms=")); Serial.print(hold_ms);
    Serial.println(F("  (feedback streamed as src=3 V / 4 I / 5 R)"));

    SmaRead s0 = readSma();
    streamSma(s0);
    op_max_err  = fabs(s0.v_ldo - vtarget);
    op_next_log = DRIVE_LOG_MS;
    smaState    = SMA_DRIVING;
}

static void startStep(uint16_t code, uint32_t ms) {
    op_hold_ms  = ms;
    op_next_log = 0;
    smaTag(); Serial.print(F("[STEP] code=")); Serial.print(code);
    Serial.print(F(" ms=")); Serial.println(ms);
    smaTag(); Serial.println(F("t_rel_ms\tV_meas"));
    op_t0 = millis();
    setDACraw(code);
    smaState = SMA_STEPPING;
}

static void startSweep(int codeStep, bool csv) {
    if (codeStep < 16)   codeStep = 16;
    if (codeStep > 2048) codeStep = 2048;
    sw_step = codeStep;
    sw_csv  = csv;
    sw_c    = 0;
    if (csv) { smaTag(); Serial.println(F("dac_code,v_pred,v_ldo_meas")); }
    else     { smaTag(); Serial.println(F("Code  V_pred  V_meas")); }
    setDACraw((uint16_t)(sw_c > 4095 ? 4095 : sw_c));
    settleBegin();
    smaState = SMA_SWEEP_SETTLE;
}

static void startFire(uint16_t code_to, uint32_t ms, uint16_t code_from) {
    fire_to   = code_to;
    fire_from = code_from;
    fire_ms   = ms;
    setDACraw(code_from);
    settleBegin();
    smaState = SMA_FIRE_SETTLE;
}

// ── Cyclic actuation (heat/cool profile, M7-timed) ────────────────────
// Enter the heating phase: V_high for cyc_fire_ms. TRIG rises at heating
// onset (scope sync); MOSFET stays ON for the whole cycle run.
static void cycleEnterHigh() {
    digitalWrite(TRIG_PIN, HIGH);
    setDACraw(vldoToCode(cyc_v_high));
    cyc_phase_t0 = millis();
    cyc_next_log = 0;
    smaState = SMA_CYCLE_HIGH;
    smaTag(); Serial.print(F("[CYCLE] heat n=")); Serial.print(cyc_n_done + 1);
    if (cyc_n_target) { Serial.print('/'); Serial.print(cyc_n_target); }
    Serial.print(F(" V=")); Serial.print(cyc_v_high, 3);
    Serial.print(F(" ms=")); Serial.println(cyc_fire_ms);
}
// Enter the cooling phase: V_low for cyc_cool_ms. TRIG falls.
static void cycleEnterLow() {
    digitalWrite(TRIG_PIN, LOW);
    setDACraw(vldoToCode(cyc_v_low));
    cyc_phase_t0 = millis();
    cyc_next_log = 0;
    smaState = SMA_CYCLE_LOW;
    smaTag(); Serial.print(F("[CYCLE] cool n=")); Serial.print(cyc_n_done + 1);
    if (cyc_n_target) { Serial.print('/'); Serial.print(cyc_n_target); }
    Serial.print(F(" V=")); Serial.print(cyc_v_low, 3);
    Serial.print(F(" ms=")); Serial.println(cyc_cool_ms);
}
// Stop cycling and return to a safe state (DAC 0, MOSFET off, TRIG low).
static void cycleStop(const __FlashStringHelper* reason) {
    setDACraw(0);
    digitalWrite(MOSFET_PIN, LOW);
    digitalWrite(TRIG_PIN, LOW);
    smaState = SMA_IDLE;
    smaTag(); Serial.print(F("[CYCLE] stop (")); Serial.print(reason);
    Serial.print(F(") after ")); Serial.print(cyc_n_done);
    Serial.println(F(" cycle(s) — safe state"));
}
static void startCycle(float v_high, float v_low,
                       uint32_t fire_ms_, uint32_t cool_ms_, uint32_t n) {
    cyc_v_high   = v_high;
    cyc_v_low    = v_low;
    cyc_fire_ms  = fire_ms_;
    cyc_cool_ms  = cool_ms_;
    cyc_n_target = n;
    cyc_n_done   = 0;
    wdt_last_ping = millis();        // arm the watchdog window
    digitalWrite(MOSFET_PIN, HIGH);  // load enabled for the whole run
    delay(2);
    smaTag(); Serial.print(F("[CYCLE] start v_high=")); Serial.print(v_high, 3);
    Serial.print(F(" v_low=")); Serial.print(v_low, 3);
    Serial.print(F(" fire_ms=")); Serial.print(fire_ms_);
    Serial.print(F(" cool_ms=")); Serial.print(cool_ms_);
    Serial.print(F(" n=")); Serial.print(n);
    Serial.print(F(" (0=continuous)  wdt_ms=")); Serial.println(wdt_timeout_ms);
    cycleEnterHigh();
}
// Watchdog: while cycling, abort to safe if no host `ping` within the
// timeout. Returns true if it tripped (caller should stop servicing).
static bool cycleWatchdogTripped() {
    if (wdt_timeout_ms == 0) return false;
    if ((uint32_t)(millis() - wdt_last_ping) > wdt_timeout_ms) {
        cycleStop(F("watchdog timeout — host silent"));
        return true;
    }
    return false;
}

// ── Per-pass service of the active op (one step, never blocks long) ────
static void serviceSma() {
    switch (smaState) {
        case SMA_IDLE:
            return;

        case SMA_SET_SETTLE:
            if (settleService()) {
                float vmeas = readLDO();
                float err   = vmeas - op_vtarget;
                smaTag();
                Serial.print(F("Target=")); Serial.print(op_vtarget, 3);
                Serial.print(F("V  Code=")); Serial.print(op_code);
                Serial.print(F("  V_pred=")); Serial.print(codeToVldo(op_code), 3);
                Serial.print(F("  V_LDO=")); Serial.print(vmeas, 3);
                Serial.print(F("V  err="));
                if (err >= 0) Serial.print('+');
                Serial.print(err * 1000.0f, 1); Serial.println(F("mV"));
                smaState = SMA_IDLE;
            }
            return;

        case SMA_CODE_SETTLE:
            if (settleService()) {
                float vldo = readLDO();
                smaTag();
                Serial.print(F("Code=")); Serial.print(op_code);
                Serial.print(F("  V_dac~")); Serial.print(codeToVdac(op_code), 3);
                Serial.print(F("  V_pred=")); Serial.print(codeToVldo(op_code), 3);
                Serial.print(F("  V_LDO_meas=")); Serial.print(vldo, 3);
                Serial.println('V');
                smaState = SMA_IDLE;
            }
            return;

        case SMA_DRIVING: {
            uint32_t t_rel = millis() - op_t0;
            if (t_rel >= op_hold_ms) {
                SmaRead sf = readSma();
                setDACraw(0);
                digitalWrite(MOSFET_PIN, LOW);
                streamSma(sf);
                smaTag(); Serial.print(F("[DRIVE] done V_final=")); Serial.print(sf.v_ldo, 4);
                Serial.print(F(" I_final=")); Serial.print(sf.i * 1000.0f, 2); Serial.print(F("mA"));
                Serial.print(F(" R_final="));
                if (isnan(sf.r)) Serial.print(F("--")); else Serial.print(sf.r, 3);
                Serial.print(F("ohm max_err=")); Serial.print(op_max_err * 1000.0f, 1); Serial.print(F("mV"));
                Serial.print(F(" elapsed_ms=")); Serial.println(t_rel);
                smaState = SMA_IDLE;
                return;
            }
            if (t_rel >= op_next_log) {
                SmaRead s = readSma();
                float e = fabs(s.v_ldo - op_vtarget);
                if (e > op_max_err) op_max_err = e;
                streamSma(s);
                op_next_log += DRIVE_LOG_MS;
            }
            return;
        }

        case SMA_STEPPING: {
            uint32_t t_rel = millis() - op_t0;
            if (t_rel >= op_hold_ms) {
                smaTag(); Serial.print(F("[STEP] done V_final=")); Serial.println(readLDO(), 4);
                smaState = SMA_IDLE;
                return;
            }
            if (t_rel >= op_next_log) {
                smaTag(); Serial.print(t_rel); Serial.print('\t'); Serial.println(readLDO(), 4);
                op_next_log += 10;
            }
            return;
        }

        case SMA_SWEEP_SETTLE:
            if (settleService()) {
                uint16_t code  = (uint16_t)(sw_c > 4095 ? 4095 : sw_c);
                float    vmeas = readLDO();
                float    vpred = codeToVldo(code);
                if (sw_csv) {
                    smaTag();
                    Serial.print(code);    Serial.print(',');
                    Serial.print(vpred, 4); Serial.print(',');
                    Serial.println(vmeas, 4);
                } else {
                    smaTag();
                    Serial.print(code);    Serial.print(F("  "));
                    Serial.print(vpred, 3); Serial.print(F("  "));
                    Serial.println(vmeas, 3);
                }
                sw_c += sw_step;
                if (sw_c > 4095) {
                    setDACraw(0);
                    smaTag(); Serial.println(F("[SWEEP] done"));
                    smaState = SMA_IDLE;
                } else {
                    setDACraw((uint16_t)(sw_c > 4095 ? 4095 : sw_c));
                    settleBegin();
                }
            }
            return;

        case SMA_FIRE_SETTLE:
            if (settleService()) {
                digitalWrite(TRIG_PIN, LOW);
                op_t0 = millis();
                smaState = SMA_FIRE_QUIET;
            }
            return;

        case SMA_FIRE_QUIET:
            if (millis() - op_t0 >= 20) {     // quiet pre-trigger baseline
                smaTag(); Serial.print(F("[FIRE] from=")); Serial.print(fire_from);
                Serial.print(F(" to=")); Serial.print(fire_to);
                Serial.print(F(" ms=")); Serial.print(fire_ms);
                Serial.print(F(" mosfet=")); Serial.println(digitalRead(MOSFET_PIN) ? F("on") : F("off"));
                // t0: trigger edge, then the DAC step (~few hundred us later).
                digitalWrite(TRIG_PIN, HIGH);
                delayMicroseconds(5);
                setDACraw(fire_to);
                op_t0 = millis();
                smaState = SMA_FIRE_HOLD;
            }
            return;

        case SMA_FIRE_HOLD:
            if (millis() - op_t0 >= fire_ms) {
                SmaRead sf = readSma();
                setDACraw(0);
                digitalWrite(TRIG_PIN, LOW);   // re-arm for next shot
                streamSma(sf);
                smaTag(); Serial.print(F("[FIRE] done V_final=")); Serial.print(sf.v_ldo, 4);
                Serial.print(F("V V_pred=")); Serial.print(codeToVldo(fire_to), 4);
                Serial.print(F("V I_final=")); Serial.print(sf.i * 1000.0f, 2);
                Serial.print(F("mA R_final="));
                if (isnan(sf.r)) Serial.print(F("--")); else Serial.print(sf.r, 3);
                Serial.println(F("ohm"));
                smaState = SMA_IDLE;
            }
            return;

        case SMA_CYCLE_HIGH: {
            if (cycleWatchdogTripped()) return;
            uint32_t t_rel = millis() - cyc_phase_t0;
            if (t_rel >= cyc_next_log) {            // stream V/I/R during heat
                streamSma(readSma());
                cyc_next_log += CYCLE_LOG_MS;
            }
            if (t_rel >= cyc_fire_ms) {
                cycleEnterLow();
            }
            return;
        }

        case SMA_CYCLE_LOW: {
            if (cycleWatchdogTripped()) return;
            uint32_t t_rel = millis() - cyc_phase_t0;
            if (t_rel >= cyc_next_log) {            // stream V/I/R during cool
                streamSma(readSma());
                cyc_next_log += CYCLE_LOG_MS;
            }
            if (t_rel >= cyc_cool_ms) {
                cyc_n_done++;
                if (cyc_n_target != 0 && cyc_n_done >= cyc_n_target) {
                    setDACraw(0);
                    digitalWrite(MOSFET_PIN, LOW);
                    digitalWrite(TRIG_PIN, LOW);
                    smaState = SMA_IDLE;
                    smaTag(); Serial.print(F("[CYCLE] done — ")); Serial.print(cyc_n_done);
                    Serial.println(F(" cycle(s) complete, safe state"));
                } else {
                    cycleEnterHigh();              // next cycle
                }
            }
            return;
        }
    }
}

// ──────────────────────────────────────────────────────────────────────
//  Non-blocking serial command reader + dispatcher
// ──────────────────────────────────────────────────────────────────────
static char    cmdBuf[96];
static uint8_t cmdLen = 0;

// Accumulate available chars; return true with a complete line on '\n'.
// Never blocks (replaces Serial.readStringUntil which blocks up to 1 s).
static bool pollCommand(String& out) {
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\r') continue;
        if (c == '\n') {
            cmdBuf[cmdLen] = '\0';
            out = String(cmdBuf);
            cmdLen = 0;
            return true;
        }
        if (cmdLen < sizeof(cmdBuf) - 1) cmdBuf[cmdLen++] = c;
        else cmdLen = 0;                 // overflow → drop the line
    }
    return false;
}

// Reject a motion command while another op is running.
static bool rejectIfBusy() {
    if (smaBusy()) {
        smaTag(); Serial.println(F("BUSY — send 'abort' to interrupt the running op"));
        return true;
    }
    return false;
}
// Reject a motion command if the DAC hardware is absent.
static bool rejectIfNoDac() {
    if (!sma_ok) {
        smaTag(); Serial.println(F("no DAC (MCP4728 absent) — SMA command ignored"));
        return true;
    }
    return false;
}

static void dispatch(String in) {
    in.trim();
    if (in.length() == 0) return;
    String low = in;
    low.toLowerCase();

    // ---- always-allowed (instant / safety / params) ----
    if (low == "info")  { cmdInfo(); return; }
    if (low == "read")  { cmdRead(); return; }
    if (low == "abort") { abortSma(); return; }
    if (low == "ping")  { wdt_last_ping = millis(); return; }   // heartbeat (silent)
    if (low == "stop") {                                        // graceful cycle stop
        if (smaState == SMA_CYCLE_HIGH || smaState == SMA_CYCLE_LOW)
            cycleStop(F("host stop"));
        else
            abortSma();
        return;
    }
    if (low.startsWith("wdt ")) {                               // heartbeat timeout
        long ms = in.substring(4).toInt();
        if (ms < 0 || ms > 600000) { smaTag(); Serial.println(F("Range: 0-600000 ms (0=off)")); return; }
        wdt_timeout_ms = (uint32_t)ms;
        wdt_last_ping = millis();
        smaTag(); Serial.print(F("wdt_timeout_ms=")); Serial.print(wdt_timeout_ms);
        Serial.println(wdt_timeout_ms ? F("") : F(" (watchdog DISABLED)"));
        return;
    }
    if (low == "reset" || low == "reboot") {
        digitalWrite(MOSFET_PIN, LOW);
        setDACraw(0);
        digitalWrite(TRIG_PIN, LOW);
        smaTag(); Serial.println(F("[RESET] rebooting MCU ..."));
        Serial.flush();
        delay(50);
        NVIC_SystemReset();
    }
    if (low.startsWith("mosfet ")) {
        String arg = low.substring(7); arg.trim();
        if      (arg == "on")  { digitalWrite(MOSFET_PIN, HIGH); smaTag(); Serial.println(F("MOSFET=HIGH (load on)")); }
        else if (arg == "off") { digitalWrite(MOSFET_PIN, LOW);  smaTag(); Serial.println(F("MOSFET=LOW (load off)")); }
        else { smaTag(); Serial.println(F("ERR: mosfet on|off")); }
        return;
    }
    if (low.startsWith("vdd ")) {
        float v = in.substring(4).toFloat();
        if (v < 2.7f || v > 5.5f) { smaTag(); Serial.println(F("Range: 2.7-5.5 V")); return; }
        VDD_MCP = v;
        smaTag(); Serial.print(F("VDD_MCP=")); Serial.print(VDD_MCP, 3);
        Serial.print(F(" V -> range ")); Serial.print(vldoMin(), 3);
        Serial.print(F(" - ")); Serial.print(vldoMax(), 3); Serial.println(F(" V"));
        return;
    }
    if (low.startsWith("offset ")) {
        float v = in.substring(7).toFloat();
        if (v < 0.0f || v > 1.0f) { smaTag(); Serial.println(F("Range: 0.0-1.0 V")); return; }
        V_OFFSET = v;
        smaTag(); Serial.print(F("V_OFFSET=")); Serial.print(V_OFFSET, 4); Serial.println(F(" V"));
        return;
    }
    if (low.startsWith("aref ")) {
        float r = in.substring(5).toFloat();
        if (r < 2.8f || r > 3.4f) { smaTag(); Serial.println(F("Range: 2.8-3.4 V")); return; }
        ADC_VREF_V = r;
        smaTag(); Serial.print(F("ADC_VREF_V=")); Serial.print(ADC_VREF_V, 4); Serial.println(F(" V"));
        return;
    }
    if (low.startsWith("gain ")) {                 // INA296A gain (V/V)
        float g = in.substring(5).toFloat();
        if (g < 1.0f || g > 1000.0f) { smaTag(); Serial.println(F("Range: 1-1000 V/V")); return; }
        INA_GAIN = g;
        smaTag(); Serial.print(F("INA_GAIN=")); Serial.print(INA_GAIN, 1);
        Serial.print(F(" V/V -> I scale ")); Serial.print(INA_GAIN * R_SHUNT_OHM, 3); Serial.println(F(" V/A"));
        return;
    }
    if (low.startsWith("shunt ")) {                // shunt resistance (ohm)
        float r = in.substring(6).toFloat();
        if (r <= 0.0f || r > 10.0f) { smaTag(); Serial.println(F("Range: >0 - 10 ohm")); return; }
        R_SHUNT_OHM = r;
        smaTag(); Serial.print(F("R_SHUNT=")); Serial.print(R_SHUNT_OHM * 1000.0f, 1);
        Serial.print(F(" mOhm -> I scale ")); Serial.print(INA_GAIN * R_SHUNT_OHM, 3); Serial.println(F(" V/A"));
        return;
    }
    if (low.startsWith("ioffset ")) {              // INA296A 0 A output (V)
        float v = in.substring(8).toFloat();
        if (v < -1.0f || v > 3.3f) { smaTag(); Serial.println(F("Range: -1.0 - 3.3 V")); return; }
        ISENSE_OFFSET_V = v;
        smaTag(); Serial.print(F("ISENSE_OFFSET=")); Serial.print(ISENSE_OFFSET_V, 4); Serial.println(F(" V"));
        return;
    }

    // ---- motion commands (need DAC + an idle machine) ----
    if (low.startsWith("code ")) {
        if (rejectIfNoDac() || rejectIfBusy()) return;
        int c = in.substring(5).toInt();
        if (c < 0 || c > 4095) { smaTag(); Serial.println(F("Range: 0-4095")); return; }
        startCode((uint16_t)c);
        return;
    }
    if (low == "sweep")          { if (rejectIfNoDac() || rejectIfBusy()) return; startSweep(128, false); return; }
    if (low == "csv")            { if (rejectIfNoDac() || rejectIfBusy()) return; startSweep(128, true);  return; }
    if (low.startsWith("sweep ")){ if (rejectIfNoDac() || rejectIfBusy()) return; startSweep(in.substring(6).toInt(), false); return; }
    if (low.startsWith("csv "))  { if (rejectIfNoDac() || rejectIfBusy()) return; startSweep(in.substring(4).toInt(), true);  return; }
    if (low.startsWith("step ")) {
        if (rejectIfNoDac() || rejectIfBusy()) return;
        String rest = in.substring(5); rest.trim();
        int sp = rest.indexOf(' ');
        uint16_t c; uint32_t ms = 1200;
        if (sp > 0) { c = (uint16_t)rest.substring(0, sp).toInt(); ms = (uint32_t)rest.substring(sp + 1).toInt(); }
        else        { c = (uint16_t)rest.toInt(); }
        if (c > 4095) c = 4095;
        if (ms == 0 || ms > 10000) ms = 1200;
        startStep(c, ms);
        return;
    }
    if (low.startsWith("fire ")) {
        if (rejectIfNoDac() || rejectIfBusy()) return;
        String rest = in.substring(5); rest.trim();
        int s1 = rest.indexOf(' ');
        uint16_t code_to, code_from = 0; uint32_t ms = 500;
        if (s1 < 0) { code_to = (uint16_t)rest.toInt(); }
        else {
            code_to = (uint16_t)rest.substring(0, s1).toInt();
            String r2 = rest.substring(s1 + 1); r2.trim();
            int s2 = r2.indexOf(' ');
            if (s2 < 0) { ms = (uint32_t)r2.toInt(); }
            else { ms = (uint32_t)r2.substring(0, s2).toInt(); code_from = (uint16_t)r2.substring(s2 + 1).toInt(); }
        }
        if (code_to   > 4095) code_to   = 4095;
        if (code_from > 4095) code_from = 4095;
        if (ms == 0 || ms > 10000) ms = 500;
        startFire(code_to, ms, code_from);
        return;
    }
    if (low.startsWith("drive ")) {
        if (rejectIfNoDac() || rejectIfBusy()) return;
        String rest = in.substring(6); rest.trim();
        int sp = rest.indexOf(' ');
        if (sp <= 0) { smaTag(); Serial.println(F("Usage: drive <V> <ms>")); return; }
        float vt = rest.substring(0, sp).toFloat();
        long ms  = rest.substring(sp + 1).toInt();
        if (ms <= 0) { smaTag(); Serial.println(F("Usage: drive <V> <ms>")); return; }
        startDrive(vt, (uint32_t)ms);
        return;
    }
    if (low.startsWith("cycle ")) {
        // cycle <v_high> <v_low> <fire_ms> <cool_ms> <n>   (n=0 → continuous)
        // The M7-timed experiment state machine. Send `ping` periodically
        // (heartbeat) and `stop` to end early.
        if (rejectIfNoDac() || rejectIfBusy()) return;
        String rest = in.substring(6); rest.trim();
        float   args_f[2]; uint32_t args_u[3];
        int idx = 0; bool ok = true;
        // 5 whitespace-separated fields: vh vl fire cool n
        for (int field = 0; field < 5; field++) {
            rest.trim();
            if (rest.length() == 0) { ok = false; break; }
            int sp = rest.indexOf(' ');
            String tok = (sp < 0) ? rest : rest.substring(0, sp);
            rest = (sp < 0) ? "" : rest.substring(sp + 1);
            if (field < 2) args_f[field] = tok.toFloat();
            else           args_u[field - 2] = (uint32_t)tok.toInt();
            idx++;
        }
        if (!ok || idx < 5) {
            smaTag(); Serial.println(F("Usage: cycle <v_high> <v_low> <fire_ms> <cool_ms> <n>"));
            return;
        }
        float vh = args_f[0], vl = args_f[1];
        uint32_t fire_ms_ = args_u[0], cool_ms_ = args_u[1], n = args_u[2];
        if (vh < 0 || vh > DRIVE_V_MAX || vl < 0 || vl > DRIVE_V_MAX) {
            smaTag(); Serial.print(F("ERR: voltages out of range [0, "));
            Serial.print(DRIVE_V_MAX, 2); Serial.println(F("]")); return;
        }
        if (fire_ms_ == 0 || fire_ms_ > CYCLE_MS_MAX ||
            cool_ms_ == 0 || cool_ms_ > CYCLE_MS_MAX) {
            smaTag(); Serial.print(F("ERR: phase ms out of range (1, "));
            Serial.print(CYCLE_MS_MAX); Serial.println(F("]")); return;
        }
        startCycle(vh, vl, fire_ms_, cool_ms_, n);
        return;
    }

    // ---- set <V> / bare-number shortcut ----
    if (rejectIfNoDac() || rejectIfBusy()) return;
    float target;
    if (low.startsWith("set ")) {
        target = in.substring(4).toFloat();
    } else {
        target = in.toFloat();
        if (target == 0.0f && in[0] != '0') { smaTag(); Serial.print(F("? ")); Serial.println(in); return; }
    }
    startSet(target);
}


// ══════════════════════════════════════════════════════════════════════
//  M7 ring→USB bridge  (pumpSensors: drain RPC + ring, emit [STATUS])
// ══════════════════════════════════════════════════════════════════════
static uint32_t last_status_ms  = 0;
static uint32_t last_dropped    = 0;
static uint32_t pop_count_src1  = 0;
static uint32_t pop_count_src2  = 0;
static uint32_t pop_count_other = 0;
static uint32_t last_seq_src1   = 0;
static uint32_t last_seq_src2   = 0;
static const uint32_t STATUS_PERIOD_MS = 1000;

// Drain boot text, drain a batch of sensor samples, emit periodic [STATUS].
// Called every loop() pass AND it is the only sensor-path work on M7, so a
// fast loop() (state machine never blocks long) keeps the stream alive.
static void pumpSensors() {
    // 1. Boot / diagnostic text from M4 via RPC.
    while (RPC.available()) Serial.write(RPC.read());

    // 2. ADC samples from the shared ring buffer (untagged sensor TSV).
    AdcSample s;
    int batch = 0;
    while (ring_pop(SAMPLE_RING, s) && batch < 64) {
        Serial.print(s.timestamp_ms);
        Serial.print('\t');
#if ENABLE_ADC1 && ENABLE_ADC2
        Serial.print((int)s.src);
        Serial.print('\t');
#endif
        Serial.print(s.raw_code);
        Serial.print('\t');
        Serial.print(s.voltage_V, 6);
        Serial.print('\t');
        Serial.print(s.hw_us);
        Serial.print('\t');
        Serial.println(s.seq);

        if      (s.src == SAMPLE_SRC_LASER) pop_count_src1++;
        else if (s.src == SAMPLE_SRC_LOAD)  pop_count_src2++;
        else                                pop_count_other++;
        batch++;
    }

    // 3. Periodic [STATUS] telemetry frame (every 1 s).
    uint32_t now = millis();
    if (now - last_status_ms >= STATUS_PERIOD_MS) {
        uint32_t dt_ms = now - last_status_ms;
        last_status_ms = now;

        uint32_t cur_seq1 = SAMPLE_RING->seq_per_src[SAMPLE_SRC_LASER];
        uint32_t cur_seq2 = SAMPLE_RING->seq_per_src[SAMPLE_SRC_LOAD];
        uint32_t prod1 = cur_seq1 - last_seq_src1;
        uint32_t prod2 = cur_seq2 - last_seq_src2;
        last_seq_src1 = cur_seq1;
        last_seq_src2 = cur_seq2;

        uint32_t cur_dropped = SAMPLE_RING->dropped;
        uint32_t dropped_delta = cur_dropped - last_dropped;
        last_dropped = cur_dropped;

        uint32_t hwm = ring_hwm_read_reset(SAMPLE_RING);

        auto per_s = [&](uint32_t n) -> uint32_t {
            return (uint32_t)(((uint64_t)n * 1000UL) / (dt_ms ? dt_ms : 1));
        };
        uint32_t rate1 = per_s(pop_count_src1);
        uint32_t rate2 = per_s(pop_count_src2);
        uint32_t rate_other = per_s(pop_count_other);
        uint32_t prate1 = per_s(prod1);
        uint32_t prate2 = per_s(prod2);
        pop_count_src1 = 0;
        pop_count_src2 = 0;
        pop_count_other = 0;

        uint32_t m4_loops = SAMPLE_RING->seq_per_src[0];
        SAMPLE_RING->seq_per_src[0] = 0;
        uint32_t m4_loops_per_s = per_s(m4_loops);

        Serial.print("[STATUS] t_ms=");   Serial.print(now);
        Serial.print(" hwm=");            Serial.print(hwm);
        Serial.print(" cap=");            Serial.print(RING_CAPACITY);
        Serial.print(" dropped=");        Serial.print(dropped_delta);
        Serial.print(" dropped_total=");  Serial.print(cur_dropped);
        Serial.print(" rate1=");          Serial.print(rate1);
        Serial.print(" rate2=");          Serial.print(rate2);
        if (rate_other) { Serial.print(" rate_other="); Serial.print(rate_other); }
        Serial.print(" prod1=");          Serial.print(prate1);
        Serial.print(" prod2=");          Serial.print(prate2);
        Serial.print(" sma_state=");      Serial.print((int)smaState);   // 0=IDLE
        Serial.print(" m4_loops_per_s="); Serial.print(m4_loops_per_s);
        Serial.println();
    }
}

void setup() {
    // Zero the ring BEFORE booting M4 (RPC.begin() starts the M4 core).
    SAMPLE_RING->write_idx = 0;
    SAMPLE_RING->read_idx  = 0;
    SAMPLE_RING->dropped   = 0;
    SAMPLE_RING->hwm       = 0;
    for (int i = 0; i < 8; i++) SAMPLE_RING->seq_per_src[i] = 0;
    __DMB();

    Serial.begin(115200);
    uint32_t t0 = millis();
    while (!Serial && (millis() - t0) < 2000) {}

    RPC.begin();     // boots M4 core
    Serial.println("[M7] Firmware_SMASensorHub_PIO — ring-buffer bridge + SMA controller");
    Serial.println("[M7] sensor TSV: t_ms\\t[src\\t]raw\\tV\\thw_us\\tseq   (src=1 laser, 2 load)");
    Serial.println("[M7] [STATUS] line once/sec; SMA I/O tagged [SMA]");

    // ── SMA drive-path init (non-fatal: a missing DAC must NOT kill the
    //    sensor bridge — the rig can still stream laser/load) ──────────
    pinMode(MOSFET_PIN, OUTPUT);
    digitalWrite(MOSFET_PIN, LOW);          // load OFF until operator enables
    pinMode(TRIG_PIN, OUTPUT);
    digitalWrite(TRIG_PIN, LOW);            // scope trigger idle LOW

    analogReadResolution(ADC_RES_BITS);
    for (int i = 0; i < 10; i++) analogRead(FB_PIN);
    for (int i = 0; i < 10; i++) analogRead(ISENSE_PIN);   // prime INA296A input (A1)

    Wire.begin();
    smaTag(); Serial.println(F("I2C scan..."));
    byte cnt = 0;
    for (byte a = 1; a < 127; a++) {
        Wire.beginTransmission(a);
        if (Wire.endTransmission() == 0) {
            smaTag(); Serial.print(F("  0x"));
            if (a < 16) Serial.print('0');
            Serial.println(a, HEX);
            cnt++;
        }
    }
    smaTag(); Serial.print(cnt); Serial.println(F(" I2C device(s)"));

    if (mcp.begin(0x60)) {
        sma_ok = true;
        setDACraw(0);                       // park DAC at 0 (safe)
        smaTag(); Serial.print(F("MCP4728 OK. Model: V_LDO = ")); Serial.print(V_OFFSET, 3);
        Serial.print(F(" + (")); Serial.print(VDD_MCP, 3);
        Serial.print(F("/4095)*code  range ")); Serial.print(vldoMin(), 3);
        Serial.print(F(" - ")); Serial.print(vldoMax(), 3); Serial.println(F(" V"));
    } else {
        sma_ok = false;
        smaTag(); Serial.println(F("MCP4728 NOT found at 0x60 — SMA disabled, sensor bridge still active."));
    }
    smaTag(); Serial.println(F("cmds: set <V> | code <N> | drive <V> <ms> | fire <code>[ms][from] |"));
    smaTag(); Serial.println(F("      cycle <v_high> <v_low> <fire_ms> <cool_ms> <n>  (n=0 continuous) |"));
    smaTag(); Serial.println(F("      ping (heartbeat) | stop | wdt <ms> (0=off) | abort |"));
    smaTag(); Serial.println(F("      step <code>[ms] | sweep|csv [step] | read | info | mosfet on|off |"));
    smaTag(); Serial.println(F("      gain|shunt|ioffset|vdd|offset|aref <x> | reset"));
}

void loop() {
    pumpSensors();                  // keep the sensor stream flowing (every pass)
    String line;
    if (pollCommand(line)) dispatch(line);
    serviceSma();                   // advance the active SMA op by one step
}


// ══════════════════════════════════════════════════════════════════════
//  M4 CORE — drive the ADS1263 (both ADCs) → shared-memory ring buffer
//            (IDENTICAL to Firmware_SensorHub_PIO; -D M4_IDLE → idle stub)
// ══════════════════════════════════════════════════════════════════════
#elif defined(CORE_CM4)

#ifdef M4_IDLE
// Bring-up only: do-nothing M4 so the M7 SMA path can be tested with no
// SPI traffic / ring writes. Flashed by [env:portenta_m4_idle].
void setup() {}
void loop()  { __WFI(); }

#else

#include <SPI.h>
#include "ADS1263_Driver.h"

ADS1263_Driver adc;

#define CP(n, msg)  do { \
    RPC.print("[M4 cp "); RPC.print(n); RPC.print("] "); RPC.println(msg); \
} while (0)

static volatile uint32_t drdy_us_latest  = 0;
static volatile uint32_t drdy_edge_count = 0;
static volatile uint32_t drdy_serviced   = 0;
static volatile bool     adc1_pending    = false;
static uint32_t          m4_loop_counter = 0;
static volatile uint32_t drdy_overrun_count = 0;

static void drdy_isr() {
    uint32_t t = micros();
    drdy_us_latest = t;
    drdy_edge_count++;
    if (adc1_pending) drdy_overrun_count++;
    adc1_pending = true;
}

void setup() {
    RPC.begin();
    delay(500);
    CP(0, "RPC up");

    Serial.begin(115200);
    CP(1, "Serial.begin done");

    RPC.println("[M4] *** Firmware_SMASensorHub_PIO — dual-ADC production stream ***");
    RPC.println("[M4]   ADC1 → AIN4/AIN5 (Keyence IL-030 laser)");
    RPC.println("[M4]   ADC2 → AIN2/AIN3 (LCA-9PC load cell)");
    RPC.println("[M4] IPC: shared-memory ring buffer (sample_ring.h, 24-byte slot)");
    RPC.println("[M4] sampling: ADC1 DRDY-ISR on PC_6; ADC2 piggy-back on STATUS.ADC2_NEW");

    RPC.println("[M4] waiting 3000 ms for ADS1263 to power up...");
    delay(3000);
    RPC.println("[M4] ADS1263 power-up settle done");

    pinMode(ADS1263_CS_PIN, OUTPUT);
    CP(2, "pinMode CS (PA_8 / J15-25 / PWM_0) done");
    pinMode(ADS1263_RESET_PIN, OUTPUT);
    CP(3, "pinMode RESET (PC_7 / J15-29 / PWM_2) done");
    pinMode(ADS1263_DRDY_PIN, INPUT_PULLUP);
    CP(4, "pinMode DRDY (PC_6 / J15-27 / PWM_1) done");

    digitalWrite(ADS1263_CS_PIN, HIGH);
    digitalWrite(ADS1263_RESET_PIN, HIGH);
    CP(5, "CS and RESET driven HIGH");

    SPI.begin();
    CP(6, "SPI.begin() returned");

    CP(7, "calling adc.begin()");
    bool ok = adc.begin();
    CP(8, ok ? "adc.begin returned TRUE" : "adc.begin returned FALSE");

    if (!ok) {
        RPC.println("[M4] FATAL: ADS1263 init failed");
        while (1) { delay(1000); }
    }

    RPC.print("[M4] ADC ready, ID=0x");
    RPC.println(adc.getDeviceID(), HEX);

#if ENABLE_ADC1
    adc.configureADC1(
        /*inpmux     =*/ 0x45,                       // AIN4(+) / AIN5(-) — laser
        /*refmux     =*/ ADS1263_REFMUX_EXT_AIN01,   // 0x09 — REF7050 on AIN0/AIN1
        /*vref_V     =*/ 5.0f,
        /*rate       =*/ ADS1263_400SPS,
        /*pga_bypass =*/ false                       // PGA in path, gain=1
    );
    adc.startADC1();
    CP(9, "ADC1 started on AIN4/AIN5 (laser), REF7050 on AIN0/AIN1, PGA in path gain=1");
#endif

#if ENABLE_ADC2
    adc.configureADC2(
        /*adc2mux =*/ 0x23,                           // AIN2(+) / AIN3(-) — load cell
        /*ref2    =*/ ADS1263_ADC2_REF_AIN01,         // external REF7050 on AIN0/AIN1
        /*vref_V  =*/ 5.0f,
        /*rate    =*/ ADS1263_ADC2_400SPS,
        /*gain    =*/ ADS1263_ADC2_GAIN_1
    );
    adc.startADC2();
    CP(10, "ADC2 started on AIN2/AIN3 (load), REF7050 shared with ADC1, 400 SPS gain=1");
#endif

    delay(100);
    adc.printConfig();

    attachInterrupt(digitalPinToInterrupt(ADS1263_DRDY_PIN), drdy_isr, FALLING);
    CP(11, "DRDY interrupt attached on PC_6 (FALLING)");

#if ENABLE_ADC1 && ENABLE_ADC2
    RPC.println("[M4] streaming via ring buffer. format: t_ms\\tsrc\\traw\\tV\\thw_us\\tseq   (src=1 laser, src=2 load)");
#elif ENABLE_ADC1
    RPC.println("[M4] streaming via ring buffer. format: t_ms\\traw\\tV\\thw_us\\tseq   (ADC1/laser only)");
#elif ENABLE_ADC2
    RPC.println("[M4] streaming via ring buffer. format: t_ms\\traw\\tV\\thw_us\\tseq   (ADC2/load only)");
#else
    #error "Neither ENABLE_ADC1 nor ENABLE_ADC2 is set — nothing to do."
#endif
}

void loop() {
    m4_loop_counter++;
    SAMPLE_RING->seq_per_src[0] = m4_loop_counter;

#if ENABLE_ADC1
    if (adc1_pending) {
        noInterrupts();
        uint32_t hw_us = drdy_us_latest;
        adc1_pending   = false;
        drdy_serviced++;
        interrupts();

        uint32_t ts_ms = millis();

        ADC_Reading r1 = adc.readADC1Direct();
        if (r1.valid) {
            ring_push(SAMPLE_RING, hw_us, ts_ms, SAMPLE_SRC_LASER, r1.raw_code, r1.voltage_V);
        }

#if ENABLE_ADC2
        if (r1.status & 0x80) {
            ADC_Reading r2 = adc.readADC2Direct();
            if (r2.valid) {
                ring_push(SAMPLE_RING, hw_us, ts_ms, SAMPLE_SRC_LOAD, r2.raw_code, r2.voltage_V);
            }
        }
#endif
    }
#else
#if ENABLE_ADC2
    static uint32_t t2_last = 0;
    if (millis() - t2_last >= 2) {
        t2_last = millis();
        uint32_t hw_us = micros();
        ADC_Reading r = adc.readADC2Direct();
        if (r.valid) {
            ring_push(SAMPLE_RING, hw_us, millis(), SAMPLE_SRC_LOAD, r.raw_code, r.voltage_V);
        }
    }
#endif
#endif
}

#endif // M4_IDLE

#else
  #error "Unknown core — build with CORE_CM7 or CORE_CM4"
#endif
