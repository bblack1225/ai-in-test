# 後端結構重構計劃

目標：把後端整理成分層清楚的 Python 套件，為之後前端改用 Vue 改寫做準備。
每一步一個分支，從 `main` 開，完成並實測後再合併，下一步才開始。

## 目前的問題

1. **相依方向反了**：`platforms/android.py`、`platforms/ios.py`（底層，負責各平台怎麼操作）
   反過來 import `run_test_case.py`、`capture_screen_map.py`（上層 CLI 腳本）的函式。
   - 已經形成循環 import，`capture_screen_map.py` 只能把 `from platforms import ...` 藏在 `main()` 裡避開
   - import 平台就會連帶載入 Gemini SDK、PIL、整個測案執行器
   - 「停止」旗標（`raise_if_stopped`）放在測案執行器的全域變數裡，掃描、爬蟲要停也得經過它
2. **入口和內部模組混在根目錄**：11 支 `.py` 平放，看不出哪些是給人跑的
3. **`viewer_server.py` 太大**：1,300 多行，HTTP 路由、靜態檔、呼叫掃描/測案邏輯全在一起，
   API 清單散在 if/elif 裡，Vue 改寫時很難對接

## 步驟

### 第 1 步：抽出共用模組（`refactor/extract-common`）

只搬移函式，不改邏輯。

| 新模組 | 內容 | 原本在 |
|---|---|---|
| `common.py` | `StopRequested`、`set_stop_checker`、`raise_if_stopped`、`xpath_literal` | `run_test_case.py` |
| `scan_core.py` | `position_bucket`、`candidate_signature`、`candidate_identity_key`、`identity_keys_in_region`、`read_and_merge_round` | `capture_screen_map.py` |

- `platforms/*` 改成從 `common`、`scan_core` import，不再 import CLI 腳本
- `run_test_case.py`、`capture_screen_map.py` 也從新模組 import，原本的名稱照樣可以從它們取用，
  `viewer_server.py`、`crawl_app_tree.py`、`navigator.py` 的 import 不用改
- `capture_screen_map.py` 藏在 `main()` 裡的 `from platforms import ...` 移回檔案開頭
- `platforms/android.py`、`platforms/ios.py` 檔名、類別名稱不變

改完後的方向：

```
aitest / viewer_server / run_test_case / capture_screen_map / crawl_app_tree / navigator
        ↓
   platforms/*
        ↓
   common.py、scan_core.py（不 import 專案內其他模組）
```

驗證：每個模組都能單獨 import（不論順序）；實機跑一次掃描、一個測案、按一次停止。

### 之後的步驟

前端由其他人改寫，這邊的終點是「後端整理好、API 文件齊全、前端好串接」。
每一步要做的細項和完成狀態都在 [REFACTOR_CHECKLIST.md](REFACTOR_CHECKLIST.md)。

| 步驟 | 內容 |
|---|---|
| 2 | 小修與工具鏈：`finally` 裡的 `break`、429 重試、ruff／pytest、`.vscode`、`test_config.py` 改名 `config.py`、過時文件 |
| 3 | 純函式測試：用 `docs/reference_data/` 的 page_source XML 當測試資料，不用接手機 |
| 4 | 平台層收斂：合併三套等畫面穩定、爬蟲的平台規則併入 `platforms/`、共用定位流程、只攔定位失敗的例外 |
| 5 | `RunContext`：取代全域變數，每次執行一份，直接改成最終樣子不留過渡期；LLM 用量跟著每次操作累計，不會互相覆蓋 |
| 6 | 改成根目錄的 `aitest/` 套件（不用 `src/`，2026-10-11 決定）＋console scripts；`print` 改 `logging` |
| 7 | 拆 `viewer_server.py`：提示詞、執行、API 路由表分開 |
| 8 | 前端串接準備：API 文件、統一錯誤格式、開發用跨站設定、進度與用量 API |
| 9 | 收尾：對照報告寫下差距、更新 CHANGELOG |

`RunContext` 的樣子：

```python
@dataclass
class RunContext:
    vars: dict = field(default_factory=dict)
    var_sources: list = field(default_factory=list)
    dismiss_dialogs: bool = False
    stop_checker: Callable[[], bool] | None = None
    progress: dict = field(default_factory=dict)
    usage: LLMUsage = field(default_factory=LLMUsage)   # 報告沒有，這次加：呼叫次數、prompt/output/total tokens 累計

    def raise_if_stopped(self): ...
```

## 暫不處理（記下來）

- 根目錄的舊資料（`element_map*.json`、`app_map_tree.json`、`screenshots/`、`crops/`）確認搬進 workspace 後可刪
