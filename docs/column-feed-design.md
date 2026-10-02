# Column feed: fog and inversion profiles on ICON Direct (option A)

Brief: SkyCast `docs/cloud/2026-10-02-fog-column-feed-brief.md` (dev branch).
Status: built and measured on real DWD data (2026-10-02); the app's reader is
not written yet (SkyCast, locally).

## Why

With the forecast source on "DWD ICON · Direct", the app's fog and inversion
layer is the last per-place call SkyCast still makes to Open-Meteo
(`fetchInvProfile` in SkyCast `src/main.js`). The column feed publishes the
same profile from our own copy of DWD ICON, so that call (and its quota) goes.

## What the app reads today, and what DWD has

`fetchInvProfile` asks Open-Meteo for one point, hourly, past day + 3 days:
`temperature`, `relative_humidity`, `geopotential_height` at 1000, 975, 950,
925, 900, 850, 800, 700 hPa; `temperature_2m`, `temperature_80m`,
`relative_humidity_2m`; the model's surface `elevation`.
`detectInversionAt` builds one sorted height profile per hour from them.

DWD ICON global open data (listing of `grib/00/*` checked 2026-10-02 by the
`columns-measure` workflow):

| app field | DWD file (`grib/<HH>/<dir>/`) | levels | used |
|---|---|---|---|
| temperature_*hPa | `pressure-level_…_<lev>_T` (dir `t`) | 1000, 950, 925, 900, 850, 800, 700 | yes |
| relative_humidity_*hPa | `pressure-level_…_<lev>_RELHUM` (dir `relhum`) | same | yes |
| geopotential_height_*hPa | `pressure-level_…_<lev>_FI` (dir `fi`), m² s⁻² | same | yes, ÷ 9.80665 |
| **975 hPa** | **not published** for ICON global (levels are 1000, 950, 925, 900, 850, 800, 700, 600 … 30) | — | **left out** |
| temperature_2m | `single-level_…_T_2M` | 2 m | yes |
| relative_humidity_2m | `single-level_…_RELHUM_2M` | 2 m | yes |
| **temperature_80m** | **no 80 m field in ICON open data** | — | interpolated (below) |
| elevation | `time-invariant_…_HSURF` | surface | yes |
| (for T 80 m) | `model-level_…_<lev>_T` (dir `t`), levels 1–120 | 117, 118, 119 | yes |
| (for T 80 m) | `time-invariant_…_<lev>_HHL` (dir `hhl`), half levels 1–121 | 117–120 | yes |
| (not used) | `single-level_…_T_G` (ground temperature) | surface | no: a skin temperature, not air at 80 m |

All pressure-level and model-level fields have the same steps as the cloud
fields: hourly to 78 h, 3-hourly to 180 h.

**975 hPa.** The app's `INVERSION_LEVELS_HPA` names it; DWD does not publish
it for the global model. The column carries the seven levels DWD has, and the
field table says which (the decoder must take the levels from the file, not
from `INVERSION_LEVELS_HPA`). `detectInversionAt` already `continue`s past a
level it has no data for, so a 975-less profile works as is; the only effect
is a coarser profile between 1000 and 950 hPa (~400 m).

**T at 80 m.** ICON has no 80 m output. It is computed per node by linear
interpolation in height between two model levels (full levels; 120 is the
lowest), whose heights are the midpoints of their half levels in HHL, minus
the surface height. Which two was measured, not assumed — DWD's HHL for the
2026-10-02 00Z run, every node of the 0.25° lattice (`columns-measure`):

| full level | above ground p1 | p50 | p99 | min | max |
|---|---|---|---|---|---|
| 116 | 123.3 | 253.4 | 255.4 | 103.0 | 283.6 |
| 117 | 87.2 | 166.7 | 167.9 | 74.1 | 185.2 |
| 118 | 54.9 | 95.6 | 96.3 | 50.4 | 105.3 |
| 119 | 30.0 | 42.1 | 42.4 | 29.9 | 45.8 |
| 120 | 10.0 | 10.0 | 10.1 | 10.0 | 10.7 |

