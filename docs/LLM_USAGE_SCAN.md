# 掃描階段 LLM 使用量（實測）

> 針對「自選庫存」畫面（`TWCTMainVC`，123個元件：12個有原生ID、111個沒有）
> 實際重新跑一次 `--describe`，記錄下真正送進去的 prompt、收到的回應、
> 用量與費用。這份只涵蓋**掃描階段**（`capture_screen_map.py` 的
> `describe_native_elements` / `describe_generated_elements`），不含
> AI定位（`/agent_xpath`）或 LLM測案對話（`/llm_chat`）——那兩個是「人工
> 互動階段」，用量算法不同，另外看 `LLM_USAGE_ESTIMATE.md`。

實測時間：2026-09-17　App：`com.cathay.securities.tw.mBroker.Demo`

---

## 模型

```
LLM_MODEL = "gemini-flash-latest"   # test_config.py
```
呼叫方式：`google.genai` SDK，`generate_content_with_retry(model=LLM_MODEL, ...)`。
`-latest` 是別名，Google 後端實際解析到哪個版本可能隨時間變動，回應物件沒有
把解析後的確切版本印出來，這裡沒有進一步確認。

一個畫面掃描（`--describe` 開啟時）固定打 **2 次** LLM，分開算：

---

## 呼叫 ①：`describe_native_elements`（12個原生ID元件，純文字，無截圖）

**送進去的 prompt（完整原文）：**
```
這是一個 iOS App 畫面「TWCTMainVC」上，一批已知 accessibility id 的 UI 元件清單。
請根據每個元件的 class、label、value，推論它的用途，用一句簡短的繁體中文描述。

輸入元件清單（JSON）：
[{"native_id": "TWCTMainVC", "class": "XCUIElementTypeNavigationBar", "label": "", "value": ""}, {"native_id": "MBkModeQuoteVCMode_MetroButton", "class": "XCUIElementTypeButton", "label": "", "value": ""}, {"native_id": "MBKMetroQuote_QuoteDataCell_8_0", "class": "XCUIElementTypeButton", "label": "", "value": ""}, ... （共12筆）]

同一個 native_id 有可能出現在不同 class 的元件上（例如按鈕與其內部的文字子元件），
請針對每一筆輸入分別產生對應的描述，不要合併。

請回傳一個 JSON 陣列，每個項目對應一個輸入元件，格式：
[{"native_id": "<與輸入相同的native_id>", "class": "<與輸入相同的class>", "description": "<用途描述>"}]

只回傳 JSON，不要有其他文字。
```
（prompt 全文 1,982 字元，這裡列出結構＋前3筆元件示意，完整JSON略）

**收到的回應（節錄，實際12筆全部回傳）：**
```json
[
  {"native_id": "TWCTMainVC", "class": "XCUIElementTypeNavigationBar", "description": "主畫面頂部導覽列"},
  {"native_id": "MBkModeQuoteVCMode_MetroButton", "class": "XCUIElementTypeButton", "description": "切換至方塊磚報價模式按鈕"},
  {"native_id": "MBKMetroQuote_QuoteDataCell_8_0", "class": "XCUIElementTypeButton", "description": "方塊報價第 8 列第 0 格商品按鈕"}
  ... （共12筆）
]
```

**實測用量：**

| prompt | output（candidates） | total |
|---|---|---|
| 605 | 559 | **2,768** |

---

## 呼叫 ②：`describe_generated_elements`（111個無ID元件＋1張截圖）

**送進去的內容** = 1張畫面截圖（`types.Part.from_bytes`，圖片本身不會出現在文字
prompt裡，但一樣算輸入token）＋以下文字 prompt：
```
這是一張 iOS App 畫面「TWCTMainVC」的截圖，附上一批「沒有 accessibility id」
的 UI 元件清單，每個元件都附上它在畫面上的座標範圍 (bounds，單位為像素)。

請對照截圖中對應座標位置的視覺內容，加上 class 類型與鄰近文字線索，推論每個元件的用途，
用一句簡短的繁體中文描述。

元件清單（JSON）：
[{"temp_id": "e0_0", "class": "XCUIElementTypeApplication", "label": "樹精靈Demo", "bounds": {"x": 0.0, "y": 0.0, "width": 390.0, "height": 844.0}, "position_bucket": "middle_center", "nearby_text": []}, {"temp_id": "e0_1", "class": "XCUIElementTypeWindow", ...}, ... （共111筆）]

請回傳一個 JSON 陣列，每個項目對應一個輸入元件，格式：
[{"temp_id": "<與輸入相同的temp_id>", "description": "<用途描述>"}]

只回傳 JSON，不要有其他文字。
```
（文字 prompt 全文 24,515 字元，另外還有1張截圖一起送進去）

