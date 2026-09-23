"""
本機小型伺服器：讓 viewer.html 上的「點擊」「驗證可見」按鈕能透過 HTTP API
呼叫 run_test_case_android.py 現成的定位邏輯，對模擬器/裝置執行真正的操作。

背景：viewer.html 本身是純前端、沒有後端，瀏覽器沒辦法直接連 Appium（瀏覽器
不能對本機的 Appium server 開 socket 連線），所以需要這支伺服器當橋樑：
伺服器維持一條 Appium 連線，瀏覽器按鈕觸發時打 API 給它，伺服器內部沿用
run_tap()/run_assert_visible() 這兩個現成的函式（native id → XPath →
UiScrollable.scrollIntoView → 座標備援，完全比照 run_test_case_android.py
執行測案時的定位順序）真的去操作模擬器，不是另外寫一套新邏輯。

這支只支援 Android（跟 run_test_case_android.py 對應）。iOS 版是
viewer_server_ios.py（跟這支提供完全一樣形狀的 API，只是連線方式跟部分
定位邏輯換成 iOS 版本，預設跑在 8767 埠，兩支可以同時開著，viewer.html
會依畫面平台自動切換要連哪一個），但因為這次沒有 iPhone 可以實機測試，
只通過語法檢查，之後有裝置時需要優先驗證。

另外提供「重新定義 XPath」用的三個輔助 API：
  - /inspect：先用現成的定位邏輯（native id / XPath / 錨點消歧義 /
    scrollIntoView）盡力找出這個元件目前對應的畫面位置，再解析當下的
    完整 tree，往上層走訪，回傳每一層容器的 class/resource-id/文字，
    給使用者在 viewer.html 上參考、手動組出更精準的 XPath用
  - /test_xpath：讓使用者在儲存前先測試自己寫的 XPath 查到幾個結果，
    不用真的存檔、真的執行測案才知道查詢對不對
  - /agent_xpath：把當下完整 tree + 目標元件的已知線索丟給 LLM，請它
    生成一個唯一定位的 XPath，並且**不是單次生成就相信**——每個候選都
    會實測（查詢是否唯一、位置是否吻合已知的元件位置），不通過就把
    失敗原因回饋給 LLM 重試，最多重試幾次，全部失敗才回報「無法定位出
    唯一XPath」

另外提供「LLM 測案產生」／「測案執行」兩個分頁用的 API：
  - /llm_chat：多輪對話代理，把系統提示詞（規定輸出格式：可能橫跨多畫面
    的JSON測試案例，target必須是真的存在的unique_id）+ 使用者選定的畫面
    元件精簡資料 + 對話歷史丟給 Gemini，回傳助理回覆。純聊天不操作裝置，
    不要求Appium session存活。
  - /run_test_script：執行一份測試案例（可能橫跨多個畫面），每個step自帶
    screen_name，直接沿用run_tap/run_assert_visible/run_input_text/
    run_assert_text_contains這幾個現成函式執行，不重寫定位邏輯。

使用方式：
  1. 先確認模擬器已開啟、目標畫面已經手動導航好
  2. 執行：python viewer_server.py
  3. 瀏覽器打開 viewer.html，上傳 element_map.json、選好畫面後，元件清單
     每一筆旁邊會出現「▶ 點擊」「👁 驗證可見」「✏️ 重新定義」「🤖 AI定位」
     按鈕，點擊/驗證可見會真的對模擬器操作，結果顯示在按鈕旁邊；
     AI定位會呼叫 Gemini（需要環境變數 GEMINI_API_KEY，跟其他腳本共用
     同一份設定）自動生成並驗證XPath，成功會直接套用，失敗會顯示原因

安全提醒：這支伺服器只綁定 127.0.0.1（本機），不會對外網開放，但按下按鈕
會「真的」操作手機/模擬器畫面。這裡沒有像 run_test_case_android.py 執行
整套測試案例前那樣的 verify_current_screen() 前置校驗（因為每次點擊是
獨立單次操作，不是一整套測試案例），所以務必自行確認手機目前導航到你要
核對的那個畫面，避免座標點擊備援機制點到不相關的元件。
"""

import json
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer

from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from google.genai import types
from selenium.common.exceptions import InvalidSessionIdException, NoSuchElementException

