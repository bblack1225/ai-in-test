"""
每次執行一份的 RunContext（測案、批次、掃描、爬蟲、LLM 對話…），以及它用到的 LLMUsage、StopRequested。

這支不 import 專案內任何其他模組，platforms/ 這類底層模組、config.py 都可以放心使用。
"""

import copy
import threading
from dataclasses import dataclass, field
from typing import Callable


class StopRequested(Exception):
    """執行端（viewer_server 的「⏹ 停止」）要求中斷時，從捲動搜尋這類長迴圈裡丟出來，
    一路往上跳出 run_tap 等函式，不會再退回座標點擊。"""


@dataclass
class LLMUsage:
    """一次操作（一次掃描、一次爬蟲、一次 LLM 對話…）累計的 LLM 用量。
    config.generate_content_with_retry 每呼叫成功一次就加進來；重試失敗的那幾次 Gemini 不計費，不算"""
    calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def __post_init__(self):
        # 同一次操作裡可能有好幾個執行緒同時呼叫 LLM，加總時要鎖
        self._lock = threading.Lock()

    def add(self, response) -> None:
        meta = getattr(response, "usage_metadata", None)
        with self._lock:
            self.calls += 1
            if meta is None:
                return
            self.prompt_tokens += meta.prompt_token_count or 0
            self.output_tokens += meta.candidates_token_count or 0
            self.total_tokens += meta.total_token_count or 0

    def as_dict(self) -> dict:
        with self._lock:
            return {"calls": self.calls, "prompt_tokens": self.prompt_tokens,
                    "output_tokens": self.output_tokens, "total_tokens": self.total_tokens}

    def summary(self) -> str:
        u = self.as_dict()
        return (f"呼叫 {u['calls']} 次，prompt={u['prompt_tokens']} output={u['output_tokens']} "
                f"total={u['total_tokens']}")


@dataclass
class RunContext:
    """
    一次執行（一次測案、一次批次、一次掃描、一次爬蟲、一次 LLM 對話…）的執行期設定與狀態。
    原本放在各模組的全域變數（set_run_vars、set_dismiss_dialogs、set_stop_checker…），
    兩個操作同時進行時會互相蓋掉；改成每次執行建一份，從呼叫端一路用參數傳下去。

      vars／var_sources  這次選的變數檔合併好的值、選了哪些檔（${KEY} 替換用）
      dismiss_dialogs    步驟失敗時要不要先關掉非預期彈窗再重試
      stop_checker       回傳 True 代表使用者按了停止；命令列執行沒有
      progress           給網頁輪詢的即時進度（結構由執行端決定），讀寫都要經過 lock
      usage              這次執行累計的 LLM 用量
      platform_state     平台物件在一次執行裡要記住的東西（例如 Android 提早打開
                         enableMultiWindows 前的原始設定），平台物件本身是共用的，不能存在裡面
      banner_capture_started  跑馬燈監看前開始錄影的時間（Android），監看完決定要不要留檔
    """
    vars: dict = field(default_factory=dict)
    var_sources: list = field(default_factory=list)
    dismiss_dialogs: bool = False
    stop_checker: Callable[[], bool] | None = None
    progress: dict = field(default_factory=dict)
    usage: LLMUsage = field(default_factory=LLMUsage)
    platform_state: dict = field(default_factory=dict)
    banner_capture_started: float | None = None

    def __post_init__(self):
        self.lock = threading.Lock()

    def raise_if_stopped(self) -> None:
        if self.stop_checker is not None and self.stop_checker():
            raise StopRequested()

    def stopped(self) -> bool:
        return self.stop_checker is not None and self.stop_checker()

    def set_vars(self, values: dict | None, sources: list | None = None) -> None:
        self.vars = dict(values or {})
        self.var_sources = list(sources or [])

    def update_progress(self, **values) -> None:
        with self.lock:
            self.progress.update(values)

    def progress_snapshot(self) -> dict:
        """網頁輪詢用：在鎖裡複製一份，回傳的東西之後怎麼被改都不影響"""
        with self.lock:
            snapshot = copy.deepcopy(self.progress)
        snapshot["usage"] = self.usage.as_dict()
        return snapshot
