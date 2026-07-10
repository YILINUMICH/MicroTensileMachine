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
#define ENABLE_ADC1   1   // AIN4/AIN5 — Keyence IL-030 laser (displacement)
#define ENABLE_ADC2   1   // AIN2/AIN3 — LCA-9PC load cell (force)


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
//  loop() services ONE step of the active op per pass. A step is short but
//  NOT free: a feedback step calls readSma() = 2x readADC (~2-4 ms incl.
//  readADC's delay(1) + 64-sample average), and setDACraw() adds delay(2).
//  So the loop can stall a few ms per pass during an active SMA op — the
//  ring (~1.28 s) absorbs it with no sample loss, but the sensor stream gets
//  bursty. pumpSensors() drains the ring every pass, so data keeps flowing
//  DURING an SMA drive and `abort` can interrupt a live op.
// ══════════════════════════════════════════════════════════════════════

enum SmaState {
    SMA_IDLE,
    // characterization — set a voltage, settle, report (typically disarmed)
    SMA_SET_SETTLE,    // set <V> / bare number
    SMA_CODE_SETTLE,   // code <N>
    SMA_STEPPING,      // step <code> <ms> : log settle transient
    SMA_SWEEP_SETTLE,  // sweep / csv      : settle each code, print
    // actuation — ONE heat/cool engine; drive/fire/cycle are presets of it
    SMA_ACT_HEAT,      // hold v_high for t_high (TRIG high)
    SMA_ACT_COOL       // hold v_idle for t_idle (TRIG low)
};
static SmaState smaState = SMA_IDLE;

// Characterization op context (set/code/step — one runs at a time).
static uint32_t op_t0;          // phase start (millis)
static uint32_t op_hold_ms;     // step hold duration
static uint16_t op_code;        // target code
static float    op_vtarget;     // set logging
static uint32_t op_next_log;    // next feedback-log time (rel ms)

// Non-blocking settle detector (replaces the blocking settleWait()).
static float    st_prev;
static int      st_quiet;
static uint32_t st_t0;
static uint32_t st_next;        // next LDO read time (abs ms)

// Sweep context.
static long     sw_c;
static int      sw_step;
static bool     sw_csv;

// ── Arming: the MOSFET (low-side return) is the master enable. Current
//    only flows when armed; the idle-low voltage is the cooling/rest level
//    (the LDO can't reach 0 V, so MOSFET-off is the only true zero-current
//    state). on/off of the MOSFET = arm/disarm. ───────────────────────────
static bool     armed  = false;
static float    V_IDLE = 0.5f;   // idle / cool / rest level (tunable: `idle <V>`)

// ── Actuation engine — drive/fire/cycle ALL run through this ───────────
// One run = repeat[ HEAT at v_high for t_high, COOL at v_idle for t_idle ]
// n times (n=0 = continuous). M7-timed (millis()) → deterministic; the host
// sets params once and sends `ping` heartbeats. cyc_fire adds the scope-
// trigger clean pre-step edge (the `fire` preset).
static float    cyc_v_high   = 0.0f;
static float    cyc_v_low    = 0.0f;   // == idle/cool level for this run
static uint32_t cyc_fire_ms  = 0;      // heat duration (t_high)
static uint32_t cyc_cool_ms  = 0;      // cool duration (t_idle)
static uint32_t cyc_n_target = 0;      // 0 = continuous until stop/disarm
static uint32_t cyc_n_done   = 0;      // completed cycles so far
static uint32_t cyc_phase_t0 = 0;      // current phase start (millis)
static uint32_t cyc_next_log = 0;      // next src=3/4/5 stream time (rel ms)
static bool     cyc_fire     = false;  // fire preset: scope trigger + clean edge
static const uint32_t CYCLE_LOG_MS = 10;
static const uint32_t CYCLE_MS_MAX = 600000;   // 10 min per phase ceiling

// While ARMED and resting at idle (SMA_IDLE), stream V/I/R at this period so
// the host has a live readout of the idle-hold (idle current makes R readable
// without any drive/heat). No streaming when disarmed (no current flows).
static const uint32_t IDLE_LOG_MS  = 100;      // ~10 Hz idle telemetry
static uint32_t       idle_next_log = 0;

