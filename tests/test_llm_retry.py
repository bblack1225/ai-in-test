"""config.generate_content_with_retry：Gemini 暫時性錯誤的重試（報告 #3）"""

import httpx
import pytest
from google.genai.errors import ClientError, ServerError

from aitest import config


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


def test_connection_dropped_retried(monkeypatch, sleeps):
    # 長時間的 Pro 呼叫被伺服器斷線時，會是 httpx 的例外，不是 google.genai 的 ServerError
    models = _use(monkeypatch, [httpx.RemoteProtocolError("Server disconnected without sending a response.")])
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


class _Chunk:
    def __init__(self, text, usage=None):
        self.text = text
        self.usage_metadata = usage


class _FakeStreamModels:
    """每次呼叫 generate_content_stream 會依序執行一個「腳本」：字串是要 yield 的 chunk，例外是要在那裡丟出去"""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls = 0

    def generate_content_stream(self, **kwargs):
        self.calls += 1
        for item in self.scripts.pop(0):
            if isinstance(item, BaseException):
                raise item
            yield item


def _use_stream(monkeypatch, scripts) -> _FakeStreamModels:
    models = _FakeStreamModels(scripts)
    monkeypatch.setattr(config, "_llm_client", type("C", (), {"models": models})())
    return models


def test_stream_collects_text(monkeypatch, sleeps):
    models = _use_stream(monkeypatch, [[_Chunk('{"a"'), _Chunk(': 1}')]])
    assert config.generate_content_stream_with_retry(model="m") == '{"a": 1}'
    assert models.calls == 1
    assert sleeps == []


def test_stream_dropped_before_any_text_retried(monkeypatch, sleeps):
    models = _use_stream(monkeypatch, [
        [httpx.RemoteProtocolError("Server disconnected without sending a response.")],
        [_Chunk("OK")],
    ])
    assert config.generate_content_stream_with_retry(model="m") == "OK"
    assert models.calls == 2
    assert sleeps == [5]


def test_stream_dropped_midway_restarts_whole_response(monkeypatch, sleeps):
    # 串流收到一半斷線：整段從頭重試，不能把前半段跟重試的後半段接在一起
    models = _use_stream(monkeypatch, [
        [_Chunk('{"scenarios": ['), httpx.ReadError("connection reset")],
        [_Chunk('{"scenarios": []}')],
    ])
    assert config.generate_content_stream_with_retry(model="m") == '{"scenarios": []}'
    assert models.calls == 2


def test_stream_usage_counted_once(monkeypatch, sleeps):
    usage_meta = type("M", (), {"prompt_token_count": 10, "candidates_token_count": 4, "total_token_count": 14})()
    _use_stream(monkeypatch, [[_Chunk("hi"), _Chunk("", usage_meta)]])
    from aitest.run_context import LLMUsage
    usage = LLMUsage()
    config.generate_content_stream_with_retry(usage=usage, model="m")
    assert usage.as_dict() == {"calls": 1, "prompt_tokens": 10, "output_tokens": 4, "total_tokens": 14}


def test_stream_failure_warning_shows_progress(monkeypatch, sleeps, capsys):
    # 斷線警告要帶這次已收到的字數，才看得出是斷在哪裡
    _use_stream(monkeypatch, [
        [_Chunk("12345"), httpx.ReadError("connection reset")],
        [_Chunk("OK")],
    ])
    config.generate_content_stream_with_retry(label="測試", model="m")
    out = capsys.readouterr().out
    assert "[測試] 開始串流（m）" in out
    assert "收到 5 字" in out
    assert "[測試] 串流完成" in out and "收到 2 字" in out


def test_stream_progress_printed_while_waiting(monkeypatch, sleeps, capsys):
    import threading
    gate = threading.Event()

    class _SlowModels:
        def generate_content_stream(self, **kwargs):
            gate.wait(1)  # 模擬 Pro 思考很久才送第一段
            yield _Chunk("OK")

    monkeypatch.setattr(config, "_llm_client", type("C", (), {"models": _SlowModels()})())
    real_print = print

    def spy(*args, **kwargs):
        real_print(*args, **kwargs)
        if "串流中" in str(args[0]):
            gate.set()

    monkeypatch.setattr("builtins.print", spy)
    assert config.generate_content_stream_with_retry(model="m", progress_interval_sec=0.01) == "OK"
    assert "串流中…已 0 秒，收到 0 字" in capsys.readouterr().out
