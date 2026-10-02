# 樹狀爬蟲（crawl_app_tree.py）交接筆記

> 給新 session 接手用。這份記錄「自動爬蟲產生 App 畫面樹地圖 → 餵 LLM 產測案」
> 這條線的所有進度、決策、踩過的坑。**效能瓶頸（`wait_stable()` 計時 bug）已於
> 2026-09-15 修復並驗證，8分鐘跑出 7 個畫面**（見「五之一」）。`recover_to` 偶爾
> 還是會真的失敗（不是慢，是沒回到預期畫面），是接下來可以深挖的方向。

最後更新：2026-09-15

---

## 一、這是在做什麼

原本的專案（`PROJECT_BRIEF.md`）是「人工導航到單一畫面 → 掃描」。使用者提出想要
**全自動爬蟲**：從一個起點畫面開始，點擊所有可互動元件、進去、重掃、再點，
遞迴遍歷，產出一份「畫面樹 + 完整導覽圖」，每個畫面都能追回上一層，最後用這份
地圖讓 LLM 產生跨畫面測案。

專案原本有一支舊的 `crawl_app.py`（扁平輸出、關鍵字黑名單、iOS），這條線是
**新寫一支 `crawl_app_tree.py`**，不動舊的。

### 使用者對「樹怎麼開頭」的定案（討論後）
- 樹根 = 「到達穩定登入狀態後看到的第一個畫面」（使用者手動導航到那裡再啟動爬蟲）
- level-1 子節點 = 底部導覽列（tab bar / 一排 nav Button）＋根畫面自己的可點元件
- **不是一棵樹，是一張圖 + 一棵生成樹**：同一畫面可從多處到達 → 記 `parent_screen_uid`
  ＋物化的 `path[]` 麵包屑（生成樹），另存一份 `edges[]` 記完整圖（含 cross-link）
- **畫面識別用「模板級」**：台積電詳情 vs 聯發科詳情 → 同一個 `screen_uid`
  （骨架一樣就是同一節點，實際是哪檔股票記在 edge 上）
- **不用 LLM 判斷「同不同畫面」**，純結構比對（延續 PROJECT_BRIEF 的取捨）

---

## 二、環境現況（新 session 先重新確認）

| 項目 | 值 |
|---|---|
| 實體手機 | `測試手機 i14`，UDID `00008110-0019098C34A8201E`，iOS 17.5.1 |
| WDA 簽章 | 2026-09-10 重簽過，**效期到 2026-09-15**（免費帳號 7 天，過期要在 Xcode 重簽，見 `TROUBLESHOOTING.md` 問題 7；重簽帳號是 `kay22223838@gmail.com` / Team `5J7FAZXLYX`） |
| WDA 專案 | `~/.appium/node_modules/appium-xcuitest-driver/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj`，target `WebDriverAgentRunner`，bundle `com.cfhtest001.WebDriverAgentRunner`，已配置好 signing |
| Appium | 3.6.0 + xcuitest driver 12.3.2，本 session 起在 :4723（log `/tmp/appium_ios_scan.log`），砍掉重開一個乾淨的即可 |
| Python | Appium-Python-Client **6.0.0**，`GEMINI_API_KEY` 只在互動式 shell（先 `source ~/.zshrc`） |
| `test_config.py` 的 `bundleId` | 目前是 `com.cathay.securities.tw.mBroker.Demo`（樹精靈Demo） |

### 重啟 Appium
```bash
pkill -f appium; sleep 1
source ~/.zshrc && nohup appium --log /tmp/appium_ios_scan.log --log-timestamp --local-timezone >/tmp/appium_nohup.log 2>&1 &
```

---

## 三、走過的路（換過的 App / 平台，及原因）