**收到的回應（節錄，實際111筆全部回傳）：**
```json
[
  {"temp_id": "e0_0", "description": "樹精靈應用程式主畫面"},
  {"temp_id": "e0_1", "description": "主應用程式視窗"},
  {"temp_id": "e0_5", "description": "頂部自選群組切換選單按鈕（目前為自選庫存）"},
  {"temp_id": "e0_14", "description": "側邊選單「美股專區」功能按鈕"}
  ... （共111筆，回應全文 8,730 字元）
]
```

**實測用量：**

| prompt | output（candidates） | total |
|---|---|---|
| 11,820 | 3,419 | **17,491** |

---

## 這個畫面的總用量

| | ①native | ②generated | **合計** |
|---|---|---|---|
| prompt | 605 | 11,820 | **12,425** |
| output（candidates） | 559 | 3,419 | **3,978** |
| **total** | 2,768 | 17,491 | **20,259** |

**注意一個地方**：`total` 並不等於 `prompt + output` 直接相加（① 605+559=1,164，
但 total 是 2,768；② 11,820+3,419=15,239，但 total 是 17,491）。多出來的部分
（① 約1,604、② 約2,252）推測是 Gemini 計費會另外算的「思考（thinking）」
token——`gemini-flash-latest` 這個別名底下的模型可能預設有開思考模式，這部分
沒有在 `usage_metadata` 裡拆出獨立欄位，這裡沒有進一步確認，但算費用時要用
`total`，不能只用 `prompt+output` 相加，不然會低估。

---

## 換算費用

Gemini 2.5 Flash 公開定價：輸入 US$0.30／百萬token，輸出 US$2.50／百萬token
（`-latest` 實際解析版本可能有落差，僅供抓量級）。

計算方式：**輸入端用 `prompt`，輸出端用 `total − prompt`**（把候選輸出＋思考
token都算進「輸出計費」，比只算 `output` 更準確、不會低估）：

```
輸入成本 = 12,425 / 1,000,000 × $0.30       = $0.0037
輸出成本 = (20,259 − 12,425) / 1,000,000 × $2.50 = $0.0196
---------------------------------------------------
這個畫面總成本                                ≈ $0.0233（約新台幣 0.75 元）
```

---

## 抓量級：整支 App 掃完＋描述

假設其他畫面的元件數量、複雜度跟這次自選庫存差不多（每畫面約 20,259 tokens）：

| 畫面數 | 總token(估) | 金額(美金) | 約新台幣 |
|---|---|---|---|
| 10 | ~20萬 | ~$0.23 | ~NT$7.5 |
| 50 | ~100萬 | ~$1.17 | ~NT$38 |
| 100 | ~200萬 | ~$2.33 | ~NT$76 |

（這次用「total 扣掉 prompt」重新算過，比上一版只用 `output` 算的金額略高，
上一版的 US$0.011/畫面是低估的，這份文件的數字才是校正後的版本。）

---

## 一頁摘要：單畫面掃描長什麼樣子

| 項目 | 內容 |
|---|---|
| 模型 | `gemini-flash-latest`（別名，實際解析到哪個版本未確認） |
| 一個畫面幾次呼叫 | 最多2次：①原生ID元件（純文字）②無ID元件（文字＋截圖） |
| 送進去什麼 | 元件的 class／label／value／bounds／position_bucket／nearby_text（依有無ID分兩組），②另外附1張截圖 |
| 算用量公式 | `這個畫面total token = ①total + ②total`（`total` 直接讀 API 回應的 `usage_metadata`，不是自己數字元） |
| 算費用公式 | `輸入成本 = prompt/1,000,000 × 輸入單價`；`輸出成本 = (total－prompt)/1,000,000 × 輸出單價`（把候選輸出＋思考token都算進去，不要只算 `output` 那個數字，會低估） |
| 實測基準 | 自選庫存畫面123元件：total 20,259 tokens ≈ US$0.0233／畫面 |

---

## 六、掃完後怎麼用 LLM 產生測案情境——兩階段策略

### 階段一：探索（只跑一次，全部畫面一次送）

