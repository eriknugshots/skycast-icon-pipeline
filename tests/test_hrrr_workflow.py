# The HRRR loop in .github/workflows/hrrr.yml, run for real under bash with
# python, node, sleep and date stubbed (fake clock): a publish follows a build
# that exited 0 and nothing else; every pass sleeps 300 s; the loop ends at 285
# min. The fog build runs in the BACKGROUND: the stub registers when it will
# finish on the fake clock, and the sleep stub waits for it at that moment, so
# the event order is deterministic.
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_RCS = [3, 0, 3, 1, 0, 3, 0, 3]   # 0 = built, 3 = nothing to do, else the build failed
BUILD = "hrrr_build.py --out site-hrrr --live-manifest https://blob.example/manifest.json"
PUBLISH = "node blob.mjs ../site-hrrr"
# (exit code, fake seconds it runs) of each fog build the loop starts; then (3, 1).
# 0 over 2 passes (published: its publish fails); 0 over 4 passes while a cloud
# publish moves the live run (stale: discarded); a failure; nothing to do; 0 again.
FOGS = [(0, 600), (0, 1200), (1, 300), (3, 300), (0, 300)]
FOG_BUILD = "hrrr_fog_build.py --out site-hrrr-fog --live-manifest https://blob.example/manifest.json"
END, DRAIN = 285 * 60, (285 + 35) * 60


def loop_script():
    lines = (ROOT / ".github/workflows/hrrr.yml").read_text().splitlines()
    i = next(n for n, line in enumerate(lines) if "name: watch NOAA, build each new HRRR run" in line)
    i = next(n for n in range(i, len(lines)) if lines[n].strip() == "run: |")
    indent = len(lines[i]) - len(lines[i].lstrip())
    body = []
    for line in lines[i + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body)) + "\n"


def expected_events(build_rcs, fogs):
    """The loop's events by hand: cloud check every pass, one fog build at a time."""
    s = {"clock": 0, "live": 0, "pub": 0, "fpub": 0, "fog": None, "discarded": 0, "killed": False}
    events = []

    def sleep(sec):
        events.append(f"sleep {sec}")
        s["clock"] += sec
        fog = s["fog"]
        if fog and not fog["done"] and s["clock"] >= fog["due"]:
            events.append(f"fogbuild {fog['rc']} R{fog['run']}")
            fog["done"] = True

    def collect():
        fog = s["fog"]
        if fog["rc"] == 0:
            if fog["run"] == s["live"]:
                events.append(f"fogpublish R{fog['run']}")
            else:
                s["discarded"] += 1
        s["fog"] = None

    n = f = 0
    while s["clock"] < END:
        if s["fog"] and s["fog"]["done"]:
            collect()
        rc = build_rcs[n] if n < len(build_rcs) else 3
        n += 1
        events.append(f"build {rc}")
        if rc == 0:
            events.append("publish")
            if s["pub"] > 0:   # the first publish fails; a good one moves the live run
                s["live"] += 1
            s["pub"] += 1
        if rc in (0, 3) and s["fog"] is None:
            frc, dur = fogs[f] if f < len(fogs) else (3, 1)
            f += 1
            s["fog"] = {"rc": frc, "due": s["clock"] + dur, "run": s["live"], "done": False}
        sleep(300)
    if s["fog"]:
        while not s["fog"]["done"] and s["clock"] < DRAIN:
            sleep(30)
        if s["fog"]["done"]:
            collect()
        else:
            s["killed"] = True
    return events, n, f, s


