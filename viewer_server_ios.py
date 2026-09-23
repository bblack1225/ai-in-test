"""
本機小型伺服器：iOS 版的 viewer_server.py，讓 viewer.html 上的按鈕能透過
HTTP API 呼叫 run_test_case.py 現成的定位邏輯，對 iPhone 執行真正的操作。

跟 Android 版（viewer_server.py）提供完全一樣形狀的 API（/tap、
/assert_visible、/inspect、/test_xpath、/agent_xpath），viewer.html 前端
不用分平台寫兩套邏輯，只要切換連線網址（Android預設8766埠、iOS這支
預設8767埠，兩支可以同時開著）就能對應到不同平台。內部沿用
run_test_case.py 的 run_tap()/run_assert_visible()/locate_generated_element()，
不是另外寫一套新邏輯。

跟 Android 版的關鍵差異：
  - 連線用 XCUITestOptions + test_config.DESIRED_CAPS（iOS caps），不是
    UiAutomator2Options
  - native 元件定位用 AppiumBy.ACCESSIBILITY_ID，不是 AppiumBy.ID
  - iOS 的 Accessibility Tree XML 節點 tag 本身就是 class（例如
    <XCUIElementTypeButton>），不像 Android 的 class 要讀屬性；座標是
    直接的 x/y/width/height 數值屬性，不是 Android 那種 "[x1,y1][x2,y2]"
    字串，/inspect 解析 tree 走訪上層時要對應調整
  - iOS 沒有 resource-id 概念，/inspect 回傳的上層元素資訊改用 name
    （accessibilityIdentifier）/label，viewer.html 前端已經對應處理
  - /agent_xpath 的 prompt 改用 iOS 的 XPath 語法（@label，不是
    @text/@content-desc）

這次沒有接 iPhone，這支伺服器的邏輯是依照 Android 版等比例移植、通過
語法檢查，但沒有實機測試過，之後有裝置時需要優先驗證。

使用方式：
  1. 先確認 iPhone 已連接、WebDriverAgent 簽署有效、目標畫面已經手動導航好
  2. 執行：python viewer_server_ios.py
  3. 瀏覽器打開 viewer.html，選 iOS 的畫面時，連線網址會自動帶入
     http://127.0.0.1:8767（跟Android版共用同一個輸入框，依畫面平台自動切換）

安全提醒：跟 Android 版相同，只綁定 127.0.0.1，不會對外網開放，但按下按鈕
會「真的」操作手機畫面，沒有 verify_current_screen() 前置校驗，請自行確認
手機導航到正確畫面。
"""

import json
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer

from appium import webdriver
from appium.options.ios import XCUITestOptions
from appium.webdriver.common.appiumby import AppiumBy
from google.genai import types
from selenium.common.exceptions import InvalidSessionIdException, NoSuchElementException

from capture_screen_map import scan_screen
from run_test_case import (
    locate_generated_element,
    run_assert_text_contains,
    run_assert_visible,
    run_input_text,
    run_tap,
    wait_after_action,
)
from test_config import APPIUM_SERVER_URL, DESIRED_CAPS, LLM_MODEL, LLM_MODEL_PRO, generate_content_with_retry

HOST = "127.0.0.1"
PORT = 8767

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
        options = XCUITestOptions().load_capabilities(DESIRED_CAPS)
        driver = webdriver.Remote(APPIUM_SERVER_URL, options=options)
        print("[viewer_server_ios] 已連線 Appium，driver 會保持開啟直到這支伺服器結束")
    return driver


def ensure_session_alive():
    """跟 Android 版同樣的道理：不依賴內層程式碼願不願意把
    InvalidSessionIdException 往外拋，每次請求最前面主動探測一次
    （見 viewer_server.py / TROUBLESHOOTING #39）。"""
    global driver
    if driver is None:
        get_driver()
        return
    try:
        driver.find_elements(AppiumBy.ACCESSIBILITY_ID, "__viewer_server_liveness_check__")
    except InvalidSessionIdException:
        print("[viewer_server_ios] [警告] Appium session 已失效，重新連線...")
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
            return driver.find_element(AppiumBy.ACCESSIBILITY_ID, el["native_id"])
        except NoSuchElementException:
            return None
    return locate_generated_element(driver, el)


