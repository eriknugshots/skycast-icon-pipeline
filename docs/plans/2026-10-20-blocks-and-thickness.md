# Day blocks and cloud thickness in the feed — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The ICON feed also publishes, on Vercel Blob only, every square as day-addressable blocks (`blocks/<run>/<sq>.icb`, ICL2) and the column cloud ice and water as `thick/<run>/<sq>.ict` (ICT1, same block layout), named by two new manifest keys.

**Architecture:** One block codec (`pipeline/blocks.py`) writes a header, the step hours, an index of byte ranges and independently zlib-compressed 6-step blocks; ICL2 carries the four cover fields, ICT1 two log-encoded column amounts. The build writes both into `blob/` after the Pages squares; thickness is optional like the column feed (it waits, then publishes without), with its own history heads in `state/thick/`.

**Tech Stack:** Python 3.12, numpy, netCDF4, cdo (CI only), pytest; Node `node --test` for `publish/`.

**Specs (in the SkyCast repo):** `docs/superpowers/specs/2026-10-19-load-the-day-you-see-design.md` (the feed section: ICL2, `blocks`) and `docs/superpowers/specs/2026-10-19-cloud-thickness-design.md` (Data, Pipeline). Read both sections before Task 1.

## Global Constraints

- Pages is unchanged: `site/` gets no new files and its manifest no new values (`blocks`/`thick` are null there); `SITE_BUDGET_BYTES` counts nothing new.
- `tiles/<run>/<sq>.icl` (ICL1) is unchanged in format and content on both hosts: installed app builds read it.
- Block layout: 14-byte header `<4shhHHH` (magic, swLat, swLon, cols, rows, steps), `steps` × u32 unix hours, `blockSteps` u16, `blockCount` u16, then `blockCount` × (`offset` u32, `length` u32), offsets from the start of the block area; blocks consecutive, in step order; each block a zlib stream (`zlib.compress(…, 6)`) of u8 `[step][field][row][col]`. All integers little-endian.
- `BLOCK_STEPS = 6`. ICL2 magic `b"ICL2"`, 4 fields (low, mid, high, total, percent, `QUANT_STEP` quantised, 255 missing). ICT1 magic `b"ICT1"`, 2 fields (ice, water).
- Thickness byte: 0 = under `W_MIN` 1 g/m², 255 = missing (no data or NaN), else `1 + round(253 · ln(W/W_MIN) / ln(W_MAX/W_MIN))` clipped to 1–254, `W` in g/m², `W_MAX` 5000 g/m².
- `THICK_FIELDS = ["TQI_DIA", "TQC_DIA"]` (ice, water; measured 2026-10-20, spec Data section). Never part of `newest_complete_run`.
- A thickness failure never fails the build: `manifest.thick` is null and `thick/` is removed from `blob/`. Wait for unlisted thickness files `THICK_WAIT_S` = 15 × 60 s, polling every `COLUMN_POLL_S`, through `deps.sleep`.
- `.ict` carries exactly the `.icl`'s step hours; an hour with no thickness (no history yet) is all 255.
- Tests: `work/.venv/bin/python -m pytest -q` (venv already made; never `pip install` into it) and `cd publish && node --test`. Do not disable the sandbox under any circumstances — stop and report. Never push to `main`: commit on the branch you are on.

## Review Focus

1. A square whose step count is not a multiple of 6 (a test build with 7 steps): the last block is short and still decodes — Task 1 pins 7 steps → blocks of 6 and 1.
2. Fill values in the regridded thickness (NaN, negative from interpolation, masked): bytes 255 for NaN/masked, 0 for ≤ 0 — Task 2 pins both.
3. A run whose thickness files are listed but one download fails mid-way: the build still publishes the cover feed with `thick` null and no partial `thick/` folder — Task 4 pins it.
4. History heads written by an older build (no `state/thick/`): the first thickness build has only its own hours and fills the past with 255 — Task 4 pins it.
5. The prune after a run whose previous manifest had no `blocks`/`thick` keys (the first publish of this change): nothing of the new run is deleted and the old ICL1 folders are kept as today — Task 5 pins it.

---

### Task 1: The block codec and the thickness byte

**Files:**
- Create: `pipeline/blocks.py`, `pipeline/thick.py`
- Test: `tests/test_blocks.py`, `tests/test_thick.py`

