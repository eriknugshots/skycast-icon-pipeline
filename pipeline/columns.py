# pipeline/columns.py — the column feed: one 5°×5° square of vertical
# profiles (fog and inversion layer), every time step, in one file.
#
# ICC1, little-endian (byte layout: docs/column-feed-design.md):
#   18-byte header  magic "ICC1", swLat i16, swLon i16, spacing u16 (milli-
#                   degrees), cols u16, rows u16, steps u16, fields u16
#   steps × u32     the steps' times as unix HOURS, ascending
#   fields × 12 B   the field table: kind u8, levelType u8, level u16,
#                   scale f32, offset f32
#   zlib stream     of the u16 body, stored as DELTAS in two byte planes
#                   (all low bytes, then all high bytes)
# Body order: field by field (table order); within a field node by node, row 0
# the SOUTH edge and column 0 the WEST edge (the cloud squares' convention);
# within a node its steps in time order — or ONE value for a static field
# (levelType 0). A code c means offset + scale × c; 65535 means missing.
# Deltas run over the whole body as one stream: d[i] = c[i] − c[i−1] mod
# 65536, c[−1] = 0, so a decoder is one running sum.
#
# Nodes are every `stride`-th sample of the 0.125° world grid, the square's
# north and east edges included (like the cloud squares), so a square has
# 40 / stride + 1 nodes a side and bilinear sampling never needs a neighbour.
import struct
import zlib
import numpy as np

from .squares import DEG, SIDE, row_indices, col_indices

MAGIC = b"ICC1"
MISSING = 0xFFFF
MAX_CODE = MISSING - 1
# zlib level: 9 took ~5 s per 0.25° square (153 steps) for 2 % fewer bytes
# than 6, which takes ~0.3 s; the cloud squares use 6 too.
ZLIB_LEVEL = 6
_HEADER = struct.Struct("<4shhHHHHH")   # magic, swLat, swLon, spacing mdeg, cols, rows, steps, fields
_FIELD = struct.Struct("<BBHff")        # kind, levelType, level, scale, offset

# Kinds. Units are the app's (Open-Meteo's): °C, %, metres above sea level.
T, RH, GH, HSURF = 1, 2, 3, 4
KIND_NAMES = {T: "T", RH: "RH", GH: "GH", HSURF: "HSURF"}
# Level types. STATIC fields hold one value per node, not one per step.
STATIC, PRESSURE_HPA, HEIGHT_AGL_M = 0, 1, 2

# Resolution per kind: 0.1 °C (the app's inversion test needs a 0.5 °C rise
# between levels; Open-Meteo also answers in 0.1 °C), 1 % RH, 1 m height.
_QUANT = {T: (0.1, -150.0), RH: (1.0, 0.0), GH: (1.0, -1000.0), HSURF: (1.0, -1000.0)}


def field_spec(plevels):
    """The field table, in body order: per pressure level (high pressure
    first) T, RH, GH; then T at 2 m, RH at 2 m, T at 80 m; then the static
    model surface height. [(kind, levelType, level, scale, offset)]"""
    out = []
    for p in plevels:
        for k in (T, RH, GH):
            out.append((k, PRESSURE_HPA, int(p)) + _QUANT[k])
    out.append((T, HEIGHT_AGL_M, 2) + _QUANT[T])
    out.append((RH, HEIGHT_AGL_M, 2) + _QUANT[RH])
    out.append((T, HEIGHT_AGL_M, 80) + _QUANT[T])
    out.append((HSURF, STATIC, 0) + _QUANT[HSURF])
    return out


def quantize(values, scale, offset):
    """Physical values (NaN = missing) → u16 codes. The scale and offset are
    stored as f32, so the codes are computed with the f32 values a decoder
    reads back."""
    scale, offset = np.float32(scale), np.float32(offset)
    v = np.asarray(values, dtype=np.float64)
    c = np.rint((v - np.float64(offset)) / np.float64(scale))
    out = np.clip(np.nan_to_num(c, nan=0), 0, MAX_CODE).astype(np.uint16)
    out[~np.isfinite(v)] = MISSING
    return out


def dequantize(codes, scale, offset):
    """u16 codes → float64 physical values, NaN where missing."""
    c = np.asarray(codes)
    out = np.float64(np.float32(offset)) + np.float64(np.float32(scale)) * c.astype(np.float64)
    return np.where(c == MISSING, np.nan, out)


def side(stride):
    if stride < 1 or (SIDE - 1) % stride:
        raise ValueError(f"stride {stride} does not divide a square's {SIDE - 1} cells")
    return (SIDE - 1) // stride + 1


def spacing_deg(stride):
    return stride * DEG


def lattice(world, stride):
    """[..][NY][NX] world → [..][LY][LX], every stride-th row and column.
    Every node a square needs is on it (square_nodes)."""
    return np.ascontiguousarray(world[..., ::stride, ::stride])


