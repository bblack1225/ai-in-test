# 專案交接文件：App 元件地圖／自動化測試 POC

交接日期：2026-09-22

## 快速導覽

### 環境需要準備什麼

- Mac + Xcode、Node.js + Appium 3（含 XCUITest driver）
- **uv**（管理 Python 版本與套件）：`uv sync` 一個指令建好環境。系統內建的
  Python 3.9 跑不起來，專案固定用 Python 3.12
- Gemini API key：填在 `config.toml` 的 `[llm] api_key`，或環境變數 `GEMINI_API_KEY`
  （LLM 描述/定位/測案/情境產生都要用到，沒設定的話這些功能會失敗，但純粹的定位/點擊/爬蟲探索不需要）
- iOS 實機：USB 接 Mac、開發者模式、Xcode 登入 Apple ID 並簽署過
  WebDriverAgent；`config.toml`（從 `config.example.toml` 複製）的 `udid`／
  `xcode_org_id`／`wda_bundle_id` 要填你自己的
- 詳細步驟見下方「二、環境準備」；Android 的差異見「2.9」

### 每個檔案／資料夾做什麼（一覽表，細節見「四、檔案總覽」）

**iOS / Android 共用（用 `--platform` 切換）**
- `crawl_app_tree.py` — 自動爬蟲，遞迴探索整支App，產出樹狀地圖
- `capture_screen_map.py` — 掃描目前畫面，產生元件清單＋LLM描述，`--platform ios|android`
- `run_test_case.py` — 執行測試案例（tap/input/assert），`--platform ios|android`
- `platforms/` — iOS／Android 的平台差異（定位、點擊、畫面解析），其他程式只透過這裡取用
- `viewer_server.py` — 本機橋接伺服器，讓瀏覽器工具能操作手機（`--platform ios|android`）
- `config.toml` — 本機設定（手機 UDID、簽署 Team ID、WDA bundle id、Gemini 模型），**換電腦/手機改這裡**；
  不進 git，範本是 `config.example.toml`。App 的 bundle id 不在這裡，在專案設定（見 3.3）
- `test_config.py` — 讀 `config.toml`，組出 Appium 連線設定、提供 Gemini 呼叫，其他程式都從這裡取用
- `compare_versions.py` — 改版前後元件差異比對（正式版本，邏輯以此為準）
- `navigator.py` — 批次執行用的畫面導航：辨識目前在哪一頁、按返回／照地圖往下點、必要時重開 App，把手機帶到測案的起始畫面
- `workspace.py` — 專案資料夾：每支 App 每個平台一個專案，地圖／截圖／測案／批次放一起，網頁和爬蟲讀寫同一份；可匯出成 zip 交給其他團隊

**瀏覽器工具（由 `viewer_server.py` 提供，打開 http://127.0.0.1:8767/）**
- `app_map_viewer.html` — **目前主力工具**：地圖檢視、手動建樹、改版比對、
  測試情境產生、LLM測案產生、測案執行，全部整合在這一頁
- `viewer.html` — 較早期的單畫面工具，Android測試目前還在用

**資料檔案**
- `workspace/projects/<專案>/` — **建議的存放方式**：`maps/`（地圖，可以依功能分好幾張）、`screenshots/`、`cases/`（測案）、`batches/`（批次），見「3.3 專案」
- 以下是沒開專案時的舊方式（跟程式同層，程式用相對路徑讀寫，不要搬動）
- `app_map_tree.json` — 爬蟲產出的樹狀地圖（沒加 `--project` 時）
- `element_map.json` — 單畫面掃描累積資料，`viewer.html`系列吃這個
- `screenshots/`／`crops/` — 畫面截圖／無ID元件裁切圖（裁切圖預設不產生，清理見 `cleanup_images.py`）
- `test_cases/`／`test_results/` — 範例測試案例／執行結果截圖

**其他**
- `docs/` — 6份技術文件＋3份真實掃描XML參考資料，細節見「四、檔案總覽」
- `README_交接文件.md` — 就是這份文件

---

## 一、這個專案在做什麼

一套「不用寫死 selector」的手機 App UI 自動化測試 POC，核心想法：

1. 掃描 App 畫面的 Accessibility Tree，抓出每個元件（不管有沒有原生
   accessibility id），用 LLM 幫沒有 id 的元件生成用途描述，存成結構化
   JSON（每個元件一個 `unique_id`）
2. 執行測試時，照「native id → 自動生成的 XPath → 鄰近文字錨點消歧義
   → 座標點擊」這個固定順序去定位元件，不是每次都要人工重寫 selector
3. 上面兩步之外，還有一個「自動爬蟲」子系統：從一個起始畫面開始，自動
   探索安全的可互動元件、遞迴掃描整支 App，畫出完整的畫面樹狀地圖
4. 最後在瀏覽器工具（`app_map_viewer.html`）上人工操作：看地圖、手動
   補畫面、產生測試情境、產生測試案例、執行測試案例，全部在同一個網頁裡

專案有 **iOS** 和 **Android** 兩條並行的程式碼，邏輯基本上一一對應，
Android 先開發驗證過，iOS 再移植，但**iOS 版直到最近才真的有實體 iPhone
可以實測**（詳見 `docs/CRAWLER_TREE_HANDOFF.md`），Android 版相對成熟。

