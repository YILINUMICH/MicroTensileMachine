"""Build the RC-filter design dataset from captures that already exist in the repo.

Four ADC channels are in scope, and they are TWO different converters with two
different problems:

    laser  (src=1)  ADS1263 AIN4/5, Sinc3, 400 SPS real   <- external delta-sigma
    load   (src=2)  ADS1263 AIN2/3, Sinc3, 400 SPS real
    V_sma  (src=3)  H7 on-chip 16-bit, pin A0, ~1 kHz     <- SAR, no anti-alias
    I_sense(src=4)  H7 on-chip 16-bit, pin A1, ~1 kHz

Nothing here is new measurement. Every output is a re-expression of a capture
already committed under Experiment_SMAThermalCharacterization/data/raw/ or
Experiment_RNoise/out/scope/, with the four corrections that a naive read gets
wrong applied once, here, instead of in every downstream notebook:

    1. hw_us is a 32-bit micros() counter -> unwrap, PER src.
    2. src=1/2 are read at ~496 Hz but CONVERT at 400 SPS, so ~19% of rows are
       zero-order-hold repeats. Dedup on raw_code change or the frequency axis
       is wrong by ~24%.
    3. src=1/2 are M4-stamped and src=3/4 are M7-stamped, ~2.19 s apart. Only
       the joined per-cycle sets (dataset E) need this; it is applied there.
    4. Scope captures re-derive volts from the raw int8 codes at 30 codes/div,
       not the 25 that the idle-baseline file has stored (which reads 20% high).

Run:  python prepare_filter_data.py            (writes ./data/)
"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT = os.path.join(HERE, "data")

THERMAL = os.path.join(REPO, "Experiment_SMAThermalCharacterization")
TROUBLE = os.path.join(THERMAL, "data", "raw", "troubleshoot")
SCOPE = os.path.join(REPO, "Experiment_RNoise", "out", "scope")

# -- sensor conversions (Calibrate_* fits; see data/raw/README.md 4) ---------
K_MV_PER_UM = -0.49779577092171906     # Calibrate_LaserHead 2026-05-27 run09
V0_MV = 2503.7500968693835
SENS_MV_PER_MN = 10.200865238052671    # Calibrate_LoadCell 2026-05-28 run07
F_V0_MV = -34.185523054186675

# Scope volts are re-derived from raw codes: 30, not the driver's assumed 25.
CODES_PER_DIV = 30.0

index_rows = []


def note(f, **kw):
    index_rows.append({"file": f, **kw})


def unwrap_us(us):
    """32-bit micros() rollover, undone. Apply PER src -- the streams interleave
    in the file, so a global unwrap reads the interleaving as backward jumps."""
    us = np.asarray(us, dtype=np.float64)
    if us.size < 2:
        return us
    return us + 2.0 ** 32 * np.cumsum(np.r_[0, np.diff(us) < -2 ** 31])


def dedup_zoh(g):
    """Collapse the zero-order-hold repeats on an ADS1263 stream.

    M4 polls the data register at ~496 Hz; the ADC converts at 400 SPS, so ~19%
    of rows re-fetch a value already reported. Spectrally those are held
    samples, not new ones. Keep the FIRST row of each run of equal raw_code and
    record how many rows it stood for in n_held (1 = no repeat)."""
    code = g["raw_code"].to_numpy()
    first = np.r_[True, np.diff(code) != 0]
    idx = np.flatnonzero(first)
    n_held = np.diff(np.r_[idx, len(code)])
    out = g.iloc[idx].copy()
    out["n_held"] = n_held
    return out


def adc_channel(df, src, t0=None):
    """One ADS1263 stream -> deduped frame with a rebased seconds clock."""
    g = df[df["src"] == src].copy()
    g["t_s"] = unwrap_us(g["hw_us"].to_numpy()) / 1e6
    g = dedup_zoh(g)
    base = g["t_s"].iloc[0] if t0 is None else t0
    g["t_s"] = np.round(g["t_s"] - base, 9)
    g = g.rename(columns={"value": "volts"})
    g["dt_s"] = np.round(g["t_s"].diff(), 9)
    if src == 1:
        g["disp_um"] = (g["volts"] * 1e3 - V0_MV) / K_MV_PER_UM
        cols = ["t_s", "dt_s", "volts", "disp_um", "raw_code", "seq", "n_held"]
    else:
        g["force_mN"] = (g["volts"] * 1e3 - F_V0_MV) / SENS_MV_PER_MN
        cols = ["t_s", "dt_s", "volts", "force_mN", "raw_code", "seq", "n_held"]
    return g[cols].reset_index(drop=True)


def sma_wide(df):
    """src=3 (V at A0) + src=4 (I at A1) share one hw_us -- streamSma() stamps
    both from a single micros() call -- so the pivot is exact.

    The two are NOT sampled at the same instant, though: readSma() reads A0
    then A1 sequentially, ~200 us apart at ADC_SAMPLES_CYCLE=4. That skew is
    real and is one of the things the anti-alias filter is meant to fix."""
    v = df[df["src"] == 3][["hw_us", "value", "raw_code"]].rename(
        columns={"value": "v_sma_V", "raw_code": "dac_code"})
    i = df[df["src"] == 4][["hw_us", "value", "seq"]].rename(
        columns={"value": "i_A"})
    w = v.merge(i, on="hw_us", how="inner").sort_values("hw_us")
    w["t_s"] = np.round(unwrap_us(w["hw_us"].to_numpy()) / 1e6, 9)
    w["t_s"] -= w["t_s"].iloc[0]
    w["dt_s"] = np.round(w["t_s"].diff(), 9)
    w["r_ohm"] = np.where(w["i_A"] > 0.02, w["v_sma_V"] / w["i_A"], np.nan)
    w["p_W"] = w["v_sma_V"] * w["i_A"]
    return w[["t_s", "dt_s", "v_sma_V", "i_A", "r_ohm", "p_W",
              "dac_code", "seq"]].reset_index(drop=True)


# ===========================================================================
# A / B -- ADS1263 noise floor at rest (laser + load), 60 s, SMA disarmed
# ===========================================================================
def dataset_ab():
    jobs = [
        ("A", os.path.join(TROUBLE, "noise_20260728_125440_quiet_baseline",
                           "h7_quiet.csv"), "idle",
         "rig at rest, nothing actuating, no SMA connected"),
        ("B", os.path.join(TROUBLE, "noise_20260728_shorted_input_test",
                           "h7_shorted.csv"), "shorted",
         "load cell SIG+ shorted to SIG- AT THE AMPLIFIER INPUT; bridge still "
         "powered and in circuit, so this is amplifier+excitation+cable+ADC "
         "with the sensor signal removed"),
    ]
    for tag, path, cond, why in jobs:
        d = pd.read_csv(path)
        for src, name in ((1, "laser"), (2, "load")):
            g = adc_channel(d, src)
            f = f"{tag}_{cond}_{name}.csv"
            g.to_csv(os.path.join(OUT, f), index=False, float_format="%.9g")
            fs = (len(g) - 1) / (g["t_s"].iloc[-1] - g["t_s"].iloc[0])
            note(f, dataset=tag, channel=name, src=src, adc="ADS1263",
                 rows=len(g), fs_hz=round(fs, 3),
                 duration_s=round(g["t_s"].iloc[-1], 3),
                 drive="none (SMA disarmed)", condition=cond, notes=why)
            print(f"  {f:34s} {len(g):7d} rows  {fs:7.2f} Hz")


# ===========================================================================
# C -- V_sma / I_sense at the DEPLOYED rate, six operating points
# ===========================================================================
def dataset_c():
    for folder in sorted(glob.glob(os.path.join(TROUBLE, "isense_*"))):
        base = os.path.basename(folder)
        label = base.split("_", 2)[2]
        meta = {}
        mp = os.path.join(folder, "h7.meta.json")
        if os.path.exists(mp):
            meta = json.load(open(mp))
        d = pd.read_csv(os.path.join(folder, "h7.csv"))

        w = sma_wide(d)
        f = f"C_{label}_vi.csv"
        w.to_csv(os.path.join(OUT, f), index=False, float_format="%.9g")
        fs = (len(w) - 1) / (w["t_s"].iloc[-1] - w["t_s"].iloc[0])
        note(f, dataset="C", channel="V_sma+I_sense", src="3,4",
             adc="H7 on-chip 16-bit (A0, A1)", rows=len(w),
             fs_hz=round(fs, 3), duration_s=round(w["t_s"].iloc[-1], 3),
             drive=f"mode={meta.get('mode', 'voltage')} "
                   f"volts={meta.get('volts')} mA={meta.get('ma')}",
             condition=label,
             notes=f"n_avg={meta.get('n_avg')} conversions averaged per streamed "
                   "sample (boxcar, NOT anti-alias); A0 then A1 read "
                   "sequentially ~200 us apart; src=3 raw_code is the DAC code")
        print(f"  {f:34s} {len(w):7d} rows  {fs:7.2f} Hz")

        # the two ADS1263 channels from the SAME capture, i.e. WITH drive on
        for src, name in ((1, "laser"), (2, "load")):
            if not (d["src"] == src).any():
                continue
            g = adc_channel(d, src)
            f = f"C_{label}_{name}.csv"
            g.to_csv(os.path.join(OUT, f), index=False, float_format="%.9g")
            fs = (len(g) - 1) / (g["t_s"].iloc[-1] - g["t_s"].iloc[0])
            note(f, dataset="C", channel=name, src=src, adc="ADS1263",
                 rows=len(g), fs_hz=round(fs, 3),
                 duration_s=round(g["t_s"].iloc[-1], 3),
                 drive=f"mode={meta.get('mode', 'voltage')} "
                       f"volts={meta.get('volts')} mA={meta.get('ma')}",
                 condition=label,
                 notes="same capture as the _vi file -- ADS1263 noise WITH the "
                       "drive live, for drive-EMI feedthrough")
            print(f"  {f:34s} {len(g):7d} rows  {fs:7.2f} Hz")


# ===========================================================================
# D -- scope, 10 MSa/s. The ONLY data in the repo that sees above Nyquist.
# ===========================================================================
SCOPE_NODES = {
    "phase2_idle_baseline": (
        "C1 = A0 pad (after the 10k/10k divider)", "C2 = A1 pad (INA296A OUT)",
        "un-armed, ZERO current -- reference only"),
    "phase2_supplyA_0p85V": (
        "C1 = A0 pad (after the 10k/10k divider)", "C2 = A1 pad (INA296A OUT)",
        "driven 0.85 V / 186 mA into a 4.9 ohm power resistor, bench supply A"),
    "phase2_supplyB_0p85V": (
        "C1 = A0 pad (after the 10k/10k divider)", "C2 = A1 pad (INA296A OUT)",
        "same drive, DIFFERENT bench supply -- supply eliminated as the source"),
    "phase2_ldoOut_0p85V": (
        "C1 = LDO OUTPUT, BEFORE the divider", "C2 = A1 pad (INA296A OUT)",
        "same drive, C1 moved upstream -- the tone is on the rail itself"),
}


def dataset_d():
    for path in sorted(glob.glob(os.path.join(SCOPE, "*.npz"))):
        name = os.path.splitext(os.path.basename(path))[0]
        z = np.load(path, allow_pickle=True)
        fs = float(z["C1_fs"])
        cols = {}
        for ch in ("C1", "C2"):
            # Re-derive from RAW CODES at the MEASURED 30 codes/div. The stored
            # _volts array used whatever constant was current when the capture
            # was written -- the idle-baseline file has 25, which reads 20% high.
            codes = np.asarray(z[f"{ch}_codes"], dtype=np.float64)
            vdiv = float(z[f"{ch}_vdiv"])
            offset = float(z[f"{ch}_offset"])
            cols[f"{ch}_V"] = codes * (vdiv / CODES_PER_DIV) - offset
        n = len(cols["C1_V"])
        df = pd.DataFrame({"t_s": np.arange(n) / fs, **cols})
        f = f"D_{name}.csv"
        df.to_csv(os.path.join(OUT, f), index=False, float_format="%.7g")
        c1, c2, cond = SCOPE_NODES.get(name, ("C1", "C2", ""))
        note(f, dataset="D", channel="C1,C2", src="-",
             adc="Siglent SDS2204X Plus, 8-bit, AC-coupled 1M, 1x probe",
             rows=n, fs_hz=fs, duration_s=round(n / fs, 6),
             drive=cond, condition=name,
             notes=f"{c1}; {c2}. AC-COUPLED so DC is stripped -- volts are "
                   f"ripple only. Volts re-derived from int8 codes at "
                   f"{CODES_PER_DIV} codes/div (stored value was "
                   f"{float(z['codes_per_div'])}). C2 scale 0.93 V/A measured. "
                   "The A0 divider is UNCOMPENSATED: /2.10 at DC but ~/1.09 at "
                   "24 kHz, so the ADC pin sees half the DC and nearly all the "
                   "ripple.")
        print(f"  {f:34s} {n:7d} rows  {fs/1e6:7.2f} MHz "
              f"({os.path.getsize(os.path.join(OUT, f))/1e6:.0f} MB)")


# ===========================================================================
# E -- the SIGNAL. What the filter must not destroy.
# ===========================================================================
E_CASES = [
    ("sweep_20260731_155129", 250, 400, 3),
    ("sweep_20260731_155129", 550, 400, 3),
    ("sweep_20260731_155129", 850, 400, 3),   # README worked example
    ("sweep_20260731_145838", 850, 100, 3),   # fastest transient = tightest spec
]


def dataset_e():
    sys.path.insert(0, os.path.join(THERMAL, "analysis"))
    from get_cycle import (get_cycle, _capture, _heat_windows, SRC,
                           unwrap_us as _uw)
    from analyze_raw import resolve_sweep  # noqa: F401  (import check)

    for sweep, lvl, ms, cyc in E_CASES:
        try:
            df = get_cycle(sweep, lvl, ms, cycle=cyc, pre_s=2.0, post_s=8.0)
        except Exception as e:                      # noqa: BLE001
            print(f"  !! {sweep} {lvl}mA {ms}ms cycle {cyc}: {e}")
            continue
        stem = f"E_{lvl}mA_{ms}ms_c{cyc}"

        f = f"{stem}_joined.csv"
        df.to_csv(os.path.join(OUT, f), index=False, float_format="%.9g")
        note(f, dataset="E", channel="all four + derived", src="1,2,3,4",
             adc="mixed", rows=len(df), fs_hz="~1000 (union grid)",
             duration_s=round(df["t_s"].iloc[-1] - df["t_s"].iloc[0], 3),
             drive=f"{lvl} mA x {ms} ms, cycle {cyc} of {sweep}",
             condition=f"{lvl}mA_{ms}ms",
             notes="M4/M7 clock offset APPLIED (src=1/2 shifted onto the M7 "
                   "clock); t_s=0 at heat onset; phase in {pre,heat,cool}. "
                   "Streams are INTERPOLATED onto the union index -- convenient "
                   "for reading rise times, mildly smoothed, so use the _raw "
                   "file for anything spectral.")
        print(f"  {f:34s} {len(df):7d} rows")

        # ...and the same window UN-interpolated, one row per real sample
        path = _capture(sweep, lvl, ms)
        meta = json.load(open(path.replace(".csv", ".meta.json")))
        off = float(meta.get("m4_clock_offset_s", 0.0))
        d = pd.read_csv(path)
        d["t_s"] = np.nan
        for _s, grp in d.groupby("src", sort=False):
            d.loc[grp.index, "t_s"] = _uw(grp["hw_us"].to_numpy()) / 1e6
        d.loc[d["src"].isin((SRC["laser"], SRC["load"])), "t_s"] += off
        # Same heat onset the joined file uses -- re-derived with the same
        # firmware-schedule + matched-filter path, not re-guessed.
        cur = d[d["src"] == SRC["sma_i"]]
        wins = _heat_windows(path, cur["t_s"].to_numpy(),
                             cur["value"].to_numpy(), ms)
        t0 = wins[cyc - 1][0]
        sel = d[(d["t_s"] >= t0 - 2.0) & (d["t_s"] <= t0 + ms / 1000.0 + 8.0)]
        sel = sel.copy()
        sel["t_s"] = np.round(sel["t_s"] - t0, 9)
        names = {v: k for k, v in SRC.items()}
        sel["stream"] = sel["src"].map(names)
        sel = sel.sort_values("t_s")[
            ["t_s", "src", "stream", "value", "raw_code", "seq"]]
        f = f"{stem}_raw.csv"
        sel.to_csv(os.path.join(OUT, f), index=False, float_format="%.9g")
        note(f, dataset="E", channel="all streams, long format", src="1,2,3,4",
             adc="mixed", rows=len(sel), fs_hz="native per stream",
             duration_s=round(sel["t_s"].iloc[-1] - sel["t_s"].iloc[0], 3),
             drive=f"{lvl} mA x {ms} ms, cycle {cyc} of {sweep}",
             condition=f"{lvl}mA_{ms}ms",
             notes="NO interpolation, NO ZOH dedup, one row per sample as the "
                   "rig wrote it. Clock offset applied, t_s=0 at heat onset. "
                   "value units differ per stream: src=1/2 volts, src=3 volts, "
                   "src=4 AMPS, src=6 volts, src=7 ohms.")
        print(f"  {f:34s} {len(sel):7d} rows")


def main():
    os.makedirs(OUT, exist_ok=True)
    print("A/B -- ADS1263 noise floor at rest")
    dataset_ab()
    print("C -- V_sma / I_sense at the deployed ~1 kHz")
    dataset_c()
    print("D -- scope, 10 MSa/s (above-Nyquist truth for A0/A1)")
    dataset_d()
    print("E -- the signal the filter must not destroy")
    dataset_e()

    idx = pd.DataFrame(index_rows)
    idx.to_csv(os.path.join(OUT, "index.csv"), index=False)
    print(f"\n{len(idx)} files + index.csv -> {OUT}")


if __name__ == "__main__":
    main()
