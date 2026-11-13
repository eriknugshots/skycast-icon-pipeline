# .github/workflows/texdev.yml: the cloud-texture dev feed. It runs only when
# dispatched by hand on the cloud-texture branch, publishes only under texdev/
# in the ICON Blob store (never blob.mjs, which writes the live manifest.json
# and prunes the live feed), and keeps its history on its own state branch so
# it can never read or overwrite the live build's state.
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github/workflows"
TEXT = (WF / "texdev.yml").read_text()
LIVE = (WF / "build.yml").read_text()
# The workflow without its comments (they name what it must never run).
CODE = "\n".join(line for line in TEXT.splitlines() if not line.lstrip().startswith("#"))


def top_block(text, key):
    """The lines of a top-level YAML block (`key:` at column 0), without the key line."""
    lines = text.splitlines()
    i = next(n for n, line in enumerate(lines) if line.rstrip() == f"{key}:")
    out = []
    for line in lines[i + 1:]:
        if line.strip() and not line.startswith((" ", "#")):
            break
        out.append(line)
    return out


def step_script(name):
    """The `run: |` body of the step whose name starts with `name`."""
    lines = TEXT.splitlines()
    i = next(n for n, line in enumerate(lines) if re.match(rf"\s*- name: {re.escape(name)}", line))
    i = next(n for n in range(i, len(lines)) if lines[n].strip() == "run: |")
    indent = len(lines[i]) - len(lines[i].lstrip())
    body = []
    for line in lines[i + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body)) + "\n"


def steps():
    """Each step's text, in order."""
    lines = TEXT.splitlines()
    starts = [n for n, line in enumerate(lines) if re.match(r"\s{6}- ", line)]
    return ["\n".join(lines[a:b]) for a, b in zip(starts, starts[1:] + [len(lines)])]


def test_dispatch_only():
    triggers = [line.strip() for line in top_block(TEXT, "on") if line.strip() and not line.strip().startswith("#")]
    assert triggers == ["workflow_dispatch:"]
    for word in ("schedule:", "push:", "pull_request", "workflow_run", "repository_dispatch", "cron:"):
        assert word not in CODE, word


def test_first_step_refuses_any_branch_but_cloud_texture(tmp_path):
    first = steps()[0]
    assert "name: refuse unless on cloud-texture" in first
    script = step_script("refuse unless on cloud-texture")
    assert "refs/heads/cloud-texture" in script
    assert "REF: ${{ github.ref }}" in first
    for ref, ok in (("refs/heads/cloud-texture", True), ("refs/heads/main", False),
                    ("refs/heads/cloud-texture-2", False), ("refs/tags/cloud-texture", False), ("", False)):
        out = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                             env=dict(os.environ, REF=ref), capture_output=True, text=True)
        assert (out.returncode == 0) == ok, (ref, out.returncode, out.stdout, out.stderr)


def test_runs_the_texdev_build_only():
    assert "python texdev_build.py" in CODE
    assert "run_build.py" not in CODE


def test_publishes_only_through_the_texdev_publisher():
    # blob.mjs uploads manifest.json at the store's root and deletes every folder
    # the live manifest does not name: it must never run here.
    assert "blob.mjs" not in CODE
    assert "prune" not in CODE
    assert re.findall(r"node (\S+\.mjs)", CODE) == ["texdev.mjs"]
    script = step_script("publish texdev/")
    assert "node texdev.mjs ../blob-texdev" in script
    assert "--out-dir blob-texdev" in step_script("build")
    assert "BLOB_READ_WRITE_TOKEN: ${{ secrets.BLOB_READ_WRITE_TOKEN }}" in TEXT


def test_state_lives_on_its_own_branch():
    live = set(re.findall(r"--heads origin (\S+)", LIVE))
    assert live == {"state"}
    named = set(re.findall(r"--heads origin (\S+)", TEXT)) | set(re.findall(r"fetch --depth 1 origin (\S+)", TEXT)) \
        | set(re.findall(r"worktree add \S+ origin/(\S+)", TEXT)) | set(re.findall(r"git init -b (\S+)", TEXT)) \
        | set(re.findall(r"\.git\"? (\S+)\s*$", TEXT, re.M))
    # Every branch the workflow names on origin, however it names it.
    named |= set(re.findall(r"origin/(\S+)", CODE)) | set(re.findall(r"\borigin (\S+)", CODE))
    assert named == {"state-texdev"}
    assert not named & live
    # Every push names the texdev state branch, and nothing else is pushed.
    pushes = re.findall(r"git push.*", TEXT)
    assert pushes and all(re.search(r"\.git\"? state-texdev$", p.rstrip()) for p in pushes)
    assert "gh-pages" not in TEXT
    # The build reads and writes that checkout, not the live state dir.
    assert "--state-dir state-texdev" in step_script("build")
    assert "worktree add state-texdev origin/state-texdev" in TEXT


def test_two_dispatches_never_overlap():
    block = "\n".join(top_block(TEXT, "concurrency"))
    assert re.search(r"group: texdev\s*$", block, re.M)
    assert "cancel-in-progress: false" in block
    live_group = re.search(r"group: (\S+)", "\n".join(top_block(LIVE, "concurrency"))).group(1)
    assert live_group != "texdev"


def test_never_dispatches_anything():
    # Not a self-restarting loop (loops-watchdog would then restart it hourly).
    assert "gh workflow run" not in TEXT
    assert "actions: write" not in CODE


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_publisher_uploads_and_deletes_only_under_texdev():
    out = subprocess.run(["node", "--test", "texdev.test.mjs"], cwd=ROOT / "publish", capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