// ── Heat watchdog (max_heat = wdt) ────────────────────────────────────
// Only while HEATing: if no `ping` arrives within wdt_timeout_ms, drop to
// idle-low (STILL ARMED → relaunch-able). `wdt 0` disables (manual bench).
// COOL is unguarded — the idle-low level is self-cooling, hence safe.
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
static void abortSma() {        // hard stop = disarm (open the return path)
    armed = false;
    digitalWrite(MOSFET_PIN, LOW);
    setDACraw(0);
    digitalWrite(TRIG_PIN, LOW);
    smaState = SMA_IDLE;
    smaTag(); Serial.println(F("[ABORT] disarmed (MOSFET off, DAC idle)"));
}

// ── Unified-stream SMA feedback (src=3 V, src=4 I, src=5 R) ────────────
// Emitted as UNTAGGED sensor-TSV lines (same 6-column format as the M4
// laser/load samples) so the host logs SMA feedback time-aligned with the
// sensor streams. M7 is the sole USB writer, so this needs NO ring
// producer — the M4-owned SPSC ring is untouched.
//
// CLOCK: t_ms / hw_us on these lines are stamped with M4's LIVE clock (read
// from the ring header), NOT M7's, so src=3/4/5 share one timeline with the
// src=1/2 sensor lines. M7's own clock and M4's are both emitted once per
// second in [STATUS] so the host can verify the alignment independently.
//
//   src=3 (SMA drive V) : raw = DAC code (currentCode), voltage = V_ldo
//   src=4 (SMA current)  : raw = 0,                      voltage = I [A]
//   src=5 (SMA R = V/I)   : raw = 0,                      voltage = R [ohm]
//                           (omitted when R is NaN, i.e. I below the floor)
// The SMA feedback lines (src=3/4/5) share the 6-column sensor TSV format and
// NEED the src column to tell V/I/R apart. M4's sensor lines only emit the src
// column when both ADCs are enabled, so the combined stream is self-consistent
// only with both on — enforce that here rather than emit ambiguous lines.
static_assert(ENABLE_ADC1 && ENABLE_ADC2,
              "SMA src=3/4/5 streaming needs both ADCs enabled (src column present)");

// sma_seq is indexed directly by src (3,4,5); indices 0-2 are unused so the
// [src] lookup needs no offset.
static uint32_t sma_seq[6] = {0, 0, 0, 0, 0, 0};   // per-src seq, idx by src