---

## 二、環境準備（第一次上手，iOS 實機）

目前主要使用 iOS 實機，以下 2.1～2.8 是實際跑通過的完整流程。Android
的差異集中在 2.9，那部分**還沒有照新流程實際驗證過**。

以下指令都在專案資料夾底下執行。

### 2.1 需要先裝好的工具

| 工具 | 用途 | 確認指令 | 沒裝的話 |
|---|---|---|---|
| Xcode | 建置、簽署 WebDriverAgent | `xcodebuild -version` | App Store 安裝 |
| uv | Python 版本與套件管理 | `uv --version` | `brew install uv` |
| Node.js | 執行 Appium | `node --version` | 用 nvm 或 Homebrew 安裝 |
| Appium 3 | 自動化伺服器 | `appium --version` | `npm install -g appium` |
| XCUITest driver | Appium 的 iOS driver | `appium driver list --installed` | `appium driver install xcuitest` |

> **不要用系統內建的 `python3`**：macOS 內建的是 Python 3.9，程式裡有
> `str | None` 這類 3.10 以後才支援的寫法，一 import 就會出現 `TypeError`。
> 一律透過 `uv run` 使用專案指定的 Python 3.12。

### 2.2 建立 Python 環境

```
uv sync
```

uv 會依照 `.python-version` 自動下載 Python 3.12（不會動到系統的
Python），並依照 `uv.lock` 在 `.venv/` 裝好所有套件（版本完全鎖定）。
確認：

```
uv run python -c "import appium, selenium, PIL, google.genai; print('套件 OK')"
```

之後：
- 執行任何程式都用 `uv run python xxx.py`，不需要 `source .venv/bin/activate`
- 新增套件用 `uv add 套件名`，**不要用 `pip install`**（不會記錄進
  `pyproject.toml`／`uv.lock`，別人拿到專案就裝不到）

### 2.3 設定 Gemini API Key

兩種方式擇一（都有設的話用環境變數）：

- **寫進 `config.toml`**（建議，`config.toml` 不進 git）：先做完 2.6 建立 `config.toml`，
  在 `[llm]` 填 `api_key = "你的key"`。注意這個檔案含 key，不要傳給別人
- **環境變數**：寫進 `~/.zshrc`，之後開新的終端機都會自動生效
  ```
  echo 'export GEMINI_API_KEY="你的key"' >> ~/.zshrc && source ~/.zshrc
  ```

確認（不會印出 key 本身）：

```
uv run python -c "import test_config as t; t.get_llm_client(); print('設定 OK，模型：', t.LLM_MODEL)"
```

沒設 key 的話只有 LLM 相關功能（描述、AI 定位、測案／情境產生）不能用，看地圖、點擊、爬蟲照常能跑。

### 2.4 準備 iPhone

1. USB 接上 Mac、解鎖，手機上點「信任這台電腦」
2. 開啟開發者模式：設定 → 隱私權與安全性 → 開發者模式（找不到這個選項，
   通常是這支手機還沒被 Xcode 連過，先完成 2.5 的步驟 3 再回來開）
3. 確認 Mac 看得到手機，State 要是 `available (paired)`：
   ```
   xcrun devicectl list devices
   ```
4. 查 UDID（`== Devices ==` 底下手機那一行最後括號裡的值）：
   ```
   xcrun xctrace list devices
   ```
5. 確認待測 App 已經裝在手機上，並查它的 bundle id：
   ```
   xcrun devicectl device info apps --device 你的UDID
   ```

> 建議使用專門的測試機：測試時會安裝開發者簽署的 WebDriverAgent、開啟
> 開發者模式，WDA 執行時手機上也會開著 8100 port，不適合用存有個人或
> 公司敏感資料的私人手機。

### 2.5 簽署 WebDriverAgent

WebDriverAgent（WDA）是 Appium 裝到手機上、實際操作畫面的 XCTest App，
手機桌面上會出現一支 **WebDriverAgentRunner-Runner**（這是正常的，不要刪）。

1. **Xcode 登入 Apple ID**：Xcode → Settings → Accounts → 左下角 ＋ →
   Apple ID。**這步一定要做**，沒登入的話 Xcode 沒辦法把新手機加進描述檔，
   Appium 連線會報 `xcodebuild failed with code 65`
2. 打開 WDA 專案：
   ```
   open ~/.appium/node_modules/appium-xcuitest-driver/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj
   ```
   TARGETS 選 **WebDriverAgentRunner** → Signing & Capabilities：
   - 勾選 Automatically manage signing
   - Team：選你的帳號
   - Bundle Identifier：自己取一個並**固定使用**，例如
     `com.你的名字.WebDriverAgentRunner`
3. 上方 Scheme 選 **WebDriverAgentRunner**、裝置選你的 iPhone →
   Product → Test（⌘U）。手機跳出「不受信任的開發者」的話：設定 → 一般 →
   VPN 與裝置管理 → 信任，再按一次 ⌘U。看到測試開始執行、手機顯示
   **Automation Running** 就代表成功，按 ⌘. 停止
