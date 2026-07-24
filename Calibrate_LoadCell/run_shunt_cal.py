#!/usr/bin/env python3
"""
run_shunt_cal.py — LCA-RTC on-board shunt calibration check.

Logs the load-cell amplifier output through Portenta+ADS1263 during repeated
cycles of:

    baseline (no SW1)  →  PRESS+HOLD SW1  →  RELEASE SW1 (post)

For each trial the script computes:

    dV = mean(V_shunt_held)  -  mean(V_baseline)
    drift = mean(V_post)    -  mean(V_baseline)

and compares dV to the Cert-derived expected value:

    dV_expected = V_FS * PCT_LOAD / 100

This is the loop-breaker for the "GSO-50 calibrated against Instron"
circularity: the shunt cal value depends only on
    - the LCA-RTC's 87.325 kOhm shunt resistor (+/-0.025 %)
    - the cell's actual R.O. and bridge resistance (Calibration Cert)
    - the amp's configured full-scale output (V_FS)
NONE of which involve the spring or Instron.

Use BEFORE pressing the operator on a span trim (manual p.10) so the
"as-found" sensitivity is captured, then AGAIN after the trim so the
delta is documented.

Usage (typical):
    python run_shunt_cal.py --port COM8 --pct-load 99.8 --v-fs 5.0
    python run_shunt_cal.py --port COM8 --trials 5 --duration 10

Outputs land in ./data/ next to the spring-transfer calibration runs:
    YYYY-MM-DD_shuntNN_raw.csv      - every sample, tagged trial+phase
    YYYY-MM-DD_shuntNN_summary.json - per-trial + overall stats
    YYYY-MM-DD_shuntNN_plot.png     - voltage timeline + dV bar chart

Author: Yilin Ma -- HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from portenta_reader import PortentaReader, Sample, parse_line

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
PHASE_BASELINE = "baseline"
PHASE_SHUNT    = "shunt_held"
PHASE_POST     = "post_release"
PHASES = (PHASE_BASELINE, PHASE_SHUNT, PHASE_POST)

DATA_DIR = Path(__file__).resolve().parent / "data"


# -----------------------------------------------------------------------------
# Data classes
# -----------------------------------------------------------------------------
@dataclass
class PhaseStats:
    phase: str
    n_samples: int
    duration_s: float
    mean_V: float
    std_V: float
    min_V: float
    max_V: float


@dataclass
class TrialResult:
    trial: int
    baseline: PhaseStats
    shunt:    PhaseStats
    post:     PhaseStats
    dV: float                  # shunt.mean - baseline.mean
    drift: float               # post.mean  - baseline.mean
    returned_to_baseline: bool # |drift| within 3*baseline.std?


@dataclass
class RunSummary:
    timestamp_utc: str
    port: str
    adc_source: int
    n_trials: int
    duration_per_phase_s: float
    pct_load: Optional[float]
    v_fs: Optional[float]
    dV_expected: Optional[float]
    trials: List[TrialResult] = field(default_factory=list)
    dV_mean: float = 0.0
    dV_std:  float = 0.0
    dV_repeatability_pct: float = 0.0
    abs_dev_pct: Optional[float] = None
    verdict: str = ""


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def countdown(prompt: str, seconds: int) -> None:
    """Print a live countdown on a single line, then a final action banner."""
    for i in range(seconds, 0, -1):
        print(f"\r  {prompt} ... {i} ", end="", flush=True)
        time.sleep(1.0)
    print(f"\r  {prompt} ... NOW!{' ' * 8}")


def collect_samples_for(reader: PortentaReader, duration_s: float) -> List[Sample]:
    """
    Pull samples from the live stream for exactly ``duration_s`` wall-clock
    seconds. The Portenta is free-running at ~400 SPS so we expect roughly
    400 * duration_s samples per call.

    Unlike PortentaReader.read_samples (which is sample-count-bounded), this
    is time-bounded -- the right primitive for shunt cal phases where the
    operator's button-press timing defines the window, not the sample count.
    """
    samples: List[Sample] = []
    t_end = time.monotonic() + duration_s
    while time.monotonic() < t_end:
        # _readline returns '' on serial timeout (configured ~1 s); the
        # outer loop just re-checks the deadline. parse_line filters out
        # banner/log lines and non-matching adc_source.
        line = reader._readline()  # noqa: SLF001 -- reader has no public time-bounded API
        if not line:
            continue
        s = parse_line(line, adc_source=reader.adc_source)
        if s is not None:
            samples.append(s)
    return samples


def stats_for(phase: str, samples: List[Sample], duration_s: float) -> PhaseStats:
    if not samples:
        return PhaseStats(phase, 0, duration_s, float("nan"), float("nan"),
                          float("nan"), float("nan"))
    vs = [s.voltage_V for s in samples]
    return PhaseStats(
        phase=phase,
        n_samples=len(vs),
        duration_s=duration_s,
        mean_V=statistics.fmean(vs),
        std_V=statistics.pstdev(vs) if len(vs) > 1 else 0.0,
        min_V=min(vs),
        max_V=max(vs),
    )


def next_run_number(data_dir: Path, date_tag: str) -> int:
    """Find the next free shuntNN index for today so existing runs aren't overwritten."""
    n = 1
    while any(data_dir.glob(f"{date_tag}_shunt{n:02d}_*")):
        n += 1
    return n


