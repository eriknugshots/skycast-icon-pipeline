import datetime as dt
import json
import zipfile
from pathlib import Path

import pytest

import places_build
from places import build as pb
from places.normalize import key_file, norm

NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.timezone.utc)
ADMIN1 = "US.OR\tOregon\tOregon\t5744337\nDE.07\tNorth Rhine-Westphalia\tNorth Rhine-Westphalia\t2861876\n"
COUNTRIES = "#comment\nUS\tUSA\t840\tUS\tUnited States\tWashington\n" \
            "DE\tDEU\t276\tGM\tGermany\tBerlin\nJP\tJPN\t392\tJA\tJapan\tTokyo\n"


def gn(i, name, cls="P", code="PPL", pop="", alts="", cc="US", adm="OR",
       elev="", dem="", lat="44.0", lon="-121.0", tz="America/Los_Angeles"):
    row = [str(i), name, name, alts, lat, lon, cls, code, cc, "", adm, "", "", "",
           str(pop), elev, dem, tz, "2020-01-01"]
    return "\t".join(row)


def make_zip(tmp_path, rows):
    p = tmp_path / "allCountries.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("allCountries.txt", "\n".join(rows) + "\n")
    return p


def run(tmp_path, rows, **kw):
    kw.setdefault("min_rows", 1)
    out = tmp_path / "out"
    m = pb.build(make_zip(tmp_path, rows), out, ADMIN1, COUNTRIES, NOW,
                 tmp_dir=tmp_path / "work", log=lambda *_: None, **kw)
    return out, m


def rows_of(out, m, key):
    """What the app does: open shard_for's file, filter by prefix."""
    f = out / m["tiles"] / pb.shard_for(key, set(m["split"]))
    if not f.exists():
        return []
    cols = [l.split("\t") for l in f.read_text().splitlines()]
    return [c for c in cols if c[0].startswith(key)]


def test_norm_vectors():
    vectors = json.loads((Path(__file__).parent / "fixtures/place_norm_vectors.json").read_text())
    assert len(vectors) == 10
    for raw, want in vectors:
        assert norm(raw) == want, raw


def test_key_file():
    assert key_file("san") == "san"
    assert key_file("東京") == "x6771x4eac"


def test_every_row_is_kept(tmp_path):
    classes = [("P", "PPL"), ("A", "ADM1"), ("T", "MT"), ("H", "LK"), ("L", "AREA"),
               ("S", "HTL"), ("R", "RD"), ("U", "SCNU"), ("V", "FRST"), ("", "")]
    rows = [gn(i, f"Zeta{chr(97 + i)}", cls, code, pop=0 if i % 2 else 5)
            for i, (cls, code) in enumerate(classes)]
    out, m = run(tmp_path, rows)
    for i, (cls, code) in enumerate(classes):
        got = rows_of(out, m, norm(f"Zeta{chr(97 + i)}"))
        assert len(got) == 1 and got[0][4] == code


def test_rank_order(tmp_path):
    rows = [gn(1, "Rankville", code="PPL", pop=10),
            gn(2, "Rankville", code="PPL", pop=900000),
            gn(3, "Rankville", code="PPLC", pop=500),
            gn(4, "Rankville", code="PPL", pop=0)]
    out, m = run(tmp_path, rows)
    pops = [r[8] for r in rows_of(out, m, "rankville")]
    assert pops == ["500", "900000", "10", "0"]
    rows = [gn(1, "Pondtown", code="PPL", pop=0),
            gn(2, "Pondtown", cls="A", code="ADM1"),
            gn(3, "Pondtown", cls="T", code="MT")]
    out, m = run(tmp_path, rows)
    codes = [r[4] for r in rows_of(out, m, "pondtown")]
    assert codes == ["ADM1", "MT", "PPL"]


def test_word_rows(tmp_path):
    rows = [gn(1, "Mount Bachelor", cls="T", code="MT"),
            gn(2, "Saint Paul", pop=300000),
            gn(3, "Mount Xy"), gn(4, "La Ab")]
    out, m = run(tmp_path, rows)
    for key in ("mountbachelor", "mtbachelor", "bachelor"):
        got = rows_of(out, m, key)
        assert got and got[0][1] == "Mount Bachelor", key
    st = rows_of(out, m, "stpaul")
    assert st and st[0][1] == "Saint Paul"
    assert rows_of(out, m, "paul")
    assert not rows_of(out, m, "xy") and not [r for r in rows_of(out, m, "ab") if r[0] == "ab"]


def test_alternates(tmp_path):
    rows = [gn(1, "Köln", pop=1000000, cc="DE", adm="07", alts="Cologne,Colonia,ケルン,Köln,Koeln")]
    out, m = run(tmp_path, rows)
    got = rows_of(out, m, "cologne")
    assert got[0][1] == "Cologne" and got[0][6] == "Germany" and got[0][7] == "North Rhine-Westphalia"
    assert rows_of(out, m, "ケルン")[0][1] == "ケルン"
    assert len(rows_of(out, m, "koln")) == 1          # name and "Köln" alternate collapse
    assert rows_of(out, m, "koeln")


