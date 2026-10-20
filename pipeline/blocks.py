# pipeline/blocks.py — a square's steps cut into independently compressed blocks.
#
# ICL2 (cloud cover) and ICT1 (cloud thickness) share one layout so one reader
# serves both: a 14-byte header (the same struct as ICL1), the steps' times as
# unix HOURS (u32 each), blockSteps (u16), blockCount (u16), then blockCount
# (offset u32, length u32) pairs, then the block area. Offsets count from the
# start of the block area. Each block is its own zlib stream of u8
# [step][field][row][col] for BLOCK_STEPS consecutive steps (the last block may
# hold fewer), so the app can fetch one day with an HTTP range request and
# inflate it without touching the rest. All integers are little-endian.
#
# Row 0 is the SOUTH edge and column 0 the WEST edge, as in ICL1.
import struct
import zlib
import numpy as np

BLOCK_STEPS = 6
SIDE = 41
MAGICS = (b"ICL2", b"ICT1")
_HEADER = struct.Struct("<4shhHHH")   # magic, swLat, swLon, cols, rows, steps
_TABLE_HEAD = struct.Struct("<HH")    # blockSteps, blockCount
_ENTRY = struct.Struct("<II")         # offset, length


def encode_blocks(magic, sw_lat, sw_lon, step_hours, cube):
    """cube: uint8 [steps][fields][41][41]. step_hours: ascending unix hours."""
    cube = np.asarray(cube)
    if magic not in MAGICS:
        raise ValueError(f"magic must be one of {MAGICS}, got {magic!r}")
    if cube.dtype != np.uint8:
        raise ValueError(f"cube must be uint8, got {cube.dtype}")
    if cube.ndim != 4 or cube.shape[2:] != (SIDE, SIDE):
        raise ValueError(f"cube must be [steps][fields][{SIDE}][{SIDE}], got {cube.shape}")
    hours = [int(h) for h in step_hours]
    steps = cube.shape[0]
    if len(hours) != steps:
        raise ValueError(f"{len(hours)} step hours for {steps} steps")
    if any(b <= a for a, b in zip(hours, hours[1:])):
        raise ValueError("step hours must be strictly ascending")
    cube = np.ascontiguousarray(cube)
    streams = [zlib.compress(cube[i:i + BLOCK_STEPS].tobytes(), 6)
               for i in range(0, steps, BLOCK_STEPS)]
    table, offset = b"", 0
    for s in streams:
        table += _ENTRY.pack(offset, len(s))
        offset += len(s)
    head = _HEADER.pack(magic, int(sw_lat), int(sw_lon), SIDE, SIDE, steps)
    times = struct.pack(f"<{steps}I", *hours)
    return (head + times + _TABLE_HEAD.pack(BLOCK_STEPS, len(streams)) + table
            + b"".join(streams))


def read_header(buf):
    """Everything before the block area. `blocks` is [(offset, length)] counted
    from `data_start`, the byte where the block area begins."""
    if len(buf) < _HEADER.size:
        raise ValueError("buffer shorter than the header")
    magic, sw_lat, sw_lon, cols, rows, steps = _HEADER.unpack_from(buf, 0)
    if magic not in MAGICS:
        raise ValueError(f"not an ICL2/ICT1 file: {magic!r}")
    off = _HEADER.size
    if len(buf) < off + 4 * steps + _TABLE_HEAD.size:
        raise ValueError("buffer ends inside the header")
    hours = list(struct.unpack_from(f"<{steps}I", buf, off))
    off += 4 * steps
    block_steps, count = _TABLE_HEAD.unpack_from(buf, off)
    off += _TABLE_HEAD.size
    if block_steps == 0 or count != -(-steps // block_steps):
        raise ValueError(f"{count} blocks of {block_steps} steps cannot cover {steps} steps")
    if len(buf) < off + _ENTRY.size * count:
        raise ValueError("buffer ends inside the block table")
    blocks = [_ENTRY.unpack_from(buf, off + _ENTRY.size * i) for i in range(count)]
    data_start = off + _ENTRY.size * count
    return {"magic": magic, "sw_lat": sw_lat, "sw_lon": sw_lon, "cols": cols,
            "rows": rows, "steps": steps, "step_hours": hours,
            "block_steps": block_steps, "blocks": blocks, "data_start": data_start}


def decode_blocks(buf, fields):
    """The inverse of encode_blocks, for tests and for checking a published file."""
    head = read_header(buf)
    steps, per = head["steps"], head["block_steps"]
    parts = []
    for i, (offset, length) in enumerate(head["blocks"]):
        start = head["data_start"] + offset
        if start + length > len(buf):
            raise ValueError(f"block {i} runs past the end of the file")
        body = zlib.decompress(buf[start:start + length])
        n = min(per, steps - i * per)
        if len(body) != n * fields * head["rows"] * head["cols"]:
            raise ValueError(f"block {i} is {len(body)} bytes, not {n} steps of {fields} fields")
        parts.append(np.frombuffer(body, dtype=np.uint8)
                     .reshape(n, fields, head["rows"], head["cols"]))
    if parts:
        cube = np.concatenate(parts)
    else:
        cube = np.zeros((0, fields, head["rows"], head["cols"]), dtype=np.uint8)
    return {**head, "cube": cube}