def inspect_element(el: dict, max_levels: int = 8) -> dict:
    """
    盡力定位這個元件在畫面上目前對應的位置，再解析當下完整 tree，往上層走訪，
    回傳每一層容器的 class/name/label，供使用者手動組出更精準的 XPath。

    跟 Android 版的解析差異：iOS 的 XML 節點 tag 本身就是 class，不用讀
    class 屬性；座標是直接的 x/y/width/height 數值屬性，不用像 Android
    那樣解析 "[x1,y1][x2,y2]" 字串。
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

    best_node, best_distance = None, None
    for node in root.iter():
        if node.tag != el.get("class"):
            continue
        attrib = node.attrib
        try:
            x = float(attrib.get("x", 0))
            y = float(attrib.get("y", 0))
        except ValueError:
            continue
        distance = abs(x - target_bounds["x"]) + abs(y - target_bounds["y"])
        if best_distance is None or distance < best_distance:
            best_distance, best_node = distance, node

    if best_node is None:
        return {"found": True, "target_bounds": target_bounds, "ancestors": []}

    ancestors = []
    current = best_node
    for level in range(1, max_levels + 1):
        parent = parent_map.get(current)
        if parent is None:
            break
        pa = parent.attrib
        ancestors.append({
            "level": level,
            "class": parent.tag,
            "name": pa.get("name", ""),
            "label": pa.get("label", ""),
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
            "name": m.get_attribute("name"),
            "label": m.get_attribute("label"),
            "value": m.get_attribute("value"),
        })
    return {"count": len(matches), "preview": preview}


# ---------------------------------------------------------------------------
# /agent_xpath：把完整 tree 丟給 LLM，請它生成能唯一定位目標元件的 XPath，
# 每個候選都要實測驗證過才採用，不是單次生成就相信（跟 Android 版同樣設計）
# ---------------------------------------------------------------------------

AGENT_XPATH_MAX_ATTEMPTS = 3
POSITION_TOLERANCE_PX = 60


def _build_agent_xpath_prompt(target_info: dict, page_source: str, feedback: str | None) -> str:
    feedback_block = ""
    if feedback:
        feedback_block = f"\n\n上一輪的失敗回饋：{feedback}\n請根據這個回饋修正，不要再犯同樣的錯誤。"

    return f"""你是 iOS UI 自動化測試專家。以下是透過 Appium XCUITest 取得的
畫面 Accessibility Tree（XML 格式，可以用 XPath 查詢；注意 iOS 這裡 XML 的
tag 名稱本身就是元件的 class，例如 <XCUIElementTypeButton>，不是像網頁那樣
用固定的 tag 加 class 屬性）。

請幫我寫一個 XPath 表達式，唯一定位到以下目標元件：
- class（同時也是XML tag名稱）: {target_info.get('class')}
- 文字內容（label）: {target_info.get('label')!r}
- 附近文字線索（同層兄弟節點的 label，可能是清單裡能唯一辨識這一列/區塊的線索）: {target_info.get('nearby_text')}
- 畫面九宮格粗略位置: {target_info.get('position_bucket')}

完整畫面 Tree（XML）：
{page_source}

規則：
1. 只能用標準 XPath 1.0 語法
2. iOS 的元件屬性用 @label 查文字內容（不是 @text 或 @content-desc，
   那是 Android 的屬性名稱）
3. 這個元件的 label 在畫面上可能不是唯一的（例如清單裡每一列都有一個
   label 相同的操作按鈕，直接用 label 查詢會查到多個結果）。這種情況要
   善用 tree 的階層結構：先用鄰近文字線索定位到唯一的錨點節點，再用
   `ancestor::{{某個容器class}}[1]` 找到該錨點「最近的」共同容器（不要
   寫成 `//{{class}}[.//@label=X]` 這種寫法，那樣會連範圍過大的外層同class
   容器也一起選中，查詢範圍沒有真的縮小），最後在這個容器範圍內查找
   目標元件，確保最終查詢結果剛好唯一一個
4. 不要用寫死絕對路徑/index位置的查詢，盡量用屬性條件加結構關係，改版
   時版面微調也比較不容易整個失效{feedback_block}

