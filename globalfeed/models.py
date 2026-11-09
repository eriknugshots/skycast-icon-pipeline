# globalfeed/models.py — the two worldwide cloud-cover models SkyCast's own
# feeds are built from, and where each one's files live.
#
# Layer heights, which differ between models and must be remembered wherever
# feeds are compared:
#   GFS (NOAA UPP, like HRRR): low = ground to 642 hPa, mid 642-350, high
#     350-150 (NOAA UPP INITPOST.F). ICON: low = ground to 800 hPa, mid
#     800-400, high above 400.
#   AIFS (ECMWF parameter database, params 186/187/188; fetched 2026-11-08):
#     lcc (186) = model levels with pressure above 0.8 x surface pressure
#       (roughly below 2 km in a standard atmosphere);
#     mcc (187) = levels between 0.45 and 0.8 x surface pressure (roughly
#       2-6 km);
#     hcc (188) = levels below 0.45 x surface pressure (roughly 6 km and up).
#     Cloud overlap between levels is assumed by ECMWF's own scheme.
#   IFS: the same lcc/mcc/hcc definitions as AIFS (ECMWF's IFS is the model
#     those parameters were defined for; same param-db entries, 186/187/188).
from dataclasses import dataclass


@dataclass(frozen=True)
class Model:
    name: str
    attribution: str
    main_steps: tuple
    tail_steps: tuple
    index_kind: str  # "ncep" | "ecmwf"
    fields: tuple    # low / mid / high / total keys
    workers: int
    _url: str
    _index_ext: tuple  # (suffix to replace or "", replacement/addition)
    # Pressure levels (hPa) of the ICMR / CLMR messages the thickness feed
    # integrates; empty = this model publishes no thickness (AIFS: its open data
    # has only tcw, which includes vapour, and humidity: no cloud ice or water).
    thick_levels: tuple = ()
    # Column totals (kg/m2) the thickness feed reads as they are, no level
    # integration: ((index param, "ice"), (index param, "water")), from the same
    # ECMWF .index as the cover. Empty = no column fields. A model carries
    # thick_levels or thick_fields, never both.
    thick_fields: tuple = ()
    # The synoptic hours whose runs this feed builds. A run hour not listed is
    # never asked for (IFS: 06/18Z are the short "scda" stream, f000-f090 only,
    # too short for the f144 tail).
    run_hours: tuple = (0, 6, 12, 18)
    # True: read each cover message's GRIB units and bring it to percent ("%" as
    # is, "(0 - 1)" x 100), refusing any other unit. eccodes knows ECMWF's lcc
    # both as paramId 3073 (%) and 186 (0 - 1), so the encoding decides. False:
    # take the values as they are (GFS and AIFS, both measured as percent).
    check_cover_units: bool = False

    def __post_init__(self):
        if self.thick_levels and self.thick_fields:
            raise ValueError(f"{self.name}: thick_levels and thick_fields are exclusive")

    @property
    def has_thick(self):
        return bool(self.thick_levels or self.thick_fields)

    @property
    def run_spacing(self):
        """Hours between two runs this feed builds (6 for four a day, 12 for two)."""
        return 24 // len(self.run_hours)

    def url(self, run, fh):
        return self._url.format(d=run.strftime("%Y%m%d"), h=run.hour, f=fh)

    def index_url(self, run, fh):
        u = self.url(run, fh)
        old, new = self._index_ext
        return (u[: -len(old)] if old else u) + new


GFS = Model(
    name="gfs",
    attribution="Cloud data: NOAA GFS",
    main_steps=tuple(range(0, 121)),
    tail_steps=tuple(range(123, 145, 3)),
    index_kind="ncep",
    fields=(
        ("LCDC", "low cloud layer"),
        ("MCDC", "middle cloud layer"),
        ("HCDC", "high cloud layer"),
        ("TCDC", "entire atmosphere"),
    ),
    workers=8,
    _url="https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{d}/{h:02d}/atmos/gfs.t{h:02d}z.pgrb2.0p25.f{f:03d}",
    _index_ext=("", ".idx"),
    # GFS's 21 mandatory levels from 100 mb (their integral matches GFS's own
    # CWAT: median ratio 0.998, correlation 0.994, plan 2026-11-09).
    thick_levels=(100, 150, 200, 250, 300, 350, 400, 450, 500, 550, 600, 650, 700, 750,
                  800, 850, 900, 925, 950, 975, 1000),
)

AIFS = Model(
    name="aifs",
    attribution="Cloud data: ECMWF AIFS (CC BY 4.0)",
    main_steps=tuple(range(0, 121, 6)),
    tail_steps=tuple(range(126, 145, 6)),
    index_kind="ecmwf",
    fields=("lcc", "mcc", "hcc", "tcc"),
    workers=4,  # ECMWF's open-data server limits parallel connections
    _url="https://data.ecmwf.int/forecasts/{d}/{h:02d}z/aifs-single/0p25/oper/{d}{h:02d}0000-{f}h-oper-fc.grib2",
    _index_ext=(".grib2", ".index"),
)


# ECMWF IFS open data (CC BY 4.0). Only the 00/12Z "oper" runs: they reach
# f240 (f000-f144 every 3 h, then every 6 h), so the AIFS horizon and steps
# (f000-f120 every 6 h, tail f126-f144 every 6 h) are all there; 06/18Z are
# the "scda" stream, f000-f090, and are not built. Its 0.25 degree open data
# carries the column cloud ice and water, so IFS has thickness where AIFS has none.
IFS = Model(
    name="ifs",
    attribution="Cloud data: © ECMWF, CC BY 4.0",
    main_steps=tuple(range(0, 121, 6)),
    tail_steps=tuple(range(126, 145, 6)),
    index_kind="ecmwf",
    fields=("lcc", "mcc", "hcc", "tcc"),
    workers=4,  # ECMWF's open-data server limits parallel connections
    _url="https://data.ecmwf.int/forecasts/{d}/{h:02d}z/ifs/0p25/oper/{d}{h:02d}0000-{f}h-oper-fc.grib2",
    _index_ext=(".grib2", ".index"),
    thick_fields=(("tciw", "ice"), ("tclw", "water")),
    run_hours=(0, 12),
    check_cover_units=True,
)