4. 查 Team ID（`config.toml` 要用）。**Xcode 的 Accounts 頁面只顯示
   團隊名稱、看不到 ID**，用以下任一方式：
   - 終端機（`organizationalUnitName` 那一行就是 Team ID）：
     ```
     security find-certificate -a -c "Apple Development" -p | openssl x509 -noout -subject -nameopt multiline
     ```
   - 鑰匙圈存取 → 登入 → 我的憑證 → 雙擊 `Apple Development: ...` →
     詳細資訊的「組織單位」
   - WDA 專案 → Build Settings（選 All）→ 搜尋 `Development Team`

> **免費 Apple ID 的限制**：
> - 簽出來的 WDA **只有 7 天效期**，到期症狀是 Appium 連線報
>   `0xe8008011` 或 WDA 打不開。處理方式：刪掉手機上的
>   WebDriverAgentRunner-Runner 讓 Appium 重新安裝，或重做上面的步驟 3。
>   **不用改 bundle id**
> - 每 7 天最多只能註冊 10 個新的 App ID，所以 bundle id 決定後就不要一直換
> - 加入公司的付費開發者團隊後，效期是 1 年，也沒有 App ID 數量限制

### 2.6 建立 `config.toml`

```
cp config.example.toml config.toml
```

打開 `config.toml`，填 `[ios]` 這一段：

| 欄位 | 填什麼 |
|---|---|
| `udid` | 2.4 查到的手機 UDID |
| `xcode_org_id` | 2.5 查到的 Team ID |
| `wda_bundle_id` | 2.5 在 Xcode 設定的 WDA Bundle Identifier |
| `launch_bundle_id` | 選填。留空＝連線時不打開任何 App，直接操作手機上目前在前景的 App（換測不同 App 時在手機上切過去就好）；填了就是每次連線都先打開這支 App |

待測 App 的 bundle id 不填在這裡，填在專案設定（`workspace.py create`／`update`，見 3.3）：
爬蟲用它判斷有沒有跑出 App、連線時打開它；批次執行用它重開 App。

`config.toml` 不進 git（每台電腦、每支手機不一樣）。要切換好幾份設定（例如同一台電腦接兩支
手機），可以用環境變數 `AITEST_CONFIG=其他檔案.toml` 指定。

確認設定有被讀到：

```
uv run python -c "import test_config as t; print(t.ios_capabilities())"
```

缺欄位的話會直接說缺哪個、要填在哪裡。WDA 建置失敗（`xcodebuild failed with code 65`）想看
詳細錯誤時，在 `config.toml` 的 `[capabilities.ios]` 加 `showXcodeLog = true`。

### 2.7 第一次連線測試

**視窗 1**：啟動 Appium。加上 `--address 127.0.0.1` 只允許本機連線，
否則同一個網路裡的其他電腦也能連進來操作手機：

```
appium --address 127.0.0.1
```

**視窗 2**：手機保持解鎖，執行連線測試（只讀取資訊，不會點擊任何東西）。
第一次會建置並安裝 WDA，可能要等 2～5 分鐘：

```
uv run python - <<'EOF'
from appium import webdriver
from appium.options.ios import XCUITestOptions
from test_config import APPIUM_SERVER_URL, ios_capabilities

d = webdriver.Remote(APPIUM_SERVER_URL, options=XCUITestOptions().load_capabilities(ios_capabilities()))
try:
    print("連線成功")
    print("螢幕大小：", d.get_window_size())
    print("前景 App：", d.execute_script("mobile: activeAppInfo"))
finally:
    d.quit()
EOF
```

看到「連線成功」，而且「前景 App」的 `bundleId` 是手機上目前開著的那支 App，就代表整條
路徑（Python → Appium → WDA → 手機）都通了。

### 2.8 常見問題

| 症狀 | 原因 | 處理方式 |
|---|---|---|
| `Could not find a driver for automationName 'XCUITest'`，但 `appium driver list` 明明有裝 | **同時開著好幾個 Appium**，Python 連到舊的那個（綁 `127.0.0.1` 的會優先接到請求，而且兩個同時開著也不會出現 port 被佔用的錯誤） | `lsof -nP -iTCP:4723 -sTCP:LISTEN` 檢查，應該只有一行；多的用 `kill <PID>` 關掉 |
| `xcodebuild failed with code 65` | WDA 建置／簽署失敗，最常見的原因是 Xcode 沒登入 Apple ID，或描述檔裡沒有這支手機 | 重做 2.5 的步驟 1～3；還是不行的話，在 capabilities 加 `"showXcodeLog": True`，看 Appium 視窗的詳細錯誤 |
| `0xe8008011`、WDA 打不開 | 免費帳號描述檔 7 天到期 | 見 2.5 的「免費 Apple ID 的限制」 |
| `TypeError: unsupported operand type(s) for \|` | 用到系統的 Python 3.9 | 改用 `uv run python ...` |
| 操作沒反應、卡住 | 手機鎖定，或 WDA 沒在執行 | 確認手機解鎖、畫面上有 Automation Running；測試期間不要按音量鍵（同時按兩顆會停止自動化） |

其他問題見 `docs/TROUBLESHOOTING.md`。

### 2.9 Android（尚未依新流程驗證）

Python 環境（2.2）和 API Key（2.3）跟 iOS 共用，其餘差異：

