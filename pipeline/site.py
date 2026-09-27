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


def build_manifest(run, built, step_hours, squares, history_hours):
    out = {
        "run": run_iso(run),
        "runMs": int(run.timestamp() * 1000),
        "builtMs": int(built.timestamp() * 1000),
        "deg": DEG, "square": SQUARE_DEG, "side": SIDE,
        "stepsMs": [int(h) * 3600 * 1000 for h in step_hours],
        "historySteps": int(history_hours),
        "squares": sorted(set(squares)),
        "tiles": tiles_path(run),
        "attribution": ATTRIBUTION,
    }
    return json.dumps(out, separators=(",", ":"))