from capture_screen_map_android import parse_bounds
from run_test_case_android import (
    locate_generated_element,
    run_assert_text_contains,
    run_assert_visible,
    run_input_text,
    run_tap,
    wait_after_action,
)
from test_config import ANDROID_DESIRED_CAPS, APPIUM_SERVER_URL, LLM_MODEL, generate_content_with_retry

HOST = "127.0.0.1"
PORT = 8766

driver = None


def get_driver(force_reconnect: bool = False):
    global driver
    if force_reconnect and driver is not None:
        try:
            driver.quit()
        except Exception:
            pass
        driver = None
    if driver is None:
        options = UiAutomator2Options().load_capabilities(ANDROID_DESIRED_CAPS)
        driver = webdriver.Remote(APPIUM_SERVER_URL, options=options)
        print("[viewer_server] 已連線 Appium，driver 會保持開啟直到這支伺服器結束")
    return driver


def ensure_session_alive():
    """
    在每次請求真正處理前，先用一個很輕量的查詢驗證 session 是不是還活著。

    這個檢查存在的原因：inspect_element()/test_xpath() 內部（以及它們呼叫的
    run_test_case_android.py 定位函式）大多用「try/except Exception 廣泛捕捉、
    捕到就當作查不到、回傳 None」的寫法——這對「單一定位方式失敗、換下一種」
    是正確的設計，但也會把 InvalidSessionIdException（session 已失效）跟
    「真的查不到」混在一起，讓 do_POST() 原本設計的自動重連邏輯永遠等不到
    這個例外傳上來（實測踩過：session失效時，/inspect 只會回報「找不到這個
    元件」，完全看不出是連線問題，白白花時間排查）。這裡改成在最前面主動探測，
    不依賴內層程式碼願不願意把例外往外拋。
    """
    global driver
    if driver is None:
        get_driver()
        return
    try:
        driver.find_elements(AppiumBy.ID, "__viewer_server_liveness_check__")
    except InvalidSessionIdException:
        print("[viewer_server] [警告] Appium session 已失效，重新連線...")
        get_driver(force_reconnect=True)


def _locate_best_effort(driver, el: dict):
    """沿用現成的定位鏈，盡力找出這個元件目前對應的畫面元素，找不到就回傳 None。"""
    if el.get("custom_xpath"):
        try:
            matches = driver.find_elements(AppiumBy.XPATH, el["custom_xpath"])
            if len(matches) == 1:
                return matches[0]
        except Exception:
            pass
    if el.get("id_source") == "native" and el.get("native_id"):
        try:
            return driver.find_element(AppiumBy.ID, el["native_id"])
        except NoSuchElementException:
            return None
    return locate_generated_element(driver, el)


def inspect_element(el: dict, max_levels: int = 8) -> dict:
    """
    盡力定位這個元件在畫面上目前對應的位置，再解析當下完整 tree，往上層走訪，
    回傳每一層容器的 class/resource-id/文字，供使用者手動組出更精準的 XPath。
    找不到起點時回傳 found=False，前端會提示使用者確認手機畫面是否正確。
    """
    d = get_driver()
    matched = _locate_best_effort(d, el)
    if matched is None:
        return {"found": False, "ancestors": []}

    target_bounds = {
        "x": matched.location["x"], "y": matched.location["y"],
        "width": matched.size["width"], "height": matched.size["height"],
    }

    root = ET.fromstring(d.page_source)
    parent_map = {child: parent for parent in root.iter() for child in parent}

    # 用即時座標比對出這個元件在目前 tree 裡對應的 XML 節點（同 class、
    # bounds 最接近的視為同一個，跟掃描端解析候選元件用同樣的座標格式）
    best_node, best_distance = None, None
    for node in root.iter():
        attrib = node.attrib
        if attrib.get("class") != el.get("class"):
            continue
        bounds = parse_bounds(attrib.get("bounds", ""))
        if bounds is None:
            continue
        distance = abs(bounds["x"] - target_bounds["x"]) + abs(bounds["y"] - target_bounds["y"])
        if best_distance is None or distance < best_distance:
            best_distance, best_node = distance, node

    if best_node is None:
        return {"found": True, "target_bounds": target_bounds, "ancestors": []}

    ancestors = []
    current = best_node
    for level in range(1, max_levels + 1):
        parent = parent_map.get(current)
        if parent is None or "class" not in parent.attrib:
            break
        pa = parent.attrib
        ancestors.append({
            "level": level,
            "class": pa.get("class", ""),
            "resource_id": pa.get("resource-id", ""),
            "text": pa.get("text", ""),
            "content_desc": pa.get("content-desc", ""),
        })
        current = parent

    return {"found": True, "target_bounds": target_bounds, "ancestors": ancestors}


