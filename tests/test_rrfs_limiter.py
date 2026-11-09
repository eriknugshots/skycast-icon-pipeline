import threading

from rrfs.build import RateLimiter, limited


class Clock:
    def __init__(self):
        self.t, self.slept = 1000.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_first_requests_go_straight_through():
    c = Clock()
    lim = RateLimiter(5, window=60.0, clock=c.now, sleep=c.sleep)
    for _ in range(5):
        lim.acquire()
    assert c.slept == []


def test_the_sixth_waits_for_the_first_to_leave_the_window():
    c = Clock()
    lim = RateLimiter(5, window=60.0, clock=c.now, sleep=c.sleep)
    for i in range(5):
        lim.acquire()
        c.t += 1.0          # requests at 1000, 1001, ... 1004; now 1005
    lim.acquire()
    # the first request (1000) leaves the window at 1060: wait 1060 - 1005 = 55
    assert c.slept == [55.0]
    assert c.t == 1060.0


def test_never_more_than_the_limit_in_any_window():
    c = Clock()
    lim = RateLimiter(7, window=60.0, clock=c.now, sleep=c.sleep)
    stamps = []
    for _ in range(40):
        lim.acquire()
        stamps.append(c.t)
        c.t += 0.5
    for i, s in enumerate(stamps):
        assert sum(1 for x in stamps if s - 60.0 < x <= s) <= 7


def test_limited_get_counts_every_call_and_passes_through():
    c = Clock()
    lim = RateLimiter(2, window=60.0, clock=c.now, sleep=c.sleep)
    seen = []
    g = limited(lambda url, rng=None: seen.append((url, rng)) or b"x", lim)
    assert g("a") == b"x" and g("b", (1, 2)) == b"x"
    assert seen == [("a", None), ("b", (1, 2))]
    g("c")
    assert c.slept == [60.0]


def test_two_threads_share_one_limit():
    lim = RateLimiter(10, window=60.0)       # real clock; 10 are immediate
    done = []

    def work():
        for _ in range(5):
            lim.acquire()
            done.append(1)

    ts = [threading.Thread(target=work) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(timeout=5) for t in ts]
    assert len(done) == 10