- 需要 Android Studio + SDK，以及模擬器或實機；確認 Appium 有裝
  `uiautomator2` driver（`appium driver list --installed`）
- **模擬器上要先自己安裝待測 App 的 APK**（Appium 會自動安裝的只有它自己
  用的 UiAutomator2 Server）：
  - 跟開發團隊要跟 iOS 對應的 Demo／測試版
  - Apple Silicon 的 Mac 上模擬器是 arm64，APK 要支援 `arm64-v8a`（或是
    universal 版本）；拿到 `.aab` 的話不能直接安裝，要先轉換，直接要 APK
    最省事
  - 金融類 App 常會偵測並擋掉模擬器，擋住的話要改用實機
  - 安裝：把 APK 拖進模擬器視窗，或 `adb install 檔案.apk`
- `config.toml` 的 `[android]` `udid` 填你的裝置代號
  （`adb devices` 查得到，模擬器預設是 `emulator-5554`）
- 對應的程式：`capture_screen_map.py --platform android`／`run_test_case.py --platform android`／
  `viewer_server.py --platform android`（port 一樣是 8767），瀏覽器工具跟
  iOS 一樣用 `app_map_viewer.html`。地圖可以用
  `crawl_app_tree.py --platform android` 自動爬（先加 `--dry_run --safe_keywords`
  看會點哪些），或用「手動建樹」建立（節點詳情的「🌱 從這裡點出新畫面…」按鈕，底層呼叫 `/scan_screen`；要用 Android
  的地圖或新建空白地圖，載入 iOS 的 `app_map_tree.json` 會因平台不一致被擋）。Android 爬蟲目前只做過離線驗證
  （用真實畫面 XML），還沒有在能正常使用的 App 上實際爬過

---

## 三、快速開始（iOS）

以下指令都在這個資料夾底下執行（`cd` 進來這個資料夾再跑，因為程式用
相對路徑讀寫 `screenshots/`／`crops/`／`element_map.json` 等檔案）。

### 3.1 每次使用的啟動順序

1. iPhone 接上 USB、解鎖，打開待測 App
2. **視窗 1**：`appium --address 127.0.0.1`（一直開著）
3. **視窗 2**：`uv run python viewer_server.py --platform ios`（一直開著），看到
   `用瀏覽器打開 http://127.0.0.1:8767/` 就代表成功
4. 用瀏覽器打開 http://127.0.0.1:8767/ （**不要雙擊 HTML 檔**：用 `file://`
   開的話截圖顯示不出來、也連不到伺服器，頁面頂端會出現提醒），在頁首「專案」
   選你的專案（上次開的專案會自動開啟）。還沒有專案的話見 3.3
5. 按「檢查連線」顯示「已連線」就代表網頁 → Python 這段通了（網頁跟 API
   同源，不用再填伺服器網址）

> `viewer_server.py` 開著的期間會一直佔用同一條 Appium 連線，**這時
> 不要再另外跑 `crawl_app_tree.py`／`capture_screen_map.py` 這類直接連
> 裝置的腳本**，會搶連線。要跑的話先在視窗 2 按 `Ctrl+C` 關掉它。

### 3.2 第一次跑最小流程（確認環境是通的）

先確認 `viewer_server.py` **沒有**在執行。

1. 手動把 iPhone 導航到一個畫面（例如 App 首頁）
2. 掃描這個畫面（會呼叫 LLM 生成描述，需要 `GEMINI_API_KEY`）：
   ```
   uv run python capture_screen_map.py --screen_name my_first_screen
   ```
   跑完會看到 `element_map.json` 多一筆 `my_first_screen`，`screenshots/`
   多一張截圖
3. 建一個專案（見 3.3），試跑自動爬蟲寫進這個專案（先用很小的範圍，確認能動就好，不要一開始就開大範圍）：
   ```
   uv run python crawl_app_tree.py --project my-app-ios --map main --root_name my_first_screen --max_depth 1 --max_screens 5
   ```
   跑完會更新 `workspace/projects/my-app-ios/maps/main.json`（這張地圖原本有畫面的話先備份到
   `backups/main-<時間>.json`）。依功能分開爬就換 `--map` 名稱，例如 `--map trading`，
   不會蓋到其他地圖。不加 `--project` 的話照舊寫 `app_map_tree.json`。
   注意：爬蟲預設**會點擊所有元件**，「下單、送出、確認」這類風險關鍵字的黑名單
   要加 `--safe_keywords` 才會啟用，連到正式環境或真實帳號時一定要加
4. 照 3.1 啟動橋接伺服器和網頁，選剛才的專案

### 3.3 專案（workspace）

地圖、截圖、測案、批次設定都存在 `workspace/projects/<專案代號>/`，一支 App 的一個
平台一個專案（iOS 和 Android 畫面結構不同，地圖不能共用）。一個專案可以有很多張地圖，
通常是依功能分開爬（例如交易、帳務各一張），彼此不會互相覆蓋。`workspace/` 不進 git
（含畫面資料），交付給其他團隊時用「⬇ 匯出專案 zip」，對方用「📂 匯入專案 zip」。