def test_xpath(xpath: str) -> dict:
    """讓使用者儲存前先測試自訂 XPath 查到幾個結果，不用真的存檔/執行測案才知道。"""
    d = get_driver()
    try:
        matches = d.find_elements(AppiumBy.XPATH, xpath)
    except Exception as e:
        return {"count": 0, "error": f"{type(e).__name__}: {e}"}

    preview = []
    for m in matches[:5]:
        preview.append({
            "location": m.location, "size": m.size,
            "resource_id": m.get_attribute("resource-id"),
            "text": m.get_attribute("text"),
            "content_desc": m.get_attribute("content-desc"),
        })
    return {"count": len(matches), "preview": preview}


# ---------------------------------------------------------------------------
# /agent_xpath：把完整 tree 丟給 LLM，請它生成能唯一定位目標元件的 XPath，
# 每個候選都要實測驗證過才採用，不是單次生成就相信
# ---------------------------------------------------------------------------

AGENT_XPATH_MAX_ATTEMPTS = 3
POSITION_TOLERANCE_PX = 60  # 判斷「LLM選到的元件位置是不是預期目標」的容許誤差


def _build_agent_xpath_prompt(target_info: dict, page_source: str, feedback: str | None) -> str:
    feedback_block = ""
    if feedback:
        feedback_block = f"\n\n上一輪的失敗回饋：{feedback}\n請根據這個回饋修正，不要再犯同樣的錯誤。"

    return f"""你是 Android UI 自動化測試專家。以下是透過 Appium UiAutomator2 取得的
畫面 Accessibility Tree（XML 格式，可以用 XPath 查詢，概念上跟網頁 DOM 類似）。

請幫我寫一個 XPath 表達式，唯一定位到以下目標元件：
- class: {target_info.get('class')}
- 文字內容（text/content-desc）: {target_info.get('label')!r}
- 附近文字線索（同層兄弟節點的文字，可能是清單裡能唯一辨識這一列/區塊的線索）: {target_info.get('nearby_text')}
- 畫面九宮格粗略位置: {target_info.get('position_bucket')}

完整畫面 Tree（XML）：
{page_source}

規則：
1. 只能用標準 XPath 1.0 語法（底層引擎不保證支援 XPath 2.0 函式）
2. 這個元件的文字內容在畫面上可能不是唯一的（例如清單裡每一列都有一個
   文字相同的操作按鈕，直接用文字查詢會查到多個結果）。這種情況要善用
   tree 的階層結構：先用鄰近文字線索定位到唯一的錨點節點，再用
   `ancestor::{{某個容器class}}[1]` 找到該錨點「最近的」共同容器（不要
   寫成 `//{{class}}[.//text=X]` 這種寫法，那樣會連範圍過大的外層同class
   容器也一起選中，查詢範圍沒有真的縮小），最後在這個容器範圍內查找
   目標元件，確保最終查詢結果剛好唯一一個
3. 不要用寫死絕對路徑/index位置的查詢，盡量用屬性條件加結構關係，改版
   時版面微調也比較不容易整個失效{feedback_block}

請回傳 JSON 格式：{{"xpath": "<你的XPath字串>"}}，只回傳 JSON，不要有其他文字，
不要用 markdown code block 包起來。
"""


