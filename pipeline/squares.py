# pipeline/squares.py — the 0.125° world grid and its 5° squares.
#
# The grid is DWD's target_grid_world_0125: 2879 columns from -180 to 179.75
# and 1441 rows from -90 to 90. A square is named by its south-west corner in
# whole degrees ("N40W125"), spans 5°, and carries 41 samples a side — the
# north and east edges included — so the app can interpolate inside it
# without a neighbour. Column 2879 does not exist: 180° E is column 0 again,
# so the square at E175 borrows the dateline column for its east edge.
import re
import numpy as np

NX, NY = 2879, 1441
DEG = 0.125
SQUARE_DEG = 5
SIDE = SQUARE_DEG * 8 + 1          # 41
_NAME = re.compile(r"^([NS])(\d{2})([EW])(\d{3})$")


def square_name(sw_lat, sw_lon):
    if sw_lat % SQUARE_DEG or sw_lon % SQUARE_DEG or not (-90 <= sw_lat <= 85) or not (-180 <= sw_lon <= 175):
        raise ValueError(f"not a square corner: {sw_lat}, {sw_lon}")
    return f"{'N' if sw_lat >= 0 else 'S'}{abs(sw_lat):02d}{'E' if sw_lon >= 0 else 'W'}{abs(sw_lon):03d}"


def parse_name(name):
    m = _NAME.match(name)
    if not m:
        raise ValueError(f"bad square name {name!r}")
    lat = int(m.group(2)) * (1 if m.group(1) == "N" else -1)
    lon = int(m.group(4)) * (1 if m.group(3) == "E" else -1)
    return lat, lon


def all_squares():
    return [square_name(lat, lon) for lat in range(-90, 90, SQUARE_DEG) for lon in range(-180, 180, SQUARE_DEG)]


def row_indices(sw_lat):
    r0 = round((sw_lat + 90) / DEG)
    return list(range(r0, r0 + SIDE))


def col_indices(sw_lon):
    c0 = round((sw_lon + 180) / DEG)
    cols = []
    for c in range(c0, c0 + SIDE):
        if c >= NX:
            # Column NX itself (179.875°) does not exist — the grid's last
            # real column (NX - 1, 179.75°) sits right before the seam, and
            # the wrap lands on column 0 (180° == -180°) one step early.
            c -= 1
        cols.append(c % NX)
    return cols


def slice_square(world, sw_lat, sw_lon):
    """world: [fields][NY][NX] → [fields][41][41], south row first, west column first."""
    rows = row_indices(sw_lat)
    cols = col_indices(sw_lon)
    return np.ascontiguousarray(world[:, rows[0]:rows[-1] + 1][:, :, cols])


LAND_MIN_FRACTION = 0.05


def land_squares(land_fraction):
    """Squares with land in them or in the eight around them. land_fraction: [NY][NX] 0-1.

    The app's window reaches 375 km from the viewer, so a square with no land
    of its own still matters when a neighbour has some; open ocean two
    squares from any coast is never in anyone's window and is the bulk of
    the globe. Measured 2026-09-27: the whole world would be ~0.6-1 GB."""
    land = set()
    for name in all_squares():
        lat, lon = parse_name(name)
        rows = row_indices(lat)
        block = land_fraction[rows[0]:rows[-1] + 1][:, col_indices(lon)]
        if (block >= LAND_MIN_FRACTION).any():
            land.add((lat, lon))
    keep = set()
    for lat, lon in land:
        for dlat in (-SQUARE_DEG, 0, SQUARE_DEG):
            for dlon in (-SQUARE_DEG, 0, SQUARE_DEG):
                la = lat + dlat
                lo = ((lon + dlon + 180) % 360) - 180
                if -90 <= la <= 85:
                    keep.add(square_name(la, lo))
    return sorted(keep)
