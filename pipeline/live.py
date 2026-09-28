# pipeline/live.py — what the published site carries (stdlib only, so the
# tick watcher can ask without installing numpy or cdo).
import json


def live_complete_run(fetch_text, pages_base):
    """The run the live site carries COMPLETELY, as its iso string, or None.
    A test build (some steps, some squares) publishes the same run id with
    complete=false and must not stop the next tick from building it whole."""
    if not pages_base:
        return None
    try:
        m = json.loads(fetch_text(pages_base.rstrip("/") + "/manifest.json"))
        return m.get("run") if m.get("complete") else None
    except Exception:
        return None
