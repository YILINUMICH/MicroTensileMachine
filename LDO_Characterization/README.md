# LDO_Characterization

> **Status: To-Test.** See [STATUS.md](STATUS.md). Plan: [`../docs/PLAN_phase6_ldo_characterization.md`](../docs/PLAN_phase6_ldo_characterization.md).

Automated time-domain characterization of the MCP4728 → TPS7A57 **DAC-margining**
LDO structure used by [`../SMA_Driver_PIO/`](../SMA_Driver_PIO/). Answers two
questions with the SDS2000X Plus scope as the instrument:

1. **How fast** does `V_LDO` settle after a DAC step? (settling time, overshoot, rise)
2. **How clean** is `V_LDO`? (steady-state ripple / noise, loaded vs unloaded)

DC accuracy is intentionally **out of scope** — the analytical model already meets
the SMA's ±0.1 V need; use the firmware `sweep`/`csv` commands for that.

## How it works

The firmware emits a **hardware trigger edge at the instant of the DAC write**
(new `fire` command pulses `TRIG_PIN` = PJ_11 / PWM4), so the scope can single-shot the
transient with a clean pre-trigger baseline. The orchestrator arms the scope,
fires, waits for capture-complete, and pulls the raw waveforms.

```
 H7 (SMA_Driver_PIO)                     SDS2000X Plus
   PJ_11/PWM4 TRIG ────────────────────► C1  (edge = t0)
   MCP4728 V_OUT (DAC node) ───────────► C2
   TPS7A57 V_OUT (LDO out)  ───────────► C3
   INA296A V_OUT (shunt I)  ───────────► C4   (optional; 1 V/A)
   USB serial (COM8) ◄── run_experiment.py ──► LAN :5025
```

**Current sense:** after the LDO output a **100 mΩ shunt + INA296A (A1, 10 V/V)**
gives **1 V/A**. The firmware reads it on A1 and reports `I`, `V_sma`, and
`R_sma = V_sma/I` (A0 taps before the shunt, so `V_sma = V_ldo − I·0.1`). Probe the
INA OUT on scope **C4** to also see inrush at the step edge. Set `channels.current:
null` in `config.yaml` to skip the C4 capture.

Loaded vs unloaded is selected by the `mosfet on|off` firmware command driving a
**fixed power resistor** (repeatable; isolates the LDO from SMA thermal drift).

## Files

| File | Role |
|---|---|
| `config.yaml` | All knobs: ports, channel map, step matrix, loads, repeats, ripple. |
| `h7_serial.py` | pyserial wrapper for the firmware (`mosfet`, `code`, `fire`). |
| `scope_trigger.py` | Single-shot arm + capture-complete poll + per-channel volts. Adds the trigger helpers the shared scope module lacks, without editing it. |
| `run_experiment.py` | **The auto experiment.** Runs the whole settling + ripple matrix and writes CSVs + manifest. |
| `analyze_ldo.py` | Metrics (settle, overshoot, rise) + plots from a run dir. Runs standalone on saved data. |

## Quick start

```bash
pip install -r requirements.txt

# 0. Flash the updated SMA_Driver_PIO firmware (adds `fire`), power-cycle the rig.
# 1. Wire C1<-PJ_11/PWM4, C2<-DAC node, C3<-LDO out, C4<-INA296A out, common ground. Resistor load on the MOSFET.

python run_experiment.py --dry-run     # sanity-check the shot plan, no hardware
python run_experiment.py               # run everything -> data/ldo_<timestamp>/
python analyze_ldo.py data/ldo_<timestamp>   # (re)generate plots + summary.csv
```

Outputs per run (`data/ldo_<timestamp>/`): one `settle_*.csv` per shot (t, v_trig,
v_dac, v_out), `manifest.json`, `meta.json`, `summary.csv`, and PNG plots
(`settling_<step>.png`, `settling_overview.png`, `ripple.png`).

## Firmware command added

`fire <code_to> [ms] [code_from]` — settle DAC at `code_from` (default 0), then at
t0 raise `TRIG_PIN` and step to `code_to`, hold `ms`, return to 0. MOSFET state is
left as-is (set `mosfet on|off` first).

Current-sense commands (also new): `read` now prints `V_LDO / I / V_sma / R`;
`gain <V/V>`, `shunt <ohm>`, `ioffset <V>` trim the INA296A conversion. `drive` and
`fire` logs now include `I_mA` and `R_ohm`. See [`../SMA_Driver_PIO/README.md`](../SMA_Driver_PIO/README.md).

## Notes / gotchas
- **One scope socket at a time** — close the web-control page first.
- **COM8 = H7** (COM5 = Zaber). The board resets when the port opens (~2 s boot).
- The trigger SCPI verbs and the `INR?`/`SAST?` completion poll are **not yet
  bench-confirmed** — see STATUS.md before trusting the first run.
