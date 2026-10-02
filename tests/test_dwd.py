import datetime as dt
from pipeline.dwd import (STEPS, FIELDS, TAIL_RUN_HOURS, TAIL_STEPS, file_name, file_url, run_from_listing,
                          missing_files, newest_complete_run, run_id, run_iso)

LISTING = """<html><body><pre><a href="../">../</a>
<a href="icon_global_icosahedral_single-level_2026092700_000_CLCL.grib2.bz2">icon_global_icosahedral_single-level_2026092700_000_CLCL.grib2.bz2</a> 27-Sep-2026 02:40:19  4559279
<a href="icon_global_icosahedral_single-level_2026092700_001_CLCL.grib2.bz2">...</a>
</pre></body></html>"""


def test_steps_are_hourly_to_78_then_3_hourly_to_120():
    assert STEPS[:3] == [0, 1, 2]
    assert STEPS[78] == 78
    assert STEPS[79:82] == [81, 84, 87]
    assert STEPS[-1] == 120
    assert len(STEPS) == 93


def test_fields_in_file_order():
    assert FIELDS == ["CLCL", "CLCM", "CLCH", "CLCT"]


def test_file_name_and_url():
    run = dt.datetime(2026, 9, 27, 0, tzinfo=dt.timezone.utc)
    assert file_name(run, 81, "CLCM") == "icon_global_icosahedral_single-level_2026092700_081_CLCM.grib2.bz2"
    assert file_url(run, 0, "CLCL") == ("https://opendata.dwd.de/weather/nwp/icon/grib/00/clcl/"
                                        "icon_global_icosahedral_single-level_2026092700_000_CLCL.grib2.bz2")


def test_run_from_listing_reads_the_run_time():
    assert run_from_listing(LISTING) == dt.datetime(2026, 9, 27, 0, tzinfo=dt.timezone.utc)
    assert run_from_listing("<html>nothing</html>") is None


def test_missing_files_names_what_a_listing_lacks():
    run = dt.datetime(2026, 9, 27, 0, tzinfo=dt.timezone.utc)
    names = {file_name(run, s, "CLCL") for s in STEPS} - {file_name(run, 84, "CLCL")}
    listing = "".join(f'<a href="{n}">{n}</a>\n' for n in names)
    assert missing_files(listing, run, "CLCL") == [file_name(run, 84, "CLCL")]


def test_newest_complete_run_picks_the_newest_dir_whose_files_are_all_there():
    run00 = dt.datetime(2026, 9, 27, 0, tzinfo=dt.timezone.utc)
    run06 = dt.datetime(2026, 9, 27, 6, tzinfo=dt.timezone.utc)
    run18 = dt.datetime(2026, 9, 26, 18, tzinfo=dt.timezone.utc)

    def full(run, field):
        return "".join(f'<a href="{file_name(run, s, field)}">x</a>\n' for s in STEPS)

    def partial(run, field):
        return "".join(f'<a href="{file_name(run, s, field)}">x</a>\n' for s in STEPS[:40])

    pages = {}
    for f in FIELDS:
        pages[f"00/{f.lower()}"] = full(run00, f)
        pages[f"06/{f.lower()}"] = partial(run06, f)      # still uploading
        pages[f"12/{f.lower()}"] = full(run18.replace(hour=12), f)
        pages[f"18/{f.lower()}"] = full(run18, f)
    listing = lambda hh, field: pages[f"{hh}/{field.lower()}"]
    assert newest_complete_run(listing) == run00


def test_newest_complete_run_is_none_when_nothing_is_complete():
    assert newest_complete_run(lambda hh, field: "<html></html>") is None


def test_run_ids():
    run = dt.datetime(2026, 9, 27, 6, tzinfo=dt.timezone.utc)
    assert run_id(run) == "2026092706"
    assert run_iso(run) == "2026-09-27T06Z"



