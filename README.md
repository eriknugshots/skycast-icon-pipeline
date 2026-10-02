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

Build: `.github/workflows/build.yml` every 10 minutes; `run_build.py` exits
early unless DWD has a newer complete run than the live manifest.
