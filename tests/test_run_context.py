"""RunContext／LLMUsage：每次執行一份的設定與狀態，LLM 用量各自加總、不互相覆蓋"""

import threading
import time
from types import SimpleNamespace

import pytest
from google.genai.errors import ServerError

from aitest import config
from aitest.run_context import LLMUsage, RunContext, StopRequested


def _response(prompt, output):
    return SimpleNamespace(usage_metadata=SimpleNamespace(
        prompt_token_count=prompt, candidates_token_count=output, total_token_count=prompt + output))


class _FakeModels:
    """回傳的用量由呼叫端在 contents 裡指定；errors 先依序丟出"""

    def __init__(self, errors=()):
        self.errors = list(errors)

    def generate_content(self, contents, **kwargs):
        if self.errors:
            raise self.errors.pop(0)
        time.sleep(0.001)  # 讓不同執行緒的呼叫交錯
        return _response(*contents)


@pytest.fixture
def fake_llm(monkeypatch):
    models = _FakeModels()
    monkeypatch.setattr(config, "_llm_client", SimpleNamespace(models=models))
    monkeypatch.setattr(config.time, "sleep", lambda s: None)
    return models


def test_usage_accumulates_per_call(fake_llm):
    usage = LLMUsage()
    config.generate_content_with_retry(usage=usage, contents=(100, 20))
    config.generate_content_with_retry(usage=usage, contents=(50, 5))
    assert usage.as_dict() == {"calls": 2, "prompt_tokens": 150, "output_tokens": 25, "total_tokens": 175}


def test_failed_attempts_not_counted(fake_llm):
    """重試前失敗的那幾次 Gemini 不計費，用量只算成功的那一次"""
    fake_llm.errors = [ServerError(503, {"error": {"message": "overloaded"}})]
    usage = LLMUsage()
    config.generate_content_with_retry(usage=usage, contents=(10, 1))
    assert usage.as_dict()["calls"] == 1


def test_usage_optional(fake_llm):
    assert config.generate_content_with_retry(contents=(1, 1)).usage_metadata.total_token_count == 2


def test_concurrent_operations_keep_separate_totals(fake_llm):
    """同時跑「掃描」（很多次小呼叫）和「LLM 對話」（幾次大呼叫），兩邊用量各自正確"""
    scan, chat = LLMUsage(), LLMUsage()

    def run_scan():
        for _ in range(50):
            config.generate_content_with_retry(usage=scan, contents=(10, 1))

    def run_chat():
        for _ in range(5):
            config.generate_content_with_retry(usage=chat, contents=(1000, 200))

    threads = [threading.Thread(target=run_scan), threading.Thread(target=run_chat)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert scan.as_dict() == {"calls": 50, "prompt_tokens": 500, "output_tokens": 50, "total_tokens": 550}
    assert chat.as_dict() == {"calls": 5, "prompt_tokens": 5000, "output_tokens": 1000, "total_tokens": 6000}


def test_same_usage_from_many_threads_adds_up(fake_llm):
    usage = LLMUsage()

    def work():
        for _ in range(100):
            config.generate_content_with_retry(usage=usage, contents=(1, 1))

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert usage.as_dict()["calls"] == 400
    assert usage.as_dict()["total_tokens"] == 800


def test_stop_checker():
    flag = {"stop": False}
    ctx = RunContext(stop_checker=lambda: flag["stop"])
    ctx.raise_if_stopped()
    flag["stop"] = True
    assert ctx.stopped()
    with pytest.raises(StopRequested):
        ctx.raise_if_stopped()
    RunContext().raise_if_stopped()  # 命令列執行沒有停止旗標


def test_progress_snapshot_is_a_copy():
    """網頁輪詢拿到的是複本，執行端之後再改不會影響已經送出去的內容"""
    ctx = RunContext()
    results = []
    ctx.progress = {"running": True, "results": results}
    snapshot = ctx.progress_snapshot()
    results.append({"index": 1})
    ctx.update_progress(running=False)
    assert snapshot["results"] == [] and snapshot["running"] is True
    assert snapshot["usage"]["calls"] == 0


def test_platform_state_is_per_run():
    """Android 跑馬燈提早打開 enableMultiWindows 前的設定，原本存在模組的全域變數"""
    from aitest.platforms import android

    class Driver:
        def __init__(self):
            self.settings = {"enableMultiWindows": False}

        def get_settings(self):
            return dict(self.settings)

        def update_settings(self, values):
            self.settings.update(values)

    a, b = RunContext(), RunContext()
    android.prepare_banner_watch(a, Driver())
    assert a.platform_state == {"android_pre_watch_settings": {"enableMultiWindows": False}}
    assert b.platform_state == {}
