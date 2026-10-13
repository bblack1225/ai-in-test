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
- [x] 驗證（實機，2026-10-12）：`uv run python aitest.py start` 掃描一個畫面、跑一個測案、爬蟲跑一小段（`--safe_keywords --max_depth 1 --max_screens 3`）

### 步驟 3：純函式測試（報告 #2） `refactor/add-tests`
- [x] 補一份 Android 的 page_source 樣本：`tests/fixtures/android_watchlist.xml`，照真實結構手寫的假資料
  （本機 `test_results/banner_debug_*.xml` 是真實 App 畫面，含真實姓名與編號，不能進 git）
- [x] 確認樣本 XML 沒有真實帳號、金額：`docs/reference_data/` 三份 iOS 樣本已經是去識別化的假資料（你好證券、王大明、`tw.foo.STP`），直接當測試資料
- [x] `tests/test_parse_candidates.py`：iOS／Android 解析、native id 判斷、label 優先順序、重複 id 降級
- [x] `tests/test_scan_core.py`：九宮格、身分 key、`read_and_merge_round`（步驟 1 搬出來的函式）
- [x] `tests/test_match_screen.py`：`match_screen_in_source`
- [x] `tests/test_skeleton.py`：`skeleton_signature`／`android_skeleton_signature`
- [x] `tests/test_compare_screens.py`：改版比對
- [x] `tests/test_workspace.py`：建專案、測案讀寫刪、`import_zip`／`merge_zip`、sensitive 變數檔不匯出、不安全路徑
- [x] `tests/test_resolve_env.py`：`${LOGIN_ID}` 替換、缺少變數的回報
- [x] `tests/test_llm_retry.py`：503／429 重試（步驟 2 的改動）
- [x] `uv run pytest` 全過（72 個，約 0.5 秒）；故意改壞解析邏輯 4 處，測試都會失敗
- [x] 爬蟲 `finally` 的修正：步驟 4 之後補上 `tests/test_crawl_screen.py`（假的平台物件跑 `crawl_screen`，子畫面中止時例外有往上傳；把 `break` 放回 `finally` 測試會失敗）
- [x] 發現的既有行為：Android 的 `nearby_text` 會把 `displayed="false"` 隱藏節點的文字也算進去 → 排進步驟 4 修

### 步驟 4：平台層收斂 `refactor/platform-unify`
- [x] 報告 #5：三套「等畫面穩定」合併成 `platforms/base.wait_until_stable`（比對什麼、等多久由呼叫端給），
  掃描版改用真實時間計時（原本「輪詢 5 次、每次隔 1.5 秒」，改成最多 7.5 秒）
  - `run_test_case.wait_after_action`、`capture_screen_map.wait_for_screen_stable` 留著當薄包裝（呼叫的地方多，各自帶好比對方式和時間上限）；爬蟲的 `wait_stable` 刪掉，直接呼叫
  - 掃描版原本每輪都印一行「第 N 次讀取…」，改成只印最後結果
- [x] 報告 #6：`IOSCrawlRules`／`AndroidCrawlRules` 併入 `IOSPlatform`／`AndroidPlatform`（`skeleton`、`analyze`、`chrome_keys`、`dismiss_modal`、`go_back`、`find_by_class_and_text`），爬蟲改用 `get_platform()`
  - 爬蟲的平台判斷函式搬到 `platforms/ios.py`、`platforms/android.py` 的「爬蟲用」那一段，共用規則（對話框按鈕清單、常駐框架範圍、`looks_like_data`…）放 `platforms/base.py`
  - 爬蟲的全域 `RULES` 拿掉，改成跟跑測案一樣把 `platform` 當第一個參數傳
  - 順便解掉 `platforms/` 反過來 import `crawl_app_tree`（`go_back`、`dismiss_dialogs` 原本放在函式裡 import 避開循環）
- [x] `crawl_app_tree.py` 不再使用平台的私有函式（`_text_xpath`、`_predicate_literal` 改成公開的 `text_xpath`、`predicate_literal`，爬蟲也不直接用了）
- [x] 報告 #7：無 ID 元件的定位流程抽成 `platforms/locate.py`，平台只提供「查一次」（`class_text_by`＋`class_text_query`）、「捲一次」（`scroll_once`）、「最後備援」（`locate_fallback`）
  - 跟原本規劃的差異：平台另外還要提供「找錨點」（`find_anchor`）和「列出可捲動容器」（`scrollable_regions`）——錨點 iOS 直接查、Android 要邊捲邊找，做法不同