| 嘗試 | 結果 / 為什麼放棄 |
|---|---|
| **國泰證券-測試**（`com.cathaysec.newapp.test`，iOS） | 有登入牆但有測試帳號、半自動可過。dry-run 發現：(a) tree 每個元件重複 2x；(b) sub-tab（委託/成交/庫存…）是原地換內容不換頁、且 tree 讀不到「哪個被選中」→ 會塌成同一節點且被記 noop；(c) **測試帳號是空的**（查無資料），沒有資料列可下鑽 → 帳務子樹幾乎是空的。使用者決定換 App |
| **Android 模擬器 `test_phone`**（emulator-5554, Android 16） | 使用者想改用 Android（`current_activity` 當畫面 id 更穩、有 `TestBankApp` 靶場）。已裝 `com.testfield.bankapp`（靶場，有 `MAP.md` 標準答案）、`com.cathaysec.eservice`（3.7.16）。**沒把爬蟲 port 成 Android 版** |
| **eService dev debug APK**（`com.cathaysec.eservice.flavorDeveloperTest` 3.7.18-AFA，Android） | 從 `https://mapptest.cathaysec.com.tw/QRCode/NewApp/eService_dev_debug_1.apk`（原連結 `.a` 要補成 `.apk`）下載、`adb install -r -g` 成功。啟動跳 root/偵錯偵測對話框（有「繼續使用」可放行）。**使用者說連不進去（IP 限制）** |
| **IP 限制排查** | 模擬器走 NAT，對外 IP = 主機 IP（實測都是 `203.74.115.97`）。使用者的 WireGuard VPN「zzz」當時是斷線的。結論：連 VPN 主機 IP 若變、模擬器通常跟著變（不保證）；也可能根本不是 IP 問題（Play Integrity / cert pinning / root 偵測降級） |
| **cathaybk 測試 APK**（`https://smuat.megatime.com.tw/EAS/Apps/cathaybk/cathaytest/...apk`） | 下載需登入「精誠隨想 SoftMobile App Store」，`302` 轉 `/EAS/login.aspx`。**沒帳號抓不到**，要使用者自己用瀏覽器登入下載 |
| **樹精靈Demo**（`com.cathay.securities.tw.mBroker.Demo` 3.3.60，iOS，TestFlight 裝的） | 換回 iOS + 這支。**一開始沒有登入牆**、直接進「自選庫存」有範例資料、無截圖保護、native id 品質好（`MBKMetroQuote_QuoteDataCell_0_0` 這種）。**首選目標**。但後來被我 `terminate_app` + `activate_app` 重啟後，掉到登入頁了（見「目前卡點」） |

---

## 四、已完成的程式（本 session）

### 4a. `capture_screen_map.py`（iOS 掃描主程式）— 加了參數，維持舊行為為預設

- **橫向捲動收集**（階段一 port，補 iOS 缺口）：新增 `find_scrollable_regions(xml, classes=)`、
  `region_signature()`、`swipe_within_region()`、`_identity_keys_in_region()`、
  `_read_and_merge_round()`、`collect_horizontal_region()`；`collect_candidates_with_scroll()`
  改成每輪垂直捲動之間先處理巢狀橫向容器。iOS 沒有 Android `mobile: scrollGesture`
  的布林回傳值 → 用「容器內元件有沒有換一批」判斷是不是真橫向容器（比 Android 脆弱，
  已知落差，`XCUIElementTypeTable` 刻意排除避免誤觸 swipe-to-delete）
- **截圖保護降級**：新增 `is_screenshot_blocked()`；`describe_generated_elements()` 加
  `screenshot_available` 參數（False → 純文字 prompt）；`scan_screen()` 加
  `screenshot_blocked` 欄位、被擋就不裁切
- **`scan_screen()` 新參數**（都預設 = 舊行為）：
  - `describe=True/False` — False 跳過所有 LLM 呼叫，`description_source="skipped"`
  - `scroll=True/False` — False 只掃最頂部一屏（爬蟲用，捲動收集到的元件座標點不到）
  - `wait_stable=True/False` — False 跳過「輪詢等畫面穩定」（報價畫面永遠不穩定，
    每畫面白等 7.5s 是爬蟲最大瓶頸）

### 4b. `run_test_case.py`（iOS 測案執行器）— 階段一 port

