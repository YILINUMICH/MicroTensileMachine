# run_night.ps1 — the 2026-08-05 overnight campaign, in RUN ORDER.
#
# WHY THIS FILE EXISTS
#   `operator_profile_queue.py <folder>` expands to SORTED order (n0..n9).
#   Sorted order is not the order this campaign should run in — see
#   "Run order" in README.md. The queue also accepts an explicit ordered
#   list of files, which is what this script passes. Nothing is renamed, so
#   `gen_night_profiles.py` stays the source of truth for the profiles and
#   the pre-registered role in each filename is untouched.
#
# USAGE
#   .\profiles\night_profiles_20260805\run_night.ps1 -DryRun   # validate first
#   .\profiles\night_profiles_20260805\run_night.ps1           # the real run
#
# Run it from the MODULE ROOT (Experiment_SMAThermalCharacterization/).
param(
    [switch]$DryRun,
    [string]$Deadline = "",          # e.g. "07:00"; empty = no deadline (default)
    [double]$BetweenS = 30.0
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent (Split-Path -Parent $here)

# ── RUN ORDER (see README.md "Run order" for the rationale) ─────────────────
#  n0 + n4a   : the ATTENDED head, ~40 min. n4a is the riskiest block (5-11 s
#               cool -> baseline ratcheting) and it runs while you are still
#               at the rig; its report lands ~1 min after it finishes.
#  first half : everything the night cannot afford to lose, + n8 as a
#               low-energy cool-down so n3 sees a coil in n0's state
#  n3         : lands at ~50 % of wall clock, not 65 % as sorted order gives
#  second half: the B halves + the two most redundant structured blocks
#  n5 before n9 for the same reason n8 sits before n3
$order = @(
    "n0_anchor_start.json",
    "n4a_shortcool_test.json",
    "n1a_shuffle_train.json",
    "n2a_shuffle_test.json",
    "n6_repeats.json",
    "n8_threshold.json",
    "n3_anchor_mid.json",
    "n1b_shuffle_train.json",
    "n2b_shuffle_test.json",
    "n4b_shortcool_test.json",
    "n7_ladder.json",
    "n5_longcool.json",
    "n9_anchor_end.json"
)

$paths = $order | ForEach-Object {
    $p = Join-Path $here $_
    if (-not (Test-Path $p)) { throw "missing profile: $p" }
    $p
}

# Every profile in the folder must appear exactly once — a profile added by a
# later `gen_night_profiles.py` run would otherwise be silently dropped.
$onDisk = Get-ChildItem -Path $here -Filter "n*.json" | ForEach-Object { $_.Name }
$missing = $onDisk | Where-Object { $order -notcontains $_ }
if ($missing) { throw "profile(s) on disk but not in `$order: $($missing -join ', ')" }

$argsList = @("operator_profile_queue.py") + $paths + @("--between-s", $BetweenS)
if ($DryRun)   { $argsList += "--dry-run" }
if ($Deadline) { $argsList += @("--deadline", $Deadline) }

Write-Host "run order:" -ForegroundColor Cyan
$i = 0
foreach ($o in $order) { $i++; Write-Host ("  {0,2}. {1}" -f $i, $o) }
Write-Host ""

Push-Location $root
try { & python @argsList }
finally { Pop-Location }
