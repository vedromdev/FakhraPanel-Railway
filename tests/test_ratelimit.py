from starlette.requests import Request

from panel.ratelimit import RateLimiter, rate_limit_key


def _request(xff: str | None = None, client_host: str = "10.0.0.1") -> Request:
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/login",
            "headers": headers,
            "client": (client_host, 12345),
        }
    )


def test_rate_limit_key_uses_rightmost_forwarded_for():
    assert rate_limit_key(_request("1.2.3.4, 9.9.9.9")) == "9.9.9.9"


def test_rate_limit_key_falls_back_to_client_host():
    assert rate_limit_key(_request()) == "10.0.0.1"


def test_separate_buckets_per_forwarded_client():
    limiter = RateLimiter()
    same_key = rate_limit_key(_request("1.2.3.4, 9.9.9.9"))
    for _ in range(5):
        assert limiter.allow(same_key) is True
    assert limiter.allow(same_key) is False  # same-XFF 6th call denied
    other_key = rate_limit_key(_request("5.6.7.8, 7.7.7.7"))
    assert limiter.allow(other_key) is True  # different client still allowed


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