請回傳 JSON 格式：{{"xpath": "<你的XPath字串>"}}，只回傳 JSON，不要有其他文字，
不要用 markdown code block 包起來。
"""


def agent_generate_xpath(el: dict) -> dict:
    """跟 Android 版（viewer_server.py）完全同樣的「生成→實測驗證→不通過就
    回饋LLM重試」設計，只是 prompt 跟驗證用的 XPath 語法換成 iOS 版本。"""
    d = get_driver()

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
            usage = getattr(response, "usage_metadata", None)
            if usage:
                log.append(f"第{attempt}次[LLM token用量] prompt={usage.prompt_token_count} "
                           f"output={usage.candidates_token_count} total={usage.total_token_count}")
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
# /llm_chat、/run_test_script：跟 Android 版（viewer_server.py）完全同樣的
# 設計，這裡只是把系統提示詞的用詞換成iOS版（label取代text/content-desc），
# 執行動作沿用 run_test_case.py 的 run_tap/run_assert_visible/run_input_text/
# run_assert_text_contains。這兩支這次一樣沒有 iPhone 可實機測試，只通過
# 語法檢查。
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
   - input_text：輸入文字，只對有 accessibility id 的 native 元件有效
     （沒有native id的輸入框無法可靠定位，不要對這種元件用input_text）
   - assert_visible：驗證元件可見
   - assert_text_contains：驗證元件「目前顯示的文字內容（label）」有包含
     指定字串，用在「這個畫面的文字內容因為別的畫面的操作而改變」這種
     跨畫面情境，assert_visible 驗證不到文字內容，只能驗證「有沒有顯示」
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
    """跟 Android 版完全同樣的無狀態聊天代理設計，見 viewer_server.py。"""
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

    usage = getattr(response, "usage_metadata", None)
    usage_info = None
    if usage:
        print(f"[llm_chat token用量] prompt={usage.prompt_token_count} "
              f"output={usage.candidates_token_count} total={usage.total_token_count}"
              f"（{len(messages)} 則對話歷史、{len(screens_context)} 個畫面的精簡資料）")
        usage_info = {
            "prompt_tokens": usage.prompt_token_count,
            "output_tokens": usage.candidates_token_count,
            "total_tokens": usage.total_token_count,
        }

    return {"reply": (response.text or "").strip(), "usage": usage_info}


# ---------------------------------------------------------------------------
# /llm_scenarios：給 app_map_viewer.html「測試情境產生」分頁用——跟 /llm_chat
# 不是同一種東西，這支是「先看過選定畫面的資料，推論出一批可能值得測試的
# 情境描述清單」，產出的是情境列表（不是逐步驟的測試案例JSON），使用者從
# 清單挑一個情境後，才送去 /llm_chat 那條路產生真正的逐步驟測試案例。
# ---------------------------------------------------------------------------

SCENARIO_PROMPT_MARKER = "<<畫面資料JSON會插入這裡>>"

SCENARIO_PROMPT_TEMPLATE = """你是資深QA工程師。以下是使用者選定、已經掃描好的畫面元件資料
（JSON，每個畫面下的元件都只精簡保留 unique_id/id_source/class/label/
description 這幾個欄位）：
<<畫面資料JSON會插入這裡>>

請根據這些畫面「實際存在」的元件，推論出這批畫面上值得寫成自動化測試的
「測試情境」清單。

規則：
1. 每個情境要根基於畫面上真實存在的元件（用它們的 label/description 描述），
   不可以編造資料以外的功能或畫面
2. 情境可以橫跨多個畫面（例如在A畫面點某個按鈕，確認能看到B畫面的某個結果），
   也可以是單一畫面內的驗證（例如確認某些元件存在、內容正確）
3. 優先挑「操作後有明確可驗證結果」的情境（會導致畫面切換、內容改變、或有
   清楚的成功/失敗判斷依據），避免太空泛、講了等於沒講的情境（例如「測試
   App穩定性」這種）
4. 每個情境的 description 要具體寫清楚「要點什麼、預期看到什麼結果」，
   寫到讓另一個不知道上下文的LLM可以直接照著這段描述去產生詳細測試步驟
5. 產出數量不用多，大約 5~10 個，實際依畫面資料豐富度增減；資料太少（例如
   只有一兩個畫面沒幾個元件）就老實產出比較少個，不要為了湊數硬掰情境