- 移除只看當下快照的 `_locate_by_xpath()`，換成 `_locate_by_xpath_with_scroll()`：
  精確 XPath 查 0 個 → 自己捲容器邊捲邊重查（比照 Android 版，現在在 `platforms/android.py`）。
  用途：長 Table/CollectionView 因 cell 重用，遠處的 row 會整個從 tree 消失
- `import` 了 `capture_screen_map` 的 `find_scrollable_regions` / `swipe_within_region`

### 4c. `crawl_app_tree.py`（新檔，樹狀爬蟲）— 本 session 主產出

輸出 `app_map_tree.json`（不動 `element_map.json`），每掃完一畫面整份重寫一次。

**7 項防護（跟舊 `crawl_app.py` 的差異）**
1. **停在 App 邊界**：tap 後 `query_app_state != 4` 或 `context != NATIVE_APP` →
   `activate_app` 拉回、記 `edges` kind=external、不遞迴。預先跳過 `XCUIElementTypeLink`、
   文字含「客服專線/撥打/0800/門號」或電話號碼的
2. **被登出偵測**：`page_source` 出現 `LOGOUT_PHRASES`（重新登入/已登出/閒置過久…）
   → 丟 `CrawlAborted` 中止、**不存那個畫面**（設定頁的「登出」按鈕用完整片語比對，不誤判）
3. **截圖保護降級**：交給 `scan_screen` 內建
4. **模板級骨架簽章 `skeleton_signature()`**：只取 `native_ids` 集合 +
   `class 直方圖(分桶 0-4 精確/5-9/10-19/20+)` + `selected_tab`(TabBar value=1) +
   `nav_title`(NavigationBar name，非數字/日期時)。**忽略會變動的資料**（`_looks_like_data`
   排除金額/百分比/日期/時間/佔位符）。只看 visible=true
5. **常駐 chrome 排除**：`persistent_chrome_keys()` = TabBar 底下的 +
   畫面最上緣（y < 11%）/ 最下緣（y > 88%）的可點元件（含 name 開頭「btn 」的一排
   Button，不限 XCUIElementTypeTabBar）。候選過濾時**位置優先**（抓得到沒 identifier
   的 icon 列 / nav cell），再具名比對。`--include_nav` 且 depth==0 時保留當 level-1
6. **樹 + 圖**：每畫面 `screen_uid`/`screen_name`(優先 nav_title)/`parent_screen_uid`/
   `entry_element_uid`/`depth`/`path[]`(物化麵包屑)/`skeleton`/`elements[]`；
   `edges[]` kind ∈ {tree, cross-link, external, noop}
7. **黑名單預設關**（`--safe_keywords` 才開，已擴充買進/賣出/改價/刪單/圈存/生物辨識…）

**其他**
- 可點的 StaticText（`interactive_statictext_keys()`）：accessible=true + 文字不像資料 +
  無 Button/Cell 祖先 → 也當候選（樹精靈Demo 的分頁列 證券庫存/期權庫存/複委託庫存
  是這種）
- **modal 自動關**（`dismiss_modal()`）：偵測 XCUIElementTypeAlert 或
  `_MODAL_HINT_MARKERS`，點 `MODAL_DISMISS_LABELS`（我知道了/關閉/略過/繼續使用…），
  找不到且看起來純資訊時才點 `MODAL_ACK_ONLY_LABELS`（確定/OK）。永遠不點
  `NEVER_TAP_LABELS`（關閉APP/登出…）。**傳 page_source 進去做字串快篩**，沒線索直接
  return（每次 tap 後都呼叫，這個快篩省很多 WDA 往返）
- **同模板取樣**（`_candidate_pattern()` + `--sample_per_pattern`，預設 3）：
  `MBKGridQuote_QuoteDataCell_0_12` → pattern `MBKGridQuote_QuoteDataCell`。同 pattern
  點過 3 個都導到同一畫面 → 跳過其餘（78 個報價卡變 ~3 個）
- **效能優化**（本 session 後段做的）：爬蟲呼叫 `scan_screen(wait_stable=False)`、
  `page_source` 抓一次傳下去、`recover_to` 用短 `wait_stable(1.5)`、`dismiss_modal`
  字串快篩。**優化後還沒對樹精靈Demo 完整跑過一次**（見卡點）

