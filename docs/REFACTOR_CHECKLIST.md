# 後端重構 Checklist

目標：
1. 把[優化報告](https://claude.ai/artifact/MDDjedLjjrtp8H1a1irbdq)（2026-10-08，對應 commit 75abd9c）提的問題改好
2. CHANGELOG.md 待做事項裡屬於後端的部分做完
3. 架構改成前端好串接：前端由其他人改寫，這邊只負責後端 API

設計與理由見 [REFACTOR_PLAN.md](REFACTOR_PLAN.md)。每個步驟一個分支，從 `main` 開，完成並驗證後合併回 `main`。

標記：`[x]` 完成 · `[ ]` 未做 · `[-]` 決定不做（後面寫原因）· **（待確認）** 範圍要先問過再做

---

## 一、重構步驟

### 步驟 1：抽出共用模組 `refactor/extract-common`
- [x] `common.py`：`StopRequested`、`set_stop_checker`、`raise_if_stopped`、`xpath_literal`
- [x] `scan_core.py`：`position_bucket`、`candidate_signature`、`candidate_identity_key`、`identity_keys_in_region`、`read_and_merge_round`
- [x] `platforms/` 不再 import `run_test_case`、`capture_screen_map`
- [x] `capture_screen_map.py` 藏在 `main()` 裡的 `platforms` import 移回檔頭
- [x] 驗證：每個模組可單獨 import；實機掃描（捲動收集）、跑測案、停止

### 步驟 2：小修與工具鏈 `refactor/quick-fixes`
- [x] 報告 #1：`crawl_app_tree.py` 的 `finally` 裡的 `break` 移到 `try/finally` 後面
- [x] 報告 #3：`generate_content_with_retry` 遇到 429 也重試（優先用回應帶的等待秒數），其他 4xx 直接拋出
  - 跟報告不同：整天額度用完的 429（quotaId 帶 `PerDay`）不重試、直接拋出，只有每分鐘上限的 429 才重試
- [x] 報告 #14：`pyproject.toml` 加 `[dependency-groups] dev`（ruff、pytest）、`[tool.ruff]`（line-length 120、select E/F/W/B/I）、`[tool.pytest.ini_options]`
- [x] 報告 #14：修掉 ruff 抓到的 B904（`raise ... from`，9 處）、B012（隨 #1 解掉）
- [ ] 其餘 ruff 項目（48 筆：行太長 24、F541 6、import 排序 6、E741 5、B905 5、其他 2），都是風格問題，之後再處理
- [x] 報告 #16：`.vscode/` 移出 git、加進 `.gitignore`
- [x] `test_config.py` 改名 `config.py`（避免被 pytest 當成測試檔），程式與文件的引用全部更新
- [x] 報告 #12：`run_test_case.py` 三處提到已刪掉的 `generate_test_case.py`；LLM 用量文件註明「模型與單價以設定檔為準」；`TROUBLESHOOTING.md` 加「依症狀找」索引，#9、#17 更新
- [x] `compare_versions.py` 說明裡提到已退役的 `viewer.html`
- [x] 步驟 1 漏掉的：`run_test_case.py` 的 `main()` 裡藏的 `platforms` import 移回檔頭
- [x] README 補上 `common.py`、`scan_core.py`、開發工具（ruff、pytest）
- [x] 驗證（不用手機）：所有模組可 import、ruff 的 B012／B904／F401 通過、429／503 重試用假的 client 模擬、`aitest.py doctor` 與各 CLI 的 `--help` 正常
- [ ] 驗證（實機）：`uv run python aitest.py start` 掃描一個畫面、跑一個測案、爬蟲跑一小段

### 步驟 3：純函式測試（報告 #2） `refactor/add-tests`
- [ ] 補一份 Android 的 page_source 樣本（目前 `docs/reference_data/` 三份都是 iOS）
- [ ] 確認樣本 XML 沒有真實帳號、金額，有的話換成假資料
- [ ] `tests/test_parse_candidates.py`：iOS／Android 解析
- [ ] `tests/test_match_screen.py`：`match_screen_in_source`
- [ ] `tests/test_skeleton.py`：`skeleton_signature`／`android_skeleton_signature`
- [ ] `tests/test_compare_screens.py`：改版比對
- [ ] `tests/test_workspace.py`：`import_zip`／`merge_zip`、sensitive 變數檔不匯出
- [ ] `tests/test_resolve_env.py`：`${LOGIN_ID}` 替換、缺少變數的回報
- [ ] `uv run pytest` 全過

### 步驟 4：平台層收斂 `refactor/platform-unify`
- [ ] 報告 #5：三套「等畫面穩定」合併成一個 `wait_until_stable`，掃描版改用真實時間計時
- [ ] 報告 #6：`crawl_app_tree.py` 的 `IOSCrawlRules`／`AndroidCrawlRules` 併入 `IOSPlatform`／`AndroidPlatform`，爬蟲改用 `get_platform()`
- [ ] `crawl_app_tree.py` 不再使用平台的私有函式（`_text_xpath`、`_predicate_literal`）
- [ ] 報告 #7：無 ID 元件的定位流程抽成共用模組，平台只提供「查一次」「捲一次」「最後備援」
- [ ] 報告 #7：iOS 補上 Android 已有的「捲容器前先縮邊界」
- [ ] 報告 #10：定位備援只攔定位失敗的例外，`InvalidSessionIdException` 往上傳（先改 `platforms/`）
- [ ] 報告 #10：爬蟲、server 裡剩下的 `except Exception` 逐步收斂
- [ ] `scan_core.py` 跟著收進平台層或 core
- [ ] 驗證：pytest 全過＋實機跑完整測案、爬蟲

### 步驟 5：RunContext 與 LLM 用量（報告 #4、CHANGELOG「token 使用量」後端部分） `refactor/run-context`
- [ ] `RunContext`：vars、var_sources、dismiss_dialogs、stop_checker、progress、usage
- [ ] `run_test_case`、`navigator`、`platforms/` 改成用參數傳 `ctx`，刪掉全域變數和 `set_xxx()`（不留過渡期）
- [ ] `common.py` 的全域 `_stop_checker` 刪掉
- [ ] `platforms/android.py` 的 `_pre_watch_settings` 全域狀態一併處理
- [ ] `generate_content_with_retry` 把用量累加到傳進來的物件，5 個 LLM 呼叫點都接上
- [ ] 一次掃描、爬蟲、測案、LLM 對話的用量各自加總，不會互相覆蓋
- [ ] `_progress`／`_batch_progress` 改成每次執行一份，讀寫加鎖
- [ ] 驗證：同時跑掃描與 LLM 對話，兩邊用量各自正確

### 步驟 6：套件化（CHANGELOG「python code 專案結構調整」） `refactor/src-package`
- [ ] 改成 `src/aitest/`：`core/`、`platforms/`、`llm/`、`cli/`、`config.py`、`common.py`
- [ ] `pyproject.toml` 加 `[project.scripts]`，指令變成 `uv run aitest ...`
- [ ] 修正用 `__file__` 找路徑的地方：`config.toml`、`web/`、`logs/`、`aitest.py` 啟動 viewer
- [ ] 工具腳本移到 `tools/`：`compare_versions`、`cleanup_images`、`capture_transient`
- [ ] README、`docs/guide/*.html`、`docs/CRAWLER_TREE_HANDOFF.md` 的指令全部更新
- [ ] 報告 #13：`print` 改成 `logging`，server 的 log 也存到 `logs/`
- [ ] 驗證：pytest 全過＋`uv run aitest start` 實機跑一輪

### 步驟 7：拆 viewer_server（報告 #8） `refactor/split-server`
- [ ] `llm/prompts.py`：三個 `build_*_prompt` 和常數字串（CHANGELOG「提示詞優化」有獨立的地方可改）
- [ ] `core/runner.py`：`run_test_script`、`run_batch`、進度
- [ ] `api/`：一個領域一個檔，路由改成查表，取代 `KNOWN_PATHS`、`PATHS_NOT_REQUIRING_DRIVER` 和 if/elif
- [ ] import server 模組時不再有副作用（建 Workspace、註冊停止旗標移到啟動時）
- [ ] 報告 #11：AI 定位送 LLM 前先剪掉 page_source 裡用不到的節點和屬性
- [ ] 要不要從 `http.server` 換成 FastAPI 等框架 **（待確認）**

### 步驟 8：前端串接準備 `refactor/api-for-frontend`
- [ ] `docs/API.md`：每支 API 的路徑、方法、參數、回傳格式、是否需要手機、錯誤格式
- [ ] 錯誤回傳格式統一（例如 `{"error": "...", "hint": "..."}` 加上正確的 HTTP 狀態碼）
- [ ] 開發時的跨站設定：目前只允許同 port 的 Origin，前端開發伺服器（例如 Vite 的 5173）會被擋。加設定可以允許指定的開發用 Origin
- [ ] 前端 build 出來的檔案放哪、server 怎麼提供（現在是 `web/`），寫進文件
- [ ] 長時間操作（掃描、爬蟲、測案、批次）的進度 API 格式統一，包含 LLM 用量
- [ ] 驗證：現在的 `web/` 前端照常可用

### 步驟 9：收尾 `refactor/wrap-up`
- [ ] 逐項比對報告 16 項，填寫下面的「與報告的差距」
- [ ] CHANGELOG.md 加更新紀錄、勾掉完成的待做事項

---

## 二、CHANGELOG 待做事項對照

前端由其他人改寫，這邊只做後端需要的部分。

| 待做事項 | 這次做的後端部分 | 狀態 |
|---|---|---|
| 測案編輯與刪除：產出就直接儲存，之後可再編輯、刪除 | 不用新 API。現有的 `/api/projects/<專案>/cases/<名稱>`：GET 讀取、PUT 新增（`?overwrite=1` 覆蓋＝編輯）、DELETE 刪除，另有 `POST …/rename` 改名。寫進 API 文件 | [ ] |
| 可以在網頁上新增專案 | 新增 `POST /api/projects`（建立）、修改專案設定的 API（2026-10-11 決定：網頁也可以建專案，CLI 保留） | [ ] |
| 新增地圖的方式更直覺 | 地圖的新增、複製、改名、匯入、匯出、刪除 API 已經有，主要是前端操作。寫進 API 文件 | [ ] |
| token 使用量顯示在網頁上 | 步驟 5 的用量累計＋步驟 8 的進度 API 回傳用量；顯示由前端做 | [ ] |
| 自動產生的元件 ID（`gen::…`）容易定位不到 | 這次不做，之後另外處理 | [-] 之後再做 |
| python code 專案結構調整 | 步驟 6 | [ ] |
| 地圖節點新增更方便，不一定都要手動建 | 這次不做，之後另外處理 | [-] 之後再做 |
| 測案編輯與測案執行的操作體驗優化 | 前端 | [-] 交給前端 |
| 提示詞優化 | 步驟 7 把提示詞抽到 `llm/prompts.py`＋報告 #11 剪枝；提示詞內容本身不動，之後另外調整 | [ ] |
| 前端頁面重構（改用前端框架） | 前端 | [-] 交給前端 |

---

## 三、報告 16 項對照

| # | 項目 | 處理步驟 | 狀態 |
|---|---|---|---|
| 1 | 爬蟲在應該中止時繼續跑（`finally` 裡的 `break`） | 步驟 2 | [x] |
| 2 | 沒有自動化測試 | 步驟 3 | [ ] |
| 3 | Gemini 429 不重試 | 步驟 2 | [x] |
| 4 | 執行期設定放在全域變數 | 步驟 1（部分）、步驟 5 | [ ] |
| 5 | 等畫面穩定有三種寫法 | 步驟 4 | [ ] |
| 6 | 爬蟲自己又做了一套平台切換 | 步驟 4 | [ ] |
| 7 | iOS／Android 定位程式複製貼上 | 步驟 4 | [ ] |
| 8 | viewer_server 一支檔案做五件事 | 步驟 7 | [ ] |
| 9 | 前端 3,600 行擠在一個檔案 | 交給前端；後端提供步驟 8 的 API 文件 | [-] 交給前端 |
| 10 | `except Exception` 讓斷線看起來像找不到元件 | 步驟 4 | [ ] |
| 11 | AI 定位每次重試都重送整個畫面結構 | 步驟 7 | [ ] |
| 12 | 文件提到已刪掉的檔案和舊模型名 | 步驟 2 | [x] |
| 13 | 全部用 print | 步驟 6 | [ ] |
| 14 | 沒有程式碼檢查工具設定 | 步驟 2（風格類的 48 筆還沒修） | [x] |
| 15 | LLM 對話每輪重送全部內容 | 報告也建議先不動，等用量顯示後確認成本再決定 | [-] 暫不做 |
| 16 | 個人編輯器設定進了 git | 步驟 2 | [x] |

---

## 四、與報告的差距（步驟 9 填寫）

全部做完後，逐項寫下實際做法跟報告建議不同的地方，以及沒做到的部分。

| # | 報告建議 | 實際做法 | 差距／原因 |
|---|---|---|---|
| | | | |
