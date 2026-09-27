import numpy as np
import netCDF4
import pytest
from pipeline.regrid import remap_command, world_from_netcdf, KIT_URL, KIT_DIR


def test_remap_command_uses_the_dwd_kit():
    cmd = remap_command("/kit", "/w/in.grib2", "/w/out.nc")
    assert cmd == ["cdo", "-s", "-O", "-f", "nc4", "-b", "F32",
                   "remap,/kit/target_grid_world_0125.txt,/kit/weights_icogl2world_0125.nc",
                   "/w/in.grib2", "/w/out.nc"]


def test_kit_location():
    assert KIT_URL.endswith("ICON_GLOBAL2WORLD_0125_EASY.tar.bz2")
    assert KIT_DIR == "ICON_GLOBAL2WORLD_0125_EASY"


def _write_nc(path, values, name="CLCL"):
    ds = netCDF4.Dataset(path, "w")
    ds.createDimension("time", 1)
    ds.createDimension("lat", values.shape[0])
    ds.createDimension("lon", values.shape[1])
    ds.createVariable("time", "f8", ("time",))[:] = [0]
    ds.createVariable("lat", "f8", ("lat",))[:] = np.linspace(-90, 90, values.shape[0])
    ds.createVariable("lon", "f8", ("lon",))[:] = np.linspace(-180, 179.75, values.shape[1])
    v = ds.createVariable(name, "f4", ("time", "lat", "lon"), fill_value=-1.0)
    v[0] = values
    ds.close()


def test_world_from_netcdf_rounds_to_uint8_percent_and_marks_missing(tmp_path):
    vals = np.full((1441, 2879), 33.4, dtype=np.float32)
    vals[0, 0] = 99.6
    vals[5, 5] = -1.0            # the fill value → masked → MISSING
    p = tmp_path / "x.nc"
    _write_nc(p, vals)
    world = world_from_netcdf(p)
    assert world.shape == (1441, 2879) and world.dtype == np.uint8
    assert world[100, 100] == 33
    assert world[0, 0] == 100
    assert world[5, 5] == 255


def test_world_from_netcdf_finds_the_field_whatever_it_is_called(tmp_path):
    vals = np.zeros((1441, 2879), dtype=np.float32)
    p = tmp_path / "y.nc"
    _write_nc(p, vals, name="param6.6.0")     # what cdo names a DWD local parameter
    assert world_from_netcdf(p).shape == (1441, 2879)


def test_world_from_netcdf_refuses_the_wrong_grid(tmp_path):
    p = tmp_path / "z.nc"
    _write_nc(p, np.zeros((10, 10), dtype=np.float32))
    with pytest.raises(ValueError):
        world_from_netcdf(p)
