"""config.generate_content_with_retry：Gemini 暫時性錯誤的重試（報告 #3）"""

import pytest
from google.genai.errors import ClientError, ServerError

import config


class _FakeModels:
    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "OK"


class _FakeClient:
    def __init__(self, errors):
        self.models = _FakeModels(errors)


@pytest.fixture
def sleeps(monkeypatch):
    waited = []
    monkeypatch.setattr(config.time, "sleep", waited.append)
    return waited


def _use(monkeypatch, errors) -> _FakeModels:
    client = _FakeClient(errors)
    monkeypatch.setattr(config, "_llm_client", client)
    return client.models


def _rate_limited(quota_id="GenerateRequestsPerMinutePerProjectPerModel-FreeTier", retry_delay="33s"):
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota_id}]}]
    if retry_delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay})
    return ClientError(429, {"error": {"code": 429, "message": "quota", "details": details}})


def test_server_error_retried(monkeypatch, sleeps):
    models = _use(monkeypatch, [ServerError(503, {"error": {"message": "overloaded"}})])
    assert config.generate_content_with_retry(model="m") == "OK"
    assert models.calls == 2
    assert sleeps == [5]


def test_per_minute_429_uses_retry_delay(monkeypatch, sleeps):
    _use(monkeypatch, [_rate_limited()])
    assert config.generate_content_with_retry(model="m") == "OK"
    assert sleeps == [33.0]


def test_429_without_retry_info_waits_longer(monkeypatch, sleeps):
    _use(monkeypatch, [_rate_limited(retry_delay=None), _rate_limited(retry_delay=None)])
    assert config.generate_content_with_retry(model="m") == "OK"
    assert sleeps == [10, 20]


def test_daily_quota_not_retried(monkeypatch, sleeps):
    models = _use(monkeypatch, [_rate_limited(quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier")])
    with pytest.raises(ClientError):
        config.generate_content_with_retry(model="m")
    assert models.calls == 1
    assert sleeps == []


def test_other_client_errors_not_retried(monkeypatch, sleeps):
    models = _use(monkeypatch, [ClientError(403, {"error": {"message": "bad key"}})])
    with pytest.raises(ClientError):
        config.generate_content_with_retry(model="m")
    assert models.calls == 1


def test_gives_up_after_max_retries(monkeypatch, sleeps):
    models = _use(monkeypatch, [_rate_limited()] * 4)
    with pytest.raises(ClientError):
        config.generate_content_with_retry(model="m")
    assert models.calls == 4
    assert len(sleeps) == 3