### 執行方式
```bash
# 先手動把 App 導到「穩定登入後的第一個畫面」
python3 crawl_app_tree.py --root_name <名字> --dry_run                    # 只列會點什麼
python3 crawl_app_tree.py --root_name <名字> --max_depth 2 --max_screens 20            # 子樹爬
python3 crawl_app_tree.py --root_name <名字> --include_nav --max_depth 2 --max_screens 20  # 整支 App 爬
```

---

## 五、對各 App 的實測發現

### 國泰證券-測試（帳務首頁 dry-run）
- tree 每個 Cell/StaticText **重複 2 份**（座標一模一樣）→ 已加去重（by class+label+bounds）
- 兩排橫向 sub-tab（總覽/台股/…、委託/成交/庫存/對帳單/交割款）都是 `Cell`，
  **選中狀態 tree 讀不到**（無 selected/value）
- 測試帳號空（查無資料）
- 有「報價連線中斷」的攔截彈窗（確定鈕）

### 樹精靈Demo（有登入態時，2026-09-10 首次跑）
**機制運作得很好：**
- `nav_title` 抓到的是 **iOS ViewController 類名**（`TWCTMainVC` / `TWCTStockOverviewVC` /
  `TWCTOrderVC`）→ 超穩的畫面 id，命名乾淨
- 模板級去重正確：點 4 個不同股票報價卡 → 全連到同一個 `TWCTStockOverviewVC`
  （1 tree edge + 3 cross-link）
- `edges` 的 `via` 記錄了點哪個元件過去（`QuoteDataCell_0_0`→總覽、`_0_1`→下單，分得出來）
- chrome 排除正確（底部 btn 自選/報價/下單/帳務/更多 + 頂部圖示列，14 個都對）
- `--max_depth 2` 保護生效：爬到 `TWCTOrderVC`（下單頁）就停，沒進去點買賣鈕

**問題：**
- 底部導覽不是 `XCUIElementTypeTabBar`，是一排 `name="btn 自選"` / `"btn 報價 normal"`
  的 Button（未選中的 name 有「 normal」後綴，選中的沒有）
- 舊版（優化前）**太慢**：~15 分鐘只爬 4 畫面。瓶頸：`wait_for_screen_stable` 7.5s/畫面、
  `dismiss_modal` 每次 tap 後列舉所有 Button、196 元素畫面的 `page_source` 很慢、
  78 個同模板報價卡逐一點。→ 已做優化，待驗證
- `TWCTMainVC`（列表模式）vs `TWCTMainVC_2`（九宮格模式，196 元素）：同一個 VC 不同
  顯示模式，骨架不同 → 目前算兩個節點（可討論是否該合併）

---

## 五之一、2026-09-15 驗證結果（登入態 + 爬蟲速度）

**背景**：手機重新接上（USB 配對信任重建）、WDA 免費簽章當天過期（`0xe8008011 provisioning profile has expired`），
在 Xcode 對 `WebDriverAgent.xcodeproj` 按 Cmd+U 重簽成功（CLI 版 `xcodebuild -allowProvisioningUpdates`
卡住不動，疑似在等 Apple ID 2FA，改用 Xcode GUI 才成功）。使用者手動登入樹精靈Demo，回到自選庫存畫面
（九宮格模式，有真實報價資料）。

**登入態**：整次驗證（含中途 WDA session 重建）**沒有再發生被登出/掉登入態**的狀況，
`driver.quit()` 收尾都沒用 terminate_app/activate_app，符合上次教訓。

**速度結果**（`--root_name shuling_watchlist --include_nav --max_depth 2 --max_screens 20`）：
- 總耗時 `11:59.13`（~12 分鐘），只收錄 3 個畫面（跟優化前、跟 9/11 那次未記錄的跑法結果一樣：
  `TWCTMainVC → TWCTMainVC_2 → TWCTStockOverviewVC`）
