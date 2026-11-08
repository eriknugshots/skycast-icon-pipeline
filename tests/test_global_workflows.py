# The GFS and AIFS loops in .github/workflows/{gfs,aifs}.yml, run for real under
# bash with python, node, git, sleep and date stubbed (fake clock): a publish and
# a state push follow a build that exited 0 (and a publish that worked) and
# nothing else; every pass sleeps 600 s; the loop ends at 5 h.
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILD_RCS = [3, 0, 1, 0]   # 0 = built, 3 = nothing to do, else the build failed
FEEDS = {
    "gfs": dict(other="aifs", step="watch GFS, build each new run", base="GFS_BASE",
                secret="GFS_BLOB_READ_WRITE_TOKEN", branch="state-gfs"),
    "aifs": dict(other="gfs", step="watch AIFS, build each new run", base="AIFS_BASE",
                 secret="AIFS_BLOB_READ_WRITE_TOKEN", branch="state-aifs"),
}


def loop_script(feed):
    lines = (ROOT / f".github/workflows/{feed}.yml").read_text().splitlines()
    i = next(n for n, line in enumerate(lines) if f"name: {FEEDS[feed]['step']}" in line)
    i = next(n for n in range(i, len(lines)) if lines[n].strip() == "run: |")
    indent = len(lines[i]) - len(lines[i].lstrip())
    body = []
    for line in lines[i + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body)) + "\n"


def expected_events(fail_first_publish=False):
    events, clock, end, n, pubs = [], 0, 5 * 3600, 0, 0
    while clock < end:
        rc = BUILD_RCS[n] if n < len(BUILD_RCS) else 3
        n += 1
        events.append(f"build {rc}")
        if rc == 0:
            events.append("publish")
            ok = not (fail_first_publish and pubs == 0)
            pubs += 1
            if ok:
                events.append("statepush")
        events.append("sleep 600")
        clock += 600
    return events


def _stub(path, text):
    path.write_text("#!/usr/bin/env bash\n" + text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_loop(tmp_path, feed, fail_first_publish=False, keep_state=""):
    f = FEEDS[feed]
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (tmp_path / "clock").write_text("0\n")
    (tmp_path / "n").write_text("0\n")
    (tmp_path / "rcs").write_text("\n".join(map(str, BUILD_RCS)) + "\n")
    (tmp_path / "publish").mkdir()
    (tmp_path / "state").mkdir()
    _stub(bin_ / "python", textwrap.dedent(f"""\
        if [ "$1" = "{feed}_build.py" ]; then
          shift; echo "$*" > "$T/build_args"
          n=$(cat "$T/n"); echo $((n + 1)) > "$T/n"
          rc=$(sed -n "$((n + 1))p" "$T/rcs"); rc=${{rc:-3}}
          if [ "$rc" = "0" ]; then mkdir -p site-{feed}; fi
          echo "build $rc" >> "$T/events"; exit "$rc"
        fi
        exit 99
        """))
    _stub(bin_ / "node", textwrap.dedent(f"""\
        echo "publish" >> "$T/events"
        echo "$* token=${{BLOB_READ_WRITE_TOKEN:-}}" >> "$T/node_args"
        test -d ../site-{feed} || exit 98
        n=$(cat "$T/node_n" 2>/dev/null || echo 0); echo $((n + 1)) > "$T/node_n"
        [ "$n" = "0" ] && [ "$FAIL_FIRST" = "1" ] && exit 1
        exit 0
        """))
    _stub(bin_ / "git", textwrap.dedent(f"""\
        case " $* " in
          *" push "*) echo "statepush" >> "$T/events"; echo "$*" >> "$T/git_push"; pwd >> "$T/git_pwd" ;;
        esac
        exit 0
        """))
    _stub(bin_ / "sleep", 'echo "sleep $1" >> "$T/events"; echo $(( $(cat "$T/clock") + $1 )) > "$T/clock"\n')
    _stub(bin_ / "date", 'if [ "$1" = "+%s" ]; then cat "$T/clock"; else exec /bin/date "$@"; fi\n')
    if not shutil.which("timeout"):   # macOS has no coreutils timeout; CI does
        _stub(bin_ / "timeout", 'shift 3; exec "$@"\n')
    script = tmp_path / "loop.sh"
    script.write_text(loop_script(feed))
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "T": str(tmp_path),
           f["base"]: "https://blob.example", "BLOB_READ_WRITE_TOKEN": "tok",
           "GITHUB_TOKEN": "gh", "GITHUB_REPOSITORY": "o/r",
           "FAIL_FIRST": "1" if fail_first_publish else "0", "STATE_KEEP": keep_state}
    # GitHub runs `run:` steps as: bash --noprofile --norc -eo pipefail {0}
    return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)


def _events(tmp_path):
    return (tmp_path / "events").read_text().splitlines()


@pytest.mark.parametrize("feed", ["gfs", "aifs"])
def test_loop_publishes_and_pushes_state_only_on_built(tmp_path, feed):
    p = run_loop(tmp_path, feed)
    assert p.returncode == 0, p.stderr
    assert _events(tmp_path) == expected_events()
    assert (tmp_path / "build_args").read_text().strip() == \
        f"--out site-{feed} --state state --live-manifest https://blob.example/manifest.json"
    assert set((tmp_path / "node_args").read_text().splitlines()) == {f"blob.mjs ../site-{feed} token=tok"}
    assert FEEDS[feed]["branch"] in (tmp_path / "git_push").read_text().splitlines()[0]
    assert "--force" in (tmp_path / "git_push").read_text().splitlines()[0]
    assert not (tmp_path / f"site-{feed}").exists()


@pytest.mark.parametrize("feed", ["gfs", "aifs"])
def test_failed_publish_does_not_push_state_or_end_loop(tmp_path, feed):
    p = run_loop(tmp_path, feed, fail_first_publish=True)
    assert p.returncode == 0, p.stderr
    assert _events(tmp_path) == expected_events(fail_first_publish=True)
    assert "publish failed, will retry" in p.stdout
    assert not (tmp_path / f"site-{feed}").exists()


@pytest.mark.parametrize("feed", ["gfs", "aifs"])
def test_unfetchable_state_branch_is_never_overwritten(tmp_path, feed):
    p = run_loop(tmp_path, feed, keep_state="1")
    assert p.returncode == 0, p.stderr
    assert "statepush" not in _events(tmp_path)
    assert "publish" in _events(tmp_path)


@pytest.mark.parametrize("feed", ["gfs", "aifs"])
def test_workflow_text_pins(feed):
    f = FEEDS[feed]
    text = (ROOT / f".github/workflows/{feed}.yml").read_text()
    assert f"name: {feed}\n" in text
    assert "cancel-in-progress: false" in text
    assert "actions: write" in text and "contents: write" in text
    assert f"secrets.{f['secret']}" in text
    assert "secrets.BLOB_READ_WRITE_TOKEN" not in text
    assert "HRRR" not in text and "hrrr" not in text
    assert f["other"].upper() not in text and f["other"] not in text
    assert f["branch"] in text
    assert f"timeout -k 10 2700 python {feed}_build.py" in text
    assert f'--live-manifest "${f["base"]}/manifest.json"' in text
    assert f"gh workflow run {feed}.yml --ref main" in text
    redispatch = text.split("name: start the next run")[1]
    assert "steps.loop.conclusion != 'skipped'" in redispatch.split("run:")[0]
