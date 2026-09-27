# pipeline/encode.py — one 5°×5° square, every time step, in one file.
#
# ICL1: a 14-byte header, the steps' times as unix HOURS (u32 each), then a
# zlib stream of u8 [step][field][row][col]. Fields are low, mid, high, total
# cloud cover in percent; 255 marks a missing sample. Row 0 is the SOUTH edge
# and column 0 the WEST edge, the app's own grid convention. The square
# carries 41 samples a side — the cell on its north and east edges belongs to
# the neighbour too — so bilinear sampling inside a square never needs a
# neighbour it did not fetch.
#
# The browser decodes the body with DecompressionStream('deflate'), which
# reads a zlib stream, so zlib.compress (not raw deflate) is the right call.
import struct
import zlib
import numpy as np

MAGIC = b"ICL1"
SIDE = 41
FIELD_COUNT = 4
MISSING = 255
_HEADER = struct.Struct("<4shhHHH")   # magic, swLat, swLon, cols, rows, steps


def encode_square(sw_lat, sw_lon, step_hours, cube):
    """cube: uint8 [steps][4][41][41]. step_hours: ascending unix hours."""
    cube = np.asarray(cube)
    if cube.dtype != np.uint8:
        raise ValueError(f"cube must be uint8, got {cube.dtype}")
    if cube.ndim != 4 or cube.shape[1:] != (FIELD_COUNT, SIDE, SIDE):
        raise ValueError(f"cube must be [steps][{FIELD_COUNT}][{SIDE}][{SIDE}], got {cube.shape}")
    hours = [int(h) for h in step_hours]
    if len(hours) != cube.shape[0]:
        raise ValueError(f"{len(hours)} step hours for {cube.shape[0]} steps")
    if any(b <= a for a, b in zip(hours, hours[1:])):
        raise ValueError("step hours must be strictly ascending")
    head = _HEADER.pack(MAGIC, int(sw_lat), int(sw_lon), SIDE, SIDE, len(hours))
    times = struct.pack(f"<{len(hours)}I", *hours)
    return head + times + zlib.compress(np.ascontiguousarray(cube).tobytes(), 6)


def decode_square(blob):
    """The inverse of encode_square, for tests and for checking a published file."""
    magic, sw_lat, sw_lon, cols, rows, steps = _HEADER.unpack_from(blob, 0)
    if magic != MAGIC:
        raise ValueError("not an ICL1 file")
    off = _HEADER.size
    hours = list(struct.unpack_from(f"<{steps}I", blob, off))
    off += 4 * steps
    body = zlib.decompress(blob[off:])
    cube = np.frombuffer(body, dtype=np.uint8).reshape(steps, FIELD_COUNT, rows, cols)
    return {"sw_lat": sw_lat, "sw_lon": sw_lon, "step_hours": hours, "cube": cube}