```
workspace/projects/mbroker-demo-ios/
├── project.json     專案設定：名稱、平台、bundle_id（爬蟲判斷有沒有跑出 App、批次執行重開 App 都用這個）
├── maps/            地圖（跟 app_map_tree.json 同格式），預設 main，依功能可以有很多張
│   ├── main.json
│   └── trading.json
├── backups/         爬蟲覆蓋地圖前的備份 <地圖>-<時間>.json
├── screenshots/     所有地圖共用
├── cases/*.json     測案（不綁地圖）
└── batches/*.json   批次設定（地圖、測案都用名稱引用，執行時用最新版）
```

- **建立專案**：
  ```
  uv run python workspace.py create mbroker-demo-ios --platform ios --name "樹精靈 Demo iOS" --bundle_id com.cathay.securities.tw.mBroker.Demo
  ```
- **把舊資料搬進專案**（複製，原檔不動；截圖會一起複製、路徑會改成相對專案資料夾）：
  ```
  uv run python workspace.py import-legacy mbroker-demo-ios --map app_map_tree.json --cases test_cases/*.json
  ```
  地圖預設存成 `main`，要存成別的名稱加 `--map_name trading`。早期只有一份 `map.json` 的專案，
  第一次被讀到時會自動搬成 `maps/main.json`
- **修改專案設定**（名稱、bundle id／package；平台不能改，地圖是照那個平台掃出來的）：
  ```
  uv run python workspace.py update mbroker-demo-ios --bundle_id com.cathay.securities.tw.mBroker.Demo
  ```
  bundle id 留空的話，iOS 用 `config.toml` 的 `launch_bundle_id`、再沒有就用當下前景的 App，Android 用當下前景的 App；
  兩支以上 App 時建議一定要填。改完網頁重新整理就會讀到
- **網頁上的行為**：
  - 頁首「地圖」切換專案裡的地圖（記住每個專案上次開哪張），旁邊可以「＋ 新地圖」「⧉ 複製」
    「✎ 改名」（專案裡存的批次會跟著改）「🗑 刪除地圖」（最後一張不能刪）
  - 地圖（手動建樹、刪節點、重新掃描、自訂 XPath）改了**自動存檔**到目前這張，頁首顯示存檔狀態；
    掃描的截圖直接存進專案資料夾
  - 「📂 匯入地圖檔成新地圖」：開著專案時載入地圖檔會新增一張，不會蓋掉現有的
  - 地圖同時被別的地方改過（另一個分頁、爬蟲）會擋下來不覆蓋，頁首提示重新載入
  - 「LLM測案產生」按「💾 存到專案」存測案，「📁 從專案開啟…」載入來修改
  - 「測案執行」可以直接從下拉選單選專案測案；批次每一組可以「📁 選專案地圖」、「📁 從專案加入」測案，
    整份批次按「💾 存到專案」，之後從「📁 開啟專案裡的批次」叫回來
  - 原本的檔案載入／匯出都還在，給沒有專案的人交換單一檔案用
- 伺服器預設讀 `./workspace`，要放別的地方用 `viewer_server.py --workspace <路徑>`
  （`crawl_app_tree.py`、`workspace.py`、`cleanup_images.py` 也都有 `--workspace`）

---

## 四、檔案總覽

### 4.1 iOS 核心程式

| 檔案 | 用途 |
|---|---|
| `capture_screen_map.py` | 對「目前畫面」執行一次掃描：抓 Accessibility Tree、有捲動內容會邊捲邊抓、用 LLM 幫無 id 元件生成描述、存進 `element_map.json`。單頁 CLI 工具，也被 `viewer_server.py` 的 `/scan_screen` 直接呼叫（重用同一個 `scan_screen()` 函式，不是另外寫邏輯）。iOS/Android 共用，`--platform android` 切換；解析 XML、捲動手勢等平台差異在 `platforms/` |
| `crawl_app_tree.py` | 自動爬蟲：從起始畫面開始自動點擊安全元件、遞迴探索整支 App，產出 `app_map_tree.json`（畫面樹狀地圖，含 parent/depth/path/skeleton 簽章）。核心設計：`skeleton_signature()`（判斷兩個畫面是不是同一個模板）、`go_back()`/`recover_to()`（導航失敗時的復原機制，**目前對「原地切換UI狀態、沒有真正返回鍵」的畫面仍有已知的復原失敗問題，見下方第六節**） |
| `run_test_case.py` | 測試案例執行器：讀 `element_map.json` + 測試案例 JSON，照 native id → XPath → 鄰近文字錨點 → 座標點擊的順序定位並執行 `tap`/`input_text`/`assert_visible`/`assert_text_contains`。iOS/Android 共用，`--platform android` 切換；平台差異在 `platforms/ios.py`／`platforms/android.py`。可選「步驟失敗時自動關閉非預期彈窗並重試」（網頁勾選框／`--dismiss_dialogs`），沿用爬蟲 `dismiss_modal()` 的按鈕清單，只在失敗時才檢查；系統權限視窗刻意不處理（偵測到就跳過） |
| `viewer_server.py` | 本機 HTTP 橋接伺服器（預設 port 8767），讓 `app_map_viewer.html`／`viewer.html` 能透過一般 `fetch()` 間接操作手機。iOS/Android 共用，啟動時用 `--platform ios|android`（預設 ios）指定這次接哪種裝置，`/ping` 會回傳目前的平台；平台差異集中在檔案裡的 `IOSPlatform`／`AndroidPlatform`，`assert_banner`（跑馬燈驗證）iOS/Android 都有實作，Android 另外會查系統 Toast 和其他視窗；測案裡連續好幾個 `assert_banner` 會合併成一次監看（見 `run_test_case.run_banner_group`），結果還是每步各自一列，預設只要求都有出現，步驟加 `"in_order": true` 則要求比前一個跑馬燈晚出現，順序不對時前後兩步都是 FAIL；`/scan_screen` 會把目前平台傳給 `scan_screen()`，iOS/Android 都能用，但 Android 還沒實機驗證過，而且網頁掃描前會先比對「地圖的平台」和「server 的平台」，不一致會直接擋下來。「測案執行」分頁另有「批次執行」：好幾組「一張地圖 → 一份或多份測案（＋選填的登入測案）」，可匯出／匯入批次設定檔，走 `/run_batch`＋`GET /batch_progress`，每份開始前用 `navigator.py` 回到起始畫面。提供 `/tap`／`/assert_visible`／`/inspect`／`/test_xpath`／`/agent_xpath`（AI定位）／`/llm_chat`（對話產生測案）／`/llm_scenarios`（情境產生，用 Pro 模型）／`/run_test_script`（執行測案）／`/scan_screen`（單畫面掃描） |

