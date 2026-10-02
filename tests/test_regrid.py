import bz2
import shutil
import subprocess
from pathlib import Path
import numpy as np
import netCDF4
import pytest
from pipeline.regrid import remap_command, world_from_netcdf, KIT_URL, KIT_DIR
from pipeline.regrid import LatticeRemap, copy_command, lattice_levels, levels_from_netcdf


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


def test_world_from_netcdf_scale_turns_a_fraction_into_percent(tmp_path):
    vals = np.full((1441, 2879), 0.5, dtype=np.float32)
    p = tmp_path / "f.nc"
    _write_nc(p, vals, name="FR_LAND")
    assert world_from_netcdf(p, scale=100)[3, 3] == 50
    assert world_from_netcdf(p)[3, 3] == 0            # unscaled, 0.5 rounds to 0


# ---- The column lattice: DWD's weights applied at every stride-th point ----


def _write_weights(path, nx, ny, links, src_size, num_wgts=1):
    """A SCRIP weights file as cdo writes one: 1-based addresses, the weight
    in remap_matrix's first column. links: [(dst row, dst col, src, w)]."""
    ds = netCDF4.Dataset(path, "w")
    ds.createDimension("src_grid_size", src_size)
    ds.createDimension("dst_grid_size", nx * ny)
    ds.createDimension("num_links", len(links))
    ds.createDimension("num_wgts", num_wgts)
    ds.createVariable("src_address", "i4", ("num_links",))[:] = [s + 1 for _, _, s, _ in links]
    ds.createVariable("dst_address", "i4", ("num_links",))[:] = [r * nx + c + 1 for r, c, _, _ in links]
    m = np.zeros((len(links), num_wgts))
    m[:, 0] = [w for *_, w in links]
    if num_wgts > 1:
        m[:, 1:] = 99.0                               # gradient terms: never a weight
    ds.createVariable("remap_matrix", "f8", ("num_links", "num_wgts"))[:] = m
    ds.close()


def test_lattice_remap_sums_the_weights_at_the_lattice_points_only(tmp_path):
    nx, ny, stride = 5, 3, 2                          # lattice: rows 0, 2; cols 0, 2, 4
    links = [(0, 0, 0, 0.25), (0, 0, 1, 0.75),        # lattice point (0, 0)
             (0, 1, 2, 1.0),                          # off the lattice: never read
             (2, 4, 3, 0.5), (2, 4, 4, 0.5),          # lattice point (1, 2)
             (2, 2, 2, 1.0)]                          # lattice point (1, 1); (0, 1), (0, 2), (1, 0) have no link
    p = tmp_path / "w.nc"
    _write_weights(p, nx, ny, links, src_size=5, num_wgts=3)
    r = LatticeRemap(p, stride, nx=nx, ny=ny)
    assert r.shape == (2, 3) and r.src_size == 5
    cells = np.array([10.0, 20.0, np.nan, 4.0, 8.0], dtype=np.float32)
    out = r.apply(cells)
    assert out.dtype == np.float32
    assert out[0, 0] == 0.25 * 10 + 0.75 * 20
    assert out[1, 2] == 0.5 * 4 + 0.5 * 8
    assert np.isnan(out[1, 1])                        # its source is missing
    assert np.isnan(out[0, 1]) and np.isnan(out[0, 2]) and np.isnan(out[1, 0])   # no link
    with pytest.raises(ValueError):
        r.apply(cells[:4])
    with pytest.raises(ValueError):
        LatticeRemap(p, stride, nx=nx + 1, ny=ny)     # weights for another target


def _cells_nc(path, values, levels=None):
    """What `cdo -f nc4 copy` makes of ICON GRIB: time × [plev] × ncells, no coordinates."""
    ds = netCDF4.Dataset(path, "w")
    ds.createDimension("time", None)
    ds.createDimension("ncells", values.shape[-1])
    ds.createVariable("time", "f8", ("time",))[:] = [0]
    dims = ("time", "ncells")
    if levels is not None:
        ds.createDimension("plev", len(levels))
        z = ds.createVariable("plev", "f8", ("plev",))
        z.units = "Pa"
        z[:] = [lv * 100 for lv in levels]
        dims = ("time", "plev", "ncells")
    ds.createVariable("t", "f4", dims)[0] = values
    ds.close()


