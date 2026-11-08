# aqm/grib.py — NOAA AQM (AQMv7, CONUS) bias-corrected hourly PM2.5.
#
# One file per cycle (06Z and 12Z carry the 72 h forecast): 72 GRIB2 messages,
# one per forecast hour, on a Lambert grid (1473 x 1025, 5 km). Units are
# ug/m3 ("(10**-6 g) m**-3"), each message is the hourly average ending at
# endStep. The bias-corrected file has no .idx sidecar, so the whole file is
# downloaded and every message walked.
import numpy as np
import eccodes

_BASE = ("https://noaa-nws-naqfc-pds.s3.amazonaws.com/AQMv7/CS/{d}/{h:02d}/"
         "aqm.t{h:02d}z.ave_1hr_pm25_bc.{d}.227.grib2")


def file_url(run):
    return _BASE.format(d=run.strftime("%Y%m%d"), h=run.hour)


def read_hours(path):
    """{"lat", "lon", "hours": {forecast hour: float32 array}} for every message.

    lon is -180..180. Negative and missing values come back as NaN."""
    out = {"hours": {}}
    with open(path, "rb") as f:
        while True:
            g = eccodes.codes_grib_new_from_file(f)
            if g is None:
                break
            try:
                vals = eccodes.codes_get_values(g)
                if "lat" not in out:
                    out["lat"] = eccodes.codes_get_array(g, "latitudes").astype(np.float32)
                    lon = eccodes.codes_get_array(g, "longitudes")
                    out["lon"] = np.where(lon > 180, lon - 360, lon).astype(np.float32)
                miss = eccodes.codes_get(g, "missingValue")
                fh = int(eccodes.codes_get(g, "endStep"))
            finally:
                eccodes.codes_release(g)
            out["hours"][fh] = np.where(np.isclose(vals, miss) | (vals < 0), np.nan, vals).astype(np.float32)
    return out