### 4.2 Android 核心程式

沒有獨立的 Android 程式了：掃描、執行測案、橋接伺服器都跟 iOS 共用（見 4.3），
用 `--platform android` 切換，平台差異集中在 `platforms/android.py`。

### 4.3 共用程式

| 檔案 | 用途 |
|---|---|
| `navigator.py` | 批次執行（`viewer_server.py` 的 `/run_batch`）在每份測案開始前，把手機帶到它的起始畫面：`identify_screen()` 拿目前畫面跟地圖每個節點比對（native id 比對，同時看「這頁的元件有幾成出現」跟「畫面上地圖認得的 id 有幾成屬於這頁」）；`Navigator.navigate_to()` 依地圖的 parent／entry 算路線，在路徑上就往下點入口元件、不在就按返回，每走一步重新辨識；辨識不出來或退到底還回不去才重開 App，重開後先跑使用者指定的登入測案。已知限制：入口是清單某一列時照位置點、清單順序變了會點錯；同模板的兩個節點可能辨識錯 |
| `test_config.py` | 讀 `config.toml`（見 2.6）：`ios_capabilities()`／`android_capabilities()` 組 Appium 連線設定（缺欄位時說缺哪個）＋ Gemini 設定（`LLM_MODEL` 給高頻率/窄範圍任務用 Flash，`LLM_MODEL_PRO` 給情境產生這種低頻率/需要判斷力的任務用 Pro）＋ `get_llm_client()`（第一次用到才建立，沒設 API key 時不用 LLM 的功能照常能跑）＋ `generate_content_with_retry()`（503錯誤自動重試）。沒有 `config.toml` 也能 import，只看地圖、編輯測案不需要 |
| `workspace.py` | 專案資料夾（見 3.3）：`Workspace` 類別負責專案、地圖（帶版本號，存檔時版本不符就拒絕，避免互相覆蓋）、測案／批次的讀寫，以及匯出／匯入 zip（匯入會檢查路徑，不會寫到專案資料夾外面）；`viewer_server.py` 的 `/api/projects/...`、`crawl_app_tree.py --project` 都透過它。CLI：`list`／`create`／`import-legacy` |
| `compare_versions.py` | 比對同一畫面新舊版本的元件差異（同一個/疑似同一個/全新/已消失），對應相似度計分規則。這是**正式版本**，`app_map_viewer.html`「改版比對」分頁裡的 JS 是從這支移植的展示版，兩邊邏輯要保持一致，但正式流程以這支 Python 版為準 |

### 4.4 瀏覽器工具（由 `viewer_server.py` 提供）

兩支 HTML 都由 `viewer_server.py` 直接提供：`http://127.0.0.1:8767/` 是
`app_map_viewer.html`，`http://127.0.0.1:8767/viewer.html` 是舊工具，截圖
（`screenshots/` 等資料夾）也從同一個網址讀。網頁和 API 同源，伺服器只接受
自己網頁發出的請求（檢查 `Host`／`Origin`），其他網站沒辦法借瀏覽器操作手機。
只看地圖不接手機也要開伺服器，但不需要 Appium（只有操作手機的功能才會連 Appium）。

