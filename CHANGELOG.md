# 更新紀錄

新的放最上面。每一版對應一個 git tag（例如 `v2.0.0`），GitHub 的 Tags／Releases 頁面可以下載那一版的原始碼。

版號規則（`主版號.功能版號.修補版號`）：
- **主版號**：舊的用法不能用了（例如指令改名），升級前要先看「升級說明」
- **功能版號**：新增功能，原本的用法照舊
- **修補版號**：修 bug、文件、介面小調整

發新版時：`pyproject.toml` 的 `version` 改成新版號 → 這裡加一段 → merge 進 `main` → 打 tag。
待做事項完成時，把 `- [ ]` 改成 `- [x]`，後面接上完成的版本連結，例如：

```markdown
- [x] 可以在網頁上新增專案（[v2.1.0](#v210)）
```

## 待做事項

- [ ] 測案編輯與刪除：產出測案就直接儲存，之後可以再編輯或刪除
- [ ] 可以在網頁上新增專案
- [ ] 新增地圖的方式更直覺
- [ ] token 使用量顯示在網頁上
- [ ] 自動產生的元件 ID（例如 `gen::4780f1d9`）容易因為 App 動態渲染或層級變動而定位不到，評估是否要改變預設產生測案的方式
- [x] python code 專案結構調整(例如 src 資料夾)（[v2.0.0](#v200)）
- [ ] 地圖的節點新增更方便，不一定都要手動建
- [ ] 測案編輯與測案執行的操作體驗優化
- [ ] 提示詞優化
- [ ] 前端頁面重構（改用前端框架）

## v2.0.0

2026-10-12

### 升級說明（一定要看）

- **指令改了**，舊的 `python xxx.py` 不能用了：

  | 舊 | 新 |
  |---|---|
  | `uv run python aitest.py start` | `uv run aitest start` |
  | `uv run python aitest.py doctor` | `uv run aitest doctor` |
  | `uv run python workspace.py create` | `uv run aitest-workspace create` |
  | `uv run python crawl_app_tree.py …` | `uv run aitest-crawl …` |
  | `uv run python capture_screen_map.py …` | `uv run aitest-scan …` |
  | `uv run python run_test_case.py …` | `uv run aitest-run …` |
  | `uv run python viewer_server.py …` | `uv run aitest-server …` |
  | `uv run python cleanup_images.py` | `uv run python tools/cleanup_images.py` |

- **更新方式**：`git pull` 之後
  - 用 uv：直接執行 `uv run aitest start`，uv 會自動裝好新的套件和指令（要手動的話是 `uv sync`）
  - 用 venv + pip：先啟用 `.venv`，再執行 `pip install -e .`（**一定要做**，不然找不到 `aitest` 指令），之後指令前面不加 `uv run`
- `workspace/`、`config.toml` 不受影響，原本的專案、地圖、測案照常可以用

### 新功能

- `uv run aitest start` 不帶專案代號時，會列出專案讓你用 ↑↓ 方向鍵選（Android 專案排前面，直接按 Enter 選第一個）；也可以選「不開專案，只選平台」

### 修正

- 爬蟲偵測到被登出時，現在會真的整個停下來；之前在「回不到上一頁」的情況下會繼續點下去，把登入頁當成新畫面收進地圖
- Gemini 回應「請求太頻繁」（429，每分鐘上限）時會自動等待後重試，連續掃描很多畫面不會整個中斷；整天的額度用完則跟以前一樣直接報錯

### 內部調整（使用方式不變）

- 程式搬進 `aitest/` 套件（`aitest/core/`、`aitest/platforms/`），偶爾用的工具放 `tools/`
- `test_config.py` 改名 `aitest/config.py`
- 新增自動化測試（`uv run pytest`，不用接手機）與程式碼檢查（`uv run ruff check .`）
- 新增依賴套件 questionary（方向鍵選單）
- 文件更新：README、安裝與使用說明、TROUBLESHOOTING 加「依症狀找」索引

## v1.0.2

2026-10-11

- 新增 `docs/guide/` 安裝步驟與使用說明（HTML），取代原本的 `docs/DELIVERY_GUIDE.html`，使用說明補上 prompt 範例與關鍵字
- 批次執行每一組的測案改用清單下方「＋ 新增測案」按鈕加入
- 新增 CHANGELOG.md，README 更新到目前的功能
- 內部重構：抽出 `common.py`、`scan_core.py`（使用方式不變）

## v1.0.1

2026-10-08

- 已確認可以自動化輸入 input 框的內容
- 新增變數設定：測案編輯時 LLM 可以把值寫成變數，再建立變數檔、編輯變數的值，不用把帳號密碼、委託股數這類值寫死在測案裡
- 測案用到還沒有值的變數（例如 input 框要填的值）時，會跳出變數設定的視窗，可以直接建立變數檔、指定要填入的值
- 測案執行「單份執行」的「步驟失敗時自動關閉非預期彈窗並重試（行銷／評分／推播邀請…）」改成預設勾選
- 測案執行「批次執行」的「步驟失敗時自動關閉非預期彈窗並重試」改成預設勾選
- 一進 App 就跳的彈窗（例如 Root／USB 偵錯警告）會在測案開始前先自動關掉，不會再讓第一步點到彈窗上（需勾選「自動關閉非預期彈窗」）
- 批次執行可以指定登入測案，每份測案也可以針對不同情境選擇用不同的登入帳號
- 登入測案可以指定自己的「登入地圖」（登入頁另外建成一張地圖的情況）
- 地圖的根節點可以往上新增一層（實際跑測案時才發現前面還需要先導頁，但一開始沒有建立的情況）
- 地圖節點新增「🤖 補 LLM 描述」：用存好的截圖和元件資料幫還沒有描述的元件產生描述，不需要接手機
- 用 Appium 輸入登入頁的欄位時，可能因為打太快而掉字，新增防呆：逐字輸入時每個字間隔一下，輸入後讀回來比對，不符合就清掉放慢重打一次
- UI 優化

## v1.0.0

2026-10-07

- 第一個交付版本
