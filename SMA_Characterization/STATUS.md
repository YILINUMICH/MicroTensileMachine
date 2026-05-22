# SMA_Characterization — STATUS

| Field | Value |
|---|---|
| **Status** | **Archived** — superseded by `SMA_CharacterizationV2/` |
| **Role** | v1 of the SMA recorder. Two daemon threads (LCR + laser) writing into a single shared output directory. Worked, but lacked the OPEN/SHORT/RAW phase state machine and the operator confirm/redo flow. |
| **Superseded by** | `SMA_CharacterizationV2/` |
| **Owner** | Yilin |
| **Should you use this?** | No. Use `SMA_CharacterizationV2/`. This folder is kept only for reproducing v1-era runs from older data. |

## Module TODOs

- [ ] **Decide whether to fully delete or move to `Archieve/`.** Currently it's still at top level despite being superseded. Either move it under `Archieve/` for consistency with `Archieve/AD2/`, or delete outright.
- [ ] **Once removed, update any sibling references** — search for `SMA_Characterization` (without the `V2`) across the repo before deleting.

See [../TODO.md](../TODO.md) for cross-cutting items.