| 檔案 | 用途 |
|---|---|
| `app_map_viewer.html` | **目前主力工具**，讀 `crawl_app_tree.py` 產出的樹狀地圖（可以直接載入 `app_map_tree.json`，也支援載入這個工具自己匯出的精簡格式，兩種都會自動判斷轉換）。功能：畫面樹狀圖檢視、手動建樹（選節點→按「🌱 從這裡點出新畫面…」開視窗→送出點擊→建立新節點→對裝置重新掃描）、刪除節點（含子節點級聯）、重新掃描既有節點、改版比對、測試情境產生（LLM看資料推論可能情境）、LLM對話式產生跨畫面測案、測案執行。iOS/Android 都能用：地圖會記錄自己是哪個平台（舊地圖沒有記錄的話看元件 class 判斷，標在頁首），定位方式／XPath 會依平台顯示；操作手機前會先比對 `viewer_server.py` 目前接的平台，不一致就擋下來不送出。開啟專案（頁首「專案」）時地圖自動存回專案、測案／批次可以存進專案，重新整理不會遺失；沒開專案時跟以前一樣靠「⬇️匯出／📂載入」JSON 檔案手動存檔。從全名格式載入的地圖，存檔時會保留爬蟲算的 `skeleton` 等精簡格式沒有的欄位 |
| `viewer.html` | 更早期的單畫面校對+測試工具（重新定義XPath、AI定位、改版比對demo、LLM測案產生、測案執行這五個功能最初都在這支上開發），`app_map_viewer.html` 已經整合了這五個功能到樹狀地圖介面裡，Android 現在也能用 `app_map_viewer.html`，這支之後會退役 |

> 原本還有一支 `app_map_editor.html`（離線手動地圖編輯器，上傳截圖手動框
> 元件、手動加節點/邊，不用連裝置）——已從交接包移除，因為
> `app_map_viewer.html` 的手動建樹功能已經涵蓋同樣的使用情境，而且是真的
> 對裝置送出點擊＋即時掃描（資料準確度更高，不是人工用眼睛判斷）。唯一
> 差別是 `app_map_editor.html` 完全不用裝置連線也能用，如果常常遇到
> 「手邊沒裝置、只想憑截圖先搭骨架」的情境，可以跟交接的人要這支檔案
> （原本在 `手機測試/` 資料夾裡，如果你拿到的是連同原始資料夾一起交接，
> 這支應該還在；如果只拿到這個交接包本身，這支就沒有隨附）。

### 4.5 資料檔案

建議用 `workspace/projects/<專案>/`（見 3.3）。下表是沒開專案時的舊方式，跟程式放同一層，
程式用相對路徑讀寫，不要搬動位置。

> 除了 `test_cases/` 之外，下表的檔案和 `workspace/` 都列在 `.gitignore` 裡，**不會進
> git**：圖片體積大且可以重新產生；地圖 JSON 和截圖則會記錄畫面上的文字，
> 可能包含帳號、金額等敏感資訊。要保留的話請另外備份。

| 路徑 | 用途 |
|---|---|
| `app_map_tree.json` | `crawl_app_tree.py` 最新一次爬蟲產出的樹狀地圖（全名欄位格式：`screen_uid`/`screen_name`/`elements[]`/`unique_id`...），`app_map_viewer.html` 主要吃這個檔案 |
| `element_map.json` | 單畫面掃描累積下來的資料（`capture_screen_map.py` 產出，iOS/Android 同一種格式），`viewer.html`/`run_test_case.py`/`compare_versions.py` 都讀這個檔案，同一畫面重掃會覆蓋、不同畫面會累加 |
| `screenshots/` | 每個畫面的代表截圖（掃描時「捲動前、最頂部」那張），HTML 工具用相對路徑 `screenshots/xxx.png` 顯示；重掃後舊截圖會留著，用 `cleanup_images.py` 清掉沒有地圖在用的 |
| `crops/` | 無 id 元件的裁切截圖，給人工核對 LLM 描述有沒有判斷正確用，沒有任何程式會讀。`capture_screen_map.py` 預設不再產生，要的話加 `--save_crops`；舊檔用 `cleanup_images.py --all_crops` 清掉 |
| `test_cases/` | 幾份範例測試案例 JSON（`generate_test_case.py`/LLM對話產生的格式），給 `run_test_case.py` 執行 |
| `test_results/` | 範例：執行測案後存下來的截圖 |

### 4.6 docs/（文件）

| 檔案 | 用途 |
|---|---|
| `CRAWLER_TREE_HANDOFF.md` | 自動爬蟲子系統的完整脈絡：`wait_stable()` 效能bug的診斷跟修復過程、`go_back()`/`recover_to()` 復原機制的已知問題、實機驗證進度 |
| `JUDGMENT_LOGIC_REFERENCE.md` | 30項解析/判斷邏輯的參考清單（涵蓋掃描→指紋→候選偵測→定位五個階段），每一項都附驗證狀態 |
| `AUTOMATION_ARCHITECTURE_FLOW.md` | Mermaid 架構圖：從腳本/瀏覽器工具到 Appium 到 WDA 到手機的完整資料流程，含「WDA 建置在電腦、執行在手機」的釐清 |
| `TROUBLESHOOTING.md` | 環境建置＋首次測試遇到的問題記錄（Xcode授權、USB連線、WDA簽署、定位失敗案例...），每個問題都標註「換App還會不會遇到」 |
| `LLM_USAGE_SCAN.md` | 掃描階段（含描述生成）的真實 token 用量實測＋費用換算，也有「情境探索/測案產生」兩階段成本策略的估算 |
| `LLM_USAGE_ESTIMATE.md` | AI定位／LLM測案對話這類「人工互動階段」的用量估算 |
| `reference_data/` | 3份真實掃描存下來的原始 Accessibility Tree XML（登入畫面×2、保單總覽畫面），給驗證/延伸解析邏輯時當真實樣本用，不是隨便編的範例 |

---

## 五、建議閱讀順序

