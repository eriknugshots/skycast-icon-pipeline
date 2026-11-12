# Synthetic profiles shared by tests/test_clues.py and tools/make_clues_fixture.py.
import numpy as np

P = np.array([1000, 925, 850, 700, 600, 500, 400, 300, 250, 200], dtype=float)


def _std_gh(p):
    # ICAO standard atmosphere, troposphere + isothermal lower stratosphere.
    z = 44330.77 * (1.0 - (p / 1013.25) ** 0.190263)
    return np.where(p >= 226.32, z, 11000.0 + 6341.62 * np.log(226.32 / p))


def _std_t(gh):
    return np.where(gh < 11000.0, 15.0 - 0.0065 * gh, -56.5)


def base(rh=20.0):
    gh = _std_gh(P)
    n = len(P)
    return {
        "p": P.copy(), "t": _std_t(gh), "rh": np.full(n, float(rh)), "gh": gh,
        "u": np.zeros(n), "v": np.zeros(n),
        "cape_con": 0.0, "cape_ml": 0.0, "hbas_con": -500.0, "htop_con": -500.0,
    }


def at(prof, p, **kw):
    i = int(np.nonzero(prof["p"] == p)[0][0])
    for k, v in kw.items():
        prof[k][i] = v


def rhw_for_rhi(rhi_pct, t_c):
    """RH over water (%) that gives rhi_pct over ice at t_c."""
    from pipeline.clues import rh_water_to_ice
    return float(rhi_pct / (rh_water_to_ice(100.0, t_c) * 100.0) * 100.0)


def thin_lid_mid():
    pr = base()
    i500 = 5
    at(pr, 500.0, rh=rhw_for_rhi(100.0, pr["t"][i500]))
    at(pr, 600.0, rh=rhw_for_rhi(55.0, pr["t"][4]))
    at(pr, 400.0, rh=rhw_for_rhi(40.0, pr["t"][6]))
    # theta jump of 3 K at 400 hPa: warm that level and above by the K that gives +3 K theta
    pr["t"][6:] += 3.0 * (pr["p"][6:] / 1000.0) ** 0.2857
    return pr


def deep_moist_mid():
    pr = base()
    for i in (4, 5, 6):
        at(pr, pr["p"][i], rh=rhw_for_rhi(100.0, pr["t"][i]))
    return pr


def shear_direction():
    pr = base()
    at(pr, 300.0, u=10.0)
    at(pr, 200.0, u=40.0)
    return pr


def rh_water_to_ice_profile():
    pr = base(rh=62.0)
    pr["t"][7:] = -50.0
    return pr


def missing_upper():
    pr = base(rh=95.0)
    for k in ("t", "rh", "gh", "u", "v"):
        pr[k][6:] = np.nan  # 400 hPa and above
    return pr


def clear_dry():
    return base(rh=15.0)


def all_synthetic():
    return {
        "thin_lid_mid": thin_lid_mid(),
        "deep_moist_mid": deep_moist_mid(),
        "shear_direction": shear_direction(),
        "rh_water_to_ice": rh_water_to_ice_profile(),
        "missing_upper": missing_upper(),
        "clear_dry": clear_dry(),
    }
