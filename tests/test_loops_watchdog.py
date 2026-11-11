# .github/workflows/loops-watchdog.yml: covers every self-restarting loop, runs
# hourly, and (run for real under bash with gh stubbed) dispatches only a loop
# whose listing says it has no live run, never one whose listing failed.
import os
import re
import stat
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github/workflows"
TEXT = (WF / "loops-watchdog.yml").read_text()


def loops_env():
    return re.search(r"LOOPS: (.+)", TEXT).group(1).split()


def script():
    lines = TEXT.splitlines()
    i = next(n for n, line in enumerate(lines) if line.strip() == "run: |")
    indent = len(lines[i]) - len(lines[i].lstrip())
    body = []
    for line in lines[i + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body)) + "\n"


def test_covers_every_self_restarting_loop():
    selfloops = sorted(
        p.stem for p in WF.glob("*.yml")
        if f"gh workflow run {p.stem}.yml" in p.read_text()
    )
    assert selfloops == sorted(loops_env())
    assert sorted(loops_env()) == ["aifs", "aqm", "gfs", "hrrr", "ifs", "rrfs", "tick"]


def test_hourly_with_dispatch_permission():
    assert 'cron: "7 * * * *"' in TEXT
    assert "actions: write" in TEXT
    assert "workflow_dispatch:" in TEXT


def test_dispatches_only_dead_loops(tmp_path):
    # gfs: no live run -> restart. aifs: listing fails -> left alone.
    # hrrr: dispatch fails -> logged, the rest still checked.
    stub = tmp_path / "gh"
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        log={tmp_path}/calls
        if [ "$1 $2" = "run list" ]; then
          case "$4" in
            gfs.yml|hrrr.yml) echo 0 ;;
            aifs.yml) exit 1 ;;
            *) echo 1 ;;
          esac
        elif [ "$1 $2" = "workflow run" ]; then
          echo "dispatch $3 $4 $5" >> "$log"
          [ "$3" = hrrr.yml ] && exit 1
          exit 0
        fi
        """))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}",
               LOOPS=" ".join(loops_env()))
    out = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script()], env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert sorted(calls) == ["dispatch gfs.yml --ref main", "dispatch hrrr.yml --ref main"]
    assert "aifs: listing failed" in out.stdout
    assert "hrrr: dispatch failed" in out.stdout
