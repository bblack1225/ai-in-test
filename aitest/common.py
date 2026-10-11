"""
掃描、爬蟲、跑測案、platforms/*.py 共用的小工具。

這支不 import 專案內任何其他模組，讓 platforms/ 這類底層模組可以放心使用，
不會反過來依賴 run_test_case.py、capture_screen_map.py 這些 CLI 腳本。
"""


class StopRequested(Exception):
    """執行端（viewer_server 的「⏹ 停止」）要求中斷時，從捲動搜尋這類長迴圈裡丟出來，
    一路往上跳出 run_tap 等函式，不會再退回座標點擊。"""


_stop_checker = None


def set_stop_checker(checker) -> None:
    """viewer_server 啟動時把自己的停止旗標註冊進來；命令列執行不註冊，行為不變。"""
    global _stop_checker
    _stop_checker = checker


def raise_if_stopped() -> None:
    if _stop_checker is not None and _stop_checker():
        raise StopRequested()


def xpath_literal(value: str) -> str:
    """把字串安全地組成 XPath 字面值，處理內含雙引號的情況"""
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    parts = value.split('"')
    return "concat(" + ', \'"\', '.join(f'"{p}"' for p in parts) + ")"
