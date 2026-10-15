---
name: release
description: 發新版：判斷版號、改 pyproject.toml／uv.lock、寫 CHANGELOG 和 Release Notes、檢查敏感資料，確認後 merge、打 tag、建 GitHub Release。只在使用者打 /release 時使用。
disable-model-invocation: true
argument-hint: "[版號，例如 v2.4.0；不填就依變更內容建議]"
---

# 發版

這個工具會交付給其他團隊。對方停在 git tag 上使用，看 CHANGELOG 和 Release Notes 決定怎麼升級，所以每一版都要：版號正確、說明寫給「使用工具的人」看、不能夾帶真實 App 資料。

整個流程分兩段：**準備**（第 1～6 步，只動本機）→ 使用者看過說可以 → **發佈**（第 7 步，會 push 到 GitHub）。準備完一定先停下來給使用者看，不要一路做到底。

回覆、CHANGELOG、Release Notes 都用繁體中文。

## 1. 確認起點

```bash
git switch main && git pull --ff-only
git status --short
git describe --tags --abbrev=0          # 上一版的 tag
```

- 工作區有未 commit 的修改：先問使用者這些要不要進這一版。不要自己 commit，也不要 stash 掉
- 列出上一版之後進了什麼：`git log --oneline --first-parent <上一版>..HEAD`

## 2. 決定版號

版號是 `主版號.功能版號.修補版號`（規則也寫在 CHANGELOG.md 開頭）：

| 情況 | 升哪一碼 | 例子 |
|---|---|---|
| 舊的用法不能用了（指令改名、設定檔格式改了、資料夾搬家） | 主版號 | v2.0.0 套件化、指令改成 `uv run aitest …` |
| 新增功能，原本的用法照舊 | 功能版號 | v2.3.0 錄製建樹、管理測案 |
| 只有修 bug、文件、介面小調整 | 修補版號 | v2.3.1 Python 最低版本放寬 |

使用者有給版號就用那個；沒給就依第 1 步的變更列表建議一個，**說明理由並等使用者確認**再往下做。

## 3. 敏感資料檢查

```bash
uv run python .claude/skills/release/scripts/check_sensitive.py <上一版>..HEAD
```

腳本會從本機的 `workspace/projects/*/project.json` 和 `config.toml` 讀出真實的專案名稱、bundle id、UDID、Team ID、API key，加上 email、電話的規則，掃這段範圍新增的每一行。系統內建的 `python3` 可能太舊（沒有 `tomllib`），所以要用 `uv run python`。

- 有 `✗`：停下來給使用者看，等處理完才繼續。git 歷史裡已經有舊資料，使用者決定**不改寫歷史**，所以不要提議 rebase 或 filter-repo，只處理這一版新增的內容
- 有 `?`（圖片等二進位檔）：用 Read 打開看，有真實 App 畫面、帳號、金額就提出來
- 讀不到 workspace 或 config.toml（例如在別台電腦）：照常跑通用規則，並跟使用者說只做了部分檢查

## 4. 開分支、改版號

```bash
git switch -c release/vX.Y.Z
```

版號要改**兩個地方**，漏改 uv.lock 的話，uv 使用者執行時會自動改它，工作區就變髒了：

- `pyproject.toml`：`version = "X.Y.Z"`
- `uv.lock`：`name = "ai-in-test"` 那一段底下的 `version`。只改這一行，不要動到其他套件（例如 iniconfig 也可能剛好是同一個版號）

改完用 `uv lock --check` 確認兩邊一致。

## 5. 寫 CHANGELOG

在 CHANGELOG.md 的 `## 待做事項` 之後、上一版之前加一段：

```markdown
## vX.Y.Z

YYYY-MM-DD

### 升級說明

- （一定要有；沒有要注意的就寫「更新方式跟 vA.B.C 一樣，指令、`workspace/`、`config.toml` 都沒有變」）
- （有新的設定項：寫在哪一節、預設值、不加會怎樣）
- （行為改變、使用者會感覺到的：粗體標出來）

### 新功能

### 改善

### 修正
```

寫法：

- 寫給**用工具的人**看：說畫面上的按鈕名稱、能做什麼，不寫函式名稱、檔案路徑、重構細節。看 commit 訊息和 diff 翻譯成使用者看得懂的話
- 沒有內容的小節整個拿掉
- 主版號一定要寫清楚舊用法改成什麼
- `## 待做事項` 裡這一版完成的項目：`- [ ]` 改成 `- [x]`，後面接 `（[vX.Y.Z](#vxyz)）`（錨點是版號去掉點，例如 `#v240`）

## 6. 驗證並停下來給使用者看

```bash
uv run ruff check .
uv run pytest -q
```

接著把 Release Notes 草稿寫進暫存檔（scratchpad 或 `mktemp`，**不要放 repo 裡**），格式照下面的範本。然後給使用者看：

1. 版號和理由
2. 敏感資料檢查結果
3. `git diff` 的摘要（應該只動 pyproject.toml、uv.lock、CHANGELOG.md）
4. Release Notes 全文

**停在這裡，不要 commit。** 使用者可能要改文案，等他明確說可以再往下。

### Release Notes 範本

````markdown
（一兩句話：這版主要做了什麼、使用者要不要特別注意）

## 升級

```bash
git fetch --tags
git checkout vX.Y.Z
```

接著依你的安裝方式：

- **uv**：直接執行 `uv run aitest start`，uv 會自動更新（要手動的話是 `uv sync`）
- **venv + pip**：先啟用 `.venv`，再執行 `pip install -e .`

原本的專案、地圖、測案、批次照常可以用。

（有額外的升級步驟就在這裡加小節）

## 新功能 ／ 改善 ／ 修正

（從 CHANGELOG 這一版的內容挑重點，可以比 CHANGELOG 精簡）

完整內容見 [CHANGELOG.md](https://github.com/<owner>/<repo>/blob/vX.Y.Z/CHANGELOG.md#vxyz)。
````

升級指令一定用 `git fetch --tags` 加 `git checkout vX.Y.Z`：交付團隊停在 tag 上（detached HEAD），`git pull` 沒有作用。`<owner>/<repo>` 從 `gh repo view --json nameWithOwner` 取得。

## 7. 發佈（使用者說可以之後）

```bash
git add pyproject.toml uv.lock CHANGELOG.md
git commit -m "版號改成 vX.Y.Z，CHANGELOG 加 vX.Y.Z"
git switch main
git merge --no-ff release/vX.Y.Z -m "Merge release/vX.Y.Z into main"
git tag -a vX.Y.Z -m "<一行摘要這版的重點；說明見 CHANGELOG.md 的 vX.Y.Z>"
```

到這裡都還只在本機。push 和建 Release 會對外公開，**做之前再跟使用者確認一次**：

```bash
git push origin main
git push origin vX.Y.Z
gh release create vX.Y.Z --title vX.Y.Z --notes-file <第 6 步的 Release Notes 檔>
```

- Release 不附任何檔案，只用 GitHub 自動產生的原始碼 zip
- 完成後給使用者 Release 的網址；release 分支要不要刪，問使用者
