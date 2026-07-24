/**
 * @file main.cpp  (Portenta H7 dual-core — Firmware_SMAConstantCurrent_PIO)
 *
 * FORK of Firmware_SMASensorHub_PIO that adds a CLOSED-LOOP CONSTANT-CURRENT
 * controller to the SMA drive path. Everything else — the M4 dual-ADC sampler,
 * the SRAM4 ring, the M7 bridge, [STATUS], and every existing voltage-mode
 * command — is carried over unchanged, so this image is a superset of the
 * SensorHub one. The parent project stays the stable/production build.
 *
 * WHY: driving an SMA at constant VOLTAGE means the current (and therefore the
 * Joule heating) drifts as the wire's resistance changes with temperature and
 * phase. Holding CURRENT makes the heating input repeatable, and the loop's
 * live plant-resistance estimate is itself a free actuation sensor.
 *
 * The control law is the port of the Uno-validated design in
 * GelBot/PIConstantCurrent/CONTROL_SKELETON.md §3 (feedforward + auto-gain
 * adaptive PI). See the CONSTANT-CURRENT CONTROLLER block below for the port
 * notes — what carried over verbatim and the three places this hardware
 * differs from the Uno.
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
 *     - NEW: the actuation engine has two SETPOINT MODES. Voltage mode is
 *       the inherited behaviour (`drive`/`fire`/`cycle` command volts);
 *       current mode (`cc`/`ccfire`/`cccycle`) commands milliamps and closes
 *       the loop on the INA296A reading every control tick. There is no
 *       `ccdrive`: `cc <mA> [ms]` IS the drive twin (and retargets in place
 *       if a run is already up).
 *
 * ── Shared USB serial: three line classes ────────────────────────────
 *   <untagged TSV>   sample stream   : t_ms\tsrc\traw\tV\thw_us\tseq
 *                    src=1 laser, 2 load  (from M4 via the ring);
 *                    src=3 SMA V (= V at SMA_P, A0), 4 SMA I, 5 SMA R
 *                    (R = src3/src4, no shunt correction)  (from M7 during
 *                    drive/fire — emitted directly, NOT via the ring);
 *                    src=6 CC command u [V], 7 CC R_est [ohm]  (M7, emitted
 *                    only while the current loop is closed).
 *   [STATUS] ...     pipeline telemetry (1 Hz)
 *   [SMA] ...        SMA driver banners / responses (human-readable)
 *   The host sensor parser already drops any line containing '[', so the
 *   [STATUS] and [SMA] classes are cleanly demultiplexed from samples.
 *
 * ── Why the merge is safe (no pin overlap) ───────────────────────────
 *   M4 sensing : PA_8 (CS), PC_6 (DRDY), PC_7 (RESET) + SPI bus
 *   M7 SMA     : Wire/I2C (PB_6/PB_7), A0 (SMA_P sense), A1 (INA296A I-sense),
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

// Optional UDP transport for the high-rate sample stream (Step 2 of the UDP
// migration; see docs/UDP_stream_migration_plan.md). Default build is unchanged.
#ifndef H7_TRANSPORT_UDP
#define H7_TRANSPORT_UDP 0
#endif
#if H7_TRANSPORT_UDP
#include <PortentaEthernet.h>
#include <Ethernet.h>
#include <EthernetUdp.h>
#endif

// ──────────────────────────────────────────────────────────────────────
//  SMA drive-path hardware (ported verbatim from Firmware_SMADriver_PIO)
// ──────────────────────────────────────────────────────────────────────
Adafruit_MCP4728 mcp;
static bool sma_ok = false;          // false if MCP4728 absent → SMA cmds no-op
                                     //   (sensor bridge still runs — non-fatal)

// -- Pins --------------------------------------------------------------
const int     MOSFET_PIN = D3;       // PWM3 = D3 = PG_7 (Mid Carrier J15-31)
const int     FB_PIN     = A0;       // SMA_P via 10k/10k divider (AFTER the shunt)
const int     ISENSE_PIN = A1;       // INA296A OUT (current sense) — ENABLED below
const PinName TRIG_PIN   = PJ_11;    // scope trigger; rising edge = DAC-step t0

// -- Analytical LDO transfer: V_LDO = V_OFFSET + (VDD_MCP/4095)*code ----
// VDD_MCP is the MCP4728's supply rail AND its full-scale output, because the
// DAC is written with MCP4728_VREF_VDD (see setDACraw) — so this constant must
// track the hardware, not the other way round.
//
// 5.5 -> 5.0 on 2026-07-24: the DAC rail was reconfigured to 5.0 V to raise the
// I2C logic-level margin. The MCP4728's VIH is ratiometric (0.7 x VDD), so at
// 5.5 V it demanded 3.85 V from a 3.3 V bus and at 5.0 V it demands 3.50 V —
// less out of spec, but STILL out of spec. The bus remains marginal until a
// level translator goes in; this only makes the intermittency rarer. Full-scale
// at the LDO drops from ~5.81 V to ~5.31 V accordingly.
//
// Runtime-tunable via `vdd <V>` (not persisted). A wrong value here skews every
// VOLTAGE-mode command by the ratio; constant-current mode is immune, since
// R_est is measured in the command domain (u/I) and absorbs any DAC-map error
// (skeleton pitfall 1).
static float       VDD_MCP   = 5.0f;          // DAC full-scale rail (slope)
static const float IREF_A    = 50e-6f;        // TPS7A57 ref current (nominal)
static const float R_SERIES  = 6200.0f;       // DAC → REF pin series resistor
static float       V_OFFSET  = IREF_A * R_SERIES;  // ~0.31 V intercept (tunable)

// -- Feedback readback divider (SMA_P → 10k/10k → A0) ------------------
const float  FB_DIV_RATIO = 0.5f;
const float  ADC_FB_SCALE = 1.0f / FB_DIV_RATIO;   // 2.0

// -- INA296A current sense (LDO out → 200 mOhm shunt → SMA_P → SMA) ----
//   V_ina = I * R_SHUNT * INA_GAIN  →  I = (V_ina - offset)/(INA_GAIN*R_SHUNT)
//   A1 variant = 10 V/V; 0.2 ohm → 2.0 V/A; unidirectional (REF=GND).
//
//   NODE ORDER (schematic-verified 2026-07-24): A0 taps SMA_P, which is the
//   node AFTER the shunt — i.e. the SMA's own high side. So A0 measures V_sma
//   DIRECTLY and R_sma = V_sma / I needs no shunt correction. The LDO output
//   is the node the firmware no longer sees; it is reconstructed for display
//   as V_ldo = V_sma + I*R_SHUNT. This is the reverse of the pre-2026-07-24
//   code, which assumed A0 was before the shunt and SUBTRACTED the drop —
//   that made V_sma (and hence R) low by 2*I*R_shunt.
static float       INA_GAIN        = 10.0f;   // INA296A1 = 10 V/V
static float       R_SHUNT_OHM     = 0.2f;    // 200 mOhm
static float       ISENSE_OFFSET_V = 0.0f;    // 0 A output (REF=GND)
static const float I_FLOOR_A       = 1e-3f;   // below this, R is undefined

// -- On-chip ADC (H7, 16-bit) ------------------------------------------
static const int   ADC_RES_BITS = 16;
static const int   ADC_RES_MAX  = (1 << ADC_RES_BITS) - 1;   // 65535
static float       ADC_VREF_V   = 3.145f;     // H7 Vref+ (1-pt cal). NOTE: this is
                                              // ~5% high — the rate ladder extrapolates
                                              // the true value to ~2.99 V at zero ADC
                                              // duty. V and I are correspondingly high;
                                              // R = V/I is immune. Recalibrating it is a
                                              // separate open TODO.

// Software-oversampling depth, split by phase (2026-07-13, from the rate ladder
// in Firmware_SMARateTest_PIO, env portenta_m7_rate1k_n4).
//
// readADC() takes N readings and averages them, and readSma() calls it twice, so
// N sets both the cost AND the ADC's conversion duty. Duty is not innocent: the
// ADC's reference sags in proportion to how busy it is, which inflates every
// voltage read from it (correlation +1.000 across 8 bench runs at a fixed DAC
// code; V_measured = 0.01508 x duty% + 2.988, R2 = 0.9996). R = V/I cancels it
// exactly, which is why it hid for so long.
//
// So the in-cycle path takes FEWER readings per sample, not more: N=4 at 1 kHz
// performs slightly fewer conversions per second than N=64 at 100 Hz did, while
// reporting 10x as many samples. The averaging moves off the ADC and onto the
// host, where it is free and does not drain the reference. Precision per sample
// drops, but you get 10x the samples to average — and the fire transient is
// finally resolved (96 points per 100 ms fire, vs 8).
//
//   ADC_SAMPLES_IDLE  — precision reads (cold-R, settle, manual, idle-hold) — 64
//   ADC_SAMPLES_CYCLE — fast reads inside the heat/cool actuation           —  4
#ifndef ADC_SAMPLES_IDLE
#define ADC_SAMPLES_IDLE   64
#endif
#ifndef ADC_SAMPLES_CYCLE
#define ADC_SAMPLES_CYCLE  4
#endif
static const int   ADC_SAMPLES  = ADC_SAMPLES_IDLE;   // default for legacy calls

// -- Drive parameters --------------------------------------------------
static const float    DRIVE_V_MAX  = 5.0f;
static const uint32_t DRIVE_MS_MAX  = 60000;  // 60 s — SMA self-heat risk above
static const uint32_t DRIVE_LOG_MS  = 10;     // feedback sample period during hold

static uint16_t currentCode = 0;

// ──────────────────────────────────────────────────────────────────────
//  ADC / DAC helpers  (electrical primitives — kept byte-for-byte; only
//  the BLOCKING control flow above them was restructured into a machine)
// ──────────────────────────────────────────────────────────────────────

// Post-mux settling time before the averaging burst. This was `delay(1)` — a
// FULL MILLISECOND — and readSma() calls readADC() twice, so ~2 ms of every SMA
// sample was pure delay. It was the single largest cost in the path and capped
// the stream near 200 Hz. The throw-away conversion already charges the
// sample-and-hold; the delay only buys settling of the external source, and 1 ms
// is wildly conservative for these low-impedance sources (an LDO divider on A0,
// an INA296A op-amp output on A1). Bench-swept 1000 -> 50 us at a fixed drive:
// the V/I means moved -0.1%, i.e. not at all. Do not cut it further without
// re-running that check — an unsettled source reads WRONG and the stream will
// not tell you so.
#ifndef SMA_SETTLE_US
#define SMA_SETTLE_US 50
#endif

// Averaged read at an analog pin (volts at the ADC input).
static float readADC(int pin, int nsamples = ADC_SAMPLES) {
    if (nsamples < 1) nsamples = 1;
    analogRead(pin);                 // throw-away (prime the input stage)
    delayMicroseconds(SMA_SETTLE_US);
    uint32_t sum = 0;
    for (int i = 0; i < nsamples; i++) sum += (uint32_t)analogRead(pin);
    float code = (float)sum / (float)nsamples;
    return (code / (float)ADC_RES_MAX) * ADC_VREF_V;
}

// Voltage at SMA_P (un-divided) — the SMA's high side, AFTER the shunt.
// NOTE: this used to be called readLDO(); the pin was always A0, but the node
// it lands on is SMA_P, not the LDO output. Renamed so the code stops lying
// about which node it reads. The DAC-characterisation commands (set/code/
// step/sweep) still call it, so their "V_meas" is now SMA_P — it sits
// I*R_SHUNT below the codeToVldo() prediction whenever current is flowing.
static float readSmaP(int nsamples = ADC_SAMPLES) {
    return readADC(FB_PIN, nsamples) * ADC_FB_SCALE;
}

// One coherent electrical read of the SMA drive path (INA296A current sense).
//   v_sma = MEASURED at A0 (SMA_P);  i = MEASURED at A1 (INA296A);
//   v_ldo = DERIVED (v_sma + i*R_shunt);  r = v_sma / i.
struct SmaRead { float v_ldo; float i; float v_sma; float r; };
static SmaRead readSma(int nsamples = ADC_SAMPLES) {
    SmaRead s;
    s.v_sma = readSmaP(nsamples);                         // A0, AFTER the shunt
    float v_ina = readADC(ISENSE_PIN, nsamples);          // A1, INA296A OUT
    float scale = INA_GAIN * R_SHUNT_OHM;                 // V/A
    s.i     = (scale > 0.0f) ? (v_ina - ISENSE_OFFSET_V) / scale : 0.0f;
    s.v_ldo = s.v_sma + s.i * R_SHUNT_OHM;                // reconstruct (display only)
    s.r     = (fabs(s.i) >= I_FLOOR_A) ? s.v_sma / s.i : NAN;
    return s;
}

// Raw DAC write — updates code + lets the DAC update; does NOT wait for
// the slow LDO output to settle. No-op (records code only) if MCP absent.
//
// `settle` keeps the inherited trailing delay(2) for the one-shot commands
// (set/code/step/sweep), where an extra 2 ms costs nothing and guarantees the
// DAC register is latched before the caller reads back. The CONTROL LOOP must
// pass false: at a 1 ms control period a 2 ms blocking delay per write is not a
// tuning detail, it is a hard rate ceiling — the loop could not even run at its
// own period, and every tick would stall the sensor drain behind it. The delay
// was never protecting the write itself (Wire.endTransmission() has already
// returned by then), only the LDO's much slower output slew, which the loop
// observes through the ADC anyway.
// ── DAC-link watchdog ─────────────────────────────────────────────────
// sma_ok is latched ONCE at boot, and the I2C write's return code used to be
// discarded. That combination is benign in voltage mode and dangerous in
// current mode. If the link drops mid-run the DAC holds its last latched code,
// so current keeps flowing at the last commanded level — while the loop, which
// still reads current fine (that is the ADC, a different path), sees a growing
// error and winds the integrator up against an output that is physically
// frozen. Should the link return, the wound-up command lands as a STEP.
//
// The open-load watchdog cannot catch this: it triggers on "command railed with
// no current" (broken load, working DAC), which is the opposite signature.
//
// So: check the write, and fault after DAC_FAIL_MAX consecutive failures. At
// the 1 kHz control rate that is ~3 ms of frozen output — far too short for the
// integrator to travel anywhere — which is why no separate anti-windup is
// needed here. The recovery is disarm(), and disarm's real cutoff is a
// digitalWrite on the MOSFET gate: a GPIO, NOT the I2C bus, so it still works
// when the DAC is exactly what has failed.
static const uint8_t DAC_FAIL_MAX  = 3;      // consecutive failed writes -> fault
static uint8_t  dac_fail_run   = 0;          // current consecutive-failure run
static uint32_t dac_fail_total = 0;          // cumulative, published in [STATUS]
static bool     dac_lost       = false;      // latched; actioned by serviceSma

static void setDACraw(uint16_t code, bool settle = true) {
    if (code > 4095) code = 4095;
    currentCode = code;
    if (sma_ok) {
        // Adafruit_MCP4728::setChannelValue returns false when the I2C
        // transaction does not ACK — the only signal we get that the DAC has
        // gone away.
        bool ok = mcp.setChannelValue(MCP4728_CHANNEL_A, code,
                                      MCP4728_VREF_VDD, MCP4728_GAIN_1X);
        if (ok) {
            dac_fail_run = 0;
        } else {
            dac_fail_total++;
            if (dac_fail_run < 255) dac_fail_run++;
            if (dac_fail_run >= DAC_FAIL_MAX) dac_lost = true;
        }
        if (settle) delay(2);
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
//  CONSTANT-CURRENT CONTROLLER — constants + state
//
//  Port of GelBot/PIConstantCurrent/CONTROL_SKELETON.md §3, validated on an
//  Uno driving THIS SAME driver board (MCP4728 → TPS7A57 → INA296A). The
//  control law is transcribed verbatim; the platform-specific pieces are
//  called out below. Read §8 of the skeleton before touching any of this —
//  every gate here exists because its absence was a real bug.
//
//  Structure (see the skeleton for the derivation):
//    u_ff = I_target * R_est     feedforward owns the step (fast path)
//    Ki   = R_est / tau          auto-gain: closed-loop tau is load-independent
//    integral trims, gated to "near target" so a step can't wind it up
//    R_est = u_cmd / I           COMMAND-domain, low-passed, gated to near-target
//
//  ── Three ways this differs from the Uno build ──────────────────────
//
//  1. NO CALIBRATION TABLE. The Uno stored a 33-point code→V_LDO sweep in
//     EEPROM to linearize the DAC→LDO path (skeleton §4). This firmware
//     already models it analytically: V_LDO = V_OFFSET + (VDD_MCP/4095)*code,
//     inverted by vldoToCode(). That map IS this port's `voltage_to_code`.
//     Any gain/offset error in it is harmless HERE — and only here — because
//     R_est is measured in the COMMAND domain (u/I, skeleton §2 + pitfall 2),
//     so a wrong map is absorbed into R_est and the feedforward stays right.
//     Do NOT "improve" this by switching R_est to V_ldo/I; that is pitfall 2
//     and it produces persistent overshoot no amount of tau will fix.
//
//  2. TAU IS NOT REDUCED, despite the 480 MHz core. Skeleton §9 suggests a
//     smaller tau on the H7, but the speed limit here is the PLANT, not the
//     MCU: the TPS7A57's CNR/SS soft-start cap dominates the response
//     (docs/PLAN_phase6_ldo_characterization.md). The Uno reached ~15 ms
//     settle against this same LDO, so 7 ms is already near the actuator's
//     limit. The H7 buys a faster, jitter-free CONTROL RATE (below), not a
//     faster plant. Re-tune tau on a load-change test only after the LDO
//     settling numbers from the Phase-6 scope plan are in hand.
//
//  3. THE CURRENT READING IS ~5-7% HIGH IN ABSOLUTE TERMS, and for a CURRENT
//     controller that is a real error, not a curiosity. ADC_VREF_V (3.145)
//     is ~5% above the true ~2.99 V, and ADC conversion duty sags the
//     reference further (see the ADC_SAMPLES notes above). Voltage-mode
//     drive never cared, and R = V/I cancels it exactly — but `cc 500` will
//     regulate to a TRUE current of roughly 470 mA, because the loop holds
//     the *measured* number at target. Fix the scale, not the loop: check
//     `read` against a DMM in series and set `aref <V>` (and `gain`/`shunt`/
//     `ioffset` for the INA path). Until then treat CC targets as repeatable
//     but not absolute.
// ══════════════════════════════════════════════════════════════════════

// -- Tunables (defaults = the Uno-validated values from skeleton §3) ----
static float cc_tau_s   = 0.007f;    // closed-loop time constant  (`tau <ms>`)
static float cc_Kp      = 0.0f;      // proportional term (`ccgain <Kp>`); 0 = pure I

static const float CC_I_FLOOR_A   = 0.020f;  // below this, u/I is not trustworthy
static const float CC_GATE_FRAC   = 0.12f;   // "near target" band, fraction of target
static const float CC_GATE_MIN_A  = 0.010f;  // ...and its absolute floor
static const float CC_KI_BOOT     = 8.0f;    // bootstrap integral gain [V/A/s]
static const float CC_R_MIN       = 0.05f;   // sane clamp on the plant estimate
static const float CC_R_MAX       = 5000.0f;
static const float CC_I_MAX_A     = 2.0f;    // hard ceiling on an accepted target

// R_est low-pass, expressed as a TIME CONSTANT rather than the skeleton's
// per-cycle R_ALPHA = 0.10. That alpha is only meaningful together with the
// rate it runs at: on the Uno's ~200 Hz loop it is a ~45 ms filter, but reused
// verbatim at 1 kHz it would be a ~9 ms one — 5x noisier tracking, purely as a
// side effect of the faster MCU. Deriving alpha from dt each tick keeps the
// filter's behaviour identical to the validated Uno build and independent of
// the control rate.
static const float CC_R_TAU_S     = 0.045f;

// Control period. The Uno ran the loop at whatever rate loop() managed
// (~200 Hz, serial-bound and jittery); here it is scheduled off micros() at a
// fixed period with the TRUE elapsed dt measured per tick, so the integral is
// correct even when a pass runs late. 1 kHz matches CYCLE_LOG_MS.
//
// NOT a timer ISR (skeleton §9 suggests one): the control output is an I2C
// write to the MCP4728, and mbed's Wire is not safe to call from interrupt
// context. The cooperative loop already sustains >1 kHz ([STATUS] loop_hz),
// and measuring dt rather than assuming it removes the jitter that the ISR
// was meant to solve.
#ifndef CC_PERIOD_US_CFG
#define CC_PERIOD_US_CFG 1000
#endif
static const uint32_t CC_PERIOD_US = CC_PERIOD_US_CFG;
static const float CC_DT_MIN_S = 100e-6f;    // dt sanity clamp: a too-small dt
static const float CC_DT_MAX_S = 0.050f;     // starves the integral, a too-large
                                             // one (after a stall) kicks it hard

// Open-load / broken-wire detection: command railed at the ceiling while
// essentially no current flows means the return path is open (snapped SMA,
// disconnected clip). Voltage mode cannot detect this — it just sits there —
// but a current loop RAMPS TO THE RAIL trying to force current through a break,
// so the fault must be caught and the output parked.
static const float CC_OPEN_LOAD_S = 0.250f;

// -- Live controller state --------------------------------------------
static bool     cc_enabled  = false;   // loop closed (current mode + actuating)
static float    cc_i_target = 0.0f;    // A
static float    cc_R_est    = 0.0f;    // ohm, command-domain (u/I), low-passed
static bool     cc_R_valid  = false;   // false until bootstrap latches a seed
static float    cc_u_i      = 0.0f;    // integral accumulator [V]
static float    cc_u_cmd    = 0.0f;    // last command written [V]
static uint32_t cc_last_us  = 0;       // for the measured dt
static uint32_t cc_next_us  = 0;       // next scheduled tick
static float    cc_rail_s   = 0.0f;    // time spent railed with no current
static bool     cc_fault    = false;   // set by ccStep, actioned by serviceSma
static uint32_t cc_ticks    = 0;       // ticks this run (rate check in [STATUS])

// Command clamp. The skeleton's fixed [0.5, 5.0] V becomes the intersection of
// what the LDO can produce and what the drive path is allowed to apply.
static inline float ccUMin() { return vldoMin(); }
static inline float ccUMax() {
    float hi = vldoMax();
    return (hi > DRIVE_V_MAX) ? DRIVE_V_MAX : hi;
}


// ══════════════════════════════════════════════════════════════════════
//  SMA NON-BLOCKING STATE MACHINE
//
//  loop() services ONE step of the active op per pass — so the loop rate is a
//  hard ceiling on the SMA stream rate ([STATUS] loop_hz reports it). A step is
//  short but NOT free: a feedback step calls readSma() = 2x readADC. In-cycle
//  that is ~0.2 ms (settle 50 us + 4-sample average); at idle, or on a manual
//  read, it is ~1.6 ms (64-sample average). setDACraw() adds delay(2).
//  So the loop can still stall a few ms per pass during an active SMA op — the
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
//
// SETPOINT MODE (the CC fork's one structural change to the engine). The phase
// levels are either VOLTS (inherited behaviour — the DAC is written once per
// phase and left alone) or AMPS (the current loop runs every tick and owns the
// DAC for the whole phase). Everything else — arming, phase timing, the heat
// watchdog, ping/stop/abort, n-cycle counting — is shared, so constant-current
// actuation inherits every safety property that voltage actuation already has
// rather than growing a parallel path that could drift out of sync with it.
enum SmaSetpointMode { SP_VOLTAGE, SP_CURRENT };
static SmaSetpointMode cyc_mode = SP_VOLTAGE;

static float    cyc_v_high   = 0.0f;
static float    cyc_v_low    = 0.0f;   // == idle/cool level for this run
static float    cyc_i_high   = 0.0f;   // SP_CURRENT: heat target [A]
static float    cyc_i_low    = 0.0f;   // SP_CURRENT: cool target [A]; 0 = park
                                       //   at V_IDLE volts with the loop open
static uint32_t cyc_fire_ms  = 0;      // heat duration (t_high)
static uint32_t cyc_cool_ms  = 0;      // cool duration (t_idle)
static uint32_t cyc_n_target = 0;      // 0 = continuous until stop/disarm
static uint32_t cyc_n_done   = 0;      // completed cycles so far
static uint32_t cyc_phase_t0 = 0;      // current phase start (millis)
static uint32_t cyc_next_log = 0;      // next src=3/4/5 stream time (rel ms)
static bool     cyc_fire     = false;  // fire preset: scope trigger + clean edge
// Period of the src=3/4/5 V/I/R stream during a cycle. Once the batched write
// and the settle fix landed, the stream sat exactly ON this ceiling, so it is
// the knob: 10 ms -> 100 Hz, 1 ms -> ~1 kHz (measured 962 Hz, 96 points per
// 100 ms fire — the whole point of the exercise; it was 8). millis() has 1 ms
// resolution, so 1 ms is the floor this scheduler can express; anything finer
// needs the schedule moved to micros().
#ifndef CYCLE_LOG_MS_CFG
#define CYCLE_LOG_MS_CFG 1
#endif
static const uint32_t CYCLE_LOG_MS = CYCLE_LOG_MS_CFG;
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

// ── Host-liveness heartbeat (PC health check) ─────────────────────────
// DISTINCT from the heat wdt above: that guards a HEAT phase and drops to
// idle-low (still armed); THIS guards the LINK and fully DISARMS. The PC sends
// `ping` at ~1 Hz for the whole time it is connected; if none arrives within
// hb_timeout_ms while ARMED — in ANY sub-state (armed-idle, a `cc` hold, a
// cycle) — the PC is presumed gone (console closed, crashed, or USB stalled)
// and the coil is de-energised. Every received command also counts as
// liveness, so an actively-driven session never trips it.
//
// DEFAULT OFF (0): the safe-stop only makes sense when a host pings CONTINUOUSLY
// (not just during actuation). Enabling it by default would disarm any recorder
// that goes quiet at idle. Enable explicitly with `hb <ms>` from a host that
// pings the whole time it is connected — then closing that host safe-stops the
// coil. The mechanism is complete and tested; it is opt-in, not absent.
static uint32_t hb_timeout_ms = 0;       // 0 = disabled (opt-in via `hb <ms>`)
static uint32_t hb_last_ms    = 0;

// Begin a settle measurement (mirrors settleWait timing: first compare at
// +18 ms, 2 mV quiet band, 5 consecutive quiet reads, 2 s hard timeout).
static void settleBegin() {
    st_prev  = sma_ok ? readSmaP() : 0.0f;
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
        float v = readSmaP();
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
// Sized SAMPLE_SRC_MAX+1 = 8: src=6/7 (CC controller state) index it directly,
// so the array MUST cover them — at 6 entries this fork would have written past
// the end on the first CC sample.
static uint32_t sma_seq[SAMPLE_SRC_MAX + 1] = {0, 0, 0, 0, 0, 0, 0, 0};

// Freshest M4 timestamps M7 has drained from the ring (updated in pumpSensors).
// Lets M7 stamp the SMA src=3/4/5 lines on the M4 timeline without M4 having to
// publish a separate clock (M4 stays a pure producer).
static uint32_t last_m4_hw_us = 0;   // newest sample hw_us (M4 micros())
static uint32_t last_m4_ms    = 0;   // newest sample timestamp_ms (M4 millis())

// Batch all three SMA rows into ONE USB-CDC write.
//
// This is the same bug the 2026-07-09 round fixed in pumpSensors(), which was
// never applied to the SMA path: six Serial.print() calls per row x three rows =
// ~18 tiny USB-CDC writes per sample, each blocking ~1 ms on the mbed stack.
// That is ~3 ms of pure write latency per sample — on its own enough to make
// 1 kHz impossible. Format into a buffer, push once (measured: 0.06 ms).
//
// Float without printf-%f (not linked on nano newlib): sign + integer +
// zero-padded 6-digit fraction, byte-identical to what Serial.print(v, 6) and
// pumpSensors() emit, so the host parser is unchanged.
// ── Sample-stream transport: USB-CDC (default) or UDP (fire-and-forget) ───
// With -D H7_TRANSPORT_UDP and after the host sends 'netcfg <ip> <port>' over
// serial, the src=1..5 lines go out as UDP datagrams. UDP has no flow control,
// so the send never blocks the M7 loop -> serviceSma() timing is never stalled
// by a slow host. Commands, [STATUS], boot banner stay on USB-CDC.
#if H7_TRANSPORT_UDP
static IPAddress   udp_h7_ip(169, 254, 245, 50);   // H7 static IP (direct link)
static const uint16_t udp_local_port = 7777;       // H7 socket port
static EthernetUDP  udp;
static IPAddress    udp_pc_ip;                      // set by 'netcfg'
static uint16_t     udp_pc_port = 0;                // 0 = not configured yet
static bool         udp_on = false;                 // armed after a valid netcfg
#endif

// Emit one already-formatted chunk of sample lines: a UDP datagram if streaming
// is armed, else USB-CDC (the original path). Callers keep len <= ~1400 (whole
// lines, under the Ethernet MTU) so each datagram carries only complete lines.
// Bytes dropped because the USB host stopped reading (TX buffer full). Published
// in [STATUS] as tx_drop so a wedged/closed host is VISIBLE, not a silent freeze.
static uint32_t tx_drop = 0;

static inline void streamWrite(const uint8_t* buf, size_t len) {
    if (len == 0) return;
#if H7_TRANSPORT_UDP
    if (udp_on) {
        udp.beginPacket(udp_pc_ip, udp_pc_port);
        udp.write(buf, len);
        udp.endPacket();
        return;
    }
#endif
    // USB-CDC: NEVER block. When the host stops reading (console closed), the
    // CDC TX buffer fills; a blocking Serial.write would stall the cooperative
    // M7 loop and FREEZE the state machine — leaving the SMA energized at its
    // last DAC code until a manual reset. Drop the chunk instead. The heartbeat
    // watchdog then safe-stops the coil, and the host 'force pull' wake
    // (operator_ccbringup.py --wake) drains whatever is buffered to revive the
    // stream without the reset button. availableForWrite() is the free space in
    // the CDC send buffer; if it can't hold the whole chunk we skip it, so the
    // subsequent write() is guaranteed not to block.
    if ((size_t)Serial.availableForWrite() < len) { tx_drop += len; return; }
    Serial.write(buf, len);
}

static char sma_batch[512];
static size_t sma_off = 0;

static void smaAppend(uint8_t src, int32_t raw, float volts,
                      uint32_t hw, uint32_t ms) {
    if (sizeof(sma_batch) - sma_off < 96) {          // guard: never overflow
        streamWrite((const uint8_t*)sma_batch, sma_off);
        sma_off = 0;
    }
    bool vneg = volts < 0.0f;
    float av  = vneg ? -volts : volts;
    unsigned long vip = (unsigned long)av;
    unsigned long vfp = (unsigned long)((av - (float)vip) * 1000000.0f + 0.5f);
    if (vfp >= 1000000UL) { vip++; vfp -= 1000000UL; }   // fraction carry
    int w = snprintf(sma_batch + sma_off, sizeof(sma_batch) - sma_off,
                     "%lu\t%d\t%ld\t%s%lu.%06lu\t%lu\t%lu\r\n",
                     (unsigned long)ms, (int)src, (long)raw,
                     vneg ? "-" : "", vip, vfp,
                     (unsigned long)hw, (unsigned long)sma_seq[src]++);
    if (w > 0) sma_off += (size_t)w;
}

static inline void smaFlush() {
    if (sma_off) {
        streamWrite((const uint8_t*)sma_batch, sma_off);   // ONE datagram/write
        sma_off = 0;
    }
}

// CLOCK — these lines are now stamped from M7's own micros()/millis(), NOT M4's.
//
// They used to carry last_m4_hw_us (the freshest sample M7 drained from the
// ring) so src=3/4/5 would share a timeline with the src=1/2 sensor lines. That
// was harmless at 100 Hz, but M4 produces at ~400-500 SPS, so its clock only
// advances every ~2 ms: at 1 kHz consecutive SMA samples would carry IDENTICAL
// timestamps and the cadence would be unmeasurable — 1 kHz of data with 500 Hz
// of time resolution.
//
// The two cores' clocks differ by a constant offset, and [STATUS] emits BOTH
// m7_us and m4_us once a second, so the host can still place src=3/4/5 on the M4
// timeline offline. In practice nothing needs to: the host analysis aligns
// streams on host_timestamp_s and only ever uses hw_us WITHIN a channel
// (relative to that channel's first sample), where the M7 clock is strictly
// better — it has real per-sample resolution.
#ifndef SMA_STAMP_M7
#define SMA_STAMP_M7 1
#endif
//
// with_cc adds the two CONTROLLER-STATE rows (src=6 command u, src=7 R_est).
// Skeleton §7 is emphatic about logging R_est: it is the adaptive state, so it
// is the only way to tell a controller problem from a load problem offline —
// and on an SMA the resistance dip during transformation makes it a free
// actuation sensor. It is streamed alongside the measured V/I/R, on the same
// timestamp, in one write.
static void streamSma(const SmaRead& s, bool with_cc = false) {
#if SMA_STAMP_M7
    uint32_t hw = micros();          // M7 clock — true per-sample resolution
    uint32_t ms = millis();
#else
    uint32_t hw = last_m4_hw_us;     // M4 clock — quantized to ~2 ms (legacy)
    uint32_t ms = last_m4_ms;
#endif
    // src=3 carries the MEASURED SMA voltage (A0 = SMA_P). Before 2026-07-24
    // it carried v_ldo; with A0 re-identified as SMA_P, v_ldo is a derived
    // quantity and streaming it would make the host's V/I disagree with the
    // firmware's own R on src=5.
    smaAppend(SAMPLE_SRC_SMA_V, (int32_t)currentCode, s.v_sma, hw, ms);
    smaAppend(SAMPLE_SRC_SMA_I, 0,                    s.i,     hw, ms);
    if (!isnan(s.r)) smaAppend(SAMPLE_SRC_SMA_R, 0,   s.r,     hw, ms);
    if (with_cc) {
        smaAppend(SAMPLE_SRC_CC_U, (int32_t)currentCode, cc_u_cmd, hw, ms);
        // R_est is meaningless before bootstrap latches it; omit rather than
        // emit a zero the host would have to know to discard.
        if (cc_R_valid) smaAppend(SAMPLE_SRC_CC_R, 0, cc_R_est, hw, ms);
    }
    smaFlush();                      // ONE USB-CDC write for the whole sample
}

// ══════════════════════════════════════════════════════════════════════
//  CONSTANT-CURRENT CONTROLLER — the control step
//  Transcription of CONTROL_SKELETON.md §3. Keep it recognisable against
//  that pseudocode; the comments here explain only what the port changed.
// ══════════════════════════════════════════════════════════════════════

// Drop all adaptive state. Called on every (re)start of a CC run — a stale
// R_est from a different load or a cold wire is worse than no estimate,
// because the feedforward would step straight to the wrong voltage.
static void ccReset() {
    cc_R_est   = 0.0f;
    cc_R_valid = false;
    cc_u_i     = 0.0f;
    cc_u_cmd   = 0.0f;
    cc_rail_s  = 0.0f;
    cc_fault   = false;
    cc_ticks   = 0;
    cc_last_us = micros();
    cc_next_us = cc_last_us;
}

// One control tick. Returns the electrical read it took, so the caller can
// stream it without paying for a second ADC pass.
static SmaRead ccStep(uint32_t now_us) {
    // TRUE elapsed time, not the nominal period: a tick that ran late must
    // integrate over the time that actually passed or the integral is wrong.
    // Clamped both ways so neither a double-call nor a long stall (a USB write
    // blocking the loop) can kick the integrator.
    float dt = (float)(uint32_t)(now_us - cc_last_us) * 1e-6f;
    cc_last_us = now_us;
    if (dt < CC_DT_MIN_S) dt = CC_DT_MIN_S;
    if (dt > CC_DT_MAX_S) dt = CC_DT_MAX_S;
    cc_ticks++;

    SmaRead s = readSma(ADC_SAMPLES_CYCLE);

    const float u_min = ccUMin();
    const float u_max = ccUMax();

    float err  = cc_i_target - s.i;
    float band = cc_i_target * CC_GATE_FRAC;
    if (band < CC_GATE_MIN_A) band = CC_GATE_MIN_A;
    const bool near = fabsf(err) < band;

    float u;

    if (!cc_R_valid) {
        // ---- BOOTSTRAP: plain integral, no feedforward (no trusted R yet) --
        cc_u_i += CC_KI_BOOT * err * dt;
        u = cc_u_i;
        if (u < u_min) u = u_min;
        if (u > u_max) u = u_max;
        cc_u_i = u;                          // integrator tracks the clamp
        const bool railed = (u <= u_min || u >= u_max);
        // Latch R from a VALID operating point: settled near target, or railed
        // (a railed point is still an honest u/I). cc_u_cmd is the command that
        // actually produced this reading — never seed from a stale/zero command
        // (skeleton pitfall 5: R~0 traps the loop there forever).
        if ((near || railed) && s.i > CC_I_FLOOR_A && cc_u_cmd > 0.0f) {
            float r = cc_u_cmd / s.i;
            if (r < CC_R_MIN) r = CC_R_MIN;
            if (r > CC_R_MAX) r = CC_R_MAX;
            cc_R_est   = r;
            cc_R_valid = true;
            cc_u_i     = 0.0f;               // hand the command to feedforward
        }
    } else {
        // ---- RUNNING: feedforward + auto-gain integral trim ---------------
        // R update is GATED to near-target: mid-step the current lags the
        // command, so u/I reads high on up-steps and low on down-steps
        // (pitfall 4). Freezing the estimate through the transient keeps it
        // honest. COMMAND domain (u/I), never V_ldo/I — see pitfall 2.
        if (near && s.i > CC_I_FLOOR_A && cc_u_cmd > 0.0f) {
            float r = cc_u_cmd / s.i;
            if (r < CC_R_MIN) r = CC_R_MIN;
            if (r > CC_R_MAX) r = CC_R_MAX;
            const float alpha = dt / (CC_R_TAU_S + dt);   // rate-independent
            cc_R_est += alpha * (r - cc_R_est);
        }

        const float u_ff = cc_i_target * cc_R_est;   // feedforward owns the step
        const float Ki   = cc_R_est / cc_tau_s;      // bandwidth-matched gain
        const float u_p  = cc_Kp * err;

        // Conditional integration: trim only near target. Far away, this Ki
        // would dump a huge voltage in a single cycle and overshoot; the
        // feedforward is what handles the transient (pitfall 3).
        if (near) cc_u_i += Ki * err * dt;

        u = u_ff + u_p + cc_u_i;

        // Back-calculation anti-windup: at a rail, rewrite the integrator to
        // exactly the value that produces the rail, so it cannot wind past the
        // actuator limit and stall the recovery.
        if (u < u_min) { u = u_min; cc_u_i = u - u_ff - u_p; }
        if (u > u_max) { u = u_max; cc_u_i = u - u_ff - u_p; }
    }

    cc_u_cmd = u;
    setDACraw(vldoToCode(u), false);         // false: no blocking settle delay

    // Open-load watchdog. Railed at the ceiling with no current means the
    // return path is broken, and unlike voltage mode the loop got there by
    // actively ramping up to force current through it. Park the output.
    if (u >= u_max - 1e-3f && s.i < CC_I_FLOOR_A) {
        cc_rail_s += dt;
        if (cc_rail_s >= CC_OPEN_LOAD_S) cc_fault = true;
    } else {
        cc_rail_s = 0.0f;
    }

    return s;
}

// ── Instant (non-state) commands ──────────────────────────────────────
static void cmdRead() {
    SmaRead s = readSma();
    smaTag();
    Serial.print(F("V_sma=")); Serial.print(s.v_sma, 4);   // measured (A0=SMA_P)
    Serial.print(F("V  I="));  Serial.print(s.i * 1000.0f, 2);
    Serial.print(F("mA  V_ldo~")); Serial.print(s.v_ldo, 4);  // derived
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
        if (cyc_mode == SP_CURRENT) {
            Serial.print(F("  [CURRENT] ih=")); Serial.print(cyc_i_high * 1000.0f, 1);
            Serial.print(F("mA il=")); Serial.print(cyc_i_low * 1000.0f, 1); Serial.print(F("mA"));
        } else {
            Serial.print(F("  [VOLTAGE] vh=")); Serial.print(cyc_v_high, 2);
            Serial.print(F(" v_idle=")); Serial.print(cyc_v_low, 2);
        }
        Serial.print(F(" t_high=")); Serial.print(cyc_fire_ms);
        Serial.print(F(" t_idle=")); Serial.println(cyc_cool_ms);
    } else {
        Serial.println(F("idle"));
    }
    // -- constant-current controller ------------------------------------
    smaTag(); Serial.print(F("CC loop           : "));
    Serial.print(cc_enabled ? F("CLOSED") : F("open"));
    Serial.print(F("  I_target=")); Serial.print(cc_i_target * 1000.0f, 1);
    Serial.print(F(" mA  u_cmd=")); Serial.print(cc_u_cmd, 3); Serial.println(F(" V"));
    smaTag(); Serial.print(F("CC R_est (u/I)    : "));
    if (cc_R_valid) { Serial.print(cc_R_est, 4); Serial.print(F(" ohm  (Ki=")); Serial.print(cc_R_est / cc_tau_s, 1); Serial.println(F(" V/A/s)")); }
    else            { Serial.println(F("-- (not bootstrapped)")); }
    smaTag(); Serial.print(F("CC tau / Kp       : ")); Serial.print(cc_tau_s * 1000.0f, 2);
        Serial.print(F(" ms / ")); Serial.println(cc_Kp, 3);
    smaTag(); Serial.print(F("CC u clamp        : ")); Serial.print(ccUMin(), 3);
        Serial.print(F(" - ")); Serial.print(ccUMax(), 3); Serial.print(F(" V  -> reachable I "));
    if (cc_R_valid && cc_R_est > 0.0f) {
        Serial.print(ccUMin() / cc_R_est * 1000.0f, 0); Serial.print(F(" - "));
        Serial.print(ccUMax() / cc_R_est * 1000.0f, 0); Serial.println(F(" mA"));
    } else Serial.println(F("(needs R_est)"));
    smaTag(); Serial.print(F("CC rate           : ")); Serial.print(1000000UL / CC_PERIOD_US);
        Serial.print(F(" Hz nominal, ticks=")); Serial.println(cc_ticks);
    smaTag(); Serial.print(F("watchdog          : "));
    if (wdt_timeout_ms) { Serial.print(wdt_timeout_ms); Serial.println(F(" ms (send 'ping')")); }
    else                  Serial.println(F("disabled"));
    smaTag(); Serial.print(F("heartbeat (PC)    : "));
    if (hb_timeout_ms) { Serial.print(hb_timeout_ms); Serial.println(F(" ms -> DISARM if PC silent")); }
    else                 Serial.println(F("disabled (hb 0)"));
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
    cc_enabled = false;                   // drop the loop FIRST: with the return
                                          // path open the current reads ~0, and a
                                          // live loop would read that as "way
                                          // under target" and ramp to the rail
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
    // v_smap_meas: A0 = SMA_P, so this trails v_pred (an LDO-output model) by
    // I*R_SHUNT whenever the wire is drawing current. Sweep with the return
    // path open (disarmed) if you want a clean DAC->LDO fit.
    if (csv) { smaTag(); Serial.println(F("dac_code,v_pred,v_smap_meas")); }
    else     { smaTag(); Serial.println(F("Code  V_pred  V_smap")); }
    setDACraw((uint16_t)(sw_c > 4095 ? 4095 : sw_c));
    settleBegin();
    smaState = SMA_SWEEP_SETTLE;
}

// ── Actuation engine helpers (drive/fire/cycle all use these) ──────────
// Engage or release the current loop for a phase. Engaging keeps the adaptive
// state across phases WITHIN one run (the wire is the same wire, so R_est is
// still the best available estimate and the next phase starts with a correct
// feedforward instead of re-bootstrapping); ccReset() only happens at run start.
static void ccEngage(float i_target) {
    cc_i_target = i_target;
    cc_enabled  = true;
    cc_last_us  = micros();      // fresh dt reference; do NOT integrate the gap
    cc_next_us  = cc_last_us;
    // Seed from the command ALREADY applied to the DAC (V_IDLE on the first
    // phase, the loop's own last write on later ones). cc_u_cmd means "the
    // command that produced the current about to be read", so this is simply
    // the truth — and it makes the very first reading eligible to bootstrap
    // R_est instead of being discarded for having no valid command behind it.
    cc_u_cmd = codeToVldo(currentCode);
    // Starting the bootstrap integral from the applied command rather than 0
    // saves the ~100 ms the CC_KI_BOOT ramp would otherwise spend climbing
    // from the LDO floor back to where the output already sits.
    if (!cc_R_valid) cc_u_i = cc_u_cmd;
}
static void ccRelease() {
    cc_enabled = false;
    setLevel(V_IDLE);            // park at a known, safe voltage
}

// HEAT: hold the high setpoint for t_high, TRIG high (scope sync). Armed already.
static void cycleEnterHigh() {
    digitalWrite(TRIG_PIN, HIGH);
    if (cyc_fire) delayMicroseconds(5);     // clean pre-step edge for the scope
    if (cyc_mode == SP_CURRENT) ccEngage(cyc_i_high);
    else                        setLevel(cyc_v_high);
    cyc_phase_t0 = millis();
    cyc_next_log = 0;
    smaState = SMA_ACT_HEAT;
    smaTag(); Serial.print(F("[ACT] heat n=")); Serial.print(cyc_n_done + 1);
    if (cyc_n_target) { Serial.print('/'); Serial.print(cyc_n_target); }
    if (cyc_mode == SP_CURRENT) { Serial.print(F(" I=")); Serial.print(cyc_i_high * 1000.0f, 1); Serial.print(F("mA")); }
    else                        { Serial.print(F(" V=")); Serial.print(cyc_v_high, 3); }
    Serial.print(F(" ms=")); Serial.println(cyc_fire_ms);
}
// COOL: hold the low setpoint for t_idle, TRIG low.
static void cycleEnterLow() {
    digitalWrite(TRIG_PIN, LOW);
    if (cyc_mode == SP_CURRENT) {
        // A nonzero cool current is worth holding closed-loop: it keeps R
        // observable through the cool phase (self-sensing) at a heating power
        // too low to actuate. A zero cool target is not a control problem —
        // it is "stop driving", so open the loop and park at the idle voltage.
        if (cyc_i_low > 0.0f) ccEngage(cyc_i_low);
        else                  ccRelease();
    } else {
        setLevel(cyc_v_low);
    }
    cyc_phase_t0 = millis();
    cyc_next_log = 0;
    smaState = SMA_ACT_COOL;
    smaTag(); Serial.print(F("[ACT] cool n=")); Serial.print(cyc_n_done + 1);
    if (cyc_n_target) { Serial.print('/'); Serial.print(cyc_n_target); }
    if (cyc_mode == SP_CURRENT) { Serial.print(F(" I=")); Serial.print(cyc_i_low * 1000.0f, 1); Serial.print(F("mA")); }
    else                        { Serial.print(F(" V=")); Serial.print(cyc_v_low, 3); }
    Serial.print(F(" ms=")); Serial.println(cyc_cool_ms);
}
// End the run → idle-low voltage, STILL ARMED (relaunch-able). The only
// hard cutoff is `disarm`/`abort`. Used by `stop`, the heat watchdog, run end.
static void cycleStop(const __FlashStringHelper* reason) {
    digitalWrite(TRIG_PIN, LOW);
    cc_enabled = false;              // release the loop before parking, or the
    setLevel(V_IDLE);                // next tick would immediately re-drive
    smaState = SMA_IDLE;
    smaTag(); Serial.print(F("[ACT] -> idle (")); Serial.print(reason);
    Serial.print(F(") after ")); Serial.print(cyc_n_done);
    Serial.println(F(" cycle(s); still armed"));
}
// Start a VOLTAGE-mode run. Caller must ensure armed. fire = scope-trig preset.
static void startCycle(float v_high, float v_idle,
                       uint32_t t_high, uint32_t t_idle, uint32_t n, bool fire) {
    cyc_mode     = SP_VOLTAGE;
    cc_enabled   = false;
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
// Start a CURRENT-mode run — same engine, same safety, amps instead of volts.
static void startCycleCC(float i_high, float i_low,
                         uint32_t t_high, uint32_t t_idle, uint32_t n, bool fire) {
    cyc_mode     = SP_CURRENT;
    cyc_i_high   = i_high;
    cyc_i_low    = i_low;
    cyc_v_high   = 0.0f;             // unused in this mode; keep them clear so
    cyc_v_low    = 0.0f;             // `info` can't print a stale voltage
    cyc_fire_ms  = t_high;
    cyc_cool_ms  = t_idle;
    cyc_n_target = n;
    cyc_n_done   = 0;
    cyc_fire     = fire;
    ccReset();                       // new run = new load; never inherit R_est
    wdt_last_ping = millis();
    smaTag(); Serial.print(fire ? F("[CCFIRE] ") : F("[CC] start "));
    Serial.print(F("i_high=")); Serial.print(i_high * 1000.0f, 1);
    Serial.print(F("mA i_low=")); Serial.print(i_low * 1000.0f, 1);
    Serial.print(F("mA t_high=")); Serial.print(t_high);
    Serial.print(F(" t_idle=")); Serial.print(t_idle);
    Serial.print(F(" n=")); Serial.print(n);
    Serial.print(F(" (0=cont) tau_ms=")); Serial.print(cc_tau_s * 1000.0f, 1);
    Serial.print(F(" wdt_ms=")); Serial.println(wdt_timeout_ms);
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

// Host-liveness heartbeat: DISARM if the PC has gone silent while anything is
// energised. Runs every loop pass, independent of the SMA state machine, so it
// covers armed-idle and manual `cc`/`drive` holds that the cycle watchdog above
// never sees. Disarmed = nothing to guard, so the window is held open (reset)
// until the next arm — arming with a dead PC then trips one timeout later,
// which is the intended fail-safe, not a nuisance.
static void serviceHeartbeat() {
    if (hb_timeout_ms == 0 || !armed) { hb_last_ms = millis(); return; }
    if ((uint32_t)(millis() - hb_last_ms) > hb_timeout_ms) {
        disarm();                        // full safe-stop: MOSFET open, DAC parked
        smaTag();
        Serial.print(F("[HB] FAULT: PC silent > "));
        Serial.print(hb_timeout_ms);
        Serial.println(F(" ms — DISARMED (safe-stop). Re-arm to resume."));
        hb_last_ms = millis();           // one report per timeout, not a spam loop
    }
}

// ── One pass of an actuation phase (HEAT or COOL) ──────────────────────
// Shared by both setpoint modes. Returns true when the phase duration has
// elapsed, so the caller can advance the cycle. Returns false after a fault
// (the machine has already been parked and disarmed) — the caller must not
// advance a phase on a disarmed machine.
static bool serviceActuationPhase(uint32_t phase_ms) {
    const uint32_t t_rel = millis() - cyc_phase_t0;

    if (cc_enabled) {
        const uint32_t now_us = micros();
        // Wrap-safe due-check: micros() rolls over every ~71 min and a run can
        // straddle that.
        if ((int32_t)(now_us - cc_next_us) < 0) return t_rel >= phase_ms;

        const SmaRead s = ccStep(now_us);

        cc_next_us += CC_PERIOD_US;
        // If a pass ran long (a blocking USB write, a slow sensor drain) the
        // schedule is behind. Skip the missed ticks instead of firing a burst
        // of catch-up steps: ccStep integrates the TRUE elapsed dt, so the
        // control effort is already correct — replaying the backlog would
        // double-count it.
        if ((int32_t)(micros() - cc_next_us) > 0) cc_next_us = micros() + CC_PERIOD_US;

        if (cc_fault) {
            disarm();                       // open the return path, park the DAC
            smaTag(); Serial.print(F("[CC] FAULT: open load — command railed at "));
            Serial.print(ccUMax(), 2); Serial.print(F(" V with I<"));
            Serial.print(CC_I_FLOOR_A * 1000.0f, 0);
            Serial.println(F("mA. DISARMED (check the SMA/clips for a break)."));
            return false;
        }

        if (t_rel >= cyc_next_log) {        // stream the read ccStep already took
            streamSma(s, true);             // ...with the controller state
            cyc_next_log += CYCLE_LOG_MS;
            // A control period longer than the log period (e.g. the cc200
            // build: 5 ms control, 1 ms log) would leave the schedule
            // permanently behind and drifting further every tick. The stream
            // can never beat the control rate anyway — one sample per tick is
            // the real ceiling — so snap forward instead of accumulating lag.
            if (cyc_next_log < t_rel) cyc_next_log = t_rel + CYCLE_LOG_MS;
        }
    } else {
        if (t_rel >= cyc_next_log) {        // voltage mode: sample + stream only
            streamSma(readSma(ADC_SAMPLES_CYCLE));   // ~1 kHz — average in post
            cyc_next_log += CYCLE_LOG_MS;
        }
    }
    return t_rel >= phase_ms;
}

// ── Per-pass service of the active op (one step, never blocks long) ────
static void serviceSma() {
    // DAC-link fault takes precedence over every op, in BOTH setpoint modes —
    // a frozen output is not something any state can usefully continue into.
    // Checked here rather than in the CC branch alone so a dropout during
    // drive/fire/cycle/step/sweep is caught too.
    if (dac_lost) {
        bool was_armed = armed;
        disarm();                 // MOSFET gate is a GPIO — works without I2C
        // Clear AFTER disarm: disarm's own DAC write will also fail, and we do
        // not want that failure to re-latch the fault we are already reporting.
        dac_lost     = false;
        dac_fail_run = 0;
        smaTag();
        Serial.print(F("[DAC] FAULT: MCP4728 stopped ACKing ("));
        Serial.print(DAC_FAIL_MAX);
        Serial.print(F(" consecutive failed writes, "));
        Serial.print(dac_fail_total);
        Serial.println(F(" total)."));
        smaTag();
        if (was_armed) {
            Serial.println(F("      DISARMED — the DAC held its last code, so "
                             "current was still flowing until now."));
        } else {
            Serial.println(F("      Output was already disarmed."));
        }
        smaTag();
        Serial.println(F("      Check the I2C wiring/power (PB_6 SDA, PB_7 SCL) "
                         "then `reset` to re-scan the bus."));
        return;
    }

    switch (smaState) {
        case SMA_IDLE:
            // Armed + resting at idle: stream V/I/R so the host sees the
            // idle-hold live. Disarmed = no current = nothing to report.
            if (armed && (int32_t)(millis() - idle_next_log) >= 0) {
                streamSma(readSma(ADC_SAMPLES_IDLE));   // 10 Hz — precision, duty ~1.5%
                idle_next_log = millis() + IDLE_LOG_MS;
            }
            return;

        case SMA_SET_SETTLE:
            if (settleService()) {
                float vmeas = readSmaP();
                float err   = vmeas - op_vtarget;
                smaTag();
                Serial.print(F("Target=")); Serial.print(op_vtarget, 3);
                Serial.print(F("V  Code=")); Serial.print(op_code);
                Serial.print(F("  V_pred=")); Serial.print(codeToVldo(op_code), 3);
                Serial.print(F("  V_smap=")); Serial.print(vmeas, 3);
                Serial.print(F("V  err="));
                if (err >= 0) Serial.print('+');
                Serial.print(err * 1000.0f, 1); Serial.println(F("mV"));
                smaState = SMA_IDLE;
            }
            return;

        case SMA_CODE_SETTLE:
            if (settleService()) {
                float vsmap = readSmaP();
                smaTag();
                Serial.print(F("Code=")); Serial.print(op_code);
                Serial.print(F("  V_dac~")); Serial.print(codeToVdac(op_code), 3);
                Serial.print(F("  V_pred=")); Serial.print(codeToVldo(op_code), 3);
                Serial.print(F("  V_smap_meas=")); Serial.print(vsmap, 3);
                Serial.println('V');
                smaState = SMA_IDLE;
            }
            return;

        case SMA_STEPPING: {
            uint32_t t_rel = millis() - op_t0;
            if (t_rel >= op_hold_ms) {
                smaTag(); Serial.print(F("[STEP] done V_final=")); Serial.println(readSmaP(), 4);
                smaState = SMA_IDLE;
                return;
            }
            if (t_rel >= op_next_log) {
                smaTag(); Serial.print(t_rel); Serial.print('\t'); Serial.println(readSmaP(), 4);
                op_next_log += 10;
            }
            return;
        }

        case SMA_SWEEP_SETTLE:
            if (settleService()) {
                uint16_t code  = (uint16_t)(sw_c > 4095 ? 4095 : sw_c);
                float    vmeas = readSmaP();
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
            if (serviceActuationPhase(cyc_fire_ms)) cycleEnterLow();
            return;
        }

        case SMA_ACT_COOL: {
            // Cooling is self-safe (low/zero power) → no watchdog, EXCEPT in
            // current mode with a nonzero cool target, where the loop is still
            // actively driving and a silent host must still be caught.
            if (cc_enabled && cycleWatchdogTripped()) return;
            if (serviceActuationPhase(cyc_cool_ms)) {
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
    // ANY received command proves the PC is alive, so it refreshes the
    // host-liveness heartbeat — not just an explicit `ping`. An actively driven
    // session therefore never trips the safe-stop.
    hb_last_ms = millis();
    String low = in;
    low.toLowerCase();

    // ---- always-allowed (instant / safety / params) ----
    if (low == "info")  { cmdInfo(); return; }
    if (low == "read")  { cmdRead(); return; }
    if (low == "abort") { abortSma(); return; }
    if (low == "ping")  { wdt_last_ping = millis(); hb_last_ms = millis(); return; }  // heartbeat (silent)
    if (low.startsWith("hb ")) {                     // host-liveness timeout (ms)
        long ms = in.substring(3).toInt();
        if (ms < 0) { smaTag(); Serial.println(F("[HB] usage: hb <ms> (0=off)")); return; }
        hb_timeout_ms = (uint32_t)ms;
        hb_last_ms = millis();
        smaTag(); Serial.print(F("[HB] hb_timeout_ms=")); Serial.print(hb_timeout_ms);
        Serial.println(hb_timeout_ms ? F("") : F(" (heartbeat DISABLED)"));
        return;
    }
#if H7_TRANSPORT_UDP
    if (low.startsWith("netcfg ")) {                // netcfg <a.b.c.d> <port>
        String rest = in.substring(7); rest.trim();
        int sp = rest.indexOf(' ');
        if (sp < 0) { smaTag(); Serial.println(F("[NET] usage: netcfg <ip> <port>")); return; }
        String ipStr = rest.substring(0, sp);
        long port = rest.substring(sp + 1).toInt();
        int oct[4], parts = 0, start = 0;
        for (int i = 0; i <= (int)ipStr.length() && parts < 4; i++) {
            if (i == (int)ipStr.length() || ipStr[i] == '.') {
                oct[parts++] = ipStr.substring(start, i).toInt();
                start = i + 1;
            }
        }
        if (parts != 4 || port <= 0 || port > 65535) {
            smaTag(); Serial.println(F("[NET] bad netcfg args")); return;
        }
        udp_pc_ip   = IPAddress(oct[0], oct[1], oct[2], oct[3]);
        udp_pc_port = (uint16_t)port;
        udp_on      = true;                          // sample stream now on UDP
        smaTag(); Serial.print(F("[NET] UDP stream -> "));
        Serial.print(udp_pc_ip); Serial.print(':'); Serial.println(udp_pc_port);
        return;
    }
#endif
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

    // ---- constant-current tuning (safe to change mid-run) ----
    if (low.startsWith("tau ")) {                  // closed-loop time constant
        float ms = in.substring(4).toFloat();
        // Lower bound is a few control periods: below that the loop is reacting
        // faster than it can observe and it rings (skeleton §6).
        float ms_min = (float)CC_PERIOD_US * 3e-3f;
        if (ms < ms_min || ms > 1000.0f) {
            smaTag(); Serial.print(F("Range: ")); Serial.print(ms_min, 1);
            Serial.println(F(" - 1000 ms")); return;
        }
        cc_tau_s = ms * 1e-3f;
        smaTag(); Serial.print(F("[CC] tau=")); Serial.print(ms, 2);
        Serial.print(F(" ms  (Ki=R/tau; at R=")); Serial.print(cc_R_est, 3);
        Serial.print(F(" -> ")); Serial.print(cc_R_est / cc_tau_s, 1);
        Serial.println(F(" V/A/s)"));
        return;
    }
    if (low.startsWith("ccgain ")) {               // proportional term (default 0)
        float kp = in.substring(7).toFloat();
        if (kp < 0.0f || kp > 100.0f) { smaTag(); Serial.println(F("Range: 0 - 100 V/A")); return; }
        cc_Kp = kp;
        smaTag(); Serial.print(F("[CC] Kp=")); Serial.println(cc_Kp, 3);
        return;
    }
    if (low == "cc") {                             // bare `cc` = controller status
        smaTag(); Serial.print(F("[CC] "));
        Serial.print(cc_enabled ? F("CLOSED") : F("open"));
        Serial.print(F("  I_target=")); Serial.print(cc_i_target * 1000.0f, 1);
        Serial.print(F("mA  u=")); Serial.print(cc_u_cmd, 3);
        Serial.print(F("V  R_est="));
        if (cc_R_valid) Serial.print(cc_R_est, 3); else Serial.print(F("-- (not bootstrapped)"));
        Serial.print(F("  tau=")); Serial.print(cc_tau_s * 1000.0f, 2);
        Serial.print(F("ms  ticks=")); Serial.println(cc_ticks);
        return;
    }

    // ---- constant-current actuation (checked BEFORE the "cc " prefix) ----
    if (low.startsWith("cccycle ")) {
        // cccycle <i_high_mA> <i_low_mA> <t_high_ms> <t_idle_ms> <n>
        // The current-mode twin of `cycle`: same engine, same watchdog.
        // i_low = 0 → the cool phase opens the loop and parks at V_IDLE.
        if (rejectIfNoDac() || rejectIfBusy()) return;
        if (!armed) { smaTag(); Serial.println(F("not armed — send 'arm' first")); return; }
        String rest = in.substring(8); rest.trim();
        float args_f[2]; uint32_t args_u[3];
        int idx = 0; bool ok = true;
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
            smaTag(); Serial.println(F("Usage: cccycle <i_high_mA> <i_low_mA> <t_high_ms> <t_idle_ms> <n>"));
            return;
        }
        float ih = args_f[0] * 1e-3f, il = args_f[1] * 1e-3f;
        uint32_t t_high = args_u[0], t_idle = args_u[1], n = args_u[2];
        if (ih <= 0.0f || ih > CC_I_MAX_A || il < 0.0f || il > CC_I_MAX_A) {
            smaTag(); Serial.print(F("ERR: current out of range (0, "));
            Serial.print(CC_I_MAX_A * 1000.0f, 0); Serial.println(F("] mA")); return;
        }
        if (t_high == 0 || t_high > CYCLE_MS_MAX || t_idle == 0 || t_idle > CYCLE_MS_MAX) {
            smaTag(); Serial.print(F("ERR: phase ms out of range (1, "));
            Serial.print(CYCLE_MS_MAX); Serial.println(F("]")); return;
        }
        startCycleCC(ih, il, t_high, t_idle, n, false);
        return;
    }
    if (low.startsWith("ccfire ")) {               // ccfire <mA> [ms] : n=1 + scope trig
        if (rejectIfNoDac() || rejectIfBusy()) return;
        if (!armed) { smaTag(); Serial.println(F("not armed — send 'arm' first")); return; }
        String rest = in.substring(7); rest.trim();
        if (rest.length() == 0) { smaTag(); Serial.println(F("Usage: ccfire <mA> [ms]")); return; }
        int sp = rest.indexOf(' ');
        float    ma = (sp < 0 ? rest : rest.substring(0, sp)).toFloat();
        uint32_t ms = (sp < 0) ? 500 : (uint32_t)rest.substring(sp + 1).toInt();
        if (ma <= 0.0f || ma * 1e-3f > CC_I_MAX_A) {
            smaTag(); Serial.print(F("ERR: current out of range (0, "));
            Serial.print(CC_I_MAX_A * 1000.0f, 0); Serial.println(F("] mA")); return;
        }
        if (ms == 0 || ms > CYCLE_MS_MAX) ms = 500;
        startCycleCC(ma * 1e-3f, 0.0f, ms, 0, 1, true);
        return;
    }
    if (low.startsWith("cc ")) {                   // cc <mA> [ms] : hold, or RETARGET
        if (rejectIfNoDac()) return;
        String rest = in.substring(3); rest.trim();
        int sp = rest.indexOf(' ');
        float    ma = (sp < 0 ? rest : rest.substring(0, sp)).toFloat();
        uint32_t ms = (sp < 0) ? 5000 : (uint32_t)rest.substring(sp + 1).toInt();
        if (ma <= 0.0f || ma * 1e-3f > CC_I_MAX_A) {
            smaTag(); Serial.print(F("ERR: current out of range (0, "));
            Serial.print(CC_I_MAX_A * 1000.0f, 0); Serial.println(F("] mA")); return;
        }
        const float i_new = ma * 1e-3f;

        // RETARGET IN PLACE if a current run is already up. This is what makes
        // step-response testing possible (`cc 200` → `cc 800` → `cc 200` in one
        // capture) and it deliberately KEEPS the adaptive state: R_est is a
        // property of the wire, not of the setpoint, so the feedforward jumps
        // straight to the right voltage for the new target instead of
        // re-bootstrapping into it. This is the H7 twin of the Uno's `cc <mA>`.
        if (cc_enabled && (smaState == SMA_ACT_HEAT || smaState == SMA_ACT_COOL)) {
            cyc_i_high  = i_new;
            cc_i_target = i_new;
            smaTag(); Serial.print(F("[CC] retarget -> ")); Serial.print(ma, 1);
            Serial.print(F(" mA (R_est="));
            if (cc_R_valid) Serial.print(cc_R_est, 3); else Serial.print(F("--"));
            Serial.println(F(" ohm kept)"));
            return;
        }

        if (rejectIfBusy()) return;
        if (!armed) { smaTag(); Serial.println(F("not armed — send 'arm' first")); return; }
        if (ms == 0 || ms > CYCLE_MS_MAX) ms = 5000;

        // Reachability warning (skeleton pitfall 6). Only meaningful if a prior
        // run left a resistance estimate; a railed loop is CV, not a bug, and
        // saying so up front saves a debugging cycle.
        if (cc_R_valid && cc_R_est > 0.0f) {
            float i_lo = ccUMin() / cc_R_est, i_hi = ccUMax() / cc_R_est;
            if (i_new < i_lo || i_new > i_hi) {
                smaTag(); Serial.print(F("[CC] WARN: target outside the reachable band ["));
                Serial.print(i_lo * 1000.0f, 0); Serial.print(F(", "));
                Serial.print(i_hi * 1000.0f, 0);
                Serial.print(F("] mA at R=")); Serial.print(cc_R_est, 3);
                Serial.println(F(" ohm — the loop will rail (CV, not CC)"));
            }
        }
        startCycleCC(i_new, 0.0f, ms, 0, 1, false);
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
        // Flush at <= ~1400 B of WHOLE lines so each USB write / UDP datagram
        // stays under the Ethernet MTU and carries only complete lines.
        if (off >= 1400 || sizeof(batch) - off < 96) {
            streamWrite((const uint8_t*)batch, off);
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
    if (off) streamWrite((const uint8_t*)batch, off);    // flush the tail

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

        // Non-blocking guard: [STATUS] is ~350 chars of individual prints. If
        // the host has stopped reading, skip the whole frame rather than block
        // partway through (which would freeze the loop). One check up front, so
        // the prints below are guaranteed room. UDP mode still emits [STATUS] on
        // serial, so this guard applies there too. Counters keep accumulating
        // when skipped — no data window is silently zeroed.
        if ((size_t)Serial.availableForWrite() < 384) { tx_drop++; } else {
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
        // dac_err: cumulative failed MCP4728 writes. Nonzero means the I2C link
        // to the DAC is marginal even if it has not yet tripped DAC_FAIL_MAX in
        // a row — the early warning for an intermittent bus, visible before it
        // becomes a fault mid-run.
        Serial.print(" dac_err=");        Serial.print(dac_fail_total);
        Serial.print(" tx_drop=");        Serial.print(tx_drop);
        Serial.print(" hb_ms=");          Serial.print(hb_timeout_ms);
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
        // M7 loop period. serviceSma() streams at most ONE sample per loop pass,
        // so loop_hz is a HARD CEILING on the SMA rate no matter what
        // CYCLE_LOG_MS says. If the achieved SMA rate ever plateaus below
        // 1/CYCLE_LOG_MS, compare it against loop_hz first — that is the wall.
        {
            extern uint32_t loop_dt_sum, loop_dt_max, loop_n;
            uint32_t avg = loop_n ? (loop_dt_sum / loop_n) : 0;
            Serial.print(" loop_us_avg=");  Serial.print(avg);
            Serial.print(" loop_us_max=");  Serial.print(loop_dt_max);
            Serial.print(" loop_hz=");      Serial.print(avg ? (1000000UL / avg) : 0);
            Serial.print(" cycle_log_ms="); Serial.print(CYCLE_LOG_MS);
            Serial.print(" n_cycle=");      Serial.print(ADC_SAMPLES_CYCLE);
            loop_dt_sum = 0; loop_dt_max = 0; loop_n = 0;
        }
        // Constant-current controller state. cc_hz is the ACHIEVED control rate
        // (ticks since the last frame), not the nominal one — if it sits below
        // 1000/CC_PERIOD_US the loop is being starved by the rest of the pass
        // and tau is no longer the time constant you think it is.
        {
            static uint32_t last_cc_ticks = 0;
            // ccReset() zeroes cc_ticks at every run start, so the counter can
            // go BACKWARDS between frames; an unsigned subtraction would then
            // report ~4 billion Hz.
            uint32_t d = (cc_ticks >= last_cc_ticks) ? (cc_ticks - last_cc_ticks) : cc_ticks;
            last_cc_ticks = cc_ticks;
            Serial.print(" cc=");        Serial.print(cc_enabled ? 1 : 0);
            Serial.print(" cc_hz=");     Serial.print(d);
            Serial.print(" cc_i_tgt=");  Serial.print(cc_i_target, 4);
            Serial.print(" cc_u=");      Serial.print(cc_u_cmd, 4);
            Serial.print(" cc_r=");      Serial.print(cc_R_valid ? cc_R_est : 0.0f, 4);
            Serial.print(" cc_tau_ms="); Serial.print(cc_tau_s * 1000.0f, 2);
        }
        Serial.println();
        }   // end availableForWrite guard
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
    Serial.println("[M7] Firmware_SMAConstantCurrent_PIO — bridge + SMA controller + CC loop");
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
    // 400 kHz (MCP4728 fast-mode ceiling). The DAC write is the control loop's
    // output stage and runs inside every tick, so its duration is a floor on
    // the achievable control period: a 3-byte write is ~270 us at the 100 kHz
    // default and ~70 us here. At a 1 ms period that is the difference between
    // 27% and 7% of the budget spent talking to the DAC.
    //
    // THIS IS THE ONE I2C DIFFERENCE vs Firmware_SMASensorHub_PIO, which never
    // calls setClock() and therefore runs the same bus at the 100 kHz default.
    // If the DAC enumerates reliably under the sensor hub but intermittently
    // here, speed is the variable — fast mode allows a 300 ns rise time where
    // standard mode allows 1000 ns, so weak pull-ups or cable capacitance that
    // pass at 100 kHz can fail at 400 kHz. Build with -D I2C_HZ=100000 to A/B
    // it without editing code; a bus that only works at 100 kHz is marginal and
    // should be FIXED (pull-ups / lead length / level translation), not merely
    // slowed down — but slowing it is a legitimate way to prove the diagnosis,
    // and an acceptable fallback on the cc200 build where a 270 us write is
    // only 5% of the 5 ms tick.
#ifndef I2C_HZ
#define I2C_HZ 400000
#endif
    Wire.setClock(I2C_HZ);
    smaTag(); Serial.print(F("I2C clock: ")); Serial.print(I2C_HZ / 1000);
    Serial.println(F(" kHz"));
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
    smaTag(); Serial.println(F("      VOLTAGE: drive <V> <ms> | fire <V> [ms] |"));
    smaTag(); Serial.println(F("               cycle <v_high> <v_idle> <t_high_ms> <t_idle_ms> <n>  (n=0 cont) |"));
    smaTag(); Serial.println(F("      CURRENT: cc <mA> [ms] (retargets live) | ccfire <mA> [ms] |"));
    smaTag(); Serial.println(F("               cccycle <i_high_mA> <i_low_mA> <t_high_ms> <t_idle_ms> <n> |"));
    smaTag(); Serial.println(F("               cc (status) | tau <ms> | ccgain <Kp> |"));
    smaTag(); Serial.println(F("      ping (heartbeat) | stop | wdt <ms> (0=off) | hb <ms> (PC-silent disarm) | abort |"));
    smaTag(); Serial.println(F("      step <code>[ms] | sweep|csv [step] | read | info |"));
    smaTag(); Serial.println(F("      gain|shunt|ioffset|vdd|offset|aref <x> | reset"));
    smaTag(); Serial.print(F("CC loop: ")); Serial.print(1000000UL / CC_PERIOD_US);
    Serial.print(F(" Hz, tau=")); Serial.print(cc_tau_s * 1000.0f, 1);
    Serial.println(F(" ms; stream adds src=6 (u cmd) + src=7 (R_est)"));

#if H7_TRANSPORT_UDP
    // Bring up Ethernet (static IP, no DHCP). The src=1..5 stream stays on
    // USB-CDC until the host sends 'netcfg <pc_ip> <port>'.
    Ethernet.begin(udp_h7_ip);
    udp.begin(udp_local_port);
    smaTag(); Serial.print(F("[NET] H7 IP ")); Serial.print(Ethernet.localIP());
    Serial.print(F("  link ")); Serial.println(Ethernet.linkStatus() == LinkON ? F("ON") : F("?"));
    smaTag(); Serial.println(F("[NET] UDP build — send 'netcfg <pc_ip> <port>' to move the stream to UDP"));
#endif
}

// M7 loop-period instrumentation. serviceSma() streams at most ONE sample per
// loop pass, so the SMA rate can never exceed the loop rate regardless of
// CYCLE_LOG_MS — to hold 1 kHz the whole loop must sustain <1000 us per pass.
// Reported in [STATUS] once a second so the bench sees the real ceiling instead
// of inferring it.
uint32_t loop_dt_sum = 0;
uint32_t loop_dt_max = 0;
uint32_t loop_n      = 0;

void loop() {
    static uint32_t last_loop_us = 0;
    uint32_t now_us = micros();
    if (last_loop_us) {
        uint32_t dt = now_us - last_loop_us;
        loop_dt_sum += dt;
        if (dt > loop_dt_max) loop_dt_max = dt;
        loop_n++;
    }
    last_loop_us = now_us;

    pumpSensors();                  // keep the sensor stream flowing (every pass)
    String line;
    if (pollCommand(line)) dispatch(line);
    serviceSma();                   // advance the active SMA op by one step
    serviceHeartbeat();             // safe-stop if the PC has gone silent
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
