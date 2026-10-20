# skycast-icon-pipeline

Turns every DWD ICON global run into 5°×5° cloud-cover squares on GitHub Pages,
for SkyCast's "DWD ICON · Direct" forecast source.

Data: Deutscher Wetterdienst (DWD), https://opendata.dwd.de, CC BY 4.0.

Site: https://eriknugshots.github.io/skycast-icon-pipeline/ — `manifest.json`
plus `tiles/<run>/<square>.icl` (format: `pipeline/encode.py`).

`tail/<run>/<square>.icl` holds hours 123–144 of the newest 00Z/12Z run, for SkyCast's days 4–5. Manifest key `tail`.

`columns/<run>/<square>.icc` holds the fog and inversion profile (T, RH and
geopotential height on pressure levels, T and RH at 2 m, T at 80 m, model
surface height) on a node lattice, same hours as the squares — on Vercel Blob
only (the build writes it to `blob/`, never to the Pages site or its budget).
Manifest key `columns` (null on Pages); format and sizes:
`docs/column-feed-design.md`.

`blocks/<run>/<square>.icb` is the same cloud-cover squares in a second layout,
ICL2: ICL1's header with the magic ICL2, then the steps' unix hours, a block
index (blockSteps u16 = 6, blockCount u16, then per block an offset u32 and a
length u32 from the start of the block area) and the data in 6-step zlib blocks,
so an app can fetch one day with one HTTP range request. Layout in
`pipeline/blocks.py`. It sits beside the ICL1 squares on Vercel Blob only
(Pages carries no blocks) because installed app builds read `manifest.tiles` and
decode ICL1. Manifest key `blocks` (a folder, null on Pages).

`thick/<run>/<square>.ict` holds how thick the clouds are: ICT1, the same layout
as the blocks with two fields, DWD's TQI_DIA (column cloud ice) and TQC_DIA
(column cloud water). They were chosen on a measured run on 2026-10-20: the plain
TQC is grid-scale only and is empty in 71% of liquid-cloudy cells. One log byte
per cell, in `pipeline/thick.py`: 0 under 1 g/m², 255 missing, else
1 + round(253·ln(W/1)/ln(5000)) with W in g/m². On Vercel Blob only, and optional
like the column feed: the build waits 15 minutes for its files, and a failure
publishes the cover feed without it. Manifest key `thick` (`{format: "ICT1", run,
runMs, tiles}`, null when absent or on Pages); its history heads live in
`state/thick/`.

Build: `.github/workflows/build.yml` every 10 minutes; `run_build.py` exits
early unless DWD has a newer complete run than the live manifest.

Alerts watchdog: every wake of `.github/workflows/tick.yml` also GETs the
SkyCast alerts server's health route and emails (Resend) when it goes down or
recovers — `alerts_watchdog.py`, in the background and time-boxed so it can
never fail or delay the tick. Its key, the repo secret `RESEND_ALERTS_KEY`, is
a send-only key made by `tools/watchdog-key-setup.sh`.