def tile_files(out, m):
    return {f.name: f for f in (out / m["tiles"]).iterdir()}


def test_shard_for_resolution():
    split = {"s", "sa", "san"}
    assert pb.shard_for("sa", split) == "sa.tsv"
    assert pb.shard_for("san", split) == "san.tsv"
    assert pb.shard_for("sant", split) == "san~.tsv"
    assert pb.shard_for("sanfrancisco", split) == "san~.tsv"
    assert pb.shard_for("sb", split) == "s~.tsv"
    assert pb.shard_for("qx", split) == "~.tsv"
    assert pb.shard_for("東京", {"東"}) == "x6771~.tsv"


def test_small_siblings_share_one_tilde_file(tmp_path):
    rows = [gn(i, "Spring%04d" % i, pop=i) for i in range(1, 400)]
    rows += [gn(1000 + i, n, pop=1) for i, n in enumerate(["Qx", "Quad", "Ruby", "Rock", "Tango"])]
    out, m = run(tmp_path, rows, max_shard_bytes=4000, top_n=10)
    files = tile_files(out, m)
    split = set(m["split"])
    assert "s" in split and "sp" in split
    assert not split & {"q", "r", "t"}
    root = files["~.tsv"].read_text().splitlines()
    keys = {l.split("\t")[0] for l in root}
    assert {"qx", "quad", "ruby", "rock", "tango"} <= keys    # five siblings, one file
    assert "q.tsv" not in files and "r.tsv" not in files
    assert not any(k.startswith("spring") for k in keys)       # the big one left
    top = files["s.tsv"].read_text().splitlines()
    assert len(top) == 10 and top[0].split("\t")[1] == "Spring0399"


def test_oversize_sibling_group_forces_the_largest_out(tmp_path):
    rows = []
    for ch, n in (("a", 60), ("b", 40), ("c", 20), ("d", 5), ("e", 5)):
        rows += [gn(len(rows) + i, f"{ch}{i:04d}x{'y' * 20}", pop=i) for i in range(n)]
    out, m = run(tmp_path, rows, max_shard_bytes=6000, top_n=5)
    files = tile_files(out, m)
    split = set(m["split"])
    assert "a" in split                    # the largest goes first
    assert "d" not in split and "e" not in split
    assert files["~.tsv"].stat().st_size <= 6000
    assert len(rows_of(out, m, "d0000")) == 1 and len(rows_of(out, m, "a0059")) == 1


def test_resolution_contract_every_key_finds_its_row(tmp_path):
    names = ["Santa Cruz", "San Diego", "San Francisco", "Santa Fe", "Sant Antoni", "Sandpoint",
            "Saint Paul", "Sa", "Sanaa", "Mount Bachelor", "東京", "京都", "Zzz", "Zürich"]
    rows = [gn(i, n, pop=i) for i, n in enumerate(names)]
    rows += [gn(100 + i, "San%03d" % i, pop=i) for i in range(150)]
    out, m = run(tmp_path, rows, max_shard_bytes=2500, top_n=1000)
    split = set(m["split"])
    assert split
    seen = 0
    # rebuild the row list the way the build keyed it, then check each resolves
    from places.build import place_rows
    for r in rows:
        p = r.split("\t")
        for key, _rank, row in place_rows(p[1], p[3], "x", p[7], 0):
            if len(key) < 2:
                continue
            f = out / m["tiles"] / pb.shard_for(key, split)
            assert f.exists(), (key, f.name)
            assert any(l.split("\t")[0] == key and l.split("\t")[1] == row.split("\t")[1]
                       for l in f.read_text().splitlines()), (key, f.name)
            seen += 1
    assert seen > 150
    # a rare first character lands in the root file
    assert pb.shard_for("zzz", split) in ("~.tsv", "z~.tsv", "zz~.tsv", "zzz.tsv")


def test_unsplittable_key_may_exceed_the_cap(tmp_path):
    rows = [gn(i, "Same", pop=i) for i in range(1, 300)]
    out, m = run(tmp_path, rows, max_shard_bytes=2000, top_n=5)
    files = tile_files(out, m)
    assert len(files["same~.tsv"].read_text().splitlines()) == 299   # over the cap, as allowed
    assert len(files["same.tsv"].read_text().splitlines()) == 5      # the top N


def test_elevation_falls_back_to_dem_and_dem_minus_9999_is_blank(tmp_path):
    rows = [gn(1, "Elevone", elev="123", dem="999"),
            gn(2, "Elevtwo", elev="", dem="777"),
            gn(3, "Elevthree", elev="", dem="-9999")]
    out, m = run(tmp_path, rows)
    assert rows_of(out, m, "elevone")[0][9] == "123"
    assert rows_of(out, m, "elevtwo")[0][9] == "777"
    assert rows_of(out, m, "elevthree")[0][9] == ""


