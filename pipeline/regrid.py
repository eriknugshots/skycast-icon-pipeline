# pipeline/regrid.py — ICON's icosahedral grid → the 0.125° world grid.
#
# DWD ships the interpolation weights for exactly this (the "EASY" kit), so
# the whole step is one cdo call per file. cdo names a DWD local parameter
# by its GRIB triple ("param6.6.0") unless its tables know it, so the reader
# takes whichever variable has the (lat, lon) shape rather than a name.
import bz2
import subprocess
import threading
import tarfile
import urllib.request
from pathlib import Path
import netCDF4
import numpy as np
from .squares import NX, NY
from .encode import MISSING

# netCDF-C is not thread-safe: six build workers opening Datasets at once
# died with "NetCDF: Not a valid ID" on the first real run (2026-09-27).
# The cdo subprocesses still overlap; only the ~50 ms read is serialised.
_NC_LOCK = threading.Lock()

KIT_URL = "https://opendata.dwd.de/weather/lib/cdo/ICON_GLOBAL2WORLD_0125_EASY.tar.bz2"
KIT_DIR = "ICON_GLOBAL2WORLD_0125_EASY"


def ensure_kit(work):
    """Download and unpack the kit once; return its directory."""
    kit = Path(work) / KIT_DIR
    if not (kit / "weights_icogl2world_0125.nc").exists():
        tgz = Path(work) / "kit.tar.bz2"
        urllib.request.urlretrieve(KIT_URL, tgz)
        with tarfile.open(tgz, "r:bz2") as t:
            t.extractall(work)
    return kit


def remap_command(kit, grib_in, nc_out):
    kit = str(kit)
    return ["cdo", "-s", "-O", "-f", "nc4", "-b", "F32",
            f"remap,{kit}/target_grid_world_0125.txt,{kit}/weights_icogl2world_0125.nc",
            str(grib_in), str(nc_out)]


def unpack_bz2(src, dst):
    with bz2.open(src, "rb") as f, open(dst, "wb") as g:
        while chunk := f.read(1 << 20):
            g.write(chunk)


def world_from_netcdf(path, scale=1.0):
    """The one data field of a remapped file as uint8 percent [NY][NX]; masked → MISSING.
    `scale` multiplies before rounding: FR_LAND is a 0-1 fraction, so 100."""
    with _NC_LOCK:
        ds = netCDF4.Dataset(path)
        try:
            var = next((v for v in ds.variables.values()
                        if v.ndim >= 2 and v.shape[-2:] == (NY, NX)), None)
            if var is None:
                raise ValueError(f"no [{NY}][{NX}] field in {path}")
            arr = var[...]
            while arr.ndim > 2:
                arr = arr[0]
            data = np.ma.filled(np.ma.masked_invalid(arr), -1.0).astype(np.float32)
        finally:
            ds.close()
    out = np.rint(np.clip(data * scale, 0, 100)).astype(np.uint8)
    out[data < 0] = MISSING
    return out


def regrid(grib_bz2, kit, work, run=subprocess.run, scale=1.0):
    """One DWD .grib2.bz2 → uint8 world array. `run` is injected for tests."""
    work = Path(work)
    grib = work / Path(grib_bz2).name.replace(".bz2", "")
    nc = grib.with_suffix(".nc")
    unpack_bz2(grib_bz2, grib)
    try:
        run(remap_command(kit, grib, nc), check=True)
        return world_from_netcdf(nc, scale)
    finally:
        grib.unlink(missing_ok=True)
        nc.unlink(missing_ok=True)