- [x] 報告 #7：iOS 補上 Android 已有的「捲容器前先縮邊界」（`base.shrink_bounds`，Android 的橫向容器收集也改用它）
- [x] 報告 #10：定位備援只攔定位失敗的例外，`InvalidSessionIdException` 往上傳（先改 `platforms/`）
  - 做法：`except Exception` 改成「先丟 `InvalidSessionIdException`，再攔 `WebDriverException`」；連不上 Appium server 的錯誤本來就不是 `WebDriverException`，也會往上傳
  - `platforms/` 裡已經沒有 `except Exception`
- [x] 報告 #10：爬蟲、server 裡剩下的 `except Exception` 逐步收斂
  - 爬蟲：全部改完（5 處）
  - 跑測案／導航：定位、讀畫面、設定相關的改完；`retry_assert` 的檢查保留廣泛攔截（檢查函式什麼錯都可能丟），但 `InvalidSessionIdException` 先往上丟；`_execute_step_safely` 本來就有處理
  - server：跟手機互動的 3 處（`_locate_best_effort`、`test_xpath`、AI 定位的 XPath 試查）改完。剩 8 處刻意保留：LLM 呼叫／回應解析（4）、HTTP 最外層回 500（2）、重連前的 `driver.quit()`（1）、批次裡一份測案出錯不影響下一份（1）——步驟 7 拆 server 時再看
- [x] `scan_core.py` 搬到 `platforms/scan_core.py`（`platforms/` 不再 import `core/`）
- [x] Android `parse_candidates_raw` 的 `nearby_text` 排除 `displayed="false"` 的隱藏節點，補測試
  - 影響：受影響的元件（旁邊有隱藏節點的）重新掃描後 `gen::` 唯一 ID 會變（指紋含 `nearby_text`）
  - iOS 確認過也有同樣情況（`visible="false"` 的兄弟節點文字也算進去），**這次沒改，待確認**：iOS 的 `visible=false` 多半是「捲出畫面外但節點還在」，錨點用 predicate 還是查得到；改的話 iOS 的 `gen::` ID 也會跟著變
- [x] 驗證（不用手機）：pytest 94 個全過（新增 `test_locate.py`、`test_wait_until_stable.py`、`test_crawl_screen.py`）；各模組單獨 import、`aitest-crawl/scan/run/server --help`、`aitest doctor`
- [x] 驗證（實機，2026-10-12）：使用者實機測試沒問題

### 步驟 5：RunContext 與 LLM 用量（報告 #4、CHANGELOG「token 使用量」後端部分） `refactor/run-context`
- [x] `RunContext`：vars、var_sources、dismiss_dialogs、stop_checker、progress、usage
  - 放在獨立的 `aitest/run_context.py`（連同 `LLMUsage`、`StopRequested`），`common.py` 只留 `xpath_literal`（2026-10-12 決定）
  - 另外加了兩個原本也是全域的執行狀態：`platform_state`（見下面 Android 那項）、`banner_capture_started`（`run_test_case` 的 `_banner_capture`，跑馬燈錄影開始時間）
- [x] `run_test_case`、`navigator`、`platforms/` 改成用參數傳 `ctx`（一律放第一個參數，平台方法放在 self 後面），刪掉全域變數和 `set_run_vars`／`set_dismiss_dialogs`（不留過渡期）
  - `Navigator` 建立時傳一次，存在 `self.ctx`
  - 爬蟲只有 `crawl`／`crawl_screen` 要 `ctx`（累計 `--describe` 的 LLM 用量），點擊、返回不用
- [x] `common.py` 的全域 `_stop_checker`、`set_stop_checker`、`raise_if_stopped` 刪掉，改成 `ctx.raise_if_stopped()`
  - server 的停止旗標 `_stop_requested` 還是一個（同一台手機一次只跑一個測案），每個 ctx 的 stop_checker 指向它