def test_manifest(tmp_path):
    out, m = run(tmp_path, [gn(1, "Bend")])
    disk = json.loads((out / "manifest.json").read_text())
    assert disk["version"] == "20261108" and disk["tiles"] == "tiles/20261108"
    assert disk["complete"] is True and disk["split"] == []
    assert disk["attribution"] == "Places: GeoNames (CC BY 4.0)"
    assert isinstance(disk["runMs"], int) and isinstance(disk["builtMs"], int)
    assert rows_of(out, disk, "bend")[0][:2] == ["bend", "Bend"]


def test_build_refuses_under_ten_million_rows(tmp_path):
    z = make_zip(tmp_path, [gn(1, "Bend")])
    code = places_build.main(
        ["--out", str(tmp_path / "site"), "--zip", str(z)],
        fetch=lambda url: ADMIN1 if "admin1" in url else COUNTRIES, now=lambda: NOW)
    assert code == 1
    assert not (tmp_path / "site" / "manifest.json").exists()


def test_fresh_live_manifest_means_nothing_to_do(tmp_path):
    def fetch(url):
        return json.dumps({"version": "20261101"})
    args = ["--out", str(tmp_path / "site"), "--live-manifest", "http://x/manifest.json"]
    assert places_build.main(args, fetch=fetch, now=lambda: NOW) == 3
    old = lambda url: json.dumps({"version": "20261001"})
    # 38 days old: builds (fails here only because there is no zip to download)
    code = places_build.main(args + ["--zip", str(tmp_path / "missing.zip")], fetch=old, now=lambda: NOW)
    assert code == 1


# ---- the workflow's build step, run for real under bash with python/node stubbed

import os
import re
import shutil
import stat
import subprocess
import textwrap

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/places.yml"


def step_script():
    lines = WORKFLOW.read_text().splitlines()
    i = next(n for n, line in enumerate(lines) if "name: build the place index" in line)
    i = next(n for n in range(i, len(lines)) if lines[n].strip() == "run: |")
    indent = len(lines[i]) - len(lines[i].lstrip())
    body = []
    for line in lines[i + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body)) + "\n"


def _stub(path, text):
    path.write_text("#!/usr/bin/env bash\n" + text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_step(tmp_path, rc, force="false"):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (tmp_path / "publish").mkdir()
    _stub(bin_ / "python", textwrap.dedent(f"""\
        echo "$*" > "$T/build_args"
        if [ "{rc}" = "0" ]; then mkdir -p site-places; fi
        exit {rc}
        """))
    _stub(bin_ / "node", textwrap.dedent("""\
        echo "$* token=${BLOB_READ_WRITE_TOKEN:-}" >> "$T/node_args"
        test -d ../site-places || exit 98
        """))
    if not shutil.which("timeout"):
        _stub(bin_ / "timeout", 'shift 3; exec "$@"\n')
    script = tmp_path / "step.sh"
    script.write_text(step_script())
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "T": str(tmp_path),
           "PLACES_BASE": "https://blob.example", "BLOB_READ_WRITE_TOKEN": "tok", "FORCE": force}
    return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)


def test_built_publishes_with_the_places_token(tmp_path):
    p = run_step(tmp_path, 0)
    assert p.returncode == 0, p.stderr
    assert (tmp_path / "build_args").read_text().strip().endswith(
        "places_build.py --out site-places --live-manifest https://blob.example/manifest.json")
    assert (tmp_path / "node_args").read_text().splitlines() == ["blob.mjs ../site-places token=tok"]


def test_force_is_passed_on(tmp_path):
    p = run_step(tmp_path, 0, force="true")
    assert p.returncode == 0, p.stderr
    assert (tmp_path / "build_args").read_text().strip().endswith("--force")


def test_recent_index_is_success_and_does_not_publish(tmp_path):
    p = run_step(tmp_path, 3)
    assert p.returncode == 0, p.stderr
    assert not (tmp_path / "node_args").exists()


@pytest.mark.parametrize("rc", [1, 2, 124, 137])
def test_a_failed_build_fails_the_step_and_does_not_publish(tmp_path, rc):
    p = run_step(tmp_path, rc)
    assert p.returncode == rc
    assert not (tmp_path / "node_args").exists()


def test_workflow_text_pins():
    text = WORKFLOW.read_text()
    assert 'cron: "17 6 2 * *"' in text
    assert "workflow_dispatch:" in text
    assert re.search(r"concurrency:\s*\n\s*group: places\n\s*cancel-in-progress: false", text)
    assert "runs-on: ubuntu-24.04" in text
    assert "python -m pytest -q" in text
    assert "https://tmqi4dwepzljptx1.public.blob.vercel-storage.com" in text
    assert "actions: write" not in text
    assert set(re.findall(r"secrets\.(\w+)", text)) == {"PLACES_BLOB_READ_WRITE_TOKEN"}
