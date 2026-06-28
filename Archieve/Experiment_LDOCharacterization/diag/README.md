# Experiment_LDOCharacterization/diag — one-off debug scripts

**Status: Diagnostic.** Not part of the experiment flow. Kept for re-use if the
scope trigger path misbehaves again.

| File | What it was for |
|---|---|
| `scope_probe.py` | Minimal SCPI probe — confirm the scope answers `*IDN?` / basic queries on `:5025`. |
| `diag_arm.py` | Isolate the single-shot arm path (`TRSE`/`TRMD`/`ARM`) without firing the H7. |
| `diag_loop.py` | Repeated arm→fire→poll loop to chase the intermittent `wait_capture_complete` hang. |
| `TRIGGER_DEBUG.md` | Running log of the scope-trigger SCPI debugging (`INR?`/`SAST?` completion-poll semantics on the SDS2000X Plus). |

The production capture path lives one level up in `scope_trigger.py` +
`run_experiment.py`.