(HHL 121 equals HSURF exactly.) So 80 m lies between levels 118 and 119
almost everywhere; where 118 sits below 80 m (it can be as low as 50 m),
between 117 and 118. The build fetches T on 117, 118 and 119 and, per node,
uses the adjacent pair that brackets 80 m:
`T80 = T_lo + (T_up − T_lo) · (80 − z_lo) / (z_up − z_lo)`. Where even 117
is below 80 m (it can be 74 m) the weight is clamped: T80 = T117, never an
extrapolation. The weights are computed once per run (HHL is
time-invariant); `run_build.t80_weights`. Effect on the app if T80 were
left out instead: `detectInversionAt` never counts a cap whose lower sample
is under 150 m above ground, so the 80 m sample never forms a cap itself;
it only adds a sample (3 are needed) and weights the 2 m RH twice in the
below-cap mean. Interpolating keeps the app's profile identical in shape to
today's.

## Node spacing

**0.25°** (`COLUMN_STRIDE = 2`: every second point of the 0.125° grid the
cloud squares use; 21 × 21 nodes a square, ~28 km north–south, ~20 km
east–west at Bend's latitude). Chosen on usefulness near the ground — where
valley inversions and radiation fog live — measured, not assumed.

ICON global's own cells are ~13 km, and DWD's 0.125° remap is
nearest-neighbour, so the 0.125° grid IS the model at its resolution (and
what Open-Meteo answers a point from). A node lattice coarser than that
makes the app interpolate between nodes, and in complex terrain that mixes
a valley floor with the ridges around it. `columns-measure` (only=interp)
took one day of the 2026-10-02 00Z run (hours 0–21 every 3 h) on the full
0.125° grid as truth, and bilinearly interpolated each spacing's nodes to
every 0.125° point between them (1 447 825 land points; 273 958 on steep
ground = more than 300 m of relief among a point and its 8 neighbours).
Absolute error, p50 / p95 / p99:

| spacing | field | land | steep ground |
|---|---|---|---|
| 0.25° | model surface height | 12 / 175 / 366 m | 94 / 373 / 599 m |
| 0.25° | T 2 m | 0.21 / 1.58 / 3.04 °C | 0.69 / 2.86 / 4.47 °C |
| 0.25° | T 80 m | 0.17 / 1.41 / 2.78 °C | 0.69 / 2.73 / 4.31 °C |
| 0.25° | **T80 − T2m** (near-surface inversion) | 0.10 / 1.06 / 2.16 °C | 0.18 / 1.65 / 2.88 °C |
| 0.25° | RH 2 m | 0.7 / 6.3 / 12.2 % | 2.3 / 11.1 / 18.3 % |
| 0.5° | model surface height | 18 / 249 / 524 m | 130 / 535 / 867 m |
| 0.5° | T 2 m | 0.31 / 2.21 / 4.17 °C | 0.97 / 3.94 / 6.09 °C |
| 0.5° | T 80 m | 0.26 / 2.02 / 3.94 °C | 0.96 / 3.84 / 6.03 °C |
| 0.5° | **T80 − T2m** | 0.15 / 1.42 / 2.81 °C | 0.26 / 2.09 / 3.59 °C |
| 0.5° | RH 2 m | 1.0 / 8.5 / 15.9 % | 3.2 / 14.7 / 23.6 % |
| 1.0° | model surface height | 29 / 371 / 762 m | 181 / 763 / 1221 m |
| 1.0° | T 2 m | 0.47 / 3.15 / 5.84 °C | 1.35 / 5.47 / 8.52 °C |
| 1.0° | T 80 m | 0.42 / 2.96 / 5.66 °C | 1.33 / 5.38 / 8.51 °C |
| 1.0° | **T80 − T2m** | 0.22 / 1.90 / 3.61 °C | 0.34 / 2.53 / 4.20 °C |
| 1.0° | RH 2 m | 1.6 / 11.4 / 20.5 % | 4.4 / 18.9 / 29.4 % |

Bend (the 0.125° point at 44.000 N, 121.250 W, which happens to be a
0.25° node, so 0.25° reads it exactly): at 0.5° the model ground comes out
55 m low, the 02:00 PDT inversion (T80 − T2m) 5.8 °C instead of 3.6 and RH
79 % instead of 72; at 1° the ground is 268 m low and the 05:00 PDT RH 96 %
instead of 82 — fog that the model does not have.

The app's thresholds are 0.5 °C for an inversion step and 85 % RH for its
fog/cloud gate, so errors of 2–4 °C and 10–20 % on steep ground decide
whether a layer is shown at all. Each halving of the spacing cuts the
steep-ground p95 by about a quarter; 0.25° is the finest the build can
hold: 0.125° would be ~4× the bytes (~6 GB a run) and ~30 GB of memory for
153 hours on a 16 GB runner (0.25°: ~8 GB). What 0.25° costs instead of
0.5°: 1.59 GB a run on Blob instead of 0.50 GB, a median square of 669 KB
instead of 202 KB for the phone (once per run per place), 117 s of
encoding instead of 39 s, and a 94 MB history head per run in `state`
(stored as six ~16 MB files, one per hour, to stay well under GitHub's
100 MB limit on a pushed file; ten runs ≈ 0.95 GB in the state branch,
of which each build pushes only its new ~94 MB).

## Hours

The column file carries exactly the hours the cloud squares carry: the
previous runs' first six hours as history (up to 60 h, from
`state/columns/<run>.h0.npz` … `.h5.npz`, one file per hour, kept and
pruned like the clouds' history), then the
run's own steps (`dwd.STEPS`: hourly to 78 h, 3-hourly to 120 h). Hours are
in the file's header and in the manifest's `columns.stepsMs`. The app's
profile needs hourly from local midnight yesterday to the end of day 3, which
the hourly part covers (the same reasoning as the cloud squares).

The column history starts empty: the first builds after this ships carry
fewer history hours (`historySteps` says how many), filling to 60 h within
ten runs (2.5 days). A history head of another layout (stride, field count)
is skipped, not misread.

## File and manifest

`columns/<run>/<square>.icc`, one per 5° square, the same squares as the
cloud tiles (every square on the globe for a full build), **on Vercel Blob
only**. The columns are not published to GitHub Pages and do not count
against its 1 GB budget (Erik, 2026-10-02: cloud accuracy first — the
columns must never push the cloud squares to the coarser 4 % steps). Old
app builds read Pages and never read columns; the new reader reads Blob and
falls back to Open-Meteo's profile for a column it cannot get.

The build therefore writes two trees: `site/` (Pages and Blob: squares,
tail, `manifest.json` with `"columns": null`) and `blob/` (Blob only: the
columns and a `manifest.json` that is site's plus the `columns` entry).
`publish/blob.mjs ../site ../blob` uploads both; a later root's file
replaces the same path (`publish/roots.mjs`), so Blob's manifest is
`blob/`'s. Blob's manifest gains

```json
"columns": {
  "format": "ICC1", "run": "2026-10-02T00Z", "runMs": 1790985600000,
  "stepsMs": [...], "historySteps": 60,
  "spacing": 0.25, "side": 21, "tiles": "columns/2026100200"
}
```

or `"columns": null` when a build has none (DWD late or a failure — below).
Every key an older app build reads is unchanged, and Pages' manifest is
byte-for-byte what it would be without the feed except `"columns": null`;
JSON readers ignore a key they do not know, so old builds are unaffected
(tests: `test_manifest_carries_the_columns_or_null_and_old_keys_are_unchanged`,
`test_the_columns_go_to_blob_only_and_never_touch_the_pages_budget`).
`publish/prune.mjs` keeps `columns/<run>/` of the current and previous
manifest in Blob, exactly as it keeps `tail/<run>/`.

## Byte format: ICC1

All integers little-endian.

| offset | size | type | field |
|---|---|---|---|
| 0 | 4 | ASCII | magic `ICC1` |
| 4 | 2 | i16 | swLat — the square's south edge, whole degrees |
| 6 | 2 | i16 | swLon — the square's west edge, whole degrees |
| 8 | 2 | u16 | spacing between nodes, millidegrees (500 = 0.5°) |
| 10 | 2 | u16 | cols — nodes west→east (= 5° / spacing + 1) |
| 12 | 2 | u16 | rows — nodes south→north |
| 14 | 2 | u16 | S — steps |
| 16 | 2 | u16 | F — fields |
| 18 | 4·S | u32[S] | each step's time, unix HOURS (ms = h × 3 600 000), ascending |
| 18+4S | 12·F | table | per field: kind u8, levelType u8, level u16, scale f32, offset f32 |
| 18+4S+12F | rest | zlib | the body (below) |

Field table:

- kind: 1 = temperature (°C), 2 = relative humidity (%), 3 = geopotential
  height (m above sea level), 4 = model surface height HSURF (m above sea level).
- levelType: 0 = static (one value per node, no time axis), 1 = pressure
  (level in hPa), 2 = height above the model surface (level in m).
- value = offset + scale × code (scale and offset as stored, f32);
  **code 65535 = missing** (NaN in DWD's field, or a NaN after remapping).
  Codes are clamped to 0…65534.

Today's table (F = 25), in body order:

| # | kind | levelType | level | scale | offset |
|---|---|---|---|---|---|
| 0–20 | T, RH, GH for each of 1000, 950, 925, 900, 850, 800, 700 hPa (T, RH, GH per level, high pressure first) | 1 | hPa | 0.1 / 1 / 1 | −150 / 0 / −1000 |
| 21 | 1 (T) | 2 | 2 | 0.1 | −150 |
| 22 | 2 (RH) | 2 | 2 | 1 | 0 |
| 23 | 1 (T) | 2 | 80 | 0.1 | −150 |
| 24 | 4 (HSURF) | 0 | 0 | 1 | −1000 |

A decoder must read the table, not assume this order.

Body: `zlib` (the browser's `DecompressionStream('deflate')`, as for ICL1) of
`2·N` bytes, N = Σ over fields of rows·cols·(S, or 1 if static). The first N
bytes are the low bytes, the next N the high bytes, of N u16 **deltas** `d`.
The codes are their running sum: `c[i] = (c[i−1] + d[i]) mod 65536`,
`c[−1] = 0`, over the whole body as one stream. Code order: field by field in
table order; within a field, node by node with row 0 the SOUTH edge and
column 0 the WEST edge (row-major: `node = row·cols + col`); within a node,
its S steps in time order (a static field: its one value).

So a dynamic field f's code for node (r, c) at step s is at
`start(f) + (r·cols + c)·S + s`, start(f) the sum of the sizes of the
fields before it. Decoder sketch:

```js
const raw = new Uint8Array(await new Response(new Blob([body]).stream()
  .pipeThrough(new DecompressionStream('deflate'))).arrayBuffer());
const n = raw.length / 2, code = new Uint16Array(n);
let acc = 0;
for (let i = 0; i < n; i++) { acc = (acc + (raw[i] | raw[n + i] << 8)) & 0xffff; code[i] = acc; }
// value = code === 65535 ? null : offset + scale * code
```

Nodes are every `spacing` of the 0.125° world grid the cloud squares use,
the north and east edges included, so the node at (r, c) is at
(swLat + r·spacing, swLon + c·spacing) and bilinear sampling inside a square
never needs a neighbour. Values at a node are DWD's own remap (the same
`ICON_GLOBAL2WORLD_0125_EASY` weights as the clouds) at that 0.125° point.

The deltas are why the file is small: hour to hour a column moves a few
codes, so most deltas are small and the high-byte plane is nearly all 0x00 or
0xFF, which deflate removes almost entirely.

## Size

Measured on the 2026-10-02 00Z run (`columns-measure`, GitHub runner), every
square on the globe (2592), F = 25 fields. "+60 h history" is what a full
build publishes once the history has filled (153 hours: 60 history + 93
own; the measurement replays the run's own hours 0–59 as stand-in history):

| spacing | nodes per square | hours | globe per run | per square min / median / max | encode (6 threads) | state history file per run |
|---|---|---|---|---|---|---|
| 0.25° | 21 × 21 | 93 | 1396 MB | 100 / 587 / 773 KB | 94 s | 94.1 MB |
| 0.25° | 21 × 21 | 153 | **1587 MB** | 118 / **669** / 858 KB | 117 s | 94.1 MB |
| 0.5° | 11 × 11 | 93 | 439 MB | 50 / 178 / 220 KB | 30 s | 29.0 MB |
| 0.5° | 11 × 11 | 153 | **498 MB** | 57 / **202** / 244 KB | 39 s | 29.0 MB |
| 1.0° | 6 × 6 | 93 | 139 MB | 20 / 55 / 68 KB | 15 s | 8.7 MB |
| 1.0° | 6 × 6 | 153 | **158 MB** | 24 / **62** / 75 KB | 22 s | 8.7 MB |

Where the bytes go (N40W125, 153 hours, 0.25°): T on the 7 pressure levels
243 KB, RH 223 KB, GH 187 KB, T 2 m 41 KB, RH 2 m 35 KB, T 80 m 37 KB,
HSURF 0.7 KB — the pressure levels are 85 % of a file.

A size bound computed from the format (header + zlib's `compressBound` of
the 2·N-byte body, N = nodes · (24 · hours + 1)) is in
`tests/test_columns.py`; at 0.25° and 153 hours it is 3.24 MB a square,
and the measured files are 21 % (median) to 26 % (largest) of it.

Blob: prune keeps the current and the previous run, so the store holds
about two runs of columns beside the clouds. The phone fetches ONE square
per place (the square holding it; edge nodes are shared, so it never needs
a neighbour).

## Build

Columns are built after the cloud squares, the tail and the Pages budget
check, inside the same `run_build.py` run (the clouds' arrays are freed
first), into `blob/`, and published by the existing Blob step
(`node blob.mjs ../site ../blob` in `build.yml`).

1. Wait until every column file of the run is listed (DWD uploads a run over
   ~20 minutes): re-list every 60 s for up to 15 min, else publish the
   clouds without columns (`columns: null`) rather than hold the feed back.
2. HSURF and HHL 117–120 (once per run) → surface height and the T80 weights.
3. Per step, in the build's worker pool: T, RELHUM and FI each as one cdo
   call over all seven levels (the GRIB files are concatenated), T_2M,
   RELHUM_2M, and model-level T 117, 118 and 119 → T80. Each field is quantised
   at once, so memory is the lattice, not the 0.125° grid.

**Remap straight onto the nodes.** The cloud squares remap with
`cdo remap` onto all 4.1 M points of the 0.125° grid. Doing that for the
columns' 25 fields a step and then keeping every `stride`-th point took
21.7 minutes of fetch for one run (measured). So the columns use cdo only
to decode the GRIB (`cdo copy`: ICON's own cells, no remap) and apply DWD's
SAME weights file, cut to the links whose target is a node, in numpy
(`regrid.LatticeRemap`): value = Σ weight · source, exactly what cdo
computes at that point. A node with a missing source in any of its links is
missing (65535). DWD's kit turns out to be nearest-neighbour (`map_method`
"Nearest neighbor", one link per point: 4 148 639 links for 4 148 639
points), so a node's value is ICON's value in the cell nearest it.
`tests/test_regrid.py` checks the numpy remap against real cdo (bilinear
and distance-weighted weights, strides 1–3); on real DWD fields
(`columns-measure`, step 0 of the 2026-10-02 00Z run) it equals cdo's full
remap at every node:

| field | levels | cdo full remap | lattice | max abs diff | missing cdo / ours |
|---|---|---|---|---|---|
| T (pressure) | 7 | 2.1 s | 2.0 s | 0 | 0 / 0 |
| T_2M | 1 | 0.6 s | 0.6 s | 0 | 0 / 0 |
| HSURF | 1 | 0.5 s | 0.4 s | 0 | 0 / 0 |

(Per call the two are close — decoding the GRIB dominates; the saving is
in the 25 × 4.1 M-point float arrays cdo no longer writes and the build no
longer reads, which is what the fetch time below shows.)
4. History from `state/columns`, this run's hours 0–5 saved back.
5. One `.icc` per square.

A failure anywhere in the column build is a warning and `columns: null` in
Blob's manifest; it never fails the build or the cloud feed.

**Build time added** (measured, 2026-10-02 00Z, a 4-vCPU GitHub runner, 6
workers):

| part | time |
|---|---|
| wait for DWD's column files | 0 when the run is complete (as for every build so far); at most 15 min, then `columns: null` |
| HSURF + HHL (once per run) | 15 s |
| fetch: 93 steps × 26 files (download + `cdo copy` + lattice + quantise) | **12.5 min** (worker-seconds: download 3462, remap 958 — DWD's download dominates) |
| encode every square (0.25° / 0.5° / 1°, 153 hours) | 117 s / 39 s / 22 s |

So a full build grows from ~6.5 min to ~20 min at 0.25° (~19 min at 0.5°);
`build.yml`'s 150 min timeout is far off, and `tick.yml` waits 30 min after
starting a build before it asks again (a tick that asks early only queues a
build that exits early). The same fetch through `cdo remap` onto the 0.125°
grid took 21.7 min, and zlib level 9 took ~5 s per 0.25° square (≈ 3.6 h
for the globe) — both replaced (above).

**Blob upload per run**: the columns are 2592 more files of
1.59 GB at 0.25° (0.50 GB at 0.5°). The live build uploads 5184 files /
833 MB in 27 s (31 MB/s, 16 at a time), so the columns add roughly 50 s at
0.25° (16 s at 0.5°) — an estimate from that rate, since this branch must
not publish to the real store; `blob.mjs` now logs files, MB and seconds
per root on every publish, so the first live build gives the real figure.

## Pages budget

`SITE_BUDGET_BYTES` (900 MB, under Pages' hard 1 GB) counts the cloud
squares and the tail only, exactly as before the feed; the columns are not
in `site/`, so they can never trigger the 4 % rewrite (test above).

## Tests

`tests/test_columns.py` (round trip, the byte layout read by an independent
decoder written from this document, the sentinel both ways, quantisation
error, a size bound computed from the format — header + zlib's
`compressBound` of the body — and the lattice), `tests/test_run_build.py`
(columns in app units from fake DWD values, T80 interpolation and the
per-node level pair computed independently, a full build's squares, late
DWD files, a failure, history from state, Blob-only and outside the Pages
budget), `tests/test_regrid.py` (the lattice remap against real cdo),
`tests/test_site.py` (manifest shape, old keys unchanged),
`tests/test_dwd.py` (file names, listing check), `tests/test_history.py`,
`publish/prune.test.mjs` (Blob keeps `columns/<run>/`),
`publish/roots.test.mjs` (Blob gets `blob/manifest.json`).

`tools/measure_columns.py` + `.github/workflows/columns-measure.yml`
(run by hand) build the columns from a real DWD run on a GitHub runner and
report the remap check, model-level heights, sizes, time and one decoded
column, publishing nothing (read-only token, no secrets).
