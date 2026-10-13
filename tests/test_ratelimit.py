from panel.ratelimit import RateLimiter


def test_sixth_call_in_window_denied():
    limiter = RateLimiter()
    for _ in range(5):
        assert limiter.allow("ip") is True
    assert limiter.allow("ip") is False


def test_window_expiry_allows_again():
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    for _ in range(5):
        assert limiter.allow("ip") is True
    assert limiter.allow("ip") is False
    now[0] += 61
    assert limiter.allow("ip") is True