請回傳 JSON 格式：
{"scenarios": [
  {"title": "<簡短標題>", "description": "<具體描述，見規則4>", "screens_involved": ["<用到的畫面 screen_name>", "..."]}
]}
只回傳這個 JSON，不要有其他文字，不要用 markdown code block 包起來。
"""


def _build_scenario_prompt(screens_context_json: str, custom_template: str | None = None) -> str:
    """
    custom_template 是使用者在 app_map_viewer.html「測試情境產生」分頁裡
    自己編輯過的提示詞（沒編輯過就是 None，直接用預設模板）。不管用哪個
    模板，都要包含 SCENARIO_PROMPT_MARKER 這個標記，用來把真正的畫面資料
    換進去——標記被刪掉的話，直接回報錯誤，不要偷偷送出一份沒有畫面資料
    的提示詞給LLM（那樣LLM只會憑空亂猜，產出的情境完全不可信）。
    """
    template = custom_template or SCENARIO_PROMPT_TEMPLATE
    if SCENARIO_PROMPT_MARKER not in template:
        raise ValueError(f"提示詞裡找不到「{SCENARIO_PROMPT_MARKER}」這個標記，沒辦法把畫面資料插進去，請保留這段標記")
    return template.replace(SCENARIO_PROMPT_MARKER, screens_context_json)


def llm_generate_scenarios(payload: dict) -> dict:
    screens_context = payload.get("screens_context", [])
    try:
        prompt = _build_scenario_prompt(json.dumps(screens_context, ensure_ascii=False), payload.get("prompt_template"))
    except ValueError as e:
        return {"error": str(e)}

    try:
        response = generate_content_with_retry(
            model=LLM_MODEL_PRO,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.4, response_mime_type="application/json"),
        )
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    usage = getattr(response, "usage_metadata", None)
    usage_info = None
    if usage:
        print(f"[llm_scenarios token用量／{LLM_MODEL_PRO}] prompt={usage.prompt_token_count} "
              f"output={usage.candidates_token_count} total={usage.total_token_count}"
              f"（{len(screens_context)} 個畫面的精簡資料）")
        usage_info = {
            "prompt_tokens": usage.prompt_token_count,
            "output_tokens": usage.candidates_token_count,
            "total_tokens": usage.total_token_count,
        }

    try:
        parsed = json.loads((response.text or "").strip())
    except Exception as e:
        return {"error": f"LLM回應不是合法JSON（{type(e).__name__}: {e}）", "usage": usage_info}

    return {"scenarios": parsed.get("scenarios", []), "usage": usage_info}


def run_test_script(payload: dict) -> dict:
    """跟 Android 版完全同樣的多畫面測案執行設計，見 viewer_server.py。"""
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


# ---------------------------------------------------------------------------
# /scan_screen：給 app_map_viewer.html 的「手動建樹」功能用——使用者手動點擊
# 一個元件把手機導到新畫面後，對「目前畫面」執行一次真正的掃描，直接沿用
# capture_screen_map.py 的 scan_screen()，不是另外寫一套解析邏輯。
#
# describe 預設 False（跳過 LLM 描述，理由跟 crawl_app_tree.py 一樣：手動
# 建樹本來就是一個節點一個節點慢慢加，先求有結構、之後要不要對這個畫面
# 補描述由使用者自己決定，不要每加一個節點就預設付一次 API 成本）。
# scroll 預設 True，跟 crawl_app_tree.py 不同——手動建樹不是「爬完立刻要用
# 座標點下一步」，使用者是特地要完整記錄這個畫面，值得多花時間把可捲動
# 內容也收集完整。
# ---------------------------------------------------------------------------

def do_scan_screen(payload: dict) -> dict:
    screen_name = (payload.get("screen_name") or "").strip()
    if not screen_name:
        return {"error": "screen_name 不可為空"}
    d = get_driver()
    screen_data = scan_screen(
        d, screen_name,
        describe=bool(payload.get("describe", False)),
        scroll=bool(payload.get("scroll", True)),
        wait_stable=True,
    )
    return {"screen_data": screen_data}


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
        if self.path == "/llm_scenarios_prompt":
            self._send_json(200, {"template": SCENARIO_PROMPT_TEMPLATE, "marker": SCENARIO_PROMPT_MARKER})
            return
        self._send_json(404, {"error": "not found"})

    KNOWN_PATHS = (
        "/tap", "/assert_visible", "/inspect", "/test_xpath", "/agent_xpath",
        "/llm_chat", "/run_test_script", "/scan_screen", "/llm_scenarios",
    )
    PATHS_NOT_REQUIRING_DRIVER = ("/llm_chat", "/llm_scenarios")

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
        if path == "/scan_screen":
            return do_scan_screen(payload)
        if path == "/llm_scenarios":
            return llm_generate_scenarios(payload)

    def log_message(self, format, *args):
        print("[viewer_server_ios]", format % args)


def main():
    server = HTTPServer((HOST, PORT), Handler)
    print(f"[viewer_server_ios] 已啟動：http://{HOST}:{PORT}（只接受本機連線）")
    print("[viewer_server_ios] 等待 viewer.html 的按鈕觸發請求，Ctrl+C 結束")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    main()
