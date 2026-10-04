# The tick loop in .github/workflows/tick.yml, run for real under bash with
# python, gh, sleep and date stubbed (fake clock): what it dispatches and how
# long it sleeps must not depend on what the watchdog does.
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TICK_RCS = [3, 3, 0, 3, 1, 3, 0, 0, 3]   # 0 = new run, 3 = current, else tick.py broke


def loop_script():
    lines = (ROOT / ".github/workflows/tick.yml").read_text().splitlines()
    i = next(n for n, line in enumerate(lines) if "name: watch DWD, build on a new run" in line)
    i = next(n for n in range(i, len(lines)) if lines[n].strip() == "run: |")
    indent = len(lines[i]) - len(lines[i].lstrip())
    body = []
    for line in lines[i + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body)) + "\n"


def expected_events():
    """What the loop did before the watchdog: tick.py, dispatch on 0, sleep."""
    events, clock, end, n = [], 0, 5 * 3600, 0
    while clock < end:
        rc = TICK_RCS[n] if n < len(TICK_RCS) else 3
        n += 1
        events.append(f"tick {rc}")
        if rc == 0:
            events.append("gh workflow run build.yml --ref main")
        events.append(f"sleep {1800 if rc == 0 else 600}")
        clock += 1800 if rc == 0 else 600
    return events


def _stub(path, text):
    path.write_text("#!/usr/bin/env bash\n" + text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_loop(tmp_path, wd_mode):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (tmp_path / "clock").write_text("0\n")
    (tmp_path / "tick_n").write_text("0\n")
    (tmp_path / "tick_rcs").write_text("\n".join(map(str, TICK_RCS)) + "\n")
    _stub(bin_ / "python", textwrap.dedent("""\
        case "$1" in
          tick.py)
            n=$(cat "$T/tick_n"); echo $((n + 1)) > "$T/tick_n"
            rc=$(sed -n "$((n + 1))p" "$T/tick_rcs"); rc=${rc:-3}
            echo "tick $rc" >> "$T/events"; exit "$rc" ;;
          alerts_watchdog.py)
            echo "$*" >> "$T/watchdog"
            case "$WD_MODE" in
              ok) echo "watchdog: ok"; exit 0 ;;
              crash) echo "Traceback: boom" >&2; exit 1 ;;
              missing) echo "python: can't open file 'alerts_watchdog.py'" >&2; exit 2 ;;
              killed) kill -9 $$ ;;
            esac ;;
        esac
        exit 99
        """))
    _stub(bin_ / "gh", 'echo "gh $*" >> "$T/events"\n')
    _stub(bin_ / "sleep", 'echo "sleep $1" >> "$T/events"; echo $(( $(cat "$T/clock") + $1 )) > "$T/clock"\n')
    _stub(bin_ / "date", 'if [ "$1" = "+%s" ]; then cat "$T/clock"; else exec /bin/date "$@"; fi\n')
    if not shutil.which("timeout"):   # macOS has no coreutils timeout; CI does
        _stub(bin_ / "timeout", 'shift 3; exec "$@"\n')
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir()
    (runner_temp / "alerts-watchdog.json").write_text('{"state": "down"}')   # a stale state must not survive
    (runner_temp / "tiles-watchdog.json").write_text('{"state": "down"}')
    script = tmp_path / "loop.sh"
    script.write_text(loop_script())
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "T": str(tmp_path), "WD_MODE": wd_mode,
           "GITHUB_REPOSITORY": "eriknugshots/skycast-icon-pipeline", "RUNNER_TEMP": str(runner_temp)}
    # GitHub runs `run:` steps as: bash --noprofile --norc -eo pipefail {0}
    p = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    read = lambda name: (tmp_path / name).read_text().splitlines() if (tmp_path / name).exists() else []
    events, watchdog = read("events"), read("watchdog")
    return p, events, watchdog, runner_temp


@pytest.mark.parametrize("wd_mode", ["ok", "crash", "missing", "killed"])
def test_the_tick_loop_does_the_same_whatever_the_watchdog_does(tmp_path, wd_mode):
    p, events, watchdog, runner_temp = run_loop(tmp_path, wd_mode)
    assert p.returncode == 0, p.stderr
    assert events == expected_events()
    wakes = sum(e.startswith("tick ") for e in events)
    # two checks per wake, started together in the background, so their order is not fixed
    alerts = f"alerts_watchdog.py --state-file {runner_temp}/alerts-watchdog.json"
    tiles = (f"alerts_watchdog.py --state-file {runner_temp}/tiles-watchdog.json"
             " --health-url https://sunset-prediction.vercel.app/api/tiles-health"
             " --name tile keys --fields ok,newestKeyAgeH,lastRunAgeH,lastErrorKind")
    assert sorted(watchdog) == sorted([alerts, tiles] * wakes)
    if wd_mode != "ok":
        assert "watchdog: gave up (exit " in p.stdout


def test_the_loop_starts_each_tick_without_watchdog_state(tmp_path):
    _, _, _, runner_temp = run_loop(tmp_path, "ok")
    assert not (runner_temp / "alerts-watchdog.json").exists()
    assert not (runner_temp / "tiles-watchdog.json").exists()


def test_the_watchdog_step_gets_the_key_from_the_repo_secret():
    text = (ROOT / ".github/workflows/tick.yml").read_text()
    assert "RESEND_ALERTS_KEY: ${{ secrets.RESEND_ALERTS_KEY }}" in text
    assert "timeout -k 5 55 python alerts_watchdog.py" in text