- [x] `platforms/android.py` 的 `_pre_watch_settings` 全域狀態改存 `ctx.platform_state`（平台物件是整個 server 共用的，不能存在物件上）
- [x] `generate_content_with_retry(usage=…)` 把用量累加到傳進來的 `LLMUsage`（只算成功的呼叫），5 個 LLM 呼叫點都接上：native 描述、無 id 描述、AI 定位、LLM 對話、情境產生
- [x] 一次掃描、爬蟲、測案、LLM 對話的用量各自加總，不會互相覆蓋
  - 回給網頁的 JSON：`/scan_screen`、`/describe_screen`、`/agent_xpath` 新增 `usage`；`/llm_chat`、`/llm_scenarios` 原本就有，多一個 `calls`；`/test_progress`、`/batch_progress` 也帶 `usage`（測案執行目前不會呼叫 LLM，都是 0）
  - 命令列：`aitest-scan` 結束時印總用量；`aitest-crawl --describe` 結束時印總用量
- [x] 網頁顯示每次操作的 LLM 用量（2026-10-12 決定只顯示每次的，不做累計）：LLM 對話每則回覆下面、情境產生結果上面、掃描（有勾描述時）、補描述、在根節點上面加一層、AI 定位的結果訊息裡；這次沒呼叫 LLM 就不顯示
- [x] `_progress`／`_batch_progress` 改成每次執行一份（存在 `ctx.progress`），server 只記最近一次是哪個 ctx；讀（輪詢）時在鎖裡複製一份，寫（步驟結果、批次每份測案的狀態）也在鎖裡
  - 原本批次執行跑步驟時會順便改到單份測案進度的 current（兩種進度共用 `_run_steps`），但批次執行時網頁不會讀單份測案的進度，使用者看不出來；現在 `_run_steps` 只在單份執行時更新步驟編號（`track_current`），避免蓋掉批次自己的「第幾組第幾份」
  - 導航過程的 log（`navigation`）append 沒有加鎖：list 的 append 本身不會讓讀的那邊出錯，頂多這次輪詢少看到一行
- [x] 驗證（不用手機）：網頁用假回應確認情境產生、LLM 對話的用量顯示正常、console 沒錯誤；pytest 107 個全過（新增 `test_run_context.py`：兩個執行緒同時跑「掃描」和「LLM 對話」用量各自正確、同一個用量多執行緒加總、只算成功的呼叫；`test_server_runs.py`：測案進度、執行中輪詢、批次 current 不被蓋掉）；server 實際啟動後 `/ping`、`/test_progress`、`/batch_progress`、`/describe_screen` 正常
- [x] 驗證（實機，2026-10-12）：使用者實機測試沒問題
- [x] 實機測試時發現、一起修掉的：
  - 登入後的「利用生物辨識登入」視窗：關閉非預期彈窗的按鈕清單加上「暫時不用」（Android 比對完整按鈕文字，原本只有「暫時不要」「暫不」），補 `tests/test_dismiss_modal.py`
  - AI 定位成功後面板重畫、結果訊息（含用量）被清掉：重畫後打開編輯區、把結果放回去

### 步驟 6：套件化（CHANGELOG「python code 專案結構調整」） `refactor/src-package`
2026-10-11 決定用根目錄的 `aitest/` 套件，不多包一層 `src/`（分層、避免撞名、console scripts 的好處都拿得到，結構比較簡單）。
提前到步驟 4、5 之前做：沒有其他進行中的分支，搬家不會造成衝突。

- [x] 改成 `aitest/`：`cli.py`、`config.py`、`common.py`、`viewer_server.py`、`platforms/`、`core/`（`git mv` 保留歷史）
  - `llm/` 等步驟 7 拆 `viewer_server` 時再建立
