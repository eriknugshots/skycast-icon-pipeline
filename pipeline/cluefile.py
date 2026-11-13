# pipeline/cluefile.py — ICX2: the per-node texture clues (pipeline/clues.py,
# 30 bytes each) of one 5° square, every time step, in one file.
#
# ICX2, little-endian:
#   18-byte header  magic "ICX2", swLat i16, swLon i16, spacing u16 (milli-
#                   degrees), cols u16, rows u16, steps u16, clueBytes u16 (=30)
#   steps × u32     the steps' times as unix HOURS, ascending
#   zlib stream     of rows·cols·steps·30 u8: node by node (row 0 the SOUTH
#                   edge, column 0 the WEST edge), each node's steps in time
#                   order, each step's 30 clue bytes.
# ICX1 was the same file with 27 bytes a node (no windDir tail, clues.py). The
# builder writes ICX2 only; decode_clue_square reads both.
import struct
import zlib
import numpy as np

from .clues import N_BYTES, N_BYTES_V1

MAGIC = b"ICX2"
FORMAT = MAGIC.decode()                       # the manifest entry's "format"
MAGIC_BYTES = {b"ICX1": N_BYTES_V1, MAGIC: N_BYTES}   # every magic read -> bytes per node per step
ZLIB_LEVEL = 6
_HEADER = struct.Struct("<4shhHHHHH")   # magic, swLat, swLon, spacing mdeg, cols, rows, steps, clueBytes


def encode_clue_square(sw_lat, sw_lon, spacing_mdeg, step_hours, clues):
    """clues: u8 [steps][rows][cols][30]. step_hours: ascending unix hours."""
    clues = np.asarray(clues)
    if clues.dtype != np.uint8:
        raise ValueError(f"clues must be uint8, got {clues.dtype}")
    if clues.ndim != 4 or clues.shape[0] != len(step_hours) or clues.shape[3] != N_BYTES:
        raise ValueError(f"clues must be [{len(step_hours)}][rows][cols][{N_BYTES}], got {clues.shape}")
    steps, rows, cols, _ = clues.shape
    hours = [int(h) for h in step_hours]
    if any(b <= a for a, b in zip(hours, hours[1:])):
        raise ValueError("step hours must be strictly ascending")
    body = np.ascontiguousarray(np.moveaxis(clues, 0, 2)).tobytes()   # [rows][cols][steps][27]
    head = _HEADER.pack(MAGIC, int(sw_lat), int(sw_lon), int(spacing_mdeg), cols, rows, steps, N_BYTES)
    return head + struct.pack(f"<{steps}I", *hours) + zlib.compress(body, ZLIB_LEVEL)


def decode_clue_square(blob):
    """The inverse, for tests and for checking a published file (ICX2, or an
    older ICX1): {format, sw_lat, sw_lon, spacing_mdeg, cols, rows, step_hours,
    clue_bytes, clues} with clues u8 [steps][rows][cols][clue_bytes]."""
    magic, sw_lat, sw_lon, sp, cols, rows, steps, nb = _HEADER.unpack_from(blob, 0)
    if magic not in MAGIC_BYTES:
        raise ValueError("not an ICX1 or ICX2 file")
    if nb != MAGIC_BYTES[magic]:
        raise ValueError(f"{magic.decode()} with {nb} clue bytes, expected {MAGIC_BYTES[magic]}")
    off = _HEADER.size
    hours = list(struct.unpack_from(f"<{steps}I", blob, off)); off += 4 * steps
    raw = zlib.decompress(blob[off:])
    if len(raw) != rows * cols * steps * nb:
        raise ValueError(f"clue body is {len(raw)} bytes, expected {rows * cols * steps * nb}")
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(rows, cols, steps, nb)
    return {"format": magic.decode(), "sw_lat": sw_lat, "sw_lon": sw_lon, "spacing_mdeg": sp, "cols": cols, "rows": rows,
            "step_hours": hours, "clue_bytes": nb, "clues": np.moveaxis(arr, 2, 0)}
