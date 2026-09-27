# skycast-icon-pipeline

Turns every DWD ICON global run into 5°×5° cloud-cover squares on GitHub Pages,
for SkyCast's "DWD ICON · Direct" forecast source.

Data: Deutscher Wetterdienst (DWD), https://opendata.dwd.de, CC BY 4.0.

Site: https://eriknugshots.github.io/skycast-icon-pipeline/ — `manifest.json`
plus `tiles/<run>/<square>.icl` (format: `pipeline/encode.py`).

Build: `.github/workflows/build.yml` every 10 minutes; `run_build.py` exits
early unless DWD has a newer complete run than the live manifest.