- [x] `pyproject.toml` 加 `[project.scripts]`（`aitest`、`aitest-server`、`aitest-workspace`、`aitest-crawl`、`aitest-scan`、`aitest-run`、`aitest-compare`）和 `[build-system]`（hatchling）
- [x] 修正用 `__file__` 找路徑的地方：`config.PROJECT_ROOT` 統一指向 repo 根目錄（`config.toml`、`web/`、`logs/`）；`aitest start` 改用 `python -m aitest.viewer_server` 啟動伺服器
- [x] 工具腳本移到 `tools/`：`cleanup_images`、`capture_transient`（`compare_versions` 是正式比對邏輯、測試也用到，放 `aitest/core/`）
- [x] README、`docs/guide/*.html`、`docs/CRAWLER_TREE_HANDOFF.md`、程式訊息、網頁裡的指令全部更新；`TROUBLESHOOTING.md` 舊紀錄保留原指令，開頭加說明
- [x] venv + pip 的安裝方式改成 `pip install -e .`（實際在新的 venv 裝過一次確認可用）
- [x] 測試改成 `from aitest… import`，拿掉 pytest 的 `pythonpath` 權宜設定
- [ ] 報告 #13：`print` 改成 `logging`，server 的 log 也存到 `logs/`（獨立一個分支做，不跟搬家混在一起）
- [x] 驗證（不用手機）：pytest 72 個全過；所有模組 import、7 個指令和 2 個 tools 的 `--help`、`aitest doctor`；
  `aitest-server` 啟動後用瀏覽器開網頁、開專案，地圖與截圖正常、console 沒錯誤；`aitest start` 能啟動伺服器
- [x] 驗證（實機，2026-10-12）：`uv run aitest start` 掃描、跑測案、爬蟲跑一小段
- [x] 額外（`feat/start-project-picker`）：`aitest start` 沒帶專案時用方向鍵選專案（questionary），實機驗證過

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
| 測案編輯與刪除：產出就直接儲存，之後可再編輯、刪除 | 不用新 API。現有的 `/api/projects/<專案>/cases/<名稱>`：GET 讀取、PUT 新增（`?overwrite=1` 覆蓋＝編輯）、DELETE 刪除。寫進 API 文件。注意：`POST …/rename` 目前只開放變數檔，測案不能改名 | [ ] |
| 可以在網頁上新增專案 | 新增 `POST /api/projects`（建立）、修改專案設定的 API（2026-10-11 決定：網頁也可以建專案，CLI 保留） | [ ] |
| 新增地圖的方式更直覺 | 地圖的新增、複製、改名、匯入、匯出、刪除 API 已經有，主要是前端操作。寫進 API 文件 | [ ] |
| token 使用量顯示在網頁上 | 步驟 5：後端用量累計＋網頁顯示每次操作的用量 | [x] |
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
| 2 | 沒有自動化測試 | 步驟 3 | [x] |
| 3 | Gemini 429 不重試 | 步驟 2 | [x] |
| 4 | 執行期設定放在全域變數 | 步驟 1（部分）、步驟 5 | [x] |
| 5 | 等畫面穩定有三種寫法 | 步驟 4 | [x] |
| 6 | 爬蟲自己又做了一套平台切換 | 步驟 4 | [x] |
| 7 | iOS／Android 定位程式複製貼上 | 步驟 4 | [x] |
| 8 | viewer_server 一支檔案做五件事 | 步驟 7 | [ ] |
| 9 | 前端 3,600 行擠在一個檔案 | 交給前端；後端提供步驟 8 的 API 文件 | [-] 交給前端 |
| 10 | `except Exception` 讓斷線看起來像找不到元件 | 步驟 4（server 剩 8 處留到步驟 7） | [x] |
| 11 | AI 定位每次重試都重送整個畫面結構 | 步驟 7 | [ ] |
| 12 | 文件提到已刪掉的檔案和舊模型名 | 步驟 2 | [x] |
| 13 | 全部用 print | 步驟 6 之後另開分支 | [ ] |
| 14 | 沒有程式碼檢查工具設定 | 步驟 2（風格類的 48 筆還沒修） | [x] |
| 15 | LLM 對話每輪重送全部內容 | 報告也建議先不動，等用量顯示後確認成本再決定 | [-] 暫不做 |
| 16 | 個人編輯器設定進了 git | 步驟 2 | [x] |

---

## 四、與報告的差距（步驟 9 填寫）

全部做完後，逐項寫下實際做法跟報告建議不同的地方，以及沒做到的部分。

| # | 報告建議 | 實際做法 | 差距／原因 |
|---|---|---|---|
| | | | |
