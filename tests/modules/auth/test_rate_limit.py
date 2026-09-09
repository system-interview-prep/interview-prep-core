from src.modules.auth.rate_limit import LoginRateLimiter


def test_rate_limiter_blocks_after_five_failures_and_can_reset() -> None:
    limiter = LoginRateLimiter(max_failures=5, window_seconds=60)
    for _ in range(5):
        assert limiter.is_blocked("127.0.0.1") is False
        limiter.record_failure("127.0.0.1")

    assert limiter.is_blocked("127.0.0.1") is True
    assert limiter.is_blocked("127.0.0.2") is False

    limiter.reset("127.0.0.1")
    assert limiter.is_blocked("127.0.0.1") is False
