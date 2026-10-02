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


# ---- The column feed: physical values, several levels per file -------------

def levels_from_netcdf(path):
    """The one data field of a remapped file as float32, NaN where missing:
    {level: [NY][NX]}, keyed by the file's vertical coordinate (pressure in
    hPa — cdo writes Pa — else the coordinate's value as cdo gives it), or
    {None: ...} for a field with no vertical axis. Several GRIB files of ONE
    parameter on one level type are remapped in one cdo call (cat'ed), so
    this reads every level, not just the first. A time axis is read at its
    first (only) step."""
    with _NC_LOCK:
        ds = netCDF4.Dataset(path)
        try:
            var = next((v for v in ds.variables.values()
                        if v.ndim >= 2 and v.shape[-2:] == (NY, NX)), None)
            if var is None:
                raise ValueError(f"no [{NY}][{NX}] field in {path}")
            arr = np.ma.filled(np.ma.masked_invalid(np.ma.asarray(var[...], dtype=np.float32)), np.nan)
            zdim = None
            for d in reversed(var.dimensions[:-2]):         # index from the back: axes shift as they go
                axis = var.dimensions.index(d)
                if d.lower().startswith("time"):
                    arr = np.take(arr, 0, axis=axis)
                elif zdim is None:
                    zdim = d
                else:
                    raise ValueError(f"{path}: two vertical axes {zdim}, {d}")
            if zdim is None:
                levels, arr = [None], arr.reshape((1,) + arr.shape[-2:])
            else:
                if zdim not in ds.variables:
                    raise ValueError(f"{path}: no coordinate for {zdim}")
                z = ds.variables[zdim]
                vals = np.asarray(z[:], dtype=np.float64)
                if getattr(z, "units", "") == "Pa":
                    vals = vals / 100.0                     # Pa → hPa
                levels = [int(round(v)) for v in vals]
                arr = arr.reshape((len(levels),) + arr.shape[-2:])
        finally:
            ds.close()
    if len(set(levels)) != len(levels):
        raise ValueError(f"{path}: repeated levels {levels}")
    return {lv: np.ascontiguousarray(arr[i]) for i, lv in enumerate(levels)}


def regrid_levels(gribs_bz2, kit, work, run=subprocess.run):
    """Several DWD .grib2.bz2 of ONE parameter (e.g. T on seven pressure
    levels) → {level: float32 [NY][NX]}, remapped in ONE cdo call: GRIB
    messages concatenate, and the weights are read once instead of seven
    times. `run` is injected for tests."""
    work = Path(work)
    gribs_bz2 = [Path(g) for g in gribs_bz2]
    grib = work / (gribs_bz2[0].name.replace(".grib2.bz2", "") + f"_x{len(gribs_bz2)}.grib2")
    nc = grib.with_suffix(".nc")
    try:
        with open(grib, "wb") as g:
            for src in gribs_bz2:
                with bz2.open(src, "rb") as f:
                    while chunk := f.read(1 << 20):
                        g.write(chunk)
        run(remap_command(kit, grib, nc), check=True)
        return levels_from_netcdf(nc)
    finally:
        grib.unlink(missing_ok=True)
        nc.unlink(missing_ok=True)