def _stub(path, text):
    path.write_text("#!/usr/bin/env bash\n" + text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_loop(tmp_path, build_rcs, fogs):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (tmp_path / "clock").write_text("0\n")
    (tmp_path / "live").write_text("0\n")
    (tmp_path / "n").write_text("0\n")
    (tmp_path / "rcs").write_text("\n".join(map(str, build_rcs)) + "\n")
    (tmp_path / "fogs").write_text("".join(f"{rc} {dur}\n" for rc, dur in fogs))
    (tmp_path / "publish").mkdir()
    _stub(bin_ / "python", textwrap.dedent("""\
        if [ "$1" = "hrrr_build.py" ]; then
          shift; echo "$*" > "$T/build_args"
          n=$(cat "$T/n"); echo $((n + 1)) > "$T/n"
          rc=$(sed -n "$((n + 1))p" "$T/rcs"); rc=${rc:-3}
          if [ "$rc" = "0" ]; then mkdir -p site-hrrr; fi
          echo "build $rc" >> "$T/events"; exit "$rc"
        fi
        if [ "$1" = "hrrr_fog_build.py" ] && [ "$2" = "--still-live" ]; then
          shift; echo "$*" > "$T/still_args"
          built=$(sed 's/.*"run":"\\([^"]*\\)".*/\\1/' "$2/manifest.json")
          [ "$built" = "R$(cat "$T/live")" ] && exit 0
          exit 3
        fi
        if [ "$1" = "hrrr_fog_build.py" ]; then
          shift; echo "$*" > "$T/fog_args"
          [ -f "$T/fog_active" ] && echo "OVERLAP" >> "$T/events"
          touch "$T/fog_active"
          n=$(cat "$T/fn" 2>/dev/null || echo 0); echo $((n + 1)) > "$T/fn"
          line=$(sed -n "$((n + 1))p" "$T/fogs"); rc=${line%% *}; dur=${line##* }
          rc=${rc:-3}; dur=${dur:-1}
          run=R$(cat "$T/live")
          due=$(( $(cat "$T/clock") + dur ))
          echo "$due" > "$T/fog_due.tmp"; mv "$T/fog_due.tmp" "$T/fog_due"
          while [ "$(cat "$T/clock")" -lt "$due" ]; do /bin/sleep 0.01; done
          # a built run leaves a folder; a rc 3 may leave stray files (the live run moved)
          if [ "$rc" = "0" ] || [ "$rc" = "3" ]; then mkdir -p site-hrrr-fog; fi
          if [ "$rc" = "0" ]; then printf '{"run":"%s"}' "$run" > site-hrrr-fog/manifest.json; fi
          rm -f "$T/fog_active"
          echo "fogbuild $rc $run" >> "$T/events"; exit "$rc"
        fi
        exit 99
        """))
    _stub(bin_ / "node", textwrap.dedent("""\
        echo "$* token=${BLOB_READ_WRITE_TOKEN:-}" >> "$T/node_args"
        if [ "$2" = "../site-hrrr-fog" ]; then
          test -d ../site-hrrr-fog || exit 98
          run=$(sed 's/.*"run":"\\([^"]*\\)".*/\\1/' ../site-hrrr-fog/manifest.json)
          echo "fogpublish $run" >> "$T/events"
          n=$(cat "$T/fnode_n" 2>/dev/null || echo 0); echo $((n + 1)) > "$T/fnode_n"
          [ "$n" = "0" ] && exit 1   # the first fog publish fails
          exit 0
        fi
        echo "publish" >> "$T/events"
        test -d ../site-hrrr || exit 98
        n=$(cat "$T/node_n" 2>/dev/null || echo 0); echo $((n + 1)) > "$T/node_n"
        [ "$n" = "0" ] && exit 1   # the first publish fails
        echo $(( $(cat "$T/live") + 1 )) > "$T/live.tmp"; mv "$T/live.tmp" "$T/live"
        exit 0
        """))
    # fog.pid (written by the loop itself) marks a fog build in flight: wait until
    # the stub has registered its finish time before the clock moves, and after
    # the clock passes it, until the build has ended (fog.rc).
    _stub(bin_ / "sleep", textwrap.dedent("""\
        echo "sleep $1" >> "$T/events"
        if [ -f fog.pid ] && [ ! -f fog.rc ]; then
          until [ -f "$T/fog_due" ] || [ -f fog.rc ]; do /bin/sleep 0.01; done
        fi
        c=$(( $(cat "$T/clock") + $1 )); echo "$c" > "$T/clock.tmp"; mv "$T/clock.tmp" "$T/clock"
        if [ -f "$T/fog_due" ] && [ "$c" -ge "$(cat "$T/fog_due")" ]; then
          until [ -f fog.rc ]; do /bin/sleep 0.01; done
          rm -f "$T/fog_due"
        fi
        """))
    _stub(bin_ / "date", 'if [ "$1" = "+%s" ]; then cat "$T/clock"; else exec /bin/date "$@"; fi\n')
    if not shutil.which("timeout"):   # macOS has no coreutils timeout; CI does
        _stub(bin_ / "timeout", 'shift 3; exec "$@"\n')
    script = tmp_path / "loop.sh"
    script.write_text(loop_script())
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "T": str(tmp_path),
           "HRRR_BASE": "https://blob.example", "BLOB_READ_WRITE_TOKEN": "tok"}
    # GitHub runs `run:` steps as: bash --noprofile --norc -eo pipefail {0}
    p = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr   # no failure ends the loop
    events = (tmp_path / "events").read_text().splitlines()
    for leftover in ("site-hrrr", "site-hrrr-fog", "fog.rc", "fog.log", "fog.pid"):
        assert not (tmp_path / leftover).exists(), leftover
    return p, events


