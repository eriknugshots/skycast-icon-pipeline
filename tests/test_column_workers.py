from run_build import column_fetch_workers


def test_column_fetch_runs_sixteen_downloads_by_default():
    assert column_fetch_workers({}) == 16


def test_column_fetch_workers_can_be_set_and_never_drop_below_one():
    assert column_fetch_workers({"COLUMN_FETCH_WORKERS": "24"}) == 24
    assert column_fetch_workers({"COLUMN_FETCH_WORKERS": "0"}) == 1
    assert column_fetch_workers({"COLUMN_FETCH_WORKERS": "lots"}) == 16