def test_tail_steps_are_3_hourly_123_to_144_from_00z_and_12z():
    assert TAIL_STEPS == list(range(123, 145, 3))
    assert TAIL_STEPS[0] == 123 and TAIL_STEPS[-1] == 144 and len(TAIL_STEPS) == 8
    assert TAIL_RUN_HOURS == ["00", "12"]


def test_completeness_can_check_other_steps_and_run_hours():
    run = dt.datetime(2026, 9, 30, 0, tzinfo=dt.timezone.utc)
    html = "".join(f'<a href="{file_name(run, s, "CLCL")}">x</a>' for s in TAIL_STEPS)
    assert missing_files(html, run, "CLCL", steps=TAIL_STEPS) == []
    assert missing_files(html, run, "CLCL")            # the near steps are not there

    def listing(hh, field):
        if hh != "00":
            return ""
        return "".join(f'<a href="{file_name(run, s, field)}">x</a>' for s in TAIL_STEPS)
    assert newest_complete_run(listing, run_hours=TAIL_RUN_HOURS, steps=TAIL_STEPS) == run
    assert newest_complete_run(listing) is None         # the near product is unaffected


def test_column_file_names_and_urls():
    from pipeline.dwd import (pressure_file_name, model_file_name, invariant_file_name, dir_url,
                              COLUMN_PLEVELS, COLUMN_MLEVELS, COLUMN_HHL_LEVELS)
    run = dt.datetime(2026, 10, 2, 6, tzinfo=dt.timezone.utc)
    assert pressure_file_name(run, 7, 950, "RELHUM") == "icon_global_icosahedral_pressure-level_2026100206_007_950_RELHUM.grib2.bz2"
    assert model_file_name(run, 81, 118, "T") == "icon_global_icosahedral_model-level_2026100206_081_118_T.grib2.bz2"
    assert invariant_file_name(run, "HHL", 119) == "icon_global_icosahedral_time-invariant_2026100206_119_HHL.grib2.bz2"
    assert invariant_file_name(run, "HSURF") == "icon_global_icosahedral_time-invariant_2026100206_HSURF.grib2.bz2"
    assert dir_url(run, "RELHUM_2M", "f") == "https://opendata.dwd.de/weather/nwp/icon/grib/06/relhum_2m/f"
    assert 975 not in COLUMN_PLEVELS and COLUMN_PLEVELS == sorted(COLUMN_PLEVELS, reverse=True)
    # The HHL half levels are exactly the bounds of the model levels used.
    assert COLUMN_HHL_LEVELS == sorted({lv for m in COLUMN_MLEVELS for lv in (m, m + 1)})


def test_missing_column_files_names_what_the_listings_lack():
    from pipeline.dwd import column_files, missing_column_files, pressure_file_name
    run = dt.datetime(2026, 10, 2, 0, tzinfo=dt.timezone.utc)
    files = column_files(run, [0, 1])
    gone = pressure_file_name(run, 1, 700, "FI")
    pages = {f: "".join(f'<a href="{n}">{n}</a>\n' for n in names if n != gone) for f, names in files.items()}
    assert missing_column_files(lambda hh, field: pages.get(field, "") if hh == "00" else "", run, [0, 1]) == [gone]
    pages["FI"] += f'<a href="{gone}">x</a>'
    assert missing_column_files(lambda hh, field: pages.get(field, "") if hh == "00" else "", run, [0, 1]) == []
    # Per step: each pressure field on each level, each model level's T, each
    # single-level field; once per run: HSURF and each HHL half level.
    from pipeline.dwd import COLUMN_PFIELDS, COLUMN_PLEVELS, COLUMN_MLEVELS, COLUMN_SFIELDS, COLUMN_HHL_LEVELS
    per_step = len(COLUMN_PFIELDS) * len(COLUMN_PLEVELS) + len(COLUMN_MLEVELS) + len(COLUMN_SFIELDS)
    assert sum(len(v) for v in files.values()) == 2 * per_step + 1 + len(COLUMN_HHL_LEVELS)
    assert len(set(n for v in files.values() for n in v)) == sum(len(v) for v in files.values())