def test_levels_from_netcdf_reads_icon_cells_by_level(tmp_path):
    p = tmp_path / "c.nc"
    vals = np.arange(3 * 7, dtype=np.float32).reshape(3, 7)
    _cells_nc(p, vals, levels=[850, 1000, 700])
    got = levels_from_netcdf(p, (7,))
    assert sorted(got) == [700, 850, 1000]
    assert np.array_equal(got[1000], vals[1]) and got[850].shape == (7,)
    _cells_nc(p, vals[0])
    assert list(levels_from_netcdf(p, (7,))) == [None]


def test_lattice_levels_decodes_with_cdo_copy_then_applies_the_lattice(tmp_path):
    links = [(r, c, (r * 4 + c) % 6, 1.0) for r in range(3) for c in range(4)]
    w = tmp_path / "w.nc"
    _write_weights(w, 4, 3, links, src_size=6)
    remap = LatticeRemap(w, 2, nx=4, ny=3)
    srcs = []
    for lv in (1000, 850):
        g = tmp_path / f"T_{lv}.grib2.bz2"
        g.write_bytes(bz2.compress(f"grib{lv}".encode()))
        srcs.append(g)
    seen = []

    def fake_cdo(cmd, check):
        seen.append(cmd)
        assert cmd[:-2] == copy_command("a", "b")[:-2]
        assert Path(cmd[-2]).read_bytes() == b"grib1000grib850"          # cat'ed, unpacked
        _cells_nc(cmd[-1], np.array([np.arange(6) + 100, np.arange(6) + 200], np.float32), [1000, 850])

    got = lattice_levels(srcs, "/kit", tmp_path, 2, run=fake_cdo, remap=remap)
    assert len(seen) == 1
    assert got[1000].shape == (2, 2)
    # lattice (1, 1) is target (2, 2): its one link reads cell (2·4 + 2) % 6 = 4.
    assert got[1000][1, 1] == 104 and got[850][1, 1] == 204
    assert not list(tmp_path.glob("*_cells.*"))                          # temporaries removed


def test_copy_command_only_decodes():
    assert copy_command("/w/in.grib2", "/w/out.nc") == ["cdo", "-s", "-O", "-f", "nc4", "-b", "F32", "copy",
                                                        "/w/in.grib2", "/w/out.nc"]


@pytest.mark.skipif(shutil.which("cdo") is None, reason="needs cdo (CI installs it)")
@pytest.mark.parametrize("method", ["genbil", "gendis"])
def test_lattice_remap_equals_cdo_remap_at_the_lattice(tmp_path, method):
    # Real cdo: make weights from a 10° source onto a small lonlat target,
    # remap with them, and compare every stride-th point with ours.
    src = tmp_path / "src.nc"
    ds = netCDF4.Dataset(src, "w")
    ds.createDimension("time", None); ds.createDimension("lat", 18); ds.createDimension("lon", 36)
    t = ds.createVariable("time", "f8", ("time",)); t.units = "hours since 2026-01-01"; t[:] = [0]
    la = ds.createVariable("lat", "f8", ("lat",)); la.units = "degrees_north"; la[:] = np.arange(-85, 90, 10)
    lo = ds.createVariable("lon", "f8", ("lon",)); lo.units = "degrees_east"; lo[:] = np.arange(-175, 180, 10)
    vals = np.add.outer(np.arange(18) * 3.0, np.sin(np.arange(36) / 5.0) * 7).astype(np.float32)
    ds.createVariable("T", "f4", ("time", "lat", "lon"))[0] = vals
    ds.close()
    tgt = tmp_path / "tgt.txt"
    tgt.write_text("gridtype = lonlat\nxsize = 19\nysize = 13\nxfirst = -180\nxinc = 18.75\nyfirst = -60\nyinc = 10\n")
    w, out = tmp_path / "w.nc", tmp_path / "out.nc"
    subprocess.run(["cdo", "-s", "-O", f"{method},{tgt}", str(src), str(w)], check=True)
    subprocess.run(["cdo", "-s", "-O", "-f", "nc4", "-b", "F32", f"remap,{tgt},{w}", str(src), str(out)], check=True)
    want = levels_from_netcdf(out, (13, 19))[None]
    for stride in (1, 2, 3):
        got = LatticeRemap(w, stride, nx=19, ny=13).apply(vals.reshape(-1))
        assert np.allclose(got, want[::stride, ::stride], atol=1e-4, equal_nan=True)
