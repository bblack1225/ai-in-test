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

### 第 2 步：改成 `src/aitest/` 套件 ＋ console scripts

```
src/aitest/
  core/        workspace、地圖、navigator、測案執行、掃描（不碰 HTTP）
  platforms/   android.py、ios.py（名稱沿用）
  llm/         Gemini 呼叫、prompt
  cli/         aitest start/doctor、workspace、crawl…
  config.py    （原 test_config.py）
  common.py
web/           現在的 html/js，Vue 完成前繼續用
tools/         compare_versions、cleanup_images、capture_transient
```

- `pyproject.toml` 加 `[project.scripts]`，指令變成 `uv run aitest start`
- 修正用 `__file__` 找路徑的地方：`config.toml`、`web/`、`logs/`、`aitest.py` 啟動 viewer 的方式
- README、`docs/guide/*.html`、`docs/CRAWLER_TREE_HANDOFF.md` 的指令一起更新

### 第 3 步：拆 `viewer_server.py` 成 `api/`

- 一個領域一個檔（projects、maps、scan、run、batch、llm…），HTTP 層只做參數解析和回傳
- 整理一份 API 清單（路徑、參數、回傳格式），給 Vue 對接用
- 要不要從 `http.server` 換成 FastAPI 等框架：到這一步再決定

### 第 4 步：`frontend/` 用 Vue 改寫

- 一個分頁一個分頁換，舊 `web/` 全部換完才刪
- 開始前先決定：交付時 commit build 好的 `dist/`，還是要求對方裝 Node

## 暫不處理（記下來）

- `crawl_app_tree.py` 直接用平台的私有函式 `_text_xpath`、`_predicate_literal`
- 根目錄的舊資料（`element_map*.json`、`app_map_tree.json`、`screenshots/`、`crops/`）確認搬進 workspace 後可刪
- `docs/reference_data/` 的 XML 含 App 畫面內容，確認要不要留在 git
- `compare_versions.py` 說明還提到已退役的 `viewer.html`
