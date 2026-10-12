"""
掃描、爬蟲、跑測案、platforms/*.py 共用的小工具。

這支不 import 專案內任何其他模組，讓 platforms/ 這類底層模組可以放心使用，
不會反過來依賴 run_test_case.py、capture_screen_map.py 這些 CLI 腳本。
每次執行的設定與狀態（RunContext）在 run_context.py。
"""


def xpath_literal(value: str) -> str:
    """把字串安全地組成 XPath 字面值，處理內含雙引號的情況"""
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    parts = value.split('"')
    return "concat(" + ', \'"\', '.join(f'"{p}"' for p in parts) + ")"