# -----------------------------------------------------------------------------
# Trial loop
# -----------------------------------------------------------------------------
def run_one_trial(reader: PortentaReader,
                  trial: int,
                  duration_s: float,
                  countdown_s: int,
                  log: logging.Logger,
                  all_samples_out: list) -> TrialResult:
    """
    Run the three-phase sequence for a single trial. Mutates
    ``all_samples_out`` by appending (trial, phase, sample) tuples for the
    final CSV dump.
    """
    log.info("")
    log.info("======== Trial %d ========", trial)

    # --- Phase 1: baseline ----------------------------------------------------
    input(f"\n  [Trial {trial}] SW1 RELEASED, cell unloaded.  Press ENTER to start "
          f"{duration_s:.0f} s baseline... ")
    log.info("  capturing baseline (%.0f s)...", duration_s)
    reader.drain()
    base_samples = collect_samples_for(reader, duration_s)
    base = stats_for(PHASE_BASELINE, base_samples, duration_s)
    log.info("    n=%d  mean=%.6f V  std=%.6f V  range=[%.6f, %.6f]",
             base.n_samples, base.mean_V, base.std_V, base.min_V, base.max_V)
    for s in base_samples:
        all_samples_out.append((trial, PHASE_BASELINE, s))

    # --- Phase 2: SW1 pressed -------------------------------------------------
    print(f"\n  [Trial {trial}] About to start SHUNT-HELD phase ({duration_s:.0f} s).")
    print( "  Get ready to PRESS AND HOLD SW1 on the LCA-RTC.")
    countdown("Press and hold SW1 in", countdown_s)
    log.info("  capturing shunt_held (%.0f s) -- KEEP SW1 PRESSED ...", duration_s)
    reader.drain()
    shunt_samples = collect_samples_for(reader, duration_s)
    shunt = stats_for(PHASE_SHUNT, shunt_samples, duration_s)
    log.info("    n=%d  mean=%.6f V  std=%.6f V  range=[%.6f, %.6f]",
             shunt.n_samples, shunt.mean_V, shunt.std_V, shunt.min_V, shunt.max_V)
    for s in shunt_samples:
        all_samples_out.append((trial, PHASE_SHUNT, s))

    # --- Phase 3: release + post-baseline ------------------------------------
    print(f"\n  [Trial {trial}] RELEASE SW1 in:")
    countdown("Release SW1 in", countdown_s)
    log.info("  capturing post_release (%.0f s) -- SW1 RELEASED ...", duration_s)
    reader.drain()
    post_samples = collect_samples_for(reader, duration_s)
    post = stats_for(PHASE_POST, post_samples, duration_s)
    log.info("    n=%d  mean=%.6f V  std=%.6f V  range=[%.6f, %.6f]",
             post.n_samples, post.mean_V, post.std_V, post.min_V, post.max_V)
    for s in post_samples:
        all_samples_out.append((trial, PHASE_POST, s))

    # --- Per-trial derived ---------------------------------------------------
    dV = shunt.mean_V - base.mean_V
    drift = post.mean_V - base.mean_V
    returned = abs(drift) <= 3.0 * max(base.std_V, 1e-9)

    log.info("  ---- trial %d derived ----", trial)
    log.info("    dV (shunt - baseline) = %.6f V", dV)
    log.info("    drift (post - baseline) = %+.6f V  (returned_to_baseline=%s)",
             drift, returned)
    if not returned:
        log.warning("    WARNING: post-release did not return within 3*sigma of baseline. "
                    "Thermal drift, or SW1 was held into the post phase?")

    return TrialResult(
        trial=trial,
        baseline=base,
        shunt=shunt,
        post=post,
        dV=dV,
        drift=drift,
        returned_to_baseline=returned,
    )


