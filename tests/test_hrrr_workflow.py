# The HRRR loop in .github/workflows/hrrr.yml, run for real under bash with
# python, node, sleep and date stubbed (fake clock): a publish follows a build
# that exited 0 and nothing else; every pass sleeps 300 s; the loop ends at 285 min.
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_RCS = [3, 0, 3, 1, 0]   # 0 = built, 3 = nothing to do, else the build failed
BUILD = "hrrr_build.py --out site-hrrr --live-manifest https://blob.example/manifest.json"
PUBLISH = "node blob.mjs ../site-hrrr"
FOG_RCS = [0, 3, 1, 0, 3]   # the fog build's exit code on each pass that runs it (cloud rc 0 or 3)
FOG_BUILD = "hrrr_fog_build.py --out site-hrrr-fog --live-manifest https://blob.example/manifest.json"


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


def expected_events():
    events, clock, end, n, f = [], 0, 285 * 60, 0, 0
    while clock < end:
        rc = BUILD_RCS[n] if n < len(BUILD_RCS) else 3
        n += 1
        events.append(f"build {rc}")
        if rc == 0:
            events.append("publish")
        if rc in (0, 3):   # the fog build follows the cloud build on built and on nothing-new
            frc = FOG_RCS[f] if f < len(FOG_RCS) else 3
            f += 1
            events.append(f"fogbuild {frc}")
            if frc == 0:
                events.append("fogpublish")
        events.append("sleep 300")
        clock += 300
    return events


def _stub(path, text):
    path.write_text("#!/usr/bin/env bash\n" + text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_loop_publishes_only_on_built(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (tmp_path / "clock").write_text("0\n")
    (tmp_path / "n").write_text("0\n")
    (tmp_path / "rcs").write_text("\n".join(map(str, BUILD_RCS)) + "\n")
    (tmp_path / "fogrcs").write_text("\n".join(map(str, FOG_RCS)) + "\n")
    (tmp_path / "publish").mkdir()
    _stub(bin_ / "python", textwrap.dedent("""\
        if [ "$1" = "hrrr_build.py" ]; then
          shift; echo "$*" > "$T/build_args"
          n=$(cat "$T/n"); echo $((n + 1)) > "$T/n"
          rc=$(sed -n "$((n + 1))p" "$T/rcs"); rc=${rc:-3}
          if [ "$rc" = "0" ]; then mkdir -p site-hrrr; fi
          echo "build $rc" >> "$T/events"; exit "$rc"
        fi
        if [ "$1" = "hrrr_fog_build.py" ]; then
          shift; echo "$*" > "$T/fog_args"
          n=$(cat "$T/fn" 2>/dev/null || echo 0); echo $((n + 1)) > "$T/fn"
          rc=$(sed -n "$((n + 1))p" "$T/fogrcs"); rc=${rc:-3}
          # a built run leaves a folder; a rc 3 may leave stray files (the live run moved)
          if [ "$rc" = "0" ] || [ "$rc" = "3" ]; then mkdir -p site-hrrr-fog; fi
          echo "fogbuild $rc" >> "$T/events"; exit "$rc"
        fi
        exit 99
        """))
    _stub(bin_ / "node", textwrap.dedent("""\
        echo "$* token=${BLOB_READ_WRITE_TOKEN:-}" >> "$T/node_args"
        if [ "$2" = "../site-hrrr-fog" ]; then
          echo "fogpublish" >> "$T/events"
          test -d ../site-hrrr-fog || exit 98
          n=$(cat "$T/fnode_n" 2>/dev/null || echo 0); echo $((n + 1)) > "$T/fnode_n"
          [ "$n" = "0" ] && exit 1   # the first fog publish fails
          exit 0
        fi
        echo "publish" >> "$T/events"
        test -d ../site-hrrr || exit 98
        n=$(cat "$T/node_n" 2>/dev/null || echo 0); echo $((n + 1)) > "$T/node_n"
        [ "$n" = "0" ] && exit 1   # the first publish fails
        exit 0
        """))
    _stub(bin_ / "sleep", 'echo "sleep $1" >> "$T/events"; echo $(( $(cat "$T/clock") + $1 )) > "$T/clock"\n')
    _stub(bin_ / "date", 'if [ "$1" = "+%s" ]; then cat "$T/clock"; else exec /bin/date "$@"; fi\n')
    if not shutil.which("timeout"):   # macOS has no coreutils timeout; CI does
        _stub(bin_ / "timeout", 'shift 3; exec "$@"\n')
    script = tmp_path / "loop.sh"
    script.write_text(loop_script())
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "T": str(tmp_path),
           "HRRR_BASE": "https://blob.example", "BLOB_READ_WRITE_TOKEN": "tok"}
    # GitHub runs `run:` steps as: bash --noprofile --norc -eo pipefail {0}
    p = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    events = (tmp_path / "events").read_text().splitlines()
    assert events == expected_events()   # the failed first publish did not stop the loop
    assert "publish failed, will retry" in p.stdout
    assert (tmp_path / "build_args").read_text().strip() == "--out site-hrrr --live-manifest https://blob.example/manifest.json"
    assert "fog publish failed, will retry" in p.stdout
    assert (tmp_path / "fog_args").read_text().strip() == "--out site-hrrr-fog --live-manifest https://blob.example/manifest.json"
    assert set((tmp_path / "node_args").read_text().splitlines()) == {
        "blob.mjs ../site-hrrr token=tok", "blob.mjs ../site-hrrr-fog token=tok"}
    assert not (tmp_path / "site-hrrr-fog").exists()   # removed after every attempt, whatever the rc
    assert not (tmp_path / "site-hrrr").exists()   # removed after the last publish pass or never left behind


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
    assert text.index("timeout -k 10 1200 python hrrr_build.py") < text.index("hrrr_fog_build.py")
    redispatch = text.split("name: start the next run")[1]
    assert "steps.loop.conclusion != 'skipped'" in redispatch.split("run:")[0]