def test_loop_publishes_only_on_built(tmp_path):
    p, events = run_loop(tmp_path, BUILD_RCS, FOGS)
    want, builds, fogs_started, s = expected_events(BUILD_RCS, FOGS)
    assert events == want
    assert "OVERLAP" not in events   # never a second fog build while one runs
    assert builds == END // 300 == events.count("build 3") + events.count("build 0") + events.count("build 1")
    assert "publish failed, will retry" in p.stdout
    assert "fog publish failed, will retry" in p.stdout
    # each fog build that built for the live run is published once, a stale one never
    assert [e for e in events if e.startswith("fogpublish")] == ["fogpublish R0", "fogpublish R2"]
    assert s["discarded"] == 1 and "discarded" in p.stdout
    assert "hrrr_fog_build.py failed (exit 1), retrying next pass" in p.stdout
    assert (tmp_path / "build_args").read_text().strip() == "--out site-hrrr --live-manifest https://blob.example/manifest.json"
    assert (tmp_path / "fog_args").read_text().strip() == "--out site-hrrr-fog --live-manifest https://blob.example/manifest.json"
    assert (tmp_path / "still_args").read_text().strip() == "--still-live site-hrrr-fog --live-manifest https://blob.example/manifest.json"
    assert set((tmp_path / "node_args").read_text().splitlines()) == {
        "blob.mjs ../site-hrrr token=tok", "blob.mjs ../site-hrrr-fog token=tok"}
    assert int((tmp_path / "fn").read_text()) == fogs_started


def test_cloud_checks_every_pass_while_one_fog_build_runs_and_drain_waits(tmp_path):
    # one fog build outlasts the whole loop: every pass still checks the clouds,
    # no second fog build starts, and the drain waits for it and publishes it
    fogs = [(0, END + 400)]
    p, events = run_loop(tmp_path, [3], fogs)
    want, builds, fogs_started, s = expected_events([3], fogs)
    assert events == want
    assert events.count("build 3") == END // 300 and fogs_started == 1
    assert int((tmp_path / "fn").read_text()) == 1
    assert "sleep 30" in events and events[-1] == "fogpublish R0"


def test_drain_stops_a_fog_build_past_the_budget(tmp_path):
    fogs = [(0, DRAIN + 600)]
    p, events = run_loop(tmp_path, [3], fogs)
    want, builds, fogs_started, s = expected_events([3], fogs)
    assert s["killed"] and events == want
    assert not any(e.startswith("fog") for e in events)   # killed: never finished, never published
    assert "still running at the time budget, stopped" in p.stdout
    assert int((tmp_path / "clock").read_text()) >= DRAIN


def test_workflow_text_pins():
    text = (ROOT / ".github/workflows/hrrr.yml").read_text()
    assert "timeout-minutes: 360" in text
    assert "$(date +%s) + 285 * 60 ))" in text
    assert "5 * 3600" not in text
    assert "cancel-in-progress: false" in text
    assert "actions: write" in text
    assert "secrets.HRRR_BLOB_READ_WRITE_TOKEN" in text
    assert "secrets.BLOB_READ_WRITE_TOKEN" not in text
    assert "timeout -k 10 1200 python hrrr_build.py" in text
    assert "timeout -k 10 1800 python hrrr_fog_build.py --out site-hrrr-fog" in text
    assert text.index("timeout -k 10 1200 python hrrr_build.py") < text.index("timeout -k 10 1800 python hrrr_fog_build.py")
    # the fog build runs in the background and never in the foreground
    assert '--live-manifest "$HRRR_BASE/manifest.json" > fog.log 2>&1 &' in text
    assert text.count("python hrrr_fog_build.py --out") == 1
    redispatch = text.split("name: start the next run")[1]
    assert "steps.loop.conclusion != 'skipped'" in redispatch.split("run:")[0]