def agent_generate_xpath(el: dict) -> dict:
    """
    把完整 tree + 目標元件已知線索丟給 LLM，請它生成唯一定位的 XPath。
    每個候選都要實測（查詢是否剛好唯一、位置是否吻合已知的目標位置）才會
    採用，不通過就把具體失敗原因回饋給 LLM 重試，最多 AGENT_XPATH_MAX_ATTEMPTS
    次，全部失敗才回報 success=False——不是單次生成就盲目相信 LLM 的答案，
    跟人工「重新定義」流程最後都要先「測試查詢」才能存檔是同一個精神。
    """
    d = get_driver()

    # 有現成定位邏輯找得到的話，拿它的即時座標當作驗證 LLM 答案對不對的基準；
    # 找不到也沒關係（這正是需要 AI 救援的情境），只是少了位置校驗這一層防呆
    ground_truth_bounds = None
    matched = _locate_best_effort(d, el)
    if matched is not None:
        ground_truth_bounds = {"x": matched.location["x"], "y": matched.location["y"]}

    page_source = d.page_source
    fingerprint = el.get("fingerprint") or {}
    target_info = {
        "class": el.get("class"),
        "label": el.get("label"),
        "nearby_text": fingerprint.get("nearby_text"),
        "position_bucket": fingerprint.get("position_bucket"),
    }

    feedback = None
    last_xpath = None
    log = []

    for attempt in range(1, AGENT_XPATH_MAX_ATTEMPTS + 1):
        prompt = _build_agent_xpath_prompt(target_info, page_source, feedback)
        try:
            response = generate_content_with_retry(
                model=LLM_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json"),
            )
            candidate = json.loads((response.text or "").strip()).get("xpath", "").strip()
        except Exception as e:
            log.append(f"第{attempt}次：LLM呼叫或回應解析失敗（{type(e).__name__}: {e}）")
            feedback = f"上一次呼叫失敗（{e}），請重新給一個XPath"
            continue

        if not candidate:
            log.append(f"第{attempt}次：LLM沒有回傳有效的XPath字串")
            feedback = "沒有收到有效的XPath字串，請重新給一個"
            continue

        last_xpath = candidate
        try:
            matches = d.find_elements(AppiumBy.XPATH, candidate)
        except Exception as e:
            log.append(f"第{attempt}次：「{candidate}」語法錯誤（{e}）")
            feedback = f"這個XPath「{candidate}」語法錯誤：{e}，請修正"
            continue

        if len(matches) == 0:
            log.append(f"第{attempt}次：「{candidate}」查到0個結果")
            feedback = f"這個XPath「{candidate}」查到0個結果，請重新給一個能查到目標元件的XPath"
            continue
        if len(matches) > 1:
            log.append(f"第{attempt}次：「{candidate}」查到{len(matches)}個結果，不唯一")
            feedback = (f"這個XPath「{candidate}」查到{len(matches)}個結果，不唯一，"
                        f"請善用tree的階層結構讓查詢更精確，只留下唯一一個結果")
            continue

        if ground_truth_bounds is not None:
            actual = matches[0].location
            distance = abs(actual["x"] - ground_truth_bounds["x"]) + abs(actual["y"] - ground_truth_bounds["y"])
            if distance > POSITION_TOLERANCE_PX:
                log.append(f"第{attempt}次：「{candidate}」查到唯一結果，但位置{actual}跟預期目標"
                           f"{ground_truth_bounds}不符，可能選錯元件")
                feedback = (f"這個XPath查到唯一結果，但位置x={actual['x']},y={actual['y']}"
                            f"跟目標元件實際位置x={ground_truth_bounds['x']},y={ground_truth_bounds['y']}"
                            f"不符，選錯元件了，請重新給一個")
                continue

        log.append(f"第{attempt}次：「{candidate}」驗證通過")
        return {"success": True, "xpath": candidate, "attempts": attempt, "log": log}

    return {"success": False, "xpath": last_xpath, "attempts": AGENT_XPATH_MAX_ATTEMPTS, "log": log}


# ---------------------------------------------------------------------------
# /llm_chat：viewer.html「LLM 測案產生」分頁的聊天代理。瀏覽器不能直接呼叫
# Gemini（會把 GEMINI_API_KEY 暴露在前端原始碼/network面板），所以跟
# /agent_xpath 一樣走伺服器端代理；差別是這裡的系統提示詞規範的是「產生一份
# 可以透過 Appium 執行、可能橫跨多個畫面的測試案例」，不是單一元件的 XPath。
# 純聊天不需要操作裝置，不強制要求 Appium session 存活（見 do_POST 的例外名單）。
# ---------------------------------------------------------------------------

