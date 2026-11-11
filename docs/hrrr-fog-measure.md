# HRRR fog files: measured size and block length

Tool: `tools/hrrr_fog_measure.py` (run with `python -I tools/hrrr_fog_measure.py 4`).

**Run:** HRRR 2026-11-11 18Z (newest 00/06/12/18Z run whose f48 `.idx` existed). Read **f00-f03 only** (4 steps, 82.1 MB downloaded)
and scaled bytes per hour linearly to 6/12/24 h blocks (ruling: slow link). Linear scaling is an approximation: the static HSURF
plane and the ~0.5 kB header are spread over 4 steps here, and deltas between neighbouring hours may compress a little better
over longer blocks, so the longer-block figures are, if anything, slightly high.

Fields per step: TMP, RH, HGT at 1000/975/950/925/900/850/800/700 mb, TMP and RH at 2 m (26 dynamic) + static HGT surface = 27 fields.
(The real ICC1 spec also has T at 80 m and WW; they are not in wrfprsf, so they are not counted here.)
Lattice: 25 mdeg spacing, 1 degree tiles, 41 x 41 nodes, nearest HRRR cell per node (sizing only), ICC1 byte layout
(u16 deltas, two byte planes, zlib 6).

## Bytes per 1-degree tile at 4 steps

| tile | bytes |
|---|---|
| N43W122 | 58,711 |
| N44W122 | 58,284 |
| N43W121 | 18,369 |
| N44W121 | 17,086 |
| mean | 38,112 (9,528 B per hour) |

The east tiles are much smaller (high terrain: the low pressure levels are below ground, so those fields are one constant
and compress to nothing). The mean over the four is what the table below uses; a lowland place would cost more than this.

## One-day place cost

Day cost = 9 tiles x bytes(tile, block) x (ceil(24/B) + 1) blocks.

| FOG_BLOCK_HOURS | kB per tile | blocks | one-day place |
|---|---|---|---|
| 6 | 57.2 | 5 | **2.57 MB** |
| 12 | 114.3 | 3 | 3.09 MB |
| 24 | 228.7 | 2 | 4.12 MB |

## Choice (history: the original stop note, superseded by the Decision below)

Rule: the largest B in {24, 12, 6} with a one-day place cost <= 2.5 MB. **None qualifies:** even B = 6 is 2.57 MB at 25 mdeg
(3 % over). Per the brief this is a STOP: resolution (or the budget) is Erik's call. (No constants were chosen at measurement time.)
Candidates: accept B = 6 at 2.57 MB; coarsen the spacing (e.g. 30 mdeg would be about 1.8 MB at B = 6); or note the Bend
sample is mountain-biased low on the eastern tiles, so a lowland place would be above 2.57 MB, not below.

## Decision (final, 2026-11-11)

- `FOG_SPACING_MDEG = 25` and `FOG_TILE_DEG = 1`: Erik, 2026-11-11. He kept the full 3 km detail after seeing 2.57 MB/day at Bend (a lowland place is estimated nearer 4 MB, extrapolated from the two west Bend tiles at about 58 kB per 4 steps against the 38 kB four-tile mean; not measured).
- `FOG_BLOCK_HOURS = 6`: controller ruling, 2026-11-11 (the smallest per-day download of the measured options).

## Grid keys for Task 2

First wrfprsf message of f00 (VVEL 50 mb, 2026-11-11 18Z; the grid is identical for every message):

```
Latin1InDegrees = 38.5
Latin2InDegrees = 38.5
LoVInDegrees = 262.5
latitudeOfFirstGridPointInDegrees = 21.138123
longitudeOfFirstGridPointInDegrees = 237.280472
DxInMetres = 3000.0
DyInMetres = 3000.0
Nx = 1799
Ny = 1059
```

`codes_get_array(g, "latitudes")` / `"longitudes"` ordering: flat index n = j * Nx + i, i (along x) fastest-varying, j = row
from the south-west first point. Longitudes come back in 0..360 (not wrapped to -180..180):

| i, j | flat n | latitude | longitude (0..360) |
|---|---|---|---|
| 0, 0 | 0 | 21.138123 | 237.280472 |
| 900, 530 | 954370 | 38.524222 | 262.528505 |
| 1798, 1058 | 1905140 | 47.842195 | 299.082807 |
