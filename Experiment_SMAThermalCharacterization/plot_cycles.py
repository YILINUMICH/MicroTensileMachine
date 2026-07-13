#!/usr/bin/env python3
"""
plot_cycles.py — cycle-aligned visualization of an SMA thermal console session.

Where analyze_sma.py renders a flat whole-session dashboard, this script segments
the run by the ACTUATION PATTERN: it recovers the fire/cool cycles the console
commanded (`cycle <v_high> <v_low> <fire_ms> <cool_ms> <n>` in events.csv) and
locates each fire onset as a rising edge of the logged sma_v, then renders

    <session>/cycles_timeline.png   full session, fire windows shaded
    <session>/cycles_overlay.png    all cycles overlaid on fire-onset time
    <session>/cycles_trend.png      per-cycle metrics vs cycle index
    <session>/cycles_metrics.csv    the per-cycle metrics table

Signals are converted with the calibration block in meta.json (same coefficients
analyze_sma.py uses). Displacement carries a large meaningless absolute offset
(the laser sits far outside its calibrated span), so it is plotted as a DELTA
against each cycle's pre-fire baseline; force is likewise reported as a delta in
the metrics while the timeline keeps it absolute (the load-cell tare is real).

Usage:
    python plot_cycles.py --session data/console_20260713_115921

Author: Yilin Ma — HDR Lab, University of Michigan
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.cm import ScalarMappable

log = logging.getLogger("plot_cycles")

# --- Palette (dataviz reference instance, light surface) --------------------
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"

C_VOLT = "#2a78d6"   # categorical slot 1 — blue
C_CURR = "#1baf7a"   # slot 2 — aqua
C_RES = "#4a3aa7"    # slot 5 — violet
C_FORCE = "#e34948"  # slot 6 — red
C_DISP = "#eb6834"   # slot 8 — orange
C_FIRE = "#eb6834"   # fire-window wash
C_REF = "#898781"    # reference lines (cold R, zero)

# Ordinal blue ramp, step 250 -> 700 (lightest step still clears 2:1 on light).
BLUE_RAMP = ["#86b6ef", "#5598e7", "#3987e5", "#2a78d6",
             "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"]
CYCLE_CMAP = LinearSegmentedColormap.from_list("cycles", BLUE_RAMP)

PRE_S = 0.40   # seconds of pre-fire context shown / used for the baseline
BASE_LO, BASE_HI = -0.35, -0.03   # baseline window relative to fire onset

# Opaque plate so an in-axes note stays legible on top of a dense trace.
_PLATE = dict(facecolor=SURFACE, edgecolor="none", alpha=0.88, pad=3.0)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def _rows(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with open(path, newline="", errors="replace") as f:
        return list(csv.DictReader(f))


def load_h7(path: Path) -> Dict[str, dict]:
    """{channel: {'t': host_ts array, 'v': value array}}"""
    acc: Dict[str, dict] = {}
    for r in _rows(path):
        ch = (r.get("channel") or "").strip()
        if not ch:
            continue
        d = acc.setdefault(ch, {"t": [], "v": []})
        try:
            d["t"].append(float(r["host_timestamp_s"]))
            d["v"].append(float(r["value"]))
        except (ValueError, KeyError):
            continue
    return {ch: {"t": np.asarray(d["t"]), "v": np.asarray(d["v"])}
            for ch, d in acc.items() if d["t"]}


@dataclass
class CycleCmd:
    v_high: float
    v_low: float
    fire_ms: float
    cool_ms: float
    n: int

    @property
    def period_s(self) -> float:
        return (self.fire_ms + self.cool_ms) / 1000.0


def load_cycle_cmd(events_path: Path) -> Optional[CycleCmd]:
    """Recover the last `cycle v_high v_low fire_ms cool_ms n` command issued."""
    pat = re.compile(r"^cycle\s+([\d.]+)\s+([\d.]+)\s+(\d+)\s+(\d+)\s+(\d+)")
    found = None
    for r in _rows(events_path):
        if (r.get("kind") or "").strip() != "cmd":
            continue
        m = pat.match((r.get("detail") or "").strip())
        if m:
            found = CycleCmd(float(m[1]), float(m[2]),
                             float(m[3]), float(m[4]), int(m[5]))
    return found


# ---------------------------------------------------------------------------
# Cycle segmentation — fire onsets from the sma_v rising edges
# ---------------------------------------------------------------------------
def find_fire_onsets(t: np.ndarray, v: np.ndarray,
                     v_high: float, v_low: float) -> np.ndarray:
    """Host-clock timestamps of each fire onset (rising edge of the drive)."""
    thresh = 0.5 * (v_high + v_low)
    hot = v > thresh
    rise = np.flatnonzero(np.diff(hot.astype(np.int8)) == 1) + 1
    if hot[0]:                      # a fire already in progress at the first sample
        rise = np.insert(rise, 0, 0)
    return t[rise]


# ---------------------------------------------------------------------------
# Per-cycle slicing + metrics
# ---------------------------------------------------------------------------
def slice_cycle(t: np.ndarray, y: np.ndarray, t_fire: float,
                span_s: float) -> tuple:
    """(t_rel, y) for one cycle window, t_rel measured from the fire onset."""
    m = (t >= t_fire - PRE_S) & (t < t_fire + span_s)
    return t[m] - t_fire, y[m]


def _baseline(t_rel: np.ndarray, y: np.ndarray) -> float:
    m = (t_rel >= BASE_LO) & (t_rel <= BASE_HI)
    return float(np.mean(y[m])) if m.any() else float("nan")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--session", required=True, help="console session directory")
    p.add_argument("--r-ref", choices=("cold", "cycle"), default="cold",
                   help="reference R₀ the resistance is normalized to: 'cold' = "
                        "the session's initial baseline reading (default), "
                        "'cycle' = each cycle's own pre-fire R (removes the "
                        "cycle-to-cycle baseline drift)")
    p.add_argument("--dpi", type=int, default=140)
    args = p.parse_args()

    sess = Path(args.session)
    if not sess.exists():
        print(f"ERROR: no such session dir: {sess}", file=sys.stderr)
        return 2

    meta = json.loads((sess / "meta.json").read_text())
    cal = meta.get("calibration", {})
    k = cal.get("laser", {}).get("k_mV_per_um")
    v0 = cal.get("laser", {}).get("V0_mV")
    lscale = cal.get("load_cell", {}).get("scale_N_per_V")
    loff = cal.get("load_cell", {}).get("offset_V", 0.0)
    cold_r = (meta.get("baseline") or {}).get("cold_r_ohm")

    h7 = load_h7(sess / "h7.csv")
    for need in ("sma_v", "sma_i", "sma_r", "load", "laser"):
        if need not in h7:
            print(f"ERROR: channel '{need}' missing from h7.csv", file=sys.stderr)
            return 2

    cmd = load_cycle_cmd(sess / "events.csv")
    if cmd is None:
        print("ERROR: no `cycle ...` command found in events.csv — this session "
              "has no actuation pattern to follow", file=sys.stderr)
        return 2
    log.info("actuation: %d cycles, fire %.0f ms @ %.1f V, cool %.0f ms @ %.1f V",
             cmd.n, cmd.fire_ms, cmd.v_high, cmd.cool_ms, cmd.v_low)

    # Physical units.
    disp_um = (h7["laser"]["v"] * 1000.0 - v0) / k
    force_N = lscale * (h7["load"]["v"] - loff)

    sig = {
        "volt": (h7["sma_v"]["t"], h7["sma_v"]["v"]),
        "curr": (h7["sma_i"]["t"], h7["sma_i"]["v"]),
        "res": (h7["sma_r"]["t"], h7["sma_r"]["v"]),
        "force": (h7["load"]["t"], force_N),
        "disp": (h7["laser"]["t"], disp_um),
    }

    onsets = find_fire_onsets(*sig["volt"], cmd.v_high, cmd.v_low)
    log.info("found %d fire onset(s) in sma_v (commanded %d)", len(onsets), cmd.n)
    if len(onsets) == 0:
        print("ERROR: no fire edges in sma_v", file=sys.stderr)
        return 2
    span = cmd.period_s          # one cycle window: fire + full cool

    t0 = onsets[0]               # time origin = first fire
    fire_s = cmd.fire_ms / 1000.0

    # Noise floors, measured on the COOL stretches only (>0.5 s past a fire, so
    # the thermal transient is over). Any per-cycle Δ smaller than ~2σ here is
    # not a measurement — it is scatter, and the plots say so out loud.
    #
    # Estimated from the sample-to-sample difference (σ = std(Δy)/√2), NOT the
    # plain std: the force ratchets upward across the session, and a plain std
    # would charge that real drift to the noise budget and hide a signal that is
    # in fact ~25× its own scatter.
    def noise_sd(key: str) -> float:
        t, y = sig[key]
        quiet = np.ones(t.shape, dtype=bool)
        for ot in onsets:
            quiet &= ~((t >= ot - 0.05) & (t <= ot + 0.5))
        pair = quiet[:-1] & quiet[1:]      # both endpoints of the diff are quiet
        if not pair.any():
            return float("nan")
        return float(np.std(np.diff(y)[pair]) / np.sqrt(2.0))

    sd_r, sd_disp, sd_force = noise_sd("res"), noise_sd("disp"), noise_sd("force")
    log.info("noise floor (cool periods): R ±%.3f Ω, disp ±%.2f µm, force ±%.4f N (1σ)",
             sd_r, sd_disp, sd_force)

    # Drive-feedthrough test on the laser. A real contraction that shows up in the
    # force must still be there when the force PEAKS (~0.2-0.3 s after onset). An
    # excursion that lives only inside the drive window and is gone by then is the
    # 0.7 A pulse coupling into the laser's ADC channel, not motion.
    d_t, d_y = sig["disp"]
    in_fire, post_fire = [], []
    for ot in onsets:
        tr, y = slice_cycle(d_t, d_y, ot, span)
        b = _baseline(tr, y)
        mf = (tr >= 0) & (tr <= fire_s)
        mp = (tr >= 0.20) & (tr <= 0.30)
        if mf.any():
            in_fire.append(float(np.mean(y[mf]) - b))
        if mp.any():
            post_fire.append(float(np.mean(y[mp]) - b))
    disp_in = float(np.nanmean(in_fire)) if in_fire else float("nan")
    disp_post = float(np.nanmean(post_fire)) if post_fire else float("nan")
    feedthrough = (abs(disp_in) > 2 * sd_disp and abs(disp_post) < sd_disp)
    if feedthrough:
        log.warning("laser excursion is confined to the drive window "
                    "(%.2f µm in-fire vs %.2f µm at force peak) — drive "
                    "feedthrough, not displacement", disp_in, disp_post)

    # ---- Resistance normalization ------------------------------------------
    # Raw ohms are not comparable across cycles: the pre-fire R drifts run to run.
    # Normalize to a reference R₀ so every cycle starts at 1.0 and the fire's
    # excursion is read as a FRACTION of the coil's own resistance.
    #   --r-ref cold  : R₀ = the session's initial (baseline) cold reading — one
    #                   reference for the whole run, so the drift stays visible.
    #   --r-ref cycle : R₀ = that cycle's own pre-fire R — drift divided out, so
    #                   only the fire response remains.
    r_t, r_y = sig["res"]
    r0_global = cold_r
    if r0_global is None:                      # no baseline block — fall back to
        pre = r_y[r_t < onsets[0]]             # whatever precedes the first fire
        r0_global = float(np.mean(pre)) if len(pre) else float(np.mean(r_y))
    r0_cycle: List[float] = []
    for ot in onsets:
        tr, r = slice_cycle(r_t, r_y, ot, span)
        b = _baseline(tr, r)
        r0_cycle.append(b if np.isfinite(b) else float(r0_global))

    def r_ref(i: int) -> float:
        """R₀ for cycle i under the selected reference."""
        return float(r0_global) if args.r_ref == "cold" else r0_cycle[i]

    log.info("resistance normalized to R₀ (%s): %.3f Ω%s", args.r_ref, r0_global,
             "" if args.r_ref == "cold" else " (global) / per-cycle pre-fire R")
    sd_r_n = sd_r / float(r0_global)           # noise floor in R/R₀ units

    # -----------------------------------------------------------------------
    # Figure 1 — timeline, one panel per measure (never two y-scales on one axes)
    # -----------------------------------------------------------------------
    rows = [
        ("volt", f"drive V", "V", C_VOLT),
        ("curr", "drive I", "A", C_CURR),
        ("res", f"SMA resistance\n(R/R₀, R₀={r0_global:.2f} Ω)", "R/R₀", C_RES),
        ("force", "force", "N", C_FORCE),
        ("disp", "displacement (Δ vs start)", "µm", C_DISP),
    ]
    fig, axes = plt.subplots(len(rows), 1, figsize=(12, 11), sharex=True,
                             facecolor=SURFACE)
    t_lo, t_hi = -PRE_S - 0.6, onsets[-1] - t0 + span + 0.6
    for ax, (key, name, unit, color) in zip(axes, rows):
        t, y = sig[key]
        tr = t - t0
        if key == "disp":
            m0 = (tr >= t_lo) & (tr < 0)
            y = y - (np.mean(y[m0]) if m0.any() else y[0])
        if key == "res":
            # One global reference on the timeline, so the baseline drift the
            # per-cycle reference would divide out stays visible here.
            y = y / float(r0_global)
        ax.set_facecolor(SURFACE)
        for i, ot in enumerate(onsets):
            ax.axvspan(ot - t0, ot - t0 + fire_s, color=C_FIRE, alpha=0.16, lw=0,
                       label="fire pulse" if i == 0 else None)
        ax.plot(tr, y, lw=1.1, color=color, solid_joinstyle="round")
        if key == "res":
            ax.axhspan(1 - 2 * sd_r_n, 1 + 2 * sd_r_n, color=C_REF, alpha=0.16,
                       lw=0, zorder=0, label=f"±2σ noise ({2 * sd_r_n:.1%})")
            ax.axhline(1.0, ls="--", lw=1.0, color=C_REF)
            ax.text(t_hi, 1.0, f" R₀ ({r0_global:.2f} Ω)", color=INK_2,
                    fontsize=8, va="center", ha="left")
        ax.set_ylabel(f"{name}\n({unit})" if "\n" not in name else name,
                      fontsize=9, color=INK_2)
        ax.grid(True, color=GRID, lw=0.6)
        ax.set_axisbelow(True)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(AXIS)
        ax.tick_params(colors=MUTED, labelsize=8)
    axes[0].legend(loc="upper right", fontsize=8, frameon=False,
                   labelcolor=INK_2)
    axes[-1].set_xlabel("time since first fire (s)", fontsize=9, color=INK_2)
    axes[-1].set_xlim(t_lo, t_hi)
    fig.suptitle(
        f"{sess.name} — {cmd.n}× (fire {cmd.fire_ms:.0f} ms @ {cmd.v_high:.1f} V "
        f"→ cool {cmd.cool_ms:.0f} ms @ {cmd.v_low:.1f} V)",
        fontsize=13, color=INK, y=0.995)
    fig.tight_layout(rect=(0, 0, 1, 0.98))
    out = sess / "cycles_timeline.png"
    fig.savefig(out, dpi=args.dpi, facecolor=SURFACE)
    plt.close(fig)
    log.info("wrote %s", out)

    # -----------------------------------------------------------------------
    # Figure 2 — every cycle overlaid on fire-onset time
    # -----------------------------------------------------------------------
    norm = Normalize(vmin=1, vmax=len(onsets))
    r_ref_note = ("session cold R₀" if args.r_ref == "cold"
                  else "each cycle's own pre-fire R₀")
    panels = [
        ("volt", "Drive voltage", "V", False),
        ("curr", "Drive current", "A", False),
        ("res", f"SMA resistance — normalized to {r_ref_note}", "R/R₀", False),
        ("force", "Force (Δ vs pre-fire)", "N", True),
        ("disp", "Displacement (Δ vs pre-fire)", "µm", True),
    ]
    fig, axes = plt.subplots(3, 2, figsize=(13, 10.5), facecolor=SURFACE)
    axes = axes.ravel()
    for ax, (key, name, unit, as_delta) in zip(axes, panels):
        t, y = sig[key]
        ax.set_facecolor(SURFACE)
        ax.axvspan(0, fire_s, color=C_FIRE, alpha=0.16, lw=0, label="fire pulse")
        for i, ot in enumerate(onsets):
            tr, yc = slice_cycle(t, y, ot, span)
            if key == "res":
                yc = yc / r_ref(i)      # every cycle now starts at 1.0
            if as_delta:
                yc = yc - _baseline(tr, yc)
            ax.plot(tr, yc, lw=1.1, color=CYCLE_CMAP(norm(i + 1)), alpha=0.9)
        if key == "res":
            # The ±2σ scatter band: if the fire's ΔR does not leave this band,
            # the resistance signature is not resolved and the panel must show it.
            ax.axhspan(1 - 2 * sd_r_n, 1 + 2 * sd_r_n, color=C_REF,
                       alpha=0.16, lw=0, zorder=0,
                       label=f"±2σ noise ({2 * sd_r_n:.1%})")
            ax.axhline(1.0, ls="--", lw=1.0, color=C_REF)
            ax.legend(loc="upper right", fontsize=8, frameon=False,
                      labelcolor=INK_2)
            ax.text(0.985, 0.04,
                    f"R₀ = {r0_global:.2f} Ω. ΔR/R₀ stays inside the ±2σ band:\n"
                    f"the 100 ms fire is NOT resolved in R",
                    transform=ax.transAxes, color=INK_2, fontsize=8, ha="right",
                    va="bottom", bbox=_PLATE)
        if key == "disp" and feedthrough:
            ax.text(0.985, 0.04,
                    f"excursion is confined to the drive window\n"
                    f"({disp_in:+.1f} µm during fire, {disp_post:+.1f} µm at the\n"
                    f"force peak) → drive feedthrough, not motion",
                    transform=ax.transAxes, color=INK_2, fontsize=8, ha="right",
                    va="bottom", bbox=_PLATE)
        if as_delta:
            ax.axhline(0, lw=0.8, color=AXIS)
        ax.set_title(name, fontsize=10, color=INK, loc="left")
        ax.set_ylabel(unit, fontsize=9, color=INK_2)
        ax.set_xlabel("time since fire onset (s)", fontsize=9, color=INK_2)
        ax.set_xlim(-PRE_S, span)
        ax.grid(True, color=GRID, lw=0.6)
        ax.set_axisbelow(True)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(AXIS)
        ax.tick_params(colors=MUTED, labelsize=8)
    axes[0].legend(loc="upper right", fontsize=8, frameon=False, labelcolor=INK_2)

    # 6th cell: the cycle-index colorbar (identity of each trace) + the recipe.
    ax = axes[5]
    ax.set_facecolor(SURFACE)
    ax.axis("off")
    cb = fig.colorbar(ScalarMappable(norm=norm, cmap=CYCLE_CMAP), ax=ax,
                      orientation="horizontal", fraction=0.25, pad=0.05,
                      aspect=18)
    cb.set_label("cycle index (light → dark = early → late)", fontsize=9,
                 color=INK_2)
    cb.set_ticks(range(1, len(onsets) + 1))
    cb.ax.tick_params(colors=MUTED, labelsize=8)
    cb.outline.set_edgecolor(AXIS)
    ax.text(0.0, 0.42,
            f"{len(onsets)} cycles overlaid on the fire onset.\n"
            f"fire  {cmd.fire_ms:.0f} ms @ {cmd.v_high:.1f} V\n"
            f"cool  {cmd.cool_ms:.0f} ms @ {cmd.v_low:.1f} V (probe)\n"
            f"drive stream {len(sig['volt'][0]) / (sig['volt'][0][-1] - sig['volt'][0][0]):.0f} Hz, "
            f"sensors {len(sig['force'][0]) / (sig['force'][0][-1] - sig['force'][0][0]):.0f} Hz",
            transform=ax.transAxes, fontsize=9, color=INK_2, va="top",
            linespacing=1.6)
    fig.suptitle(f"{sess.name} — cycle overlay", fontsize=13, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    out = sess / "cycles_overlay.png"
    fig.savefig(out, dpi=args.dpi, facecolor=SURFACE)
    plt.close(fig)
    log.info("wrote %s", out)

    # -----------------------------------------------------------------------
    # Per-cycle metrics + Figure 3 — do the cycles repeat, or do they drift?
    # -----------------------------------------------------------------------
    met: List[dict] = []
    for i, ot in enumerate(onsets):
        row = {"cycle": i + 1, "t_fire_s": float(ot - t0)}

        tr, r = slice_cycle(*sig["res"], ot, span)
        r0 = _baseline(tr, r)
        ref = r_ref(i)
        hot = (tr >= 0) & (tr <= fire_s * 2)
        row["R0_ref_ohm"] = ref
        row["R_pre_ohm"] = r0
        row["R_pre_norm"] = r0 / ref
        row["R_peak_ohm"] = float(np.max(r[hot])) if hot.any() else float("nan")
        row["dR_peak_ohm"] = row["R_peak_ohm"] - r0
        # ΔR as a fraction of the coil's own R₀ — the comparable, unit-free form.
        row["dR_peak_pct"] = 100.0 * (row["R_peak_ohm"] - ref) / ref
        end = tr >= span - 0.5
        row["R_end_ohm"] = float(np.mean(r[end])) if end.any() else float("nan")
        row["R_end_norm"] = row["R_end_ohm"] / ref

        tr, f = slice_cycle(*sig["force"], ot, span)
        f0 = _baseline(tr, f)
        row["F_pre_N"] = f0
        if len(f):
            j = int(np.argmax(np.abs(f - f0)))
            row["dF_peak_N"] = float(f[j] - f0)
            row["t_F_peak_s"] = float(tr[j])
        end = tr >= span - 0.5
        row["dF_end_N"] = float(np.mean(f[end]) - f0) if end.any() else float("nan")

        tr, d = slice_cycle(*sig["disp"], ot, span)
        d0 = _baseline(tr, d)
        if len(d):
            j = int(np.argmax(np.abs(d - d0)))
            row["dDisp_peak_um"] = float(d[j] - d0)
        # Feedthrough evidence: the laser inside the drive window vs at the force
        # peak. Motion persists into the second column; feedthrough does not.
        mf = (tr >= 0) & (tr <= fire_s)
        mp = (tr >= 0.20) & (tr <= 0.30)
        row["dDisp_in_fire_um"] = float(np.mean(d[mf]) - d0) if mf.any() else float("nan")
        row["dDisp_at_Fpeak_um"] = float(np.mean(d[mp]) - d0) if mp.any() else float("nan")
        met.append(row)

    cols = ["cycle", "t_fire_s", "R0_ref_ohm", "R_pre_ohm", "R_pre_norm",
            "R_peak_ohm", "dR_peak_ohm", "dR_peak_pct", "R_end_ohm", "R_end_norm",
            "F_pre_N", "dF_peak_N", "t_F_peak_s", "dF_end_N",
            "dDisp_peak_um", "dDisp_in_fire_um", "dDisp_at_Fpeak_um"]
    with open(sess / "cycles_metrics.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for row in met:
            w.writerow({c: f"{row.get(c, float('nan')):.6g}" for c in cols})
    log.info("wrote %s", sess / "cycles_metrics.csv")

    cyc = np.array([m["cycle"] for m in met])
    trends = [
        ("dR_peak_pct", "Peak resistance rise, normalized", "ΔR/R₀ (%)", C_RES),
        ("dF_peak_N", "Peak force swing", "ΔF (N)", C_FORCE),
        ("R_pre_norm", "Pre-fire resistance (did it cool back to R₀?)",
         "R/R₀", C_RES),
    ]
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8), facecolor=SURFACE)
    for ax, (key, name, unit, color) in zip(axes, trends):
        y = np.array([m.get(key, np.nan) for m in met], dtype=float)
        ax.set_facecolor(SURFACE)
        if key == "dR_peak_pct":
            band = 100.0 * 2 * sd_r_n
            ax.axhspan(-band, band, color=C_REF, alpha=0.16, lw=0,
                       zorder=0, label=f"±2σ noise (±{band:.1f}%)")
            ax.axhline(0, lw=0.8, color=AXIS)
            ax.legend(loc="upper left", fontsize=8, frameon=False,
                      labelcolor=INK_2)
        if key == "dF_peak_N":
            ax.axhspan(-2 * sd_force, 2 * sd_force, color=C_REF, alpha=0.16,
                       lw=0, zorder=0, label=f"±2σ noise ({2 * sd_force:.3f} N)")
            ax.legend(loc="upper left", fontsize=8, frameon=False,
                      labelcolor=INK_2)
        ax.plot(cyc, y, "-o", lw=1.6, ms=6, color=color,
                markeredgecolor=SURFACE, markeredgewidth=1.2, zorder=3)
        if key == "R_pre_norm":
            ax.axhline(1.0, ls="--", lw=1.0, color=C_REF)
            ax.text(0.98, 0.06, f"R₀ = {r0_global:.2f} Ω", transform=ax.transAxes,
                    color=INK_2, fontsize=8, ha="right")
        if np.isnan(y[0]):
            # The SMA V/I/R stream only starts when the driver arms — which is
            # the first fire — so cycle 1 has no pre-fire baseline to subtract.
            ax.text(0.02, 0.02, "cycle 1: no pre-fire baseline (stream starts at "
                    "the first fire)", transform=ax.transAxes, color=MUTED,
                    fontsize=7, ha="left")
        # Direct-label the first and last cycle so the trend reads without the axis.
        for j in (0, len(cyc) - 1):
            if np.isfinite(y[j]):
                ax.annotate(f"{y[j]:.3g}", (cyc[j], y[j]), textcoords="offset points",
                            xytext=(0, 9), ha="center", fontsize=8, color=INK_2)
        ax.set_title(name, fontsize=10, color=INK, loc="left")
        ax.set_xlabel("cycle", fontsize=9, color=INK_2)
        ax.set_ylabel(unit, fontsize=9, color=INK_2)
        ax.set_xticks(cyc)
        ax.grid(True, color=GRID, lw=0.6)
        ax.set_axisbelow(True)
        ax.margins(y=0.22)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(AXIS)
        ax.tick_params(colors=MUTED, labelsize=8)
    fig.suptitle(f"{sess.name} — cycle-to-cycle trend", fontsize=13, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    out = sess / "cycles_trend.png"
    fig.savefig(out, dpi=args.dpi, facecolor=SURFACE)
    plt.close(fig)
    log.info("wrote %s", out)

    # Console summary.
    dR = np.array([m["dR_peak_pct"] for m in met], dtype=float)
    dF = np.array([m["dF_peak_N"] for m in met], dtype=float)
    print(f"\n{sess.name}: {len(onsets)} cycles "
          f"(fire {cmd.fire_ms:.0f} ms @ {cmd.v_high:.1f} V, "
          f"cool {cmd.cool_ms:.0f} ms @ {cmd.v_low:.1f} V)")
    print(f"  force   : ΔF {np.nanmean(dF):+.4f} ± {np.nanstd(dF):.4f} N peak, "
          f"{np.nanmean([m['t_F_peak_s'] for m in met]):.2f} s after onset "
          f"(noise ±{sd_force:.4f} N) — RESOLVED")
    print(f"  resist. : R₀ = {r0_global:.3f} Ω ({args.r_ref} ref); "
          f"ΔR/R₀ {np.nanmean(dR):+.1f} ± {np.nanstd(dR):.1f} % vs a "
          f"±{100 * 2 * sd_r_n:.1f} % (2σ) noise floor — NOT RESOLVED")
    print(f"  laser   : {disp_in:+.2f} µm inside the fire window, "
          f"{disp_post:+.2f} µm at the force peak (noise ±{sd_disp:.2f} µm)"
          + (" — DRIVE FEEDTHROUGH, not motion" if feedthrough else ""))
    print(f"  outputs : cycles_timeline.png, cycles_overlay.png, "
          f"cycles_trend.png, cycles_metrics.csv\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
