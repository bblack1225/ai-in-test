---
name: web-verify
description: 在瀏覽器驗證網頁（web/app_map_viewer）改動時，用暫存的 workspace 複本啟動伺服器，避免自動存檔改到真實專案的地圖、測案、批次、變數。只要要開瀏覽器測、預覽、截圖、點點看這個專案的網頁，或改完 web/ 底下的檔案要驗證，就一定要用這個 skill，就算使用者只說「幫我測一下」「看一下畫面」也一樣。
argument-hint: "[專案 id，例如 xxx-android；不填就問]"
---

# 用暫存 workspace 驗證網頁

## 為什麼要這樣做

網頁開著專案時，任何修改地圖的操作在 0.8 秒後就會自動 PUT 回 `workspace/projects/<專案>/`。會觸發的操作包括 AI 定位套用 XPath、儲存或清除自訂 XPath、補描述、掃描、改名、刪除。測案、批次、變數也有各自的儲存和刪除 API（批次修改後約 1 秒自動存）。

`workspace/` 不進 git，存檔也沒有備份。在真實專案上點一下就可能改掉使用者的資料，而且沒辦法還原。以前就發生過：驗證時套用的假 XPath 被自動存進真實地圖。

所以驗證時讓伺服器用 `--workspace` 指向一份**複本**，網頁怎麼存都只會改到複本。

## 什麼時候可以不用複本

只有在確定整個驗證過程**只讀**的時候：只看畫面、切換分頁、開關視窗、情境產生和對話（不按儲存）。只要可能點到任何儲存、套用、刪除、改名、掃描、錄製，或是批次頁，就用複本。拿不準就用複本，成本只是複製一個資料夾。

## 步驟

### 1. 選專案、建複本

```bash
ls workspace/projects/
```

用使用者指定的專案；沒指定就列出來問。只複製要測的那個專案：

```bash
VERIFY_WS="$(mktemp -d)/workspace"      # 有 scratchpad 目錄的話放 scratchpad 底下
mkdir -p "$VERIFY_WS/projects"
cp -R workspace/projects/<專案> "$VERIFY_WS/projects/"
```

再留一個時間標記檔，最後拿來比對真實專案有沒有在這之後被改過：

```bash
touch "$VERIFY_WS/.started"
```

### 2. 用複本啟動伺服器

`.claude/launch.json` 是每個人自己的設定，不進 git。在 `configurations` 裡加一組（或更新已有的）`aitest-verify`。port 跟平常的不一樣，免得跟使用者自己開著的伺服器衝突：

```json
{
  "name": "aitest-verify",
  "runtimeExecutable": "uv",
  "runtimeArgs": ["run", "aitest-server", "--platform", "<android|ios>",
                  "--port", "8798", "--workspace", "<VERIFY_WS 的絕對路徑>"],
  "port": 8798
}
```

`--platform` 用專案 `project.json` 裡的 `platform`。用 `preview_start` 啟動 `aitest-verify`，再開 `http://localhost:8798/?project=<專案>`。

專案、地圖、測案、批次、變數的讀寫都不需要接手機。錄製、掃描、跑測案會連 Appium 和手機：這些功能先跟使用者確認手機有接、要不要真的跑，不要擅自操作手機上的 App。

### 3. 驗證

- `read_console_messages` 和 `preview_logs` 看有沒有錯誤
- `read_page` 和 `find` 確認畫面內容，`computer` 點擊操作
- 改到存檔的功能：`read_network_requests` 確認 PUT、POST、DELETE 成功，再看 `$VERIFY_WS` 裡的檔案內容對不對
- 有改到版面：深色、淺色都要看（標題列有切換開關）
- 最後截圖給使用者看

### 4. 確認真實專案沒被改到

```bash
find workspace/projects/<專案> -newer "$VERIFY_WS/.started" -type f
```

應該什麼都沒有。有的話馬上跟使用者說是哪些檔案、可能是哪個操作造成的，不要自己試著還原。

### 5. 收尾

- `preview_stop` 停掉 `aitest-verify` 伺服器
- 刪掉 `$VERIFY_WS` 所在的暫存目錄
- 回報驗證結果，附上截圖。程式改動**停在未 commit**，等使用者看過說可以才 commit 或 merge
