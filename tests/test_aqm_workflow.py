# The AQM step in .github/workflows/aqm.yml, run for real under bash with
# python, node and timeout stubbed: a publish follows a build that exited 0
# and nothing else; exit 3 is success without publishing; any other code fails.
import os
import re
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/aqm.yml"


def step_script():
    lines = WORKFLOW.read_text().splitlines()
    i = next(n for n, line in enumerate(lines) if "name: build the newest AQM run" in line)
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


def run_step(tmp_path, rc):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (tmp_path / "publish").mkdir()
    _stub(bin_ / "python", textwrap.dedent(f"""\
        echo "$*" > "$T/build_args"
        if [ "{rc}" = "0" ]; then mkdir -p site-aqm; fi
        exit {rc}
        """))
    _stub(bin_ / "node", textwrap.dedent("""\
        echo "$* token=${BLOB_READ_WRITE_TOKEN:-}" >> "$T/node_args"
        test -d ../site-aqm || exit 98
        """))
    if not shutil.which("timeout"):   # macOS has no coreutils timeout; CI does
        _stub(bin_ / "timeout", 'shift 3; exec "$@"\n')
    script = tmp_path / "step.sh"
    script.write_text(step_script())
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "T": str(tmp_path),
           "AQ_BASE": "https://blob.example", "BLOB_READ_WRITE_TOKEN": "tok"}
    # GitHub runs `run:` steps as: bash --noprofile --norc -eo pipefail {0}
    return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)


def test_built_publishes_with_the_aqm_token(tmp_path):
    p = run_step(tmp_path, 0)
    assert p.returncode == 0, p.stderr
    assert (tmp_path / "build_args").read_text().strip().endswith(
        "aqm_build.py --out site-aqm --live-manifest https://blob.example/manifest.json")
    assert (tmp_path / "node_args").read_text().splitlines() == ["blob.mjs ../site-aqm token=tok"]


def test_nothing_new_is_success_and_does_not_publish(tmp_path):
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
    assert 'cron: "41 * * * *"' in text
    assert "workflow_dispatch:" in text
    assert re.search(r"concurrency:\s*\n\s*group: aqm\n\s*cancel-in-progress: false", text)
    assert "runs-on: ubuntu-24.04" in text
    assert "python -m pytest -q" in text
    assert "https://u6zo6dgnfgsj7bwo.public.blob.vercel-storage.com" in text
    assert "timeout -k 10 2400 python aqm_build.py" in text
    assert "actions: write" not in text            # no self-restarting chain
    assert "gh workflow run" not in text
    assert set(re.findall(r"secrets\.(\w+)", text)) == {"AQ_BLOB_READ_WRITE_TOKEN"}