def _build_llm_testgen_system_prompt(screens_context_json: str) -> str:
    return f"""你是 Android/iOS App 自動化測試專家，協助使用者根據「已經掃描好的畫面
元件資料」設計、產生可以透過 Appium 執行的自動化測試案例。

以下是使用者選定、目前可用的畫面元件資料（JSON，每個畫面下的元件都只精簡保留
unique_id/id_source/class/label/description 這幾個欄位）：
{screens_context_json}

【輸出格式規則，非常重要，一定要遵守】
1. 最終的測試案例一律用下面這個 JSON 結構表示，並且**一定要用一個以
   ```json 開頭、``` 結尾的 code block 包起來**（前端會自動解析這個
   code block，格式錯誤會讓解析失敗），這個 code block 裡只能有這一份
   合法 JSON，不要加註解、不要有多餘文字：
   {{
     "test_name": "<簡短描述這個測試案例在測什麼>",
     "steps": [
       {{"action": "tap", "screen_name": "<畫面名稱>", "target": "<unique_id>", "note": "<這步在做什麼，給人看的>"}},
       {{"action": "input_text", "screen_name": "<畫面名稱>", "target": "<unique_id>", "value": "<要輸入的文字>", "note": "..."}},
       {{"action": "assert_visible", "screen_name": "<畫面名稱>", "target": "<unique_id>", "note": "..."}},
       {{"action": "assert_text_contains", "screen_name": "<畫面名稱>", "target": "<unique_id>", "value": "<預期文字要包含的內容>", "note": "..."}}
     ]
   }}
2. action 只能是這4種：
   - tap：點擊
   - input_text：輸入文字，只對有 resource-id/accessibility id 的 native
     元件有效（沒有native id的輸入框無法可靠定位，不要對這種元件用
     input_text）
   - assert_visible：驗證元件可見
   - assert_text_contains：驗證元件「目前顯示的文字內容」有包含指定字串，
     用在「這個畫面的文字內容因為別的畫面的操作而改變」這種跨畫面情境
     （例如在設定頁存了一個名稱，回首頁後問候語應該要出現這個名稱），
     assert_visible 驗證不到文字內容，只能驗證「有沒有顯示」
3. target 的值**只能是上面提供的畫面資料裡真的存在的 unique_id**，不可以
   自己編造，也不可以用畫面上的文字、猜測的id、或憑印象記得的id代替。
   如果找不到能達成使用者需求的元件，要在說明文字裡老實講清楚缺少什麼、
   建議使用者重新掃描或補充哪個畫面的資料，不要硬湊一個不存在的unique_id
   湊出一份看起來完整但實際上執行會失敗的測試案例。
4. steps 裡的每一步都要標明 screen_name（這一步操作/驗證的是哪個畫面）。
   測試案例可以橫跨多個畫面（先在畫面A點擊一個會切到畫面B的按鈕，接著對
   畫面B的元件做assert），不用侷限在單一畫面內，執行端會依序照步驟執行，
   不需要額外的「切換畫面」步驟——只要tap的目標本身就是會切換畫面的按鈕，
   下一步驟直接接著描述新畫面的元件即可，執行端會自動用該步驟指定的
   screen_name去查對應的元件資料。
5. 每次回覆除了 code block 外，也用簡短繁體中文說明你設計了哪些步驟、
   為什麼這樣設計；如果使用者要求調整，就根據對話上下文修改後，重新輸出
   「完整」的（不是增量片段的）JSON code block，不要只回傳有變動的部分。
6. 如果使用者的需求不清楚（例如沒說清楚要測哪個畫面、哪個操作、預期結果
   是什麼），可以先反問澄清，不用勉強硬猜然後生成不精確的測試案例。
"""


def run_llm_chat(payload: dict) -> dict:
    """
    多輪對話代理：把系統提示詞（含使用者選定的畫面元件精簡資料）+ 目前為止的
    對話歷史丟給 Gemini，回傳這一輪的助理回覆文字。每次都是無狀態呼叫（伺服器
    不保存對話紀錄），完整歷史由前端 viewer.html 保存、每次呼叫都整包送過來，
    這支伺服器只負責代理呼叫、不碰 API 金鑰以外的狀態。
    """
    messages = payload.get("messages", [])
    screens_context = payload.get("screens_context", [])

    system_prompt = _build_llm_testgen_system_prompt(json.dumps(screens_context, ensure_ascii=False))

    contents = [
        types.Content(
            role="user" if m.get("role") == "user" else "model",
            parts=[types.Part.from_text(text=m.get("text", ""))],
        )
        for m in messages
    ]

    try:
        response = generate_content_with_retry(
            model=LLM_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(temperature=0.3, system_instruction=system_prompt),
        )
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    return {"reply": (response.text or "").strip()}