def square_nodes(sw_lat, sw_lon, stride):
    """The square's node rows and columns as LATTICE indices, south and west
    first. Its world rows/columns are the cloud square's (squares.py), every
    stride-th; the east edge at E175 wraps to column 0, a multiple of any
    stride."""
    rows = row_indices(sw_lat)[::stride]
    cols = col_indices(sw_lon)[::stride]
    if any(r % stride for r in rows) or any(c % stride for c in cols):
        raise ValueError(f"square {sw_lat},{sw_lon} is off the stride-{stride} lattice")
    return [r // stride for r in rows], [c // stride for c in cols]


def slice_nodes(lat_arr, sw_lat, sw_lon, stride):
    """[..][LY][LX] lattice → [..][side][side] for one square."""
    rows, cols = square_nodes(sw_lat, sw_lon, stride)
    return np.ascontiguousarray(lat_arr[..., rows[0]:rows[-1] + 1, :][..., cols])


def _planes(stream):
    """u16 codes → delta stream → low-byte plane ‖ high-byte plane."""
    c = stream.astype(np.uint16)
    d = np.diff(c, prepend=np.uint16(0)).astype(np.uint16)   # wraps mod 65536
    return (d & 0xFF).astype(np.uint8).tobytes() + (d >> 8).astype(np.uint8).tobytes()


def _unplanes(raw, n):
    b = np.frombuffer(raw, dtype=np.uint8)
    if b.size != 2 * n:
        raise ValueError(f"column body is {b.size} bytes, expected {2 * n}")
    d = b[:n].astype(np.uint16) | (b[n:].astype(np.uint16) << 8)
    return np.cumsum(d, dtype=np.uint64).astype(np.uint64) % 65536


def encode_columns(sw_lat, sw_lon, stride, step_hours, spec, steps_codes, static_codes):
    """steps_codes: u16 [steps][dynamic fields][side][side] — every field of
    `spec` but the static ones, in spec order. static_codes: u16 [static
    fields][side][side]. step_hours: ascending unix hours."""
    n = side(stride)
    steps_codes = np.asarray(steps_codes)
    static_codes = np.asarray(static_codes)
    dyn = [f for f in spec if f[1] != STATIC]
    sta = [f for f in spec if f[1] == STATIC]
    for name, arr, want in (("steps_codes", steps_codes, (len(step_hours), len(dyn), n, n)),
                            ("static_codes", static_codes, (len(sta), n, n))):
        if arr.dtype != np.uint16:
            raise ValueError(f"{name} must be uint16, got {arr.dtype}")
        if arr.shape != want:
            raise ValueError(f"{name} must be {want}, got {arr.shape}")
    hours = [int(h) for h in step_hours]
    if any(b <= a for a, b in zip(hours, hours[1:])):
        raise ValueError("step hours must be strictly ascending")
    parts, di, si = [], 0, 0
    for f in spec:
        if f[1] == STATIC:
            parts.append(static_codes[si].reshape(-1)); si += 1
        else:
            # [steps][rows][cols] → [rows][cols][steps]: a node's hours together.
            parts.append(np.moveaxis(steps_codes[:, di], 0, -1).reshape(-1)); di += 1
    body = np.concatenate(parts) if parts else np.zeros(0, np.uint16)
    head = _HEADER.pack(MAGIC, int(sw_lat), int(sw_lon), int(round(spacing_deg(stride) * 1000)),
                        n, n, len(hours), len(spec))
    times = struct.pack(f"<{len(hours)}I", *hours)
    table = b"".join(_FIELD.pack(k, lt, lv, sc, off) for k, lt, lv, sc, off in spec)
    return head + times + table + zlib.compress(_planes(body), ZLIB_LEVEL)


def header_bytes(steps, fields):
    """Bytes before the zlib stream."""
    return _HEADER.size + 4 * steps + _FIELD.size * fields


def decode_columns(blob):
    """The inverse of encode_columns, for tests and for checking a published
    file: {sw_lat, sw_lon, spacing_mdeg, cols, rows, step_hours, spec,
    fields: [codes]} — a dynamic field's codes are [rows][cols][steps], a
    static field's [rows][cols]."""
    magic, sw_lat, sw_lon, sp, cols, rows, steps, nf = _HEADER.unpack_from(blob, 0)
    if magic != MAGIC:
        raise ValueError("not an ICC1 file")
    off = _HEADER.size
    hours = list(struct.unpack_from(f"<{steps}I", blob, off)); off += 4 * steps
    spec = []
    for _ in range(nf):
        k, lt, lv, sc, of = _FIELD.unpack_from(blob, off); off += _FIELD.size
        spec.append((k, lt, lv, sc, of))
    sizes = [rows * cols * (1 if f[1] == STATIC else steps) for f in spec]
    codes = _unplanes(zlib.decompress(blob[off:]), sum(sizes)).astype(np.uint16)
    fields, at = [], 0
    for f, n in zip(spec, sizes):
        shape = (rows, cols) if f[1] == STATIC else (rows, cols, steps)
        fields.append(codes[at:at + n].reshape(shape)); at += n
    return {"sw_lat": sw_lat, "sw_lon": sw_lon, "spacing_mdeg": sp, "cols": cols, "rows": rows,
            "step_hours": hours, "spec": spec, "fields": fields}