- **單一畫面的 `scan_screen()` 本身已經很快**（11~15 秒/畫面，`wait_stable=False` 那個優化有效）
- **真正吃掉時間的是 `recover_to()`（退回上一層）**：depth 見底要退回 `TWCTMainVC_2` 時，
  從 15:49:11 等到 15:53:15（**約 4 分鐘**）才放棄回報「無法確認回到」；再退回 `TWCTMainVC` 時，
  15:53:15 等到 15:57:58（**又約 4 分 43 秒**）才又放棄。這兩次失敗的 recover 就吃掉了
  ~9 分鐘（總 12 分鐘的大部分），而且**放棄後直接丟掉那個畫面剩下的所有候選**
  （`TWCTMainVC_2` 剩 232 個候選、`TWCTMainVC` 剩 38 個候選整批沒點）

**根因（已定位，尚未修）**：`crawl_app_tree.py` 的 `wait_stable()`（約行 416-428）：
```python
def wait_stable(driver, max_wait: float = 3.0, poll: float = 0.4) -> None:
    previous = None
    elapsed = 0.0
    while elapsed < max_wait:
        current = driver.page_source   # 實際耗時完全沒被計入 elapsed
        if current == previous:
            return
        previous = current
        time.sleep(poll)
        elapsed += poll                # 只用固定 poll 累加，不是真實流逝時間
```
1. `elapsed` 只靠迴圈次數 × `poll` 累加，**沒扣掉 `driver.page_source` 本身的耗時** ——
   畫面元件多（如 386 個的 `TWCTMainVC_2`）、`page_source` 一次要跑好幾秒時，
   實際等待時間會遠超傳進來的 `max_wait`（`recover_to` 傳的是 1.5 秒，實際觀察是分鐘等級）
2. **報價類畫面的股價是即時跳動的**，每次 `page_source` 內容幾乎一定跟上一次不同
   （`current == previous` 永遠不成立）→ 這種畫面在 `wait_stable` 眼中**永遠不會穩定**，
   每次呼叫都會被迫跑滿（而且因為問題1會遠超）上限
3. **這條路徑跟 `capture_screen_map.py` 的 `scan_screen(wait_stable=False)` 優化是分開的兩套邏輯**——
   `crawl_app_tree.py` 自己在 `recover_to()`（第502行）、候選點擊後（第706、721行）另外呼叫了
   `wait_stable()`，那次優化沒覆蓋到這裡

**已修（2026-09-15，同一 session）**：`wait_stable()` 改用 `time.monotonic()` 量測真實流逝
時間，取代原本「迴圈次數 × 固定 poll」的假 elapsed。改法很單純，沒動 `recover_to`/呼叫端邏輯。

**修復前後對比**（同一組參數 `--root_name shuling_watchlist --include_nav --max_depth 2 --max_screens 20`，
同一個已登入 session 連續兩次跑）：

| | 修復前 | 修復後 |
|---|---|---|
| 總耗時 | 11:59（~12分） | 7:59（~8分） |
| 收錄畫面數 | 3 | **7** |
| edges | 2 | 12（6 tree + 4 noop + 2 cross-link） |
| 單次 recover_to 失敗要等多久 | ~4分~4分43秒（2次，吃掉9分鐘） | ~40秒~39秒（2次，吃掉~80秒） |
| 單次「點擊→進新畫面」 | 首次 2分6秒 | 8~22秒 |
| 安全性 | — | 全程無登出、無跑出 App 外、無例外 |

**結論**：`recover_to` 還是偶爾會失敗（這次跑最後仍有 2 次「無法確認回到」），
但失敗的代價從「災難性拖累整個 run」降到「小失血」，整體吞吐量提升到約
68 秒/畫面，落在原本設定的 ~1-2 分鐘/畫面目標範圍內。剩下的「recover_to 為什麼還會
失敗」（不是慢、是真的沒回到預期畫面）是下一個可以深挖的方向，但不再是效能瓶頸的
主因了。

---

## 六、（已解決 2026-09-15）舊卡點：登入態掉了

> 以下是 2026-09-10 的舊記錄，**已在 2026-09-15 解決**（使用者手動重新登入回到自選庫存），
> 保留原文供參考。最新狀態、真正的效能瓶頸見上方「五之一」。

