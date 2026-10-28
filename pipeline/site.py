# pipeline/site.py — manifest.json, the one file the app reads first.
#
# The live site is the state (an orphan gh-pages commit per build). The app
# reads run/runMs to know whether anything changed, stepsMs to place every
# sample in time, squares to know what exists, and tiles for the path.
import json
from .dwd import run_id, run_iso
from .squares import DEG, SQUARE_DEG, SIDE

ATTRIBUTION = "Cloud data: Deutscher Wetterdienst (DWD), CC BY 4.0"


def tiles_path(run):
    return f"tiles/{run_id(run)}"


def tail_path(run):
    return f"tail/{run_id(run)}"


def columns_path(run):
    return f"columns/{run_id(run)}"


def blocks_path(run):
    return f"blocks/{run_id(run)}"


def thick_path(run):
    return f"thick/{run_id(run)}"


def build_manifest(run, built, step_hours, squares, history_hours, complete=True, tail=None, columns=None,
                   blocks=None, thick=None, repairs=0):
    out = {
        "run": run_iso(run),
        # False for a test build (some steps, some squares). Only a complete
        # live manifest lets the next tick skip the run: the smoke pipeline
        # once froze a half-uploaded cycle for six hours on an id match.
        "complete": bool(complete),
        "runMs": int(run.timestamp() * 1000),
        "builtMs": int(built.timestamp() * 1000),
        "deg": DEG, "square": SQUARE_DEG, "side": SIDE,
        "stepsMs": [int(h) * 3600 * 1000 for h in step_hours],
        "historySteps": int(history_hours),
        "squares": sorted(set(squares)),
        "tiles": tiles_path(run),
        # Hours 123-144 of the newest whole 00Z/12Z run, for the app's days
        # 4-5 ({run, runMs, stepsMs, tiles}), or None. See run_build.build_tail.
        "tail": tail,
        # The column feed (fog and inversion profiles, ICC1 — see
        # docs/column-feed-design.md): {format, run, runMs, stepsMs,
        # historySteps, spacing, side, tiles}, or None. Only the Vercel Blob
        # copy of the manifest names it — the columns are not on Pages.
        # Builds that predate it never read this key.
        "columns": columns,
        # The same squares as day blocks (ICL2, see pipeline/blocks.py): the
        # folder under which <square>.icb lives, or None. Blob only, like the
        # columns; the ICL1 files above stay for the builds that read them.
        "blocks": blocks,
        # Cloud thickness (ICT1, cloud ice and water per square on the same
        # hours as the squares): {format, run, runMs, tiles}, or None. Blob
        # only as well. See run_build.build_thick.
        "thick": thick,
        # How many times this run was rebuilt because it went live with a
        # family missing (pipeline/live.py MAX_REPAIRS).
        "repairs": int(repairs),
        "attribution": ATTRIBUTION,
    }
    return json.dumps(out, separators=(",", ":"))