**Interfaces:**
- Produces: `blocks.BLOCK_STEPS = 6`; `blocks.encode_blocks(magic: bytes, sw_lat, sw_lon, step_hours: list[int], cube: np.ndarray[uint8, (steps, fields, 41, 41)]) -> bytes`; `blocks.read_header(buf: bytes) -> dict` with keys `magic, sw_lat, sw_lon, cols, rows, steps, step_hours, block_steps, blocks` (list of `(offset, length)`), `data_start` (int: byte offset of the block area); `blocks.decode_blocks(buf: bytes, fields: int) -> dict` (`read_header`'s keys plus `cube`, the whole cube). `thick.W_MIN_G = 1.0`, `thick.W_MAX_G = 5000.0`, `thick.thick_bytes(kg_m2: np.ndarray) -> np.ndarray[uint8]`, `thick.grams_of_byte(b: int) -> float` (0 for 0, nan for 255).

- [ ] **Step 1: Write the failing tests** — `tests/test_blocks.py`:
  - `test_round_trip_7_steps`: cube `rng.integers(0, 101, (7, 4, 41, 41), uint8)`, hours `[500000 + k for k in range(7)]`, `buf = encode_blocks(b"ICL2", 40, -125, hours, cube)`; `decode_blocks(buf, 4)["cube"]` equals cube; header `block_steps == 6`, `len(blocks) == 2`; `data_start == 62` (14 + 4·7 + 4 + 8·2, computed); `data_start + sum(lengths) == len(buf)`; offsets contiguous (`offsets[i+1] == offsets[i] + lengths[i]`, `offsets[0] == 0`).
  - `test_each_block_decodes_alone`: for the 7-step cube, `zlib.decompress(buf[data_start + off : data_start + off + ln])` reshaped `(n, 4, 41, 41)` equals `cube[6*i : 6*i + n]`, n = 6 then 1.
  - `test_153_steps_header_is_838_bytes` (hours ascending, zeros cube): `data_start == 838`, 26 blocks.
  - `test_rejects` — `encode_blocks` raises `ValueError` for non-uint8, wrong side, hours not ascending, hours count ≠ steps; `decode_blocks` raises `ValueError` for a buffer whose magic is not ICL2/ICT1 and when `fields` does not fit a block's length.
  - `tests/test_thick.py` `test_bytes`: `thick_bytes(np.array([0, 0.0005, 0.001, 0.01, 0.027, 0.1, 1.0, 5.0, 20.0, np.nan, -0.002]))` equals `[0, 0, 1, 69, 99, 138, 206, 254, 254, 255, 0]` (computed); `grams_of_byte(99)` ≈ 27.090 (abs 1e-3), `grams_of_byte(1) == 1.0`, `grams_of_byte(254) == 5000.0`, `grams_of_byte(0) == 0`, `math.isnan(grams_of_byte(255))`.
- [ ] **Step 2: Run** `work/.venv/bin/python -m pytest -q tests/test_blocks.py tests/test_thick.py` — expect import errors.
- [ ] **Step 3: Implement** both modules. `encode_blocks` validates like `encode.encode_square` (reuse its checks' wording), slices `cube[i:i+6]` per block. `decode_blocks` checks magic in `{b"ICL2", b"ICT1"}` and that each decompressed block is `n × fields × rows × cols` bytes.
- [ ] **Step 4: Run** the two test files — PASS — then the whole suite (`-q`): 139 + new passed, 2 skipped.
- [ ] **Step 5: Commit** `feat(feed): block codec (ICL2/ICT1 layout) and the thickness byte`.

### Task 2: Regridding a physical field, and the thickness fields

**Files:**
- Modify: `pipeline/regrid.py` (after `regrid`), `pipeline/dwd.py` (after `TAIL_STEPS`), `run_build.py` (`Deps`, `REAL`)
- Test: `tests/test_regrid.py`, `tests/test_dwd.py`

**Interfaces:**
- Consumes: `thick.thick_bytes` (Task 1).
- Produces: `regrid.world_values_from_netcdf(path) -> np.ndarray[float32, (NY, NX)]` (masked/invalid → NaN, no clipping, no scaling); `regrid.regrid_values(grib_bz2, kit, work, run=subprocess.run) -> float32 world` (same steps and cleanup as `regrid`); `dwd.THICK_FIELDS = ["TQI_DIA", "TQC_DIA"]`; `dwd.missing_thick_files(listing, run, steps) -> list[str]` (file names not yet listed, across both fields); `run_build.Deps` gains a LAST field `regrid_values` defaulting to `regrid.regrid_values` (so every existing `Deps(...)` call still works).

- [ ] **Step 1: Failing tests.** `test_regrid.py`: `test_values_keep_physical_units` — write a nc (the file's `_write_nc` helper) with values `[[0.0273, -0.001], [nan, 3.5]]`-patterned over `(NY, NX)` and a masked cell; `world_values_from_netcdf` returns float32 with 0.0273 kept to 1e-6, the masked and NaN cells NaN, 3.5 kept (no clip at 100 either: also write 250.0 and read 250.0). `test_regrid_values_runs_cdo_and_cleans_up` mirrors the existing `regrid` test with an injected `run`. `test_dwd.py`: `test_thick_fields` — `THICK_FIELDS == ["TQI_DIA", "TQC_DIA"]`; `file_url(run, 5, "TQI_DIA")` ends `/00/tqi_dia/icon_global_icosahedral_single-level_2026092700_005_TQI_DIA.grib2.bz2`; `missing_thick_files` on a listing holding every `TQI_DIA` step but no `TQC_DIA` returns exactly the `TQC_DIA` names; `newest_complete_run` is unchanged by thickness (a listing with cover files only still returns the run — pin it).
- [ ] **Step 2: Run** them — FAIL.
- [ ] **Step 3: Implement.** `missing_thick_files(listing, run, steps)` calls `listing(f"{run:%H}", field)` per field and `missing_files`.
- [ ] **Step 4: Run** the suite — PASS.
- [ ] **Step 5: Commit** `feat(feed): regrid a physical field; TQI_DIA/TQC_DIA (measured 2026-10-20)`.

### Task 3: Publish every square as day blocks on Blob

**Files:**
- Modify: `run_build.py` (`build`: after the Pages budget check, before `stacks.clear()`; `site_manifest`), `pipeline/site.py` (`blocks_path`, `build_manifest`)
- Test: `tests/test_run_build.py`, `tests/test_site.py`

**Interfaces:**
- Consumes: `blocks.encode_blocks` (Task 1).
- Produces: `site.blocks_path(run) -> "blocks/<YYYYMMDDHH>"`; `site.build_manifest(..., columns=None, blocks=None, thick=None)` emitting `"blocks"` and `"thick"` keys (null when not given); `run_build.site_manifest(run, built, hours, names, history_steps, complete, tail=None, columns=None, blocks=None, thick=None)`; `run_build.write_blocks(blob_dir, run, names, stacks, hours) -> int` (bytes written). The Blob manifest carries `blocks: blocks_path(run)`; the Pages manifest `blocks: null`.

- [ ] **Step 1: Failing tests** (reuse `_fake_deps` as it is):
  - `test_blocks_go_to_blob_with_the_same_values_as_the_squares`: build steps `[0..6]`, squares `["N40W125"]`, `world_value=lambda s, f: min(100, s + FIELDS.index(f))`; Blob manifest (`tmp_path/"blob"/"manifest.json"`) `blocks == "blocks/2026092700"`; `decode_blocks((blob/"blocks/2026092700/N40W125.icb").read_bytes(), 4)` has magic ICL2, the `.icl`'s `step_hours`, and a cube equal to the `.icl`'s.
  - `test_pages_gets_no_blocks`: `not (site/"blocks").exists()`; Pages manifest `blocks is None`.
  - `test_blocks_use_the_2_percent_step_even_over_the_pages_budget`: with `SITE_BUDGET_BYTES` 1 and value 13 (the existing budget test's setup), the `.icl` holds 12 (13 in 4% steps, computed) while the `.icb` holds 14 (2% steps).
  - `test_site.py`: `build_manifest` with `blocks="blocks/x"`, `thick={"tiles": "thick/x"}` round-trips both; without them both are `None`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** `write_blocks` slices each square from `stacks` exactly as `write_squares` does and encodes `quantize(cube, QUANT_STEP)` with `encode_blocks(b"ICL2", …)` into `blob_dir/blocks_path(run)/<name>.icb`. Call it with the Blob directory before `stacks.clear()`; pass `blocks=site.blocks_path(run)` to the Blob manifest only. Print `blocks: N squares, X MB`.
- [ ] **Step 4: Run** the suite — PASS (every existing test still passes).
- [ ] **Step 5: Commit** `feat(feed): every square as day blocks on Blob (ICL2), manifest key blocks`.

### Task 4: Cloud thickness

**Files:**
- Modify: `run_build.py` (new `build_thick` after `build_tail`; called from `build` after `stacks.clear()`, before `build_columns`)
- Test: `tests/test_run_build.py`

**Interfaces:**
- Consumes: `dwd.THICK_FIELDS`, `dwd.missing_thick_files`, `deps.regrid_values`, `thick.thick_bytes`, `blocks.encode_blocks`, `history.save_run_head/load_history_steps/prune` with `sub="thick"`.
- Produces: `run_build.THICK_WAIT_S = 15 * 60`; `run_build.site_thick_root() -> "thick"`; `site.thick_path(run) -> "thick/<YYYYMMDDHH>"`; `run_build.build_thick(blob_dir, state, work, deps, kit, run, steps, names, workers, hours) -> dict | None`, the manifest entry `{"format": "ICT1", "run": run_iso, "runMs": int, "tiles": thick_path(run)}`.

- [ ] **Step 1: Failing tests.** First give `_fake_deps` a `regrid_values` (passed into `Deps`) that records `(step, field)` in `calls["regrids_values"]` and returns `np.full((NY, NX), v, np.float32)` with v = 0.027 for `TQI_DIA`, 0.1 for `TQC_DIA`, overridable by a `thick_value=lambda step, field: …` argument — from this task on every build calls it, and no test may reach cdo. Then:
  - `test_thickness_goes_to_blob_by_the_squares_hours`: steps `[0..6]`, one square; Blob manifest `thick == {"format": "ICT1", "run": "2026-09-27T00Z", "runMs": …, "tiles": "thick/2026092700"}`; the `.ict` decodes (`decode_blocks(…, 2)`) with the `.icl`'s `step_hours`, field 0 all 99, field 1 all 138 (computed in Task 1's test); Pages has no `thick/`; `calls["regrids_values"]` holds 7 × 2 entries.
  - `test_hours_before_the_thickness_history_are_missing`: a cover history head for the previous run in state (as `test_history_from_state_is_prepended` does) and no `state/thick`; the `.ict`'s first 6 steps are all 255, the run's own steps hold values.
  - `test_thickness_history_comes_from_state`: also save a `sub="thick"` head of all 50 for the previous run; the first 6 steps are 50.
  - `test_a_run_saves_its_thickness_head`: after a build with steps 0–5, `state/thick/2026092700.npz` exists with head shape `(6, 2, NY, NX)`.
  - `test_unlisted_thickness_waits_then_publishes_without`: a listing with no `tqi_dia`/`tqc_dia` names; `calls["sleeps"]` sums to `THICK_WAIT_S`; Blob manifest `thick is None`; no `blob/thick/`; return code `BUILT`.
  - `test_a_thickness_failure_never_fails_the_build`: `regrid_values` raises on step 3 of `TQC_DIA`; `BUILT`, `thick is None`, no `blob/thick/`, the `.icl` and `.icb` still there.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** `build_thick` on `build_columns`' pattern. Convert each regridded field to bytes INSIDE its download job and keep only the bytes (a float32 world is 16.6 MB; 153 steps × 2 fields would be 5 GB on a 16 GB runner, bytes 1.2 GB). The rest follows (wait loop, `try` around everything, `shutil.rmtree(blob/"thick")` on failure, a `::warning::` line). Download and regrid `THICK_FIELDS` per step with the build's thread pool; bytes per step `np.stack([thick_bytes(ice), thick_bytes(water)])`; past hours from `load_history_steps(state, run, sub="thick", shape=(2, NY, NX))`; any of `hours` without data is `np.full((2, NY, NX), 255)`; save the head (steps 0–5) and prune like the cover history. Per square `encode_blocks(b"ICT1", …)` over exactly `hours`.
- [ ] **Step 4: Run** the suite — PASS.
- [ ] **Step 5: Commit** `feat(feed): cloud ice and water per square as ICT1 on Blob, manifest key thick`.

### Task 5: Prune keeps the new folders; README

**Files:**
- Modify: `publish/prune.mjs` (`manifestFolders`), `README.md`
- Test: `publish/prune.test.mjs`

**Interfaces:**
- Consumes: the manifest keys from Tasks 3–4 (`blocks`: string folder; `thick`: object with `tiles`).

- [ ] **Step 1: Failing tests** in `prune.test.mjs`: `manifestFolders` of `{tiles: "tiles/A", blocks: "blocks/A", thick: {tiles: "thick/A"}, tail: null, columns: null}` is `["tiles/A/", "blocks/A/", "thick/A/"]` (order: tiles, blocks, then tail/columns/thick objects — pin the exact array); `pathsToDelete` with `current` = that manifest and `previous` = an old manifest with only `tiles: "tiles/Z"` keeps `blocks/A/x.icb`, `thick/A/x.ict`, `tiles/Z/x.icl` and deletes `blocks/Q/x.icb`.
- [ ] **Step 2: Run** `cd publish && node --test` — FAIL.
- [ ] **Step 3: Implement**: `blocks` handled like `tiles` (a string); `thick` added to the object keys loop. README: one paragraph each for `blocks/` (ICL2, Blob only, why beside ICL1 — installed builds) and `thick/` (ICT1, TQI_DIA/TQC_DIA, byte encoding, optional like the columns), with the header layout pointer to `pipeline/blocks.py`.
- [ ] **Step 4: Run** `node --test` and the pytest suite — PASS.
- [ ] **Step 5: Commit** `feat(publish): prune keeps blocks/ and thick/; README`.