我用 `driver.terminate_app()` + `activate_app()` 重啟樹精靈Demo 想回到乾淨狀態，
結果 App 走了「啟動 → 公告頁 → **登入頁**」。原本首次跑的時候是直接進「自選庫存」
有範例資料的（`noReset: True` 有一個持久化 session），這個 session 在我重啟後掉了。

**教訓仍然有效：不要再隨便 `terminate_app` / `activate_app` 真實 App**，會弄掉登入態。
要重置畫面優先用 App 自己的返回/首頁，或問使用者。

**下一步（新 session 接手）**：先修「五之一」定位到的 `wait_stable()` 真實耗時 bug，
再重跑速度驗證。

---

## 七、已知問題 / 待討論

1. **in-place 換內容的 sub-tab / 顯示模式切換**：點了不換頁（skeleton 一樣）→ 記成
   noop，不會變成子節點。國泰證券的 委託/成交/庫存 和樹精靈的 列表/九宮格都是這種。
   要不要「偵測到分頁列 → 每個 tab 強制當一個 view 節點（screen_uid = 骨架 + 點的 tab 文字）」
   還沒定案（使用者上次在這個問題被中斷、還沒回答）
2. **骨架簽章對「無 nav_title、無 native_id」的畫面很弱**：只剩 class 直方圖，可能誤合併。
   國泰證券帳務內頁就是這種
3. **同 VC 不同模式算幾個節點**（TWCTMainVC vs TWCTMainVC_2）
4. **`go_back` / `recover_to` 對自訂導覽的 App 可能不準**（找 NavigationBar 第一個 Button
   / 左上角 Button / driver.back() / 邊緣滑）
5. **登入牆 / 空測試帳號**是反覆遇到的結構性障礙
6. **`--include_nav` 全 App 爬 + 黑名單關**會走進下單頁並點買賣鈕（樹精靈是 Demo 應該
   sandbox，但真 App 要小心）

---

## 八、相關檔案

| 檔案 | 說明 |
|---|---|
| `crawl_app_tree.py` | **本條線主程式**（新，iOS 樹狀爬蟲） |
| `app_map_tree.json` | 爬蟲輸出（screens[] + edges[] + skipped[]） |
| `crawl_app.py` | 舊的扁平爬蟲，保留當參考，沒動 |
| `capture_screen_map.py` | iOS 掃描主程式，本 session 加了橫向捲動、截圖保護、scan_screen 三參數 |
| `run_test_case.py` | iOS 測案執行器，本 session port 了 `_locate_by_xpath_with_scroll` |
| `platforms/android.py` | Android 版（原本的 `capture_screen_map_android.py` / `run_test_case_android.py`，已併入共用程式），是這次 iOS port 的來源參考 |
| `test_config.py` | 讀 `config.toml` 組 iOS／Android 連線設定；App 的 bundle id 在專案設定（`workspace.py`），換 App 不用改設定檔 |
| `IOS_FULL_SCAN_HANDOFF.md` | 更早的交接筆記（iOS 全功能驗證，另一條線） |
| `TROUBLESHOOTING.md` | 所有踩坑記錄（問題 5/6/7 = WDA 環境；31/34/42 = 捲動；41 = iOS 移植） |
| `ANDROID_TEST_APP_SPEC.md` | TestBankApp 靶場規格（若要 port 爬蟲到 Android 可用） |
| `TestBankApp/MAP.md` | TestBankApp 的 Activity / resource-id 對照表（標準答案） |

## 九、操作提醒（延續舊教訓）

- `viewer_server.py` 常駐時不要另開腳本連同一裝置（並行 session 不穩）。跑
  `crawl_app_tree.py` / `capture_screen_map.py` 前先停掉 viewer
- Bash 工具跑 Python 前先 `source ~/.zshrc`（`GEMINI_API_KEY`）
- 長時間背景跑的 Python 記得 `-u` 或 `stdbuf -oL`，否則 stdout 被 block-buffer 到看不到進度
- **不要隨便 terminate/relaunch 真實 App**（弄掉登入態，見卡點）