// Freshest M4 timestamps M7 has drained from the ring (updated in pumpSensors).
// Lets M7 stamp the SMA src=3/4/5 lines on the M4 timeline without M4 having to
// publish a separate clock (M4 stays a pure producer).
static uint32_t last_m4_hw_us = 0;   // newest sample hw_us (M4 micros())
static uint32_t last_m4_ms    = 0;   // newest sample timestamp_ms (M4 millis())
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
    // Stamp these M7-produced SMA lines with the M4 timeline so src=3/4/5 share
    // one clock with the src=1/2 sensor lines. M4 is a pure producer (it no
    // longer publishes a live clock into the ring header), so we use the
    // timestamps of the freshest sample M7 drained from the ring — captured in
    // pumpSensors() as last_m4_hw_us / last_m4_ms. At 500 SPS that's within
    // ~2 ms of "now", plenty for correlating actuation with sensor response.
    uint32_t hw = last_m4_hw_us;
    uint32_t ms = last_m4_ms;
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
    smaTag(); Serial.print(F("armed (MOSFET)    : ")); Serial.println(armed ? F("YES (return closed)") : F("no (disarmed)"));
    smaTag(); Serial.print(F("idle level        : ")); Serial.print(V_IDLE, 3); Serial.println(F(" V"));
    smaTag(); Serial.print(F("state             : ")); Serial.println(smaBusy() ? F("BUSY") : F("IDLE"));
    bool acting = (smaState == SMA_ACT_HEAT || smaState == SMA_ACT_COOL);
    smaTag(); Serial.print(F("actuation         : "));
    if (acting) {
        Serial.print(smaState == SMA_ACT_HEAT ? F("HEAT") : F("COOL"));
        Serial.print(F(" n=")); Serial.print(cyc_n_done + 1);
        if (cyc_n_target) { Serial.print('/'); Serial.print(cyc_n_target); }
        Serial.print(F("  vh=")); Serial.print(cyc_v_high, 2);
        Serial.print(F(" v_idle=")); Serial.print(cyc_v_low, 2);
        Serial.print(F(" t_high=")); Serial.print(cyc_fire_ms);
        Serial.print(F(" t_idle=")); Serial.println(cyc_cool_ms);
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

// ── Arm / set-level / disarm: the ONLY owners of the MOSFET ────────────
// MOSFET (low-side return) = master enable. setLevel only moves the DAC
// (voltage modulation, clamped to the LDO range); the idle-low level is the
// cooling/rest state. Current flows only while armed.
static void arm() { armed = true; digitalWrite(MOSFET_PIN, HIGH); }
static void setLevel(float v) {
    if (v < vldoMin()) v = vldoMin();
    if (v > vldoMax()) v = vldoMax();
    setDACraw(vldoToCode(v));
}
static void disarm() {                    // hard cutoff: open the return path
    armed = false;
    digitalWrite(MOSFET_PIN, LOW);
    setLevel(V_IDLE);
    digitalWrite(TRIG_PIN, LOW);
    smaState = SMA_IDLE;
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

// ── Actuation engine helpers (drive/fire/cycle all use these) ──────────
// HEAT: v_high for t_high, TRIG high (scope sync). Assumes already armed.
static void cycleEnterHigh() {
    digitalWrite(TRIG_PIN, HIGH);
    if (cyc_fire) delayMicroseconds(5);     // clean pre-step edge for the scope
    setLevel(cyc_v_high);
    cyc_phase_t0 = millis();
    cyc_next_log = 0;
    smaState = SMA_ACT_HEAT;
    smaTag(); Serial.print(F("[ACT] heat n=")); Serial.print(cyc_n_done + 1);
    if (cyc_n_target) { Serial.print('/'); Serial.print(cyc_n_target); }
    Serial.print(F(" V=")); Serial.print(cyc_v_high, 3);
    Serial.print(F(" ms=")); Serial.println(cyc_fire_ms);
}
// COOL: v_idle for t_idle, TRIG low.
static void cycleEnterLow() {
    digitalWrite(TRIG_PIN, LOW);
    setLevel(cyc_v_low);
    cyc_phase_t0 = millis();
    cyc_next_log = 0;
    smaState = SMA_ACT_COOL;
    smaTag(); Serial.print(F("[ACT] cool n=")); Serial.print(cyc_n_done + 1);
    if (cyc_n_target) { Serial.print('/'); Serial.print(cyc_n_target); }
    Serial.print(F(" V=")); Serial.print(cyc_v_low, 3);
    Serial.print(F(" ms=")); Serial.println(cyc_cool_ms);
}
// End the run → idle-low voltage, STILL ARMED (relaunch-able). The only
// hard cutoff is `disarm`/`abort`. Used by `stop`, the heat watchdog, run end.
static void cycleStop(const __FlashStringHelper* reason) {
    digitalWrite(TRIG_PIN, LOW);
    setLevel(V_IDLE);
    smaState = SMA_IDLE;
    smaTag(); Serial.print(F("[ACT] -> idle (")); Serial.print(reason);
    Serial.print(F(") after ")); Serial.print(cyc_n_done);
    Serial.println(F(" cycle(s); still armed"));
}
// Start a run. Caller must ensure armed. fire = scope-trigger preset (n=1).
static void startCycle(float v_high, float v_idle,
                       uint32_t t_high, uint32_t t_idle, uint32_t n, bool fire) {
    cyc_v_high   = v_high;
    cyc_v_low    = v_idle;
    cyc_fire_ms  = t_high;
    cyc_cool_ms  = t_idle;
    cyc_n_target = n;
    cyc_n_done   = 0;
    cyc_fire     = fire;
    wdt_last_ping = millis();        // arm the heat-watchdog window
    smaTag(); Serial.print(fire ? F("[FIRE] ") : F("[ACT] start "));
    Serial.print(F("v_high=")); Serial.print(v_high, 3);
    Serial.print(F(" v_idle=")); Serial.print(v_idle, 3);
    Serial.print(F(" t_high=")); Serial.print(t_high);
    Serial.print(F(" t_idle=")); Serial.print(t_idle);
    Serial.print(F(" n=")); Serial.print(n);
    Serial.print(F(" (0=cont) wdt_ms=")); Serial.println(wdt_timeout_ms);
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
            // Armed + resting at idle: stream V/I/R so the host sees the
            // idle-hold live. Disarmed = no current = nothing to report.
            if (armed && (int32_t)(millis() - idle_next_log) >= 0) {
                streamSma(readSma());
                idle_next_log = millis() + IDLE_LOG_MS;
            }
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

        case SMA_ACT_HEAT: {
            if (cycleWatchdogTripped()) return;     // heat-only watchdog → idle-low
            uint32_t t_rel = millis() - cyc_phase_t0;
            if (t_rel >= cyc_next_log) {            // stream V/I/R during heat
                streamSma(readSma());
                cyc_next_log += CYCLE_LOG_MS;
            }
            if (t_rel >= cyc_fire_ms) cycleEnterLow();
            return;
        }

        case SMA_ACT_COOL: {
            uint32_t t_rel = millis() - cyc_phase_t0;   // cooling is self-safe → no wdt
            if (t_rel >= cyc_next_log) {            // stream V/I/R during cool
                streamSma(readSma());
                cyc_next_log += CYCLE_LOG_MS;
            }
            if (t_rel >= cyc_cool_ms) {
                cyc_n_done++;
                if (cyc_n_target != 0 && cyc_n_done >= cyc_n_target)
                    cycleStop(F("done"));           // → idle-low, still armed
                else
                    cycleEnterHigh();               // next cycle
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
    if (low == "stop") {                            // graceful stop → idle-low (armed)
        cycleStop(F("host stop"));
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
    if (low == "arm") {                             // close the return path
        if (rejectIfNoDac()) return;
        arm(); setLevel(V_IDLE);
        smaTag(); Serial.print(F("ARMED (MOSFET on); idle=")); Serial.print(V_IDLE, 3); Serial.println(F(" V"));
        return;
    }
    if (low == "disarm") {                          // immediate hard cutoff
        disarm();
        smaTag(); Serial.println(F("DISARMED (MOSFET off, return open)"));
        return;
    }
    if (low.startsWith("idle ")) {                  // set the idle / cool / rest level
        float v = in.substring(5).toFloat();
        if (v < 0.0f || v > DRIVE_V_MAX) { smaTag(); Serial.print(F("Range: 0-")); Serial.print(DRIVE_V_MAX, 1); Serial.println(F(" V")); return; }
        V_IDLE = v;
        if (armed && !smaBusy()) setLevel(V_IDLE);  // apply now if resting
        smaTag(); Serial.print(F("V_IDLE=")); Serial.print(V_IDLE, 3); Serial.println(F(" V"));
        return;
    }
    if (low.startsWith("mosfet ")) {                // back-compat alias for arm/disarm
        String arg = low.substring(7); arg.trim();
        if      (arg == "on")  { if (rejectIfNoDac()) return; arm(); setLevel(V_IDLE); smaTag(); Serial.println(F("ARMED (mosfet on)")); }
        else if (arg == "off") { disarm(); smaTag(); Serial.println(F("DISARMED (mosfet off)")); }
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
    if (low == "fire" || low.startsWith("fire ")) {     // fire <v_high> [t_high_ms] : n=1 + scope trig
        if (rejectIfNoDac() || rejectIfBusy()) return;
        if (!armed) { smaTag(); Serial.println(F("not armed — send 'arm' first")); return; }
        String rest = in.substring(4); rest.trim();
        if (rest.length() == 0) { smaTag(); Serial.println(F("Usage: fire <v_high> [t_high_ms]")); return; }
        int sp = rest.indexOf(' ');
        float    vh = (sp < 0 ? rest : rest.substring(0, sp)).toFloat();
        uint32_t th = (sp < 0) ? 500 : (uint32_t)rest.substring(sp + 1).toInt();
        if (vh < 0 || vh > DRIVE_V_MAX) { smaTag(); Serial.print(F("ERR: V out of range [0, ")); Serial.print(DRIVE_V_MAX, 2); Serial.println(F("]")); return; }
        if (th == 0 || th > CYCLE_MS_MAX) th = 500;
        startCycle(vh, V_IDLE, th, 0, 1, true);         // single heat, scope trigger
        return;
    }
    if (low.startsWith("drive ")) {                     // drive <V> <ms> : single heat → idle
        if (rejectIfNoDac() || rejectIfBusy()) return;
        if (!armed) { smaTag(); Serial.println(F("not armed — send 'arm' first")); return; }
        String rest = in.substring(6); rest.trim();
        int sp = rest.indexOf(' ');
        if (sp <= 0) { smaTag(); Serial.println(F("Usage: drive <V> <ms>")); return; }
        float vt = rest.substring(0, sp).toFloat();
        long  ms = rest.substring(sp + 1).toInt();
        if (vt < 0 || vt > DRIVE_V_MAX) { smaTag(); Serial.print(F("ERR: V out of range [0, ")); Serial.print(DRIVE_V_MAX, 2); Serial.println(F("]")); return; }
        if (ms <= 0 || (uint32_t)ms > CYCLE_MS_MAX) { smaTag(); Serial.println(F("Usage: drive <V> <ms>")); return; }
        startCycle(vt, V_IDLE, (uint32_t)ms, 0, 1, false);
        return;
    }
    if (low.startsWith("cycle ")) {
        // cycle <v_high> <v_idle> <t_high_ms> <t_idle_ms> <n>   (n=0 → continuous)
        // Host sets the profile once; M7 runs the timing. Send `ping` heartbeats,
        // `stop` to end early. Cools to v_idle between heats.
        if (rejectIfNoDac() || rejectIfBusy()) return;
        if (!armed) { smaTag(); Serial.println(F("not armed — send 'arm' first")); return; }
        String rest = in.substring(6); rest.trim();
        float   args_f[2]; uint32_t args_u[3];
        int idx = 0; bool ok = true;
        // 5 whitespace-separated fields: v_high v_idle t_high t_idle n
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
            smaTag(); Serial.println(F("Usage: cycle <v_high> <v_idle> <t_high_ms> <t_idle_ms> <n>"));
            return;
        }
        float vh = args_f[0], vidle = args_f[1];
        uint32_t t_high = args_u[0], t_idle = args_u[1], n = args_u[2];
        if (vh < 0 || vh > DRIVE_V_MAX || vidle < 0 || vidle > DRIVE_V_MAX) {
            smaTag(); Serial.print(F("ERR: voltages out of range [0, "));
            Serial.print(DRIVE_V_MAX, 2); Serial.println(F("]")); return;
        }
        if (t_high == 0 || t_high > CYCLE_MS_MAX ||
            t_idle == 0 || t_idle > CYCLE_MS_MAX) {
            smaTag(); Serial.print(F("ERR: phase ms out of range (1, "));
            Serial.print(CYCLE_MS_MAX); Serial.println(F("]")); return;
        }
        V_IDLE = vidle;                             // align rest level with the cycle idle
        startCycle(vh, vidle, t_high, t_idle, n, false);
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
static uint32_t last_crc_err    = 0;
static uint32_t pop_count_src1  = 0;
static uint32_t pop_count_src2  = 0;
static uint32_t pop_count_other = 0;
static uint32_t last_seq_src1   = 0;
static uint32_t last_seq_src2   = 0;
static const uint32_t STATUS_PERIOD_MS = 1000;

// Drain boot text, drain a batch of sensor samples, emit periodic [STATUS].
// Called every loop() pass AND it is the only sensor-path work on M7, so a
// fast loop() (state machine never blocks long) keeps the stream alive.
// Max sensor lines drained + written per loop pass. Build-flag overridable.
#ifndef SENSOR_BATCH
#define SENSOR_BATCH 64
#endif

static void pumpSensors() {
    // 1. Boot / diagnostic text from M4 via RPC. Guarantee it ends on a newline
    //    before the sensor batch below: a not-yet-terminated RPC message (e.g.
    //    the periodic [ADC1] PGA alarm, delivered in chunks across passes) would
    //    otherwise concatenate with the first sensor line and the host parser
    //    would drop that line.
    int rpc_last = -1;
    while (RPC.available()) { int c = RPC.read(); Serial.write((uint8_t)c); rpc_last = c; }
    if (rpc_last >= 0 && rpc_last != '\n') Serial.write('\n');

    // 2. ADC samples from the shared ring buffer (untagged sensor TSV).
    //    BATCHED WRITE — each small USB-CDC Serial.print() blocks ~1 ms on the
    //    mbed stack (it waits on the USB frame, not bandwidth: ~40 KB/s is <10 %
    //    of the link). Doing ~6 prints × 64 lines/pass gated the M7 loop — and
    //    thus the once-per-pass SMA src=3/4/5 stream — to ~15 Hz (~1.5 pts per
    //    100 ms fire). Formatting the whole batch into one buffer + a SINGLE
    //    Serial.write() amortizes the per-write latency and lifts the SMA stream
    //    to the CYCLE_LOG_MS ceiling (~99 Hz, ~10 pts/fire). Verified in the
    //    Firmware_SMARateTest_PIO fork (2026-07-09). Float is formatted without
    //    printf-%f (not linked on nano newlib): sign + int + 6-digit fraction.
    static char batch[8192];
    size_t off = 0;
    AdcSample s;
    int n_batch = 0;
    while (ring_pop(SAMPLE_RING, s) && n_batch < SENSOR_BATCH) {
        if (sizeof(batch) - off < 96) {           // guard: never overflow the buffer
            Serial.write((const uint8_t*)batch, off);
            off = 0;
        }
        bool vneg = s.voltage_V < 0.0f;
        float av  = vneg ? -s.voltage_V : s.voltage_V;
        unsigned long vip = (unsigned long)av;
        unsigned long vfp = (unsigned long)((av - (float)vip) * 1000000.0f + 0.5f);
        if (vfp >= 1000000UL) { vip++; vfp -= 1000000UL; }  // fraction carry
        int w = snprintf(batch + off, sizeof(batch) - off,
#if ENABLE_ADC1 && ENABLE_ADC2
            "%lu\t%d\t%ld\t%s%lu.%06lu\t%lu\t%lu\r\n",
            (unsigned long)s.timestamp_ms, (int)s.src, (long)s.raw_code,
            vneg ? "-" : "", vip, vfp,
            (unsigned long)s.hw_us, (unsigned long)s.seq);
#else
            "%lu\t%ld\t%s%lu.%06lu\t%lu\t%lu\r\n",
            (unsigned long)s.timestamp_ms, (long)s.raw_code,
            vneg ? "-" : "", vip, vfp,
            (unsigned long)s.hw_us, (unsigned long)s.seq);
#endif
        if (w > 0) off += (size_t)w;

        // Track the newest M4 clock so SMA src=3/4/5 can share the M4 timeline.
        last_m4_hw_us = s.hw_us;
        last_m4_ms    = s.timestamp_ms;

        if      (s.src == SAMPLE_SRC_LASER) pop_count_src1++;
        else if (s.src == SAMPLE_SRC_LOAD)  pop_count_src2++;
        else                                pop_count_other++;
        n_batch++;
    }
    if (off) Serial.write((const uint8_t*)batch, off);   // one USB write per pass

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

        // Checksum-invalid reads (discarded before they get a seq) — M4 still
        // counts these in the loop. Report per-window delta + running total.
        uint32_t cur_crc_err = SAMPLE_RING->crc_err;
        uint32_t crc_err_delta = cur_crc_err - last_crc_err;
        last_crc_err = cur_crc_err;

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

        Serial.print("[STATUS] t_ms=");   Serial.print(now);
        Serial.print(" hwm=");            Serial.print(hwm);
        Serial.print(" cap=");            Serial.print(RING_CAPACITY);
        Serial.print(" dropped=");        Serial.print(dropped_delta);
        Serial.print(" dropped_total=");  Serial.print(cur_dropped);
        Serial.print(" crc_err=");        Serial.print(crc_err_delta);
        Serial.print(" crc_err_total=");  Serial.print(cur_crc_err);
        Serial.print(" rate1=");          Serial.print(rate1);
        Serial.print(" rate2=");          Serial.print(rate2);
        if (rate_other) { Serial.print(" rate_other="); Serial.print(rate_other); }
        Serial.print(" prod1=");          Serial.print(prate1);
        Serial.print(" prod2=");          Serial.print(prate2);
        Serial.print(" sma_state=");      Serial.print((int)smaState);   // 0=IDLE
        // Clock-alignment check: M7's own clock vs the freshest M4 sample clock
        // (lets the host derive the M4↔M7 offset for the src=3/4/5 lines) + LDO
        // model params so the host can compute V_pred = offset + (code/4095)*vdd.
        uint32_t m7_now_us = micros();
        Serial.print(" m7_us=");          Serial.print(m7_now_us);
        Serial.print(" m4_us=");          Serial.print(last_m4_hw_us);
        Serial.print(" vdd=");            Serial.print(VDD_MCP, 3);
        Serial.print(" offset=");         Serial.print(V_OFFSET, 4);
        Serial.print(" aref=");           Serial.print(ADC_VREF_V, 3);
        Serial.println();
    }
}

void setup() {
    // Zero the ring BEFORE booting M4 (RPC.begin() starts the M4 core).
    SAMPLE_RING->write_idx = 0;
    SAMPLE_RING->read_idx  = 0;
    SAMPLE_RING->dropped   = 0;
    SAMPLE_RING->crc_err   = 0;
    SAMPLE_RING->overrun   = 0;
    SAMPLE_RING->m4_now_us = 0;
    SAMPLE_RING->m4_now_ms = 0;
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
    // BUILD-ID: confirms the running image actually has the cache-line fix.
    // Expect slot=32 cap=512 after the 2026-06-29 alignment change; slot=24
    // cap=1024 means this core is still the OLD build (reflash / clean build).
    Serial.print("[M7] ring build-id: slot=");   Serial.print((unsigned)sizeof(AdcSample));
    Serial.print("B cap=");                       Serial.print((unsigned)RING_CAPACITY);
    Serial.print(" base=0x");                     Serial.println((unsigned long)RING_BASE, HEX);

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
    smaTag(); Serial.println(F("cmds: arm | disarm | idle <V> | set <V> | code <N> |"));
    smaTag(); Serial.println(F("      drive <V> <ms> | fire <V> [ms] |"));
    smaTag(); Serial.println(F("      cycle <v_high> <v_idle> <t_high_ms> <t_idle_ms> <n>  (n=0 cont) |"));
    smaTag(); Serial.println(F("      ping (heartbeat) | stop | wdt <ms> (0=off) | abort |"));
    smaTag(); Serial.println(F("      step <code>[ms] | sweep|csv [step] | read | info |"));
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

// ── ADS1263 production config (settled 2026-06-29) ────────────────────────
// POWER=0x13 (INTREF + VBIAS on) — matches the original working driver; the
//   bare 0x02 was a regression. ADC1 PGA is IN-PATH at gain=1 (lower noise).
// Sampling is TIMED POLLING (no DRDY ISR): each ADC is read on its own ~2 ms
//   timer, every valid read pushed to the ring. This is the proven-tracking
//   path (verbatim from the bench-verified stable SensorHub loop).
// NOTE (open item): ADC1 can raise PGAL_ALM because the laser's AIN5 sits near
//   ground (low PGA common-mode). If that becomes a problem, bypass ADC1's PGA
//   — pass pga_bypass=true to configureADC1() below (full 0..VREF range, no
//   common-mode restriction, ~3.5 µV vs 1.3 µV noise). See the troubleshooting
//   doc's PGAL_ALM note.
#define ADS1263_POWER_CFG  0x13

#define CP(n, msg)  do { \
    RPC.print("[M4 cp "); RPC.print(n); RPC.print("] "); RPC.println(msg); \
} while (0)

void setup() {
    RPC.begin();
    delay(500);
    CP(0, "RPC up");

    Serial.begin(115200);
    CP(1, "Serial.begin done");

    RPC.println("[M4] *** Firmware_SMASensorHub_PIO — dual-ADC production stream ***");
    RPC.println("[M4]   ADC1 → AIN4/AIN5 (Keyence IL-030 laser)");
    RPC.println("[M4]   ADC2 → AIN2/AIN3 (LCA-9PC load cell)");
    // BUILD-ID (must match the M7 banner). If M4 prints slot/cap different
    // from M7, the two core images disagree on the ring layout → garbage.
    RPC.print("[M4] ring build-id: slot="); RPC.print((unsigned)sizeof(AdcSample));
    RPC.print("B cap=");                     RPC.print((unsigned)RING_CAPACITY);
    RPC.print(" base=0x");                   RPC.println((unsigned long)RING_BASE, HEX);
    RPC.println("[M4] sampling: timed poll (ADC1 laser; ADC2 if enabled)");

    RPC.println("[M4] waiting 3000 ms for ADS1263 to power up...");
    delay(3000);
    RPC.println("[M4] ADS1263 power-up settle done");

    // CS/RESET/DRDY pin modes, their idle levels, and SPI.begin() are all
    // owned by adc.begin() (ADS1263_Driver) — don't duplicate them here.
    CP(7, "calling adc.begin()");
    bool ok = adc.begin(ADS1263_POWER_CFG);
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
        /*pga_bypass =*/ false                        // PGA in path, gain=1 (set true if PGAL_ALM)
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

    // ── Raw ADC register dump (bench diagnostic, 2026-06-26) ──────────────
    // Reads back the bytes actually programmed so we can confirm the input
    // mux (INPMUX/ADC2MUX), PGA+filter (MODE2/MODE1), and reference path
    // (REFMUX/POWER/ADC2CFG) live, instead of trusting the source. Runs
    // BEFORE the DRDY ISR attaches, so no SPI contention. The '[' prefix
    // means the host sample parser ignores it; read it in `pio device monitor`.
    RPC.print("[M4] REGDUMP ID=0x");      RPC.print(adc.getDeviceID(), HEX);
    RPC.print(" POWER=0x");   RPC.print(adc.peekRegister(ADS1263_REG_POWER), HEX);
    RPC.print(" INPMUX=0x");  RPC.print(adc.peekRegister(ADS1263_REG_INPMUX), HEX);
    RPC.print(" MODE1=0x");   RPC.print(adc.peekRegister(ADS1263_REG_MODE1), HEX);
    RPC.print(" MODE2=0x");   RPC.print(adc.peekRegister(ADS1263_REG_MODE2), HEX);
    RPC.print(" REFMUX=0x");  RPC.print(adc.peekRegister(ADS1263_REG_REFMUX), HEX);
    RPC.print(" ADC2CFG=0x"); RPC.print(adc.peekRegister(ADS1263_REG_ADC2CFG), HEX);
    RPC.print(" ADC2MUX=0x"); RPC.println(adc.peekRegister(ADS1263_REG_ADC2MUX), HEX);

    // Timed-poll sampling — no DRDY interrupt attached.
    CP(11, "sampling via timed poll (no DRDY ISR)");

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
    // ── M4 = PURE SENSOR PRODUCER ─────────────────────────────────────────
    // M4 reads the ADS1263 and ring_pushes — nothing else. No housekeeping
    // writes into the shared ring header (clock/status are derived on M7), so
    // M4 stays a minimal producer matching the proven-stable SensorHub model.
    //
    // Timed polling: each ADC on its own ~2 ms timer. 2 ms is slightly faster
    // than the 2.5 ms (400 SPS) conversion period, so some reads re-fetch the
    // same data register — still valid; the ring absorbs the rate. Every valid
    // read is pushed; a bad checksum bumps crc_err and is dropped.
#if ENABLE_ADC1
    static uint32_t t1_last = 0;
    if (millis() - t1_last >= 2) {
        t1_last = millis();
        ADC_Reading r1 = adc.readADC1Direct();
        if (r1.valid) {
            ring_push(SAMPLE_RING, micros(), millis(), SAMPLE_SRC_LASER, r1.raw_code, r1.voltage_V);
        } else {
            SAMPLE_RING->crc_err++;            // bad checksum → sample discarded
        }
    }
#endif
#if ENABLE_ADC2
    static uint32_t t2_last = 0;
    if (millis() - t2_last >= 2) {
        t2_last = millis();
        ADC_Reading r2 = adc.readADC2Direct();
        if (r2.valid) {
            ring_push(SAMPLE_RING, micros(), millis(), SAMPLE_SRC_LOAD, r2.raw_code, r2.voltage_V);
        } else {
            SAMPLE_RING->crc_err++;            // bad checksum → sample discarded
        }
    }
#endif
}

#endif // M4_IDLE

#else
  #error "Unknown core — build with CORE_CM7 or CORE_CM4"
#endif