| 項目 | 內容 |
|---|---|
| 用途 | 把全部已掃描畫面的資料一次送給 LLM，請它瀏覽過後**提出3-5個候選測試情境**，先不產生詳細操作步驟 |
| 建議模型 | 用比 Flash 更強的模型（目前查到的旗艦是 **Gemini 3.1 Pro**，2M token 上下文視窗；上線前務必到 [Gemini 官方定價頁](https://ai.google.dev/gemini-api/docs/pricing) 核對當下實際可用的模型名稱與價格，不要繼續用 `-latest` 別名） |
| 為什麼可以用貴一點的模型 | 這一步**只跑一次**（不是每次對話都重跑），絕對金額再貴也是小錢，換取「看過全部畫面、抓出有意義的跨畫面情境」這種需要判斷力的任務用更強的模型比較划算 |
| 送什麼資料 | 100個畫面的完整精簡欄位（`unique_id`／`id_source`／`class`／`label`／`description`）——因為 Pro 系列上下文視窗夠大（2M），**不需要像 Flash 那樣先拿掉 `unique_id` 省空間** |
| 資料量／視窗占比 | 以100個畫面、每畫面123元件估算 ≈ 93.6萬 tokens，占 2M 視窗約 **47%**，留有安全邊界 |
| 概算費用（Pro，$4/$18／百萬token，超過200K整包適用高階級距） | 輸入 93.6萬 tokens ≈ $2.02　輸出（候選情境說明）~3,000 tokens ≈ $0.05　**合計 ≈ US$2.07（約新台幣67元）** |

### 階段二：產生詳細測試步驟（會來回很多輪，只送相關畫面）

| 項目 | 內容 |
|---|---|
| 用途 | 針對階段一挑出的某一個候選情境，來回對話調整、產生真正可執行的 `{action, screen_name, target, value, note}` 步驟 |
| 建議模型 | 換回便宜快速的 `gemini-flash-latest`（或當下查到的同等級 Flash 模型），這裡不需要 Pro 等級的推理力 |
| 送什麼資料 | 只送**這個情境牽涉到的2-5個畫面**（不是全部100個），欄位要完整（含 `unique_id`，因為這階段真的要拿去定位、執行操作） |
| 為什麼要嚴格限縮畫面數 | `/llm_chat` 是無狀態設計，**每一輪對話都要重送一次資料**，情境牽涉的畫面越多、對話輪數越多，成本線性疊加；限縮在2-5個畫面可以放心來回調整很多輪也不會有感 |
| 概算費用／輪 | 5個畫面資料 ≈ 123元件×5×157字元÷2.07字元/token ≈ 4.7萬 tokens，輸入 $0.014＋輸出(1,500 tokens) $0.004 ≈ **每輪 US$0.018（約新台幣0.6元）**，10輪來回也才約新台幣6元 |

### 兩階段合計，抓量級

| | 一次性成本 | 說明 |
|---|---|---|
| 階段一（探索，跑1次） | ~US$2.07 | 用 Pro，全部畫面 |
| 階段二（產生測案，抓10輪） | ~US$0.18 | 用 Flash，5個相關畫面/輪 |
| **合計** | **~US$2.25（約新台幣73元）** | 產出：一份跨畫面的可執行測試案例 |

跟「全部畫面、全程都用完整資料丟給 `/llm_chat` 硬聊」比（前面算過單輪就要 US$0.285、還會撞上下文上限）比起來，這個兩階段設計**便宜、安全、而且能來回調整很多輪**，是比較務實的做法。

---

## 七、總結：全流程費用總表

以「掃描100個畫面 ＋ 產生1份跨畫面測案」為情境，每個畫面複雜度抓跟自選庫存
（123元件）差不多：

| 動作 | 使用模型 | 預估次數 | token／次 | 費用／次 | 小計 |
|---|---|---|---|---|---|
| 掃描：原生ID元件描述 | `gemini-flash-latest` | 100（每畫面1次） | 2,768 | $0.0056 | $0.56 |
| 掃描：無ID元件描述＋截圖 | `gemini-flash-latest` | 100（每畫面1次） | 17,491 | $0.0177 | $1.77 |
| 測案探索：全畫面瀏覽找候選情境 | Pro 等級（如 Gemini 3.1 Pro，需上線前核對） | 1（一次性） | ~936,000 | $2.07 | $2.07 |
| 測案產生：詳細步驟來回調整 | `gemini-flash-latest` | 10（來回輪數，可調） | ~47,000 | $0.018 | $0.18 |
| **合計** | | | | | **≈ US$4.58（約新台幣148元）** |

**怎麼看這張表**：
- 前兩行（掃描）花費隨**畫面數量**線性增加，是固定要付的成本，且已經是校正過（含隱藏思考token）的實測基準
- 第三行（探索）是**一次性**成本，畫面再多次數也不會變，只有 token 量會變
- 第四行（測案產生）花費隨**對話輪數**線性增加，是唯一「用越多次、多付越多」的地方，但單輪很便宜
- 這份表格的模型名稱、定價都可能隨時間變動（尤其 Pro 那格），實際導入前務必到官方定價頁重新核對一次