1. 這份文件（環境準備＋檔案總覽）
2. `docs/CRAWLER_TREE_HANDOFF.md` — 了解自動爬蟲目前的完成度跟已知問題
3. `docs/JUDGMENT_LOGIC_REFERENCE.md` — 了解解析/定位邏輯的設計依據
4. `docs/AUTOMATION_ARCHITECTURE_FLOW.md` — 了解整體資料流程
5. 啟動 `viewer_server.py`、打開 http://127.0.0.1:8767/ ，選一個專案（沒有的話照 3.3 建立或匯入），實際操作一輪
   熟悉介面（地圖檢視→手動建樹→情境產生→測案產生→測案執行）
6. 需要時再查 `docs/TROUBLESHOOTING.md`

---

## 六、目前已知的限制／未解決問題

1. **`go_back()`/`recover_to()` 導航復原機制不可靠**：對於「原地切換UI
   狀態、沒有真正返回鍵」的畫面（例如分頁切換型的畫面），爬蟲失敗後嘗試
   自動復原會誤觸到錯誤的元件（例如點到下拉選單而不是返回鍵），導致
   landing 到錯誤畫面。已經嘗試兩種修法（naive path-replay、anchor驗證
   的 path-replay）都在實機測試失敗，目前只有人工手動點擊能可靠脫困。
   詳見 `docs/CRAWLER_TREE_HANDOFF.md`
2. **`app_map_viewer.html` 手動建樹（「🌱 從這裡點出新畫面…」）時，若勾選「同時捲動收集完整
   清單」，捲動後抓到的元件座標會跟畫面截圖對不齊**（因為代表截圖固定是
   捲動前最頂部那張，捲動後各輪的座標是相對那一輪自己的畫面，不是相對
   代表截圖）——這是已知限制，介面上有提示文字，預設不勾選這個選項
3. **手動建樹產生的節點沒有真正的 `skeleton` 簽章**：`app_map_viewer.html`
   手動新增的節點存檔／匯出成全名格式時，`skeleton` 欄位會是
   `null`（因為只有篩選過的候選元件清單，算不出跟爬蟲一致的模板簽章；爬蟲
   產生的節點會保留原本的 `skeleton`）。另外 `crawl_app_tree.py --project`
   對同一張地圖是整份重爬、會蓋掉那張地圖（覆蓋前會先備份到 `backups/<地圖>-<時間>.json`），
   手動建樹的節點要自己從備份補回來；想保留的話爬到另一張（換 `--map` 名稱）。
   如果之後爬蟲重新爬到同一個畫面，不會自動認得這是同一個節點，可能會
   多長出重複節點，需要人工核對合併
4. **`gemini-flash-latest`／`gemini-pro-latest` 是會自動換版本的別名**，
   API 回應不會告訴你實際解析到哪個確切版本，`config.example.toml` 預設沿用這個
   設計但要注意：Google 更新別名指向的版本時可能導致行為/計費突然改變，
   正式上線前建議在 `config.toml` 的 `[llm]` 改成寫死的版本號
5. **`viewer_server.py` 同一時間只能服務一個 Appium session**：不要同時執行 Python 腳本（`crawl_app_tree.py` 等）
   又開著橋接伺服器對同一台裝置操作，會搶同一個連線

---

## 七、沒有複製過來的歷史檔案

原始 `手機測試/` 資料夾裡還有幾份專案早期（8月）的文件跟腳本，內容大多
已經被後來的功能取代，這個交接包**沒有**複製這些檔案，只在這裡註明歷史
脈絡（如果你拿到的只有這個交接包、沒有原始資料夾，這些檔案就是真的拿不
到，但底下每一項都有寫「被什麼取代了」，正常操作用不到這些舊檔案）：

- `PROJECT_BRIEF.md`／`WORKFLOW.md`／`SYSTEM_SUMMARY.md`／
  `PROGRESS_LOG.md`：專案最早期的規劃/總覽文件，後來大多被
  `CRAWLER_TREE_HANDOFF.md`／`JUDGMENT_LOGIC_REFERENCE.md` 取代
- `IOS_FULL_SCAN_HANDOFF.md`：9/8 寫的「iOS還沒實機驗證」交接筆記，
  當時決策都還沒定案，後續實機驗證的實際結果已經寫進
  `CRAWLER_TREE_HANDOFF.md`，這份是規劃階段的舊快照
- `crawl_app.py`：`crawl_app_tree.py` 的前身（沒有樹狀地圖/skeleton
  簽章設計的早期自動爬蟲版本）
- `generate_test_case.py`：單畫面測試案例產生的 CLI 版本（POC第二步），
  後來被 `app_map_viewer.html`「LLM測案產生」對話式功能取代
- `backup_20260818/`：8/18 的資料快照備份，內容已被現在的
  `element_map.json`/`screenshots`/`crops` 取代
- `element_map_20260818.json`／`element_map_new.json`：`element_map.json`
  的舊版/局部快照，主要是拿來示範/測試「改版比對」功能用的新舊版本配對
  範例，不是損毀或需要修復的資料
- `ppt_prompt*.md`／`ppt_outline*.md`：單次用途的簡報草稿產生提示詞，
  跟工程交接無關

如果需要找這些檔案，跟交接的人要原始 `手機測試/` 資料夾即可。
