import datetime as dt
import json
from pipeline.site import build_manifest, ATTRIBUTION

UTC = dt.timezone.utc


def test_manifest_shape():
    run = dt.datetime(2026, 9, 27, 0, tzinfo=UTC)
    built = dt.datetime(2026, 9, 27, 3, 5, tzinfo=UTC)
    m = json.loads(build_manifest(run, built, step_hours=[496_740, 497_352, 496_753],
                                  squares=["N40W125", "N40W130"], history_hours=1))
    assert m["run"] == "2026-09-27T00Z"
    assert m["runMs"] == 497_352 * 3600 * 1000
    assert m["builtMs"] == int(built.timestamp() * 1000)
    assert m["deg"] == 0.125 and m["square"] == 5 and m["side"] == 41
    assert m["stepsMs"] == [h * 3600 * 1000 for h in [496_740, 497_352, 496_753]]
    assert m["historySteps"] == 1
    assert m["squares"] == ["N40W125", "N40W130"]
    assert m["tiles"] == "tiles/2026092700"
    assert m["attribution"] == ATTRIBUTION
    assert m["complete"] is True


def test_a_partial_build_says_so():
    run = dt.datetime(2026, 9, 27, 0, tzinfo=UTC)
    m = json.loads(build_manifest(run, run, [1], ["N40W125"], 0, complete=False))
    assert m["complete"] is False


def test_squares_are_sorted_and_unique():
    run = dt.datetime(2026, 9, 27, 0, tzinfo=UTC)
    m = json.loads(build_manifest(run, run, [1], ["N40W130", "N40W125", "N40W125"], 0))
    assert m["squares"] == ["N40W125", "N40W130"]


def test_manifest_carries_the_tail_or_null():
    import datetime as dt, json
    from pipeline.site import build_manifest, tail_path
    run = dt.datetime(2026, 9, 30, 6, tzinfo=dt.timezone.utc)
    built = dt.datetime(2026, 9, 30, 10, tzinfo=dt.timezone.utc)
    tail_run = dt.datetime(2026, 9, 30, 0, tzinfo=dt.timezone.utc)
    assert tail_path(tail_run) == "tail/2026093000"
    m = json.loads(build_manifest(run, built, [1, 2], ["N40W125"], 0))
    assert m["tail"] is None
    tail = {"run": "2026-09-30T00Z", "runMs": 1, "stepsMs": [2], "tiles": "tail/2026093000"}
    m = json.loads(build_manifest(run, built, [1, 2], ["N40W125"], 0, tail=tail))
    assert m["tail"] == tail
