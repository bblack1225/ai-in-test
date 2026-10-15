# ai-in-test 專案規則

App 自動化測試工具（Appium＋LLM），交付給多個團隊使用：工具程式碼在這個 repo，各團隊的 App 資料在自己的 `workspace/projects/<專案>/`。完整說明見 README.md。

## 溝通

- 回覆、進度更新、總結、commit 訊息、CHANGELOG 都用繁體中文；程式碼、指令、檔名維持原樣
- 只做被要求的事。覺得「順便可以加」的功能先用一句話問，不要直接做；修 bug 用最小修法

## 架構要點

- 相依方向：`viewer_server`／`cli` → `core/*` → `platforms/*` → `common`、`run_context`、`config`，不能反過來
- 平台差異只放 `platforms/`，其他程式透過 `get_platform()` 呼叫，不在 `core/` 寫 `if platform == ...`
- 執行狀態放 `RunContext`（當第一個參數傳），不要加模組層級的全域狀態；平台物件全伺服器共用，狀態放 `ctx.platform_state`
- LLM 一律走 `config.generate_content_with_retry(usage=ctx.usage, ...)`
- 定位備援只攔 `WebDriverException`，`InvalidSessionIdException` 要往上傳（不然斷線會被當成找不到元件）
- 改元件解析或指紋邏輯會讓既有地圖的 `gen::` ID 全部變掉、測案定位不到，要先問

## 改程式

- 工具程式碼不能為單一 App 加特例，App 專屬的設定放各專案的 `project.json`
- `workspace/`、`config.toml`、截圖、掃描資料都不進 git（含真實 App 畫面、帳號、金額）
- 新功能需要前端配合時，直接改現有的 `web/app_map_viewer.*`
- 改完跑 `uv run ruff check .` 和 `uv run pytest -q`：pytest 要全過；ruff 有既有的風格問題還沒清，標準是不增加新的
- 測試不能需要手機。實機驗證由使用者做，回報時講清楚哪些沒在實機驗過
- **改完停在未 commit**：回報結果，等使用者看過說可以才 commit、merge。分支命名 `feat/`、`fix/`、`refactor/`、`chore/`，merge 用 `git merge --no-ff`

## 驗證網頁

網頁開著專案時會**自動存檔**回 `workspace/`，而且沒有備份。用瀏覽器驗證網頁一律走 `/web-verify`（用暫存的 workspace 複本），不要在真實專案上點會存檔的操作。

## 敏感資料

repo 偶爾會短暫公開。新加的內容（文件、截圖、測試資料）commit 前，不能有真實 App 名稱、bundle id、帳號、Apple ID、電話。可以用這個檢查：

```bash
uv run python .claude/skills/release/scripts/check_sensitive.py
```

git 歷史裡有舊資料，已決定不改寫歷史，不要提議 rebase 或 filter-repo。

## 發版

用 `/release`。版號、CHANGELOG、Release Notes 的規則都寫在那個 skill 裡。

## 要找資料時

- 未完成的重構、優化報告 16 項的狀態：`docs/REFACTOR_CHECKLIST.md`
- 已知限制、暫時移除的功能：README 六、八
- 踩過的坑（修 bug 前先搜）：`docs/TROUBLESHOOTING.md`
- 判定規則（解析、爬蟲、定位）：`docs/JUDGMENT_LOGIC_REFERENCE.md`