# -----------------------------------------------------------------------------
# Outputs
# -----------------------------------------------------------------------------
def write_raw_csv(path: Path, samples_tagged: list) -> None:
    """One row per sample with (trial, phase, timestamp_us, voltage_V, raw_code, adc_source)."""
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["trial", "phase", "timestamp_us", "voltage_V", "raw_code", "adc_source"])
        for trial, phase, s in samples_tagged:
            w.writerow([trial, phase, s.timestamp_us,
                        f"{s.voltage_V:.8f}",
                        s.raw_code if s.raw_code is not None else "",
                        s.adc_source if s.adc_source is not None else ""])


def write_summary_json(path: Path, summary: RunSummary) -> None:
    payload = asdict(summary)
    path.write_text(json.dumps(payload, indent=2))


def make_plot(path: Path,
              samples_tagged: list,
              summary: RunSummary) -> None:
    """
    Two-panel plot:
      top    -- voltage timeline, color-coded by phase, vertical separators between trials
      bottom -- dV per trial bar chart vs the Cert-expected dV (if provided)
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax_t, ax_b) = plt.subplots(
        2, 1, figsize=(11, 7),
        gridspec_kw={"height_ratios": [3, 2]}
    )

    # --- top: voltage vs time -----------------------------------------------
    phase_colors = {
        PHASE_BASELINE: "#2b8cbe",
        PHASE_SHUNT:    "#d7301f",
        PHASE_POST:     "#74a9cf",
    }
    # rebase time to first sample for readability
    if samples_tagged:
        t0_us = samples_tagged[0][2].timestamp_us
    else:
        t0_us = 0

    by_phase = {p: ([], []) for p in PHASES}
    for trial, phase, s in samples_tagged:
        t_s = (s.timestamp_us - t0_us) / 1e6
        by_phase[phase][0].append(t_s)
        by_phase[phase][1].append(s.voltage_V)

    for phase, (xs, ys) in by_phase.items():
        if xs:
            ax_t.scatter(xs, ys, s=2, color=phase_colors[phase],
                         label=phase, alpha=0.6)

    # mark trial boundaries with vertical lines
    trial_boundaries_us = []
    prev_trial = None
    for trial, _phase, s in samples_tagged:
        if prev_trial is not None and trial != prev_trial:
            trial_boundaries_us.append(s.timestamp_us)
        prev_trial = trial
    for tb in trial_boundaries_us:
        ax_t.axvline((tb - t0_us) / 1e6, color="k", lw=0.4, alpha=0.3)

    ax_t.set_xlabel("time (s, from start of run)")
    ax_t.set_ylabel("V_out (V)")
    ax_t.set_title("LCA-RTC shunt cal — voltage timeline")
    ax_t.legend(loc="upper right", fontsize=8, markerscale=4)
    ax_t.grid(alpha=0.3)

    # --- bottom: per-trial dV with expected --------------------------------
    trials = [tr.trial for tr in summary.trials]
    dVs    = [tr.dV    for tr in summary.trials]
    sigmas = [tr.shunt.std_V for tr in summary.trials]

    ax_b.bar(trials, dVs, yerr=sigmas, color="#d7301f",
             alpha=0.7, capsize=4, label="dV (shunt − baseline)")
    ax_b.axhline(summary.dV_mean, color="k", ls="-", lw=1,
                 label=f"mean dV = {summary.dV_mean:.4f} V  "
                       f"(spread {summary.dV_repeatability_pct:.3f} %)")
    if summary.dV_expected is not None:
        ax_b.axhline(summary.dV_expected, color="green", ls="--", lw=1.5,
                     label=f"expected dV = {summary.dV_expected:.4f} V "
                           f"(cert-based)")
    ax_b.set_xlabel("trial")
    ax_b.set_ylabel("dV (V)")
    ax_b.set_xticks(trials)
    title = "Shunt cal dV per trial"
    if summary.abs_dev_pct is not None:
        title += f" — abs deviation from expected: {summary.abs_dev_pct:+.2f} %"
    ax_b.set_title(title)
    ax_b.legend(loc="best", fontsize=8)
    ax_b.grid(alpha=0.3, axis="y")

    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--port", default="COM13",
                   help="Portenta serial port (default: COM8 per current rig)")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--adc-source", type=int, default=2, choices=(1, 2),
                   help="which ADC to log (2 = load-cell channel on AIN2/3; "
                        "default 2)")
    p.add_argument("--trials", type=int, default=3,
                   help="number of press/release cycles (default: 3)")
    p.add_argument("--duration", type=float, default=10.0,
                   help="seconds per phase: baseline / shunt-held / post "
                        "(default: 10)")
    p.add_argument("--countdown", type=int, default=3,
                   help="seconds of countdown before press/release prompts "
                        "(default: 3)")
    p.add_argument("--pct-load", type=float, default=None,
                   help="PCT LOAD from the GSO-50 Calibration Cert "
                        "(e.g. 99.8 for ~100%% of FS). Required for "
                        "expected-value comparison; omit for repeatability-"
                        "only mode.")
    p.add_argument("--v-fs", type=float, default=None,
                   help="amplifier full-scale output in volts (e.g. 5.0 or "
                        "10.0 per E3 jumper). Used with --pct-load to "
                        "compute expected dV.")
    p.add_argument("--no-plot", action="store_true",
                   help="skip the matplotlib plot (useful in headless CI)")
    return p


def main() -> int:
    args = build_argparser().parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s %(message)s")
    log = logging.getLogger("shunt_cal")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    date_tag = datetime.now().strftime("%Y-%m-%d")
    run_n = next_run_number(DATA_DIR, date_tag)
    stem = f"{date_tag}_shunt{run_n:02d}"
    raw_csv     = DATA_DIR / f"{stem}_raw.csv"
    summary_json = DATA_DIR / f"{stem}_summary.json"
    plot_png    = DATA_DIR / f"{stem}_plot.png"

    # Expected dV from cert (loop-breaker)
    dV_expected: Optional[float] = None
    if args.pct_load is not None and args.v_fs is not None:
        dV_expected = args.v_fs * args.pct_load / 100.0
        log.info("Expected dV from cert: %.6f V  (V_FS=%.2f V * PCT_LOAD=%.3f %%)",
                 dV_expected, args.v_fs, args.pct_load)
    else:
        log.warning("PCT_LOAD and/or V_FS not supplied -- "
                    "running in REPEATABILITY-ONLY mode (no absolute check).")

    print()
    print("=" * 72)
    print("  LCA-RTC ON-BOARD SHUNT CALIBRATION")
    print("=" * 72)
    print(f"  port            : {args.port}")
    print(f"  trials          : {args.trials}")
    print(f"  phase duration  : {args.duration:.0f} s  (baseline / shunt / post)")
    print(f"  countdown       : {args.countdown} s")
    print(f"  output stem     : {stem}")
    print()
    print("  Pre-flight checklist:")
    print("    [ ] Load cell wired to LCA-RTC and POWERED")
    print("    [ ] No load / no test article on the cell fixture")
    print("    [ ] E7 jumper installed (uses on-board 87.325 kOhm shunt)")
    print("    [ ] Rig warmed up >= 30 min (per LCA manual p.10)")
    print("    [ ] Portenta + ADS1263 streaming on the port above")
    print()
    input("  Press ENTER when ready to start...")

    all_samples_tagged: list = []
    trial_results: List[TrialResult] = []

    with PortentaReader(port=args.port, baud=args.baud,
                        adc_source=args.adc_source) as reader:
        log.info("Portenta reader open on %s (adc_source=%d)",
                 args.port, args.adc_source)

        for trial in range(1, args.trials + 1):
            tr = run_one_trial(
                reader=reader,
                trial=trial,
                duration_s=args.duration,
                countdown_s=args.countdown,
                log=log,
                all_samples_out=all_samples_tagged,
            )
            trial_results.append(tr)

    # -------------------- aggregate stats ------------------------------------
    dVs = [tr.dV for tr in trial_results]
    dV_mean = statistics.fmean(dVs)
    dV_std  = statistics.pstdev(dVs) if len(dVs) > 1 else 0.0
    rep_pct = (dV_std / abs(dV_mean) * 100.0) if dV_mean != 0 else float("nan")

    abs_dev_pct: Optional[float] = None
    verdict = ""
    if dV_expected is not None and dV_expected != 0:
        abs_dev_pct = (dV_mean - dV_expected) / dV_expected * 100.0
        if abs(abs_dev_pct) <= 0.5 and rep_pct <= 0.1:
            verdict = "PASS - amp chain reproduces cert-expected dV within 0.5% (independent of Instron)"
        elif abs(abs_dev_pct) <= 1.0:
            verdict = "MARGINAL - dV within 1% of cert; consider span trim per manual p.10"
        else:
            verdict = f"FAIL - dV deviates {abs_dev_pct:+.2f}% from cert; check jumper config + PCT_LOAD"
    else:
        verdict = (f"INFO ONLY - repeatability {rep_pct:.3f}% across {args.trials} trials. "
                   "Re-run with --pct-load and --v-fs for absolute check.")

    summary = RunSummary(
        timestamp_utc=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        port=args.port,
        adc_source=args.adc_source,
        n_trials=args.trials,
        duration_per_phase_s=args.duration,
        pct_load=args.pct_load,
        v_fs=args.v_fs,
        dV_expected=dV_expected,
        trials=trial_results,
        dV_mean=dV_mean,
        dV_std=dV_std,
        dV_repeatability_pct=rep_pct,
        abs_dev_pct=abs_dev_pct,
        verdict=verdict,
    )

    # -------------------- write artifacts ------------------------------------
    write_raw_csv(raw_csv, all_samples_tagged)
    write_summary_json(summary_json, summary)
    if not args.no_plot:
        make_plot(plot_png, all_samples_tagged, summary)

    # -------------------- console report -------------------------------------
    print()
    print("=" * 72)
    print("  SUMMARY")
    print("=" * 72)
    print(f"  trials          : {args.trials}")
    print(f"  dV mean         : {dV_mean:.6f} V")
    print(f"  dV spread (std) : {dV_std:.6f} V  ({rep_pct:.3f} %)")
    if dV_expected is not None:
        print(f"  dV expected     : {dV_expected:.6f} V  (V_FS={args.v_fs:.2f}, "
              f"PCT_LOAD={args.pct_load:.3f}%)")
        print(f"  abs deviation   : {abs_dev_pct:+.3f} %")
    print(f"  verdict         : {verdict}")
    print()
    print(f"  raw     : {raw_csv}")
    print(f"  summary : {summary_json}")
    if not args.no_plot:
        print(f"  plot    : {plot_png}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