# ---------------------------------------------------------------------------
# /run_test_script：viewer.html「測案執行」分頁——執行 LLM（或人工）產生、
# 可能橫跨多個畫面的測試案例。跟 run_test_case_android.py 這支CLI的差異：
# CLI假設整個測案只在單一畫面內（element_lookup只從一個screen_name建立），
# 這裡的每個step都自帶screen_name，要能同時從測案引用到的所有畫面裡找元件，
# 實際執行動作（run_tap/run_assert_visible/...）直接沿用同一批函式，
# 不重寫定位邏輯，維持「同一套定位規則，不管從哪裡觸發都一樣」。
# ---------------------------------------------------------------------------

def run_test_script(payload: dict) -> dict:
    element_map = payload.get("element_map") or {}
    test_case = payload.get("test_case") or {}

    screens_by_name = {s["screen_name"]: s for s in element_map.get("screens", [])}
    steps = test_case.get("steps", [])

    d = get_driver()
    results = []
    for i, step in enumerate(steps, 1):
        action = step.get("action")
        screen_name = step.get("screen_name")
        target = step.get("target")

        screen = screens_by_name.get(screen_name)
        el = None
        if screen is not None:
            el = next((e for e in screen.get("elements", []) if e.get("unique_id") == target), None)

        if el is None:
            outcome = f"FAIL（在畫面「{screen_name}」的資料裡找不到 {target}，請確認 element_map.json 是最新的）"
        elif action == "tap":
            outcome = run_tap(d, el)
        elif action == "input_text":
            outcome = run_input_text(d, el, step.get("value", ""))
        elif action == "assert_visible":
            outcome = run_assert_visible(d, el)
        elif action == "assert_text_contains":
            outcome = run_assert_text_contains(d, el, step.get("value", ""))
        else:
            outcome = f"FAIL（未知的 action: {action}）"

        results.append({
            "index": i, "action": action, "screen_name": screen_name,
            "target": target, "value": step.get("value"), "note": step.get("note"),
            "outcome": outcome,
        })

        if action in ("tap", "input_text"):
            wait_after_action(d)

    return {"results": results}


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/ping":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": "not found"})

    KNOWN_PATHS = (
        "/tap", "/assert_visible", "/inspect", "/test_xpath", "/agent_xpath",
        "/llm_chat", "/run_test_script",
    )
    # /llm_chat 純粹是LLM聊天代理，不操作裝置，不需要Appium session存活，
    # 讓使用者就算模擬器還沒開也能先設計測試案例
    PATHS_NOT_REQUIRING_DRIVER = ("/llm_chat",)

    def do_POST(self):
        if self.path not in self.KNOWN_PATHS:
            self._send_json(404, {"error": f"not found，可用路徑: {', '.join(self.KNOWN_PATHS)}"})
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send_json(400, {"error": "請求內容不是合法JSON"})
            return

        try:
            if self.path not in self.PATHS_NOT_REQUIRING_DRIVER:
                ensure_session_alive()
            result = self._dispatch(self.path, payload)
        except Exception as e:
            self._send_json(500, {"error": f"{type(e).__name__}: {e}"})
            return

        self._send_json(200, result)

    def _dispatch(self, path: str, payload: dict) -> dict:
        if path == "/tap":
            return {"result": run_tap(get_driver(), payload)}
        if path == "/assert_visible":
            return {"result": run_assert_visible(get_driver(), payload)}
        if path == "/inspect":
            return inspect_element(payload)
        if path == "/test_xpath":
            return test_xpath(payload.get("xpath", ""))
        if path == "/agent_xpath":
            return agent_generate_xpath(payload)
        if path == "/llm_chat":
            return run_llm_chat(payload)
        if path == "/run_test_script":
            return run_test_script(payload)

    def log_message(self, format, *args):
        print("[viewer_server]", format % args)


def main():
    server = HTTPServer((HOST, PORT), Handler)
    print(f"[viewer_server] 已啟動：http://{HOST}:{PORT}（只接受本機連線）")
    print("[viewer_server] 等待 viewer.html 的按鈕觸發請求，Ctrl+C 結束")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    main()
