# The AQM loop in .github/workflows/aqm.yml, run for real under bash with
# python, node, sleep and date stubbed (fake clock): a publish follows a build
# that exited 0 and nothing else; every pass sleeps 600 s and removes site-aqm;
# the loop ends at 285 min and a failure never ends it.
import os
import re
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/aqm.yml"
BUILD_RCS = [3, 0, 1, 3]   # 0 = built, 3 = nothing new, else the build failed (the 0 publish fails)


def loop_script():
    lines = WORKFLOW.read_text().splitlines()
    i = next(n for n, line in enumerate(lines) if "name: watch NOAA, build each new AQM run" in line)
    i = next(n for n in range(i, len(lines)) if lines[n].strip() == "run: |")
    indent = len(lines[i]) - len(lines[i].lstrip())
    body = []
    for line in lines[i + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body)) + "\n"


def expected_events():
    events, clock, end, n = [], 0, 285 * 60, 0
    while clock < end:
        rc = BUILD_RCS[n] if n < len(BUILD_RCS) else 3
        n += 1
        events.append(f"build {rc}")
        if rc == 0:
            events.append("publish")
        events.append("sleep 600")
        clock += 600
    return events


def _stub(path, text):
    path.write_text("#!/usr/bin/env bash\n" + text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_loop_publishes_only_on_built_and_never_stops_on_failure(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (tmp_path / "clock").write_text("0\n")
    (tmp_path / "n").write_text("0\n")
    (tmp_path / "rcs").write_text("\n".join(map(str, BUILD_RCS)) + "\n")
    (tmp_path / "publish").mkdir()
    _stub(bin_ / "python", textwrap.dedent("""\
        shift; echo "$*" > "$T/build_args"
        n=$(cat "$T/n"); echo $((n + 1)) > "$T/n"
        rc=$(sed -n "$((n + 1))p" "$T/rcs"); rc=${rc:-3}
        # every pass leaves a folder (even a failed one), so cleanup is observable
        mkdir -p site-aqm
        echo "build $rc" >> "$T/events"; exit "$rc"
        """))
    _stub(bin_ / "node", textwrap.dedent("""\
        echo "$* token=${BLOB_READ_WRITE_TOKEN:-}" >> "$T/node_args"
        echo "publish" >> "$T/events"
        test -d ../site-aqm || exit 98
        n=$(cat "$T/node_n" 2>/dev/null || echo 0); echo $((n + 1)) > "$T/node_n"
        exit 1   # the publish fails
        """))
    _stub(bin_ / "sleep", 'test ! -e site-aqm || exit 97; echo "sleep $1" >> "$T/events"; echo $(( $(cat "$T/clock") + $1 )) > "$T/clock"\n')
    _stub(bin_ / "date", 'if [ "$1" = "+%s" ]; then cat "$T/clock"; else exec /bin/date "$@"; fi\n')
    if not shutil.which("timeout"):   # macOS has no coreutils timeout; CI does
        _stub(bin_ / "timeout", 'shift 3; exec "$@"\n')
    script = tmp_path / "loop.sh"
    script.write_text(loop_script())
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "T": str(tmp_path),
           "AQ_BASE": "https://blob.example", "BLOB_READ_WRITE_TOKEN": "tok"}
    # GitHub runs `run:` steps as: bash --noprofile --norc -eo pipefail {0}
    p = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    events = (tmp_path / "events").read_text().splitlines()
    assert events == expected_events()   # the failed publish and the failed build did not stop the loop
    assert "publish failed, will retry" in p.stdout
    assert "aqm_build.py failed (exit 1), retrying next pass" in p.stdout
    assert (tmp_path / "build_args").read_text().strip() == "--out site-aqm --live-manifest https://blob.example/manifest.json"
    assert (tmp_path / "node_args").read_text().splitlines() == ["blob.mjs ../site-aqm token=tok"]
    assert not (tmp_path / "site-aqm").exists()   # removed after every pass, before the sleep


def test_workflow_text_pins():
    text = WORKFLOW.read_text()
    assert 'cron: "53 */4 * * *"' in text
    assert 'cron: "41 * * * *"' not in text
    assert "workflow_dispatch:" in text
    assert "timeout-minutes: 360" in text
    assert "$(date +%s) + 285 * 60 ))" in text
    assert "actions: write" in text
    assert re.search(r"concurrency:\s*\n\s*group: aqm\n\s*cancel-in-progress: false", text)
    assert "runs-on: ubuntu-24.04" in text
    assert "python -m pytest -q" in text
    assert "https://u6zo6dgnfgsj7bwo.public.blob.vercel-storage.com" in text
    assert "timeout -k 10 2400 python aqm_build.py" in text
    assert "sleep 600" in text
    assert set(re.findall(r"secrets\.(\w+)", text)) == {"AQ_BLOB_READ_WRITE_TOKEN"}
    redispatch = text.split("name: start the next run")[1]
    assert "steps.loop.conclusion != 'skipped'" in redispatch.split("run:")[0]
    assert "GH_TOKEN: ${{ github.token }}" in redispatch
    assert "gh workflow run aqm.yml --ref main" in redispatch
