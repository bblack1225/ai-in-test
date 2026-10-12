"""
本機小型伺服器：讓 app_map_viewer.html 上的按鈕能透過 HTTP API
呼叫 run_test_case.py 現成的定位邏輯，對手機執行真正的操作。iOS、Android 共用
這一支，啟動時用 --platform 指定這次接哪種裝置。

背景：瀏覽器沒辦法直接連 Appium（不能對本機的 Appium server 開 socket 連線），
所以需要這支伺服器當橋樑：伺服器維持一條 Appium 連線，瀏覽器按鈕觸發時打 API
給它，伺服器內部沿用 run_tap()/run_assert_visible() 等現成函式真的去操作手機，
不是另外寫一套新邏輯。

一個 Appium session 只綁一台裝置，所以一支伺服器一次只接一台（iOS 或
Android 二選一）。前端打 /ping 會拿到 {"platform": "ios"|"android"}，
據此知道目前接的是哪種手機，不用再依平台切換連線網址。

平台差異（連線設定、native id 定位方式、Accessibility Tree 的解析、/agent_xpath
prompt 的平台用語）都在 platforms/ios.py、platforms/android.py，這支只透過
PLATFORM 取用。

API：
  - /tap、/assert_visible：單次點擊/驗證可見
  - /inspect：先用現成的定位邏輯盡力找出這個元件目前對應的畫面位置，再解析
    當下的完整 tree，往上層走訪，回傳每一層容器的資訊，給使用者手動組出
    更精準的 XPath 用
  - /test_xpath：讓使用者在儲存前先測試自己寫的 XPath 查到幾個結果
  - /agent_xpath：把當下完整 tree + 目標元件的已知線索丟給 LLM，請它生成
    唯一定位的 XPath，**不是單次生成就相信**——每個候選都會實測（查詢是否
    唯一、位置是否吻合），不通過就把失敗原因回饋給 LLM 重試
  - /llm_chat：「LLM 測案產生」的多輪對話代理，純聊天不操作裝置
  - /llm_scenarios（+ GET /llm_scenarios_prompt）：「測試情境產生」
  - /run_test_script、/stop_test_script：執行／停止一份可能橫跨多個畫面的測案
  - /scan_screen：「手動建樹」對目前畫面執行一次掃描（帶 project 的話截圖存進專案資料夾）
  - /api/projects/...：workspace 專案（地圖、測案、批次）的讀寫、匯出／匯入 zip，
    見 workspace.py；不需要手機

使用方式：
  1. 先確認手機已連接、目標畫面已經手動導航好（iOS 另需 WebDriverAgent 簽署有效）
  2. 執行：uv run aitest-server --platform ios（或 --platform android）
  3. 瀏覽器打開 http://127.0.0.1:8767/ （網頁由這支伺服器提供，跟 API 同源；
     直接雙擊 HTML 檔用 file:// 開啟的話，網頁連不到這支伺服器）

安全提醒：只綁定 127.0.0.1，而且只接受這支伺服器自己網頁發出的請求（見
Handler._request_allowed），其他網站沒辦法借你的瀏覽器操作手機。按下按鈕會「真的」操作手機
畫面，單次操作（點擊、驗證可見等）沒有 verify_current_screen() 前置校驗，
請自行確認手機導航到正確畫面。「測案執行」（/run_test_script）例外：
執行前會先做第0步畫面檢查，畫面不符就整份不執行（可從 viewer 取消勾選略過）。
"""

import argparse
import base64
import binascii
import copy
import json
import mimetypes
import signal
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from appium import webdriver
from appium.webdriver.common.appiumby import AppiumBy
from google.genai import types
from selenium.common.exceptions import InvalidSessionIdException, NoSuchElementException, WebDriverException

from aitest.config import APPIUM_SERVER_URL, LLM_MODEL, LLM_MODEL_PRO, PROJECT_ROOT, generate_content_with_retry
from aitest.core.capture_screen_map import describe_generated_elements, describe_native_elements, scan_screen
from aitest.core.navigator import MapIndex, Navigator
from aitest.core.run_test_case import (
    StopRequested,
    check_current_screen,
    run_assert_visible,
    run_step_in_case,
    run_tap,
    should_wait_after,
    try_dismiss_dialogs,
    wait_after_action,
    with_dismiss_note,
)
from aitest.core.workspace import DEFAULT_ROOT, Workspace, WorkspaceError
from aitest.platforms import PLATFORMS, get_platform
from aitest.run_context import LLMUsage, RunContext

HOST = "127.0.0.1"
DEFAULT_PORT = 8767
PORT = DEFAULT_PORT  # main() 依 --port 設定，檢查 Host／Origin 標頭用

# 網頁由這支伺服器直接提供（http://127.0.0.1:8767/），跟 API 同源，不需要 CORS。
# 網頁（HTML／CSS／JS）放在 web/；截圖等資料沿用其他腳本的慣例，相對於啟動時的工作目錄。
WEB_DIR = PROJECT_ROOT / "web"
WEB_PAGES = {"/": "app_map_viewer.html", "/app_map_viewer.html": "app_map_viewer.html"}
# web/ 裡可以直接給網頁讀的檔案類型（只看 web/ 第一層，不會讀到子資料夾或外面）
WEB_ASSET_SUFFIXES = (".css", ".js")
DATA_DIRS = ("screenshots", "crops", "test_results")
# 專案資料夾裡可以給網頁讀的子資料夾：/projects/<專案>/screenshots/xxx.png
PROJECT_STATIC_DIRS = ("screenshots", "crops")
WORKSPACE = Workspace(DEFAULT_ROOT)  # main() 依 --workspace 設定


PLATFORM = None  # main() 依 --platform 設定，平台差異都在 platforms/ 裡
driver = None

# 測案執行的停止機制：/run_test_script 跑的時候，viewer 另外送 /stop_test_script
# 設旗標，run_test_script 每一步開始前檢查，已經送到 Appium 的那一步沒辦法
# 中途取消，所以是「目前這步跑完才停」。server 因此改成 ThreadingHTTPServer，
# 不然 /stop_test_script 會排在測案後面，等測案跑完才被處理。
# 多執行緒後，操作裝置的請求用 _device_lock 排隊；測案執行中則直接擋掉，
# 避免手動點擊跟測案同時操作手機。
_device_lock = threading.Lock()
_test_running = threading.Event()
_stop_requested = threading.Event()
# 測案（/run_test_script）、批次（/run_batch）的即時進度：這兩個是同步請求，要跑完才回傳，
# 所以另外開 GET /test_progress、/batch_progress 讓 viewer 執行中輪詢。進度存在每次執行自己的
# RunContext.progress 裡（讀寫都經過 ctx.lock），這裡只記「最近一次」是哪個 ctx，跑完也留著，
# 讓最後一次輪詢看得到 running=False 和完整結果
_IDLE_PROGRESS = {
    "test": {"running": False, "current": None, "total": 0, "results": [], "precheck": None},
    "batch": {"running": False, "groups": [], "current": None},
}
_latest_runs: dict = {"test": None, "batch": None}
_latest_runs_lock = threading.Lock()


def _new_ctx(**values) -> RunContext:
    """這支伺服器每個操作手機的請求都建一份新的 RunContext；按「⏹ 停止」會讓它的 raise_if_stopped() 丟出例外"""
    return RunContext(stop_checker=_stop_requested.is_set, **values)


def _register_run(kind: str, ctx: RunContext) -> None:
    with _latest_runs_lock:
        _latest_runs[kind] = ctx


def _progress_snapshot(kind: str) -> dict:
    with _latest_runs_lock:
        ctx = _latest_runs[kind]
    return ctx.progress_snapshot() if ctx is not None else copy.deepcopy(_IDLE_PROGRESS[kind])


def _locked_update(ctx: RunContext, target: dict, **values) -> None:
    """改批次進度裡某一份測案的欄位（網頁輪詢的執行緒同時在讀）"""
    with ctx.lock:
        target.update(values)


def get_driver(force_reconnect: bool = False):
    global driver
    if force_reconnect and driver is not None:
        try:
            driver.quit()
        except Exception:
            pass
        driver = None
    if driver is None:
        driver = webdriver.Remote(APPIUM_SERVER_URL, options=PLATFORM.make_options())
        print(f"[viewer_server] 已連線 Appium（{PLATFORM.name}），driver 會保持開啟直到這支伺服器結束")
    return driver


def ensure_session_alive():
    """
    在每次請求真正處理前，先用一個很輕量的查詢驗證 session 是不是還活著。

    這個檢查存在的原因：inspect_element()/test_xpath() 內部（以及它們呼叫的
    run_test_case 定位函式）大多用「try/except Exception 廣泛捕捉、捕到就當作
    查不到、回傳 None」的寫法——這對「單一定位方式失敗、換下一種」是正確的
    設計，但也會把 InvalidSessionIdException（session 已失效）跟「真的查不到」
    混在一起，讓 do_POST() 原本設計的自動重連邏輯永遠等不到這個例外傳上來
    （實測踩過：session失效時，/inspect 只會回報「找不到這個元件」，完全看不出
    是連線問題，見 TROUBLESHOOTING #39）。這裡改成在最前面主動探測，不依賴
    內層程式碼願不願意把例外往外拋。（重構步驟 4 之後 platforms/、定位相關的程式
    已經改成讓 InvalidSessionIdException 往上傳，這個探測保留：重連要在處理請求前做）
    """
    global driver
    if driver is None:
        get_driver()
        return
    try:
        driver.find_elements(PLATFORM.native_by, "__viewer_server_liveness_check__")
    except InvalidSessionIdException:
        print("[viewer_server] [警告] Appium session 已失效，重新連線...")
        get_driver(force_reconnect=True)


def _locate_best_effort(ctx, driver, el: dict):
    """沿用現成的定位鏈，盡力找出這個元件目前對應的畫面元素，找不到就回傳 None。"""
    if el.get("custom_xpath"):
        try:
            matches = driver.find_elements(AppiumBy.XPATH, el["custom_xpath"])
            if len(matches) == 1:
                return matches[0]
        except InvalidSessionIdException:
            raise
        except WebDriverException:
            pass
    if el.get("id_source") == "native" and el.get("native_id"):
        try:
            return driver.find_element(PLATFORM.native_by, el["native_id"])
        except NoSuchElementException:
            return None
    return PLATFORM.locate_generated_element(ctx, driver, el)


def inspect_element(el: dict, max_levels: int = 8) -> dict:
    """
    盡力定位這個元件在畫面上目前對應的位置，再解析當下完整 tree，往上層走訪，
    回傳每一層容器的資訊（iOS: class/name/label；Android: class/resource-id/
    text/content-desc），供使用者手動組出更精準的 XPath。
    找不到起點時回傳 found=False，前端會提示使用者確認手機畫面是否正確。
    """
    d = get_driver()
    matched = _locate_best_effort(_new_ctx(), d, el)
    if matched is None:
        return {"found": False, "ancestors": []}

    target_bounds = {
        "x": matched.location["x"], "y": matched.location["y"],
        "width": matched.size["width"], "height": matched.size["height"],
    }

    root = ET.fromstring(d.page_source)
    parent_map = {child: parent for parent in root.iter() for child in parent}

    # 用即時座標比對出這個元件在目前 tree 裡對應的 XML 節點（同 class、
    # 座標最接近的視為同一個）
    best_node, best_distance = None, None
    for node in root.iter():
        if PLATFORM.node_class(node) != el.get("class"):
            continue
        bounds = PLATFORM.node_bounds(node)
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
        if parent is None:
            break
        info = PLATFORM.ancestor_info(parent)
        if info is None:
            break
        ancestors.append({"level": level, **info})
        current = parent

    return {"found": True, "target_bounds": target_bounds, "ancestors": ancestors}


def test_xpath(xpath: str) -> dict:
    """讓使用者儲存前先測試自訂 XPath 查到幾個結果，不用真的存檔/執行測案才知道。"""
    d = get_driver()
    try:
        matches = d.find_elements(AppiumBy.XPATH, xpath)
    except InvalidSessionIdException:
        raise
    except WebDriverException as e:
        return {"count": 0, "error": f"{type(e).__name__}: {e}"}

    preview = [{"location": m.location, "size": m.size, **PLATFORM.match_preview(m)} for m in matches[:5]]
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

    p = PLATFORM
    return f"""{p.xpath_intro}

請幫我寫一個 XPath 表達式，唯一定位到以下目標元件：
- {p.xpath_class_field}: {target_info.get('class')}
- {p.xpath_text_field}: {target_info.get('label')!r}
- 附近文字線索（{p.xpath_nearby_desc}，可能是清單裡能唯一辨識這一列/區塊的線索）: {target_info.get('nearby_text')}
- 畫面九宮格粗略位置: {target_info.get('position_bucket')}

完整畫面 Tree（XML）：
{page_source}

規則：
1. 只能用標準 XPath 1.0 語法（底層引擎不保證支援 XPath 2.0 函式）
2. {p.xpath_attr_rule}
3. 這個元件的文字內容在畫面上可能不是唯一的（例如清單裡每一列都有一個
   文字相同的操作按鈕，直接用文字查詢會查到多個結果）。這種情況要善用
   tree 的階層結構：先用鄰近文字線索定位到唯一的錨點節點，再用
   `ancestor::{{某個容器class}}[1]` 找到該錨點「最近的」共同容器（不要
   寫成 `{p.xpath_bad_example}` 這種寫法，那樣會連範圍過大的外層同class
   容器也一起選中，查詢範圍沒有真的縮小），最後在這個容器範圍內查找
   目標元件，確保最終查詢結果剛好唯一一個
4. 不要用寫死絕對路徑/index位置的查詢，盡量用屬性條件加結構關係，改版
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
    ctx = _new_ctx()

    # 有現成定位邏輯找得到的話，拿它的即時座標當作驗證 LLM 答案對不對的基準；
    # 找不到也沒關係（這正是需要 AI 救援的情境），只是少了位置校驗這一層防呆
    ground_truth_bounds = None
    matched = _locate_best_effort(ctx, d, el)
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
                usage=ctx.usage,
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
        except InvalidSessionIdException:
            raise
        except WebDriverException as e:
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
        return {"success": True, "xpath": candidate, "attempts": attempt, "log": log, "usage": ctx.usage.as_dict()}

    return {"success": False, "xpath": last_xpath, "attempts": AGENT_XPATH_MAX_ATTEMPTS, "log": log,
            "usage": ctx.usage.as_dict()}


# ---------------------------------------------------------------------------
# /llm_chat：「LLM 測案產生」分頁的聊天代理。瀏覽器不能直接呼叫 Gemini
# （會把 GEMINI_API_KEY 暴露在前端原始碼/network面板），所以跟 /agent_xpath
# 一樣走伺服器端代理；差別是這裡的系統提示詞規範的是「產生一份可以透過
# Appium 執行、可能橫跨多個畫面的測試案例」，不是單一元件的 XPath。
# 純聊天不需要操作裝置，不強制要求 Appium session 存活（見 do_POST 的例外名單）。
# ---------------------------------------------------------------------------

def _var_keys_prompt(var_keys: dict) -> str:
    """專案變數檔裡有哪些 KEY（只給名稱和檔名，不給值）"""
    if not var_keys:
        return "     專案目前還沒有任何變數檔。"
    lines = [f"       - {k}（{'、'.join(files)}）" for k, files in sorted(var_keys.items())]
    return "     專案已經有的變數（KEY 和有這個 KEY 的變數檔名稱，值不提供），對應得上的一定要用這些 KEY：\n" + "\n".join(lines)


def _build_llm_testgen_system_prompt(screens_context_json: str, var_keys: dict | None = None) -> str:
    # assert_banner 只有支援的平台才告訴 LLM，不然會產生執行端只能 SKIP 的步驟
    banner_step = banner_rule = ""
    action_count = 4
    target_scope = "target "
    if PLATFORM.supports_banner:
        action_count = 5
        target_scope = "target（assert_banner 以外）"
        banner_step = (',\n       {"action": "assert_banner", "screen_name": "<畫面名稱>", '
                       '"value": ["<跑馬燈上要出現的文字>", "..."], "note": "..."}')
        banner_rule = """
   - assert_banner：驗證「只出現幾秒就消失」的跑馬燈/通知橫幅（例如下單後
     上方的「委託回報」）。不需要 target；value 是跑馬燈上要出現的文字陣列，
     例如 ["委託回報", "2882 你好金"]。這一步必須**緊接在觸發它的 tap 後面**
     （例如送出委託的按鈕），中間不要插入其他步驟，否則跑馬燈會先消失。
     接連會跳出好幾個跑馬燈時，每個跑馬燈各寫一個 assert_banner、連續排在一起，
     執行端會自動合併成一次監看，不用擔心前一個等太久讓後一個消失。預設只檢查
     「都有出現」、不管先後；使用者明確要求依序出現（例如「先…再…」「依序」）時，
     在後面那一步加上 "in_order": true，代表它必須比前一個 assert_banner 晚出現"""

    var_keys_text = _var_keys_prompt(var_keys or {})
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
       {{"action": "assert_text_contains", "screen_name": "<畫面名稱>", "target": "<unique_id>", "value": "<預期文字要包含的內容>", "timeout": <選填，秒數>, "note": "..."}}{banner_step}
     ]
   }}
2. action 只能是這{action_count}種：
   - tap：點擊
   - input_text：輸入文字。target 是使用者要輸入的那個欄位就好，有沒有 native id 都可以；
     畫面上顯示數值、點了才跳出輸入列的元件（例如顯示「0 股」的 TextView）也直接對它用
     input_text，執行時會自動先點開、輸入到跳出來的輸入框再送出，不要另外加 tap 步驟。
     同一個輸入欄位常常有好幾筆元件疊在一起（外層有 id 的容器、裡面沒有 id 的輸入框），
     target 優先選 id_source 是 "native" 的那筆（即使它是容器，例如描述寫「…輸入欄位容器」），
     執行時會自動往裡面找到真正的輸入框；id_source 是 "generated" 而且 label 是空的元件，
     執行時定位不到，不要選。只剩這種元件可選時照樣產生，但在 JSON 外面的說明文字提醒
     使用者這一步可能定位不到，建議在網頁上幫這個元件設定自訂 XPath。
     帳號、密碼、身分證字號這類登入資料**不要寫真的值**，一律用變數代號：
     帳號 "${{LOGIN_ID}}"、密碼 "${{LOGIN_PASSWORD}}"，其他的照 "${{大寫英文名稱}}" 命名，
     執行時從使用者選的變數檔讀入；使用者在對話裡給了真的密碼也不要照抄進 JSON。
     股票代號、張數、價格、金額這類「換一組資料就能重跑」的測試資料也用變數代號
     （例如 "${{STOCK_CODE}}"、"${{QTY}}"），input_text、assert_text_contains、assert_banner
     的 value 都可以用。有用到變數時，在 JSON 外面的說明文字列出每個變數代表什麼、
     使用者這次提到的值是多少（密碼類不要列值），方便使用者建立變數檔。
{var_keys_text}
   - assert_visible：驗證元件可見。target 的選法跟 input_text 一樣：優先選 id_source 是
     "native" 的元件；id_source 是 "generated" 而且 label 是空的元件執行時定位不到，這一步
     會被略過（SKIP）、等於沒驗證，不要選。同一個區塊有好幾筆重疊時（外層有 id 的容器、
     裡面沒 id 的元件），選有 id 的那筆。只剩這種元件可選時照樣產生，但在說明文字提醒使用者
     這一步可能會被略過，建議在網頁上幫這個元件設定自訂 XPath。
   - assert_text_contains：驗證元件「目前顯示的文字內容」有包含指定字串，
     用在「這個畫面的文字內容因為別的畫面的操作而改變」這種跨畫面情境
     （例如在設定頁存了一個名稱，回首頁後問候語應該要出現這個名稱），
     assert_visible 驗證不到文字內容，只能驗證「有沒有顯示」{banner_rule}
   - assert_visible 和 assert_text_contains 可以加選填的 "timeout"（秒數，
     數字），代表「在這麼多秒內反覆檢查，一通過就算 PASS，時間到還沒通過
     才算 FAIL」。用在操作後畫面要隔一段時間才會自己更新的情境，例如
     「取消委託後 3~5 秒委託狀態才會變成委託取消」：在取消的 tap 後面接
     {{"action": "assert_text_contains", ..., "value": "委託取消", "timeout": 8}}。
     timeout 抓使用者說的最長等待時間再多留幾秒緩衝；使用者沒提到要等、
     畫面也是立刻更新的情境就不要加。不要另外編一個「等待」步驟，沒有這種 action
3. {target_scope}的值**只能是上面提供的畫面資料裡真的存在的 unique_id**，不可以
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
7. 使用者可能會附上流程圖、規格書 PDF、截圖等附件（訊息裡標示【附件：檔名】），
   用來說明要測的流程與預期結果。附件只是「要測什麼」的參考：步驟的 target
   仍然只能用上面畫面資料裡真的存在的 unique_id（規則3），不能因為附件上
   畫了某個按鈕就編造 id。附件裡的流程有步驟對應不到任何畫面元件時，在說明
   文字裡列出是哪幾步、建議補掃哪個畫面；附件內容看不清楚或有歧義時，先反問
"""


# /llm_chat 的附件：Gemini 能直接讀的圖片、PDF 原封不動交給它；純文字類解碼後當文字送
# （比當成檔案送穩定，CSV/JSON/Markdown 都適用）。Word/Excel 這類 Gemini 讀不了，前端就擋掉
LLM_ATTACHMENT_BINARY_TYPES = {"image/png", "image/jpeg", "image/webp", "application/pdf"}
LLM_ATTACHMENT_TEXT_TYPES = {"text/plain", "text/markdown", "text/csv", "application/json"}


def _message_parts(message: dict) -> list:
    """一則對話訊息轉成 Gemini 的 parts：附件在前、文字在後（先看資料再看指示）"""
    parts = []
    for att in message.get("attachments") or []:
        name, mime = att.get("name", "附件"), att.get("mime", "")
        data = base64.b64decode(att.get("data", ""))
        if mime in LLM_ATTACHMENT_BINARY_TYPES:
            parts.append(types.Part.from_text(text=f"【附件：{name}】"))
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))
        elif mime in LLM_ATTACHMENT_TEXT_TYPES:
            parts.append(types.Part.from_text(text=f"【附件：{name}】\n{data.decode('utf-8', errors='replace')}"))
        else:
            raise ValueError(f"不支援的附件格式：{name}（{mime}）")
    text = message.get("text", "")
    if text or not parts:
        parts.append(types.Part.from_text(text=text))
    return parts


def run_llm_chat(payload: dict) -> dict:
    """
    多輪對話代理：把系統提示詞（含使用者選定的畫面元件精簡資料）+ 目前為止的
    對話歷史丟給 Gemini，回傳這一輪的助理回覆文字。每次都是無狀態呼叫（伺服器
    不保存對話紀錄），完整歷史由前端保存、每次呼叫都整包送過來，這支伺服器
    只負責代理呼叫、不碰 API 金鑰以外的狀態。
    """
    messages = payload.get("messages", [])
    screens_context = payload.get("screens_context", [])

    var_keys = {}
    if payload.get("project"):
        try:
            var_keys = WORKSPACE.var_keys(payload["project"])
        except WorkspaceError as e:
            print(f"[llm_chat] 讀不到專案變數，這次不提供給 LLM：{e}")
    system_prompt = _build_llm_testgen_system_prompt(json.dumps(screens_context, ensure_ascii=False), var_keys)

    try:
        contents = [
            types.Content(role="user" if m.get("role") == "user" else "model", parts=_message_parts(m))
            for m in messages
        ]
    except (ValueError, binascii.Error) as e:
        return {"error": f"附件讀取失敗：{e}"}
    attachment_count = sum(len(m.get("attachments") or []) for m in messages)

    usage = LLMUsage()  # 這一輪對話自己的用量，跟同時在跑的掃描、其他對話分開算
    try:
        response = generate_content_with_retry(
            usage=usage,
            model=LLM_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(temperature=0.3, system_instruction=system_prompt),
        )
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    print(f"[llm_chat token用量] {usage.summary()}"
          f"（{len(messages)} 則對話歷史、{len(screens_context)} 個畫面的精簡資料、{attachment_count} 個附件）")
    return {"reply": (response.text or "").strip(), "usage": usage.as_dict()}


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

    usage = LLMUsage()
    try:
        response = generate_content_with_retry(
            usage=usage,
            model=LLM_MODEL_PRO,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.4, response_mime_type="application/json"),
        )
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    usage_info = usage.as_dict()
    print(f"[llm_scenarios token用量／{LLM_MODEL_PRO}] {usage.summary()}（{len(screens_context)} 個畫面的精簡資料）")

    try:
        parsed = json.loads((response.text or "").strip())
    except Exception as e:
        return {"error": f"LLM回應不是合法JSON（{type(e).__name__}: {e}）", "usage": usage_info}

    return {"scenarios": parsed.get("scenarios", []), "usage": usage_info}


# ---------------------------------------------------------------------------
# /run_test_script：「測案執行」分頁——執行 LLM（或人工）產生、可能橫跨多個
# 畫面的測試案例。跟 run_test_case*.py 這兩支CLI的差異：CLI假設整個測案只在
# 單一畫面內（element_lookup只從一個screen_name建立），這裡的每個step都自帶
# screen_name，要能同時從測案引用到的所有畫面裡找元件，實際執行動作
# （run_tap/run_assert_visible/...）直接沿用同一批函式，不重寫定位邏輯，
# 維持「同一套定位規則，不管從哪裡觸發都一樣」。
# ---------------------------------------------------------------------------

def run_test_script(payload: dict) -> dict:
    element_map = payload.get("element_map") or {}
    test_case = payload.get("test_case") or {}

    screens_by_name = {s["screen_name"]: s for s in element_map.get("screens", [])}
    steps = test_case.get("steps", [])
    var_names = payload.get("vars") or []
    if var_names and not payload.get("project"):
        return {"error": "選了變數檔，但沒有開啟專案，讀不到變數檔"}
    try:
        var_files = WORKSPACE.read_vars(payload["project"], var_names) if var_names else {}
    except WorkspaceError as e:
        return {"error": f"變數檔讀不到：{e}"}

    d = get_driver()
    merged = {}
    for name in var_names:  # 後面選的蓋前面的
        merged.update(var_files[name])
    ctx = _new_ctx(dismiss_dialogs=bool(payload.get("dismiss_dialogs", False)))
    ctx.set_vars(merged, var_names)
    results = []
    ctx.progress = {"running": True, "current": 0, "total": len(steps), "results": results, "precheck": None}
    _stop_requested.clear()
    _register_run("test", ctx)
    _test_running.set()
    try:
        with PLATFORM.page_source_context(d):
            # 開始前先關掉擋在前面的彈窗（例如一進 App 就跳的 Root／USB 偵錯警告）：彈窗是 App 裡的一層
            # 覆蓋時，底下的元件還找得到，第一步的 tap 會「成功」點在遮罩上，等不到步驟失敗再處理
            dismissed = try_dismiss_dialogs(ctx, PLATFORM, d)
            if payload.get("check_screen", True):
                precheck = _precheck_screen(ctx, d, steps, screens_by_name)
            else:
                precheck = {"status": "skipped", "screen_name": None, "message": "已略過執行前的畫面檢查"}
            if dismissed:
                precheck["message"] = with_dismiss_note(precheck["message"], dismissed)
            ctx.update_progress(precheck=precheck)
            print(f"[測案執行] 第0步畫面檢查：{precheck['status']}，{precheck['message']}")
            if precheck["status"] == "fail":
                results = [_step_result(i, step, "SKIP（第0步畫面檢查沒通過，沒有執行）")
                           for i, step in enumerate(steps, 1)]
                ctx.update_progress(results=results)
            else:
                _run_steps(ctx, d, steps, screens_by_name, results, track_current=True)
    finally:
        _test_running.clear()
        ctx.update_progress(running=False, current=None)
    return {"results": results, "stopped": ctx.stopped(), "precheck": precheck}


def _precheck_screen(ctx, d, steps: list, screens_by_name: dict) -> dict:
    """
    第0步：確認手機目前停在第一步要操作的畫面上。忘了先把手機導回起始畫面時，
    每一步都會找不到元件、把畫面上的容器一個一個捲過一輪（很久），最後還會退回
    用 element_map 的舊座標在錯的畫面上亂點，所以畫面不符就整份不執行。
    assert_banner 不綁畫面，找第一個真的有畫面的步驟來比對。
    """
    first = next((s for s in steps if s.get("action") != "assert_banner"), None)
    if first is None:
        return {"status": "unchecked", "screen_name": None, "message": "測案裡沒有綁定畫面的步驟，無法檢查"}
    screen_name = first.get("screen_name")
    screen = screens_by_name.get(screen_name)
    if screen is None:
        return {"status": "unchecked", "screen_name": screen_name,
                "message": f"第一步的畫面「{screen_name}」不在地圖資料裡，無法檢查"}
    status, message = check_current_screen(PLATFORM, d, screen_name, screen.get("elements", []))
    if status == "fail":
        # 畫面對不上可能只是被非預期彈窗蓋住（例如一打開 App 就跳的公告）：
        # 勾選了自動關閉彈窗時，關掉再檢查一次，不然整份測案會直接 SKIP，輪不到步驟失敗時的處理
        dismissed = try_dismiss_dialogs(ctx, PLATFORM, d)
        if dismissed:
            status, message = check_current_screen(PLATFORM, d, screen_name, screen.get("elements", []))
            message = with_dismiss_note(message, dismissed)
    return {"status": status, "screen_name": screen_name, "message": message}


def _step_result(i: int, step: dict, outcome: str) -> dict:
    return {
        "index": i, "action": step.get("action"), "screen_name": step.get("screen_name"),
        "target": step.get("target"), "value": step.get("value"), "note": step.get("note"),
        "outcome": outcome,
    }


def _run_steps(ctx, d, steps: list, screens_by_name: dict, results: list, track_current: bool = False):
    """
    逐步執行，每做完一步就把結果 append 到 results（results 是 ctx.progress 裡的 list，網頁輪詢看得到）。
    track_current：單份測案執行時把「目前第幾步」寫進 ctx.progress["current"]；批次執行的 current
    是「第幾組第幾份」，不能被蓋掉
    """
    banner_outcomes = {}
    for i, step in enumerate(steps, 1):
        if ctx.stopped():
            with ctx.lock:
                results.append(_step_result(i, step, "SKIP（使用者已停止測試）"))
            continue

        if track_current:
            ctx.update_progress(current=i)
        print(f"[測案執行] 第{i}/{len(steps)}步：{step.get('action')} {step.get('target')}")
        screen = screens_by_name.get(step.get("screen_name"))
        el = None
        if screen is not None:
            el = next((e for e in screen.get("elements", []) if e.get("unique_id") == step.get("target")), None)
        try:
            outcome = run_step_in_case(ctx, PLATFORM, d, steps, i, el, banner_outcomes)
        except StopRequested:
            outcome = "SKIP（使用者已停止，這一步做到一半就中斷）"
        with ctx.lock:
            results.append(_step_result(i, step, outcome))

        if should_wait_after(PLATFORM, steps, i) and not ctx.stopped():
            wait_after_action(PLATFORM, d)


# ---------------------------------------------------------------------------
# /run_batch：一次跑多份測案。payload 是好幾組「一張地圖 → 一份或多份測案」：
#   {"groups": [{"map_name", "map", "setup_case": {"name", "case"} | null, "setup_map": {...} | null,
#                "cases": [{"name", "case", "login"?, "vars"?: [...]}, ...]}, ...],
#    "options": {"auto_navigate", "allow_restart", "dismiss_dialogs"}, "project"}
# 每份測案開始前用 navigator 把手機帶到它的起始畫面（見 navigator.py）；
# setup_case 是「重開 App 後要先跑的登入測案」，畫面在 setup_map（登入頁另外爬成一張地圖時），
# 沒給就跟這組的測案用同一張地圖。
# login 是登入用的變數檔名稱（專案 vars/）：跟目前登入的不同時，重開 App 再用這個檔的值跑
# setup_case（${LOGIN_ID} 等代號換成檔裡的值）；沒選的測案照目前狀態直接跑。
# vars 是這份測案另外要用的變數檔（股票代號、張數…），只拿來換代號、不會觸發重新登入；
# 同一個 KEY 的優先順序：後面的 vars → 前面的 vars → login → 環境變數。
# 一份測案失敗或回不到起點不會中斷整批，標成 FAIL／SKIP 後接著跑下一份。
# ---------------------------------------------------------------------------

def _case_status(results: list, stopped: bool) -> str:
    outcomes = [r["outcome"] for r in results]
    if any(o.startswith("FAIL") for o in outcomes):
        return "fail"
    if stopped or not outcomes or all(o.startswith("SKIP") for o in outcomes):
        return "skip"
    return "pass"


def _first_screen(steps: list) -> str | None:
    first = next((s for s in steps if s.get("action") != "assert_banner"), None)
    return first.get("screen_name") if first else None


def run_batch(payload: dict) -> dict:
    groups_in = payload.get("groups") or []
    options = payload.get("options") or {}
    auto_navigate = options.get("auto_navigate", True)

    project_id = payload.get("project")
    var_files = _load_var_files(project_id, [n for g in groups_in for c in g.get("cases") or []
                                             for n in [_case_login(c), *(c.get("vars") or [])] if n])
    if var_files is None:
        return {"error": "有測案選了變數檔，但沒有開啟專案，讀不到變數檔"}

    d = get_driver()
    app_id = payload.get("app_id") or PLATFORM.app_id(d)
    groups = [{
        "map_name": g.get("map_name") or "（未命名地圖）",
        "setup_name": (g.get("setup_case") or {}).get("name"),
        "cases": [{"name": c.get("name") or c.get("case", {}).get("test_name") or "（未命名測案）",
                   "test_name": c.get("case", {}).get("test_name"), "login": _case_login(c),
                   "vars": c.get("vars") or [], "status": "pending",
                   "navigation": [], "precheck": None, "results": [],
                   "total": len(c.get("case", {}).get("steps", [])), "duration": None, "reason": None}
                  for c in g.get("cases") or []],
    } for g in groups_in]

    # 整批共用一份 ctx：關彈窗開關整批一樣；變數每份測案開始前依它選的檔重新設定（_apply_case_vars）
    ctx = _new_ctx(dismiss_dialogs=bool(options.get("dismiss_dialogs", False)))
    ctx.progress = {"running": True, "groups": groups, "current": None, "app_id": app_id}
    _stop_requested.clear()
    _register_run("batch", ctx)
    _test_running.set()
    # 整批共用：換組不會登出，同一個登入檔不用重登；values 是目前登入那個檔的值
    login = {"name": None, "values": {}, "files": var_files}
    print(f"[批次執行] 開始：{len(groups)} 組地圖、{sum(len(g['cases']) for g in groups)} 份測案，App：{app_id}")
    try:
        with PLATFORM.page_source_context(d):
            for gi, (g_in, g_out) in enumerate(zip(groups_in, groups)):
                _run_batch_group(ctx, d, gi, g_in, g_out, app_id, auto_navigate,
                                 options.get("allow_restart", True), login)
    finally:
        _test_running.clear()
        ctx.update_progress(running=False, current=None)
    return {"groups": groups, "stopped": ctx.stopped(), "app_id": app_id}


def _case_login(c: dict) -> str | None:
    return c.get("login") or c.get("account")  # account 是舊版批次設定的欄位名稱


def _load_var_files(project_id: str | None, names: list[str]) -> dict | None:
    """讀批次用到的變數檔 → {檔名: {KEY: 值} | None}，讀不到的是 None（那份測案 SKIP，不中斷整批）。
    有用到變數檔卻沒開專案回傳 None"""
    names = list(dict.fromkeys(names))
    if not names:
        return {}
    if not project_id:
        return None
    files = {}
    for name in names:
        try:
            files[name] = WORKSPACE.read_vars(project_id, [name])[name]
        except WorkspaceError as e:
            print(f"[批次執行] 變數檔「{name}」讀不到：{e}")
            files[name] = None
    return files


def _apply_case_vars(ctx, login: dict, var_names: list[str], extra_login: tuple | None = None) -> None:
    """依優先順序合併：登入檔 → vars（後面的蓋前面的）；extra_login 是正要切換的 (名稱, 值)"""
    login_name, login_values = extra_login or (login["name"], login["values"])
    merged = dict(login_values)
    for name in var_names:
        merged.update(login["files"][name])
    ctx.set_vars(merged, ([login_name] if login_name else []) + var_names)


def _switch_login(ctx, navigator: Navigator, name: str, var_names: list[str], has_setup: bool,
                  login: dict, log: list) -> str | None:
    """重開 App、用這個登入檔跑登入測案；成功回傳 None，失敗回傳要顯示的原因"""
    if not has_setup:
        return f"這組沒有指定登入測案，沒辦法用「{name}」登入"
    log.append(f"切換登入：{login['name'] or '目前狀態不明'} → {name}")
    values = login["files"][name]
    login.update(name=None, values={})  # 登入成功前都算不知道目前是誰
    _apply_case_vars(ctx, login, var_names, (name, values))
    if not navigator.restart_app(log):
        return f"用「{name}」登入失敗"
    login.update(name=name, values=values)
    return None


def _run_batch_group(ctx, d, gi: int, g_in: dict, g_out: dict, app_id, auto_navigate: bool, allow_restart: bool,
                     login: dict):
    index = MapIndex(g_in.get("map") or {})
    screens_by_name = index.screens_for_execution()
    setup = g_in.get("setup_case")
    setup_map = g_in.get("setup_map")
    setup_screens = MapIndex(setup_map).screens_for_execution() if setup_map else screens_by_name

    def run_setup():
        steps = (setup.get("case") or {}).get("steps", [])
        results = []
        _run_steps(ctx, d, steps, setup_screens, results)
        failed = [r for r in results if r["outcome"].startswith("FAIL")]
        summary = f"{len(results) - len(failed)}/{len(results)} 步通過"
        if failed:
            summary += f"，第 {failed[0]['index']} 步失敗：{failed[0]['outcome']}"
        return not failed, summary

    navigator = Navigator(ctx, PLATFORM, d, app_id, run_setup=run_setup if setup else None,
                          # 登入頁在另一張地圖時，導航用的這張地圖認不出登入頁，不做「停在登入頁先登入」
                          setup_start=_first_screen((setup or {}).get("case", {}).get("steps", [])) if setup and not setup_map else None,
                          allow_restart=allow_restart)

    for ci, (c_in, c_out) in enumerate(zip(g_in.get("cases") or [], g_out["cases"])):
        steps = (c_in.get("case") or {}).get("steps", [])
        if ctx.stopped():
            _locked_update(ctx, c_out, status="skip", reason="使用者已停止")
            continue
        ctx.update_progress(current={"group": gi, "case": ci})
        _locked_update(ctx, c_out, status="running")
        started = time.time()
        print(f"[批次執行] 第 {gi + 1} 組第 {ci + 1} 份：{c_out['name']}")

        start_screen = _first_screen(steps)
        try:
            dismissed = try_dismiss_dialogs(ctx, PLATFORM, d)  # 同 run_test_script：開始前先關掉擋在前面的彈窗
            if dismissed:
                c_out["navigation"].append(f"畫面上有彈窗，先關掉（按了「{'」「'.join(dismissed)}」）")
            login_name, var_names = _case_login(c_in), c_in.get("vars") or []
            unreadable = [n for n in [login_name, *var_names] if n and login["files"].get(n) is None]
            if unreadable:
                _locked_update(ctx, c_out, status="skip",
                               reason=f"變數檔讀不到（可能已改名或刪除）：{'、'.join(unreadable)}",
                               duration=round(time.time() - started, 1))
                continue
            if login_name and login_name != login["name"]:
                failed = _switch_login(ctx, navigator, login_name, var_names, bool(setup), login, c_out["navigation"])
                if failed:
                    _locked_update(ctx, c_out, status="skip", reason=failed, duration=round(time.time() - started, 1))
                    continue
            _apply_case_vars(ctx, login, var_names)
            if auto_navigate and start_screen:
                if not navigator.navigate_to(index, start_screen, c_out["navigation"]):
                    _locked_update(ctx, c_out, status="skip", reason=f"沒辦法回到起始畫面「{start_screen}」",
                                   duration=round(time.time() - started, 1))
                    continue
            else:
                _locked_update(ctx, c_out, precheck=_precheck_screen(ctx, d, steps, screens_by_name))
                if c_out["precheck"]["status"] == "fail":
                    _locked_update(ctx, c_out, status="skip", reason="第0步畫面檢查沒通過",
                                   duration=round(time.time() - started, 1))
                    continue
            _run_steps(ctx, d, steps, screens_by_name, c_out["results"])
        except StopRequested:
            _locked_update(ctx, c_out, reason="使用者已停止")
        except Exception as e:
            # 導航途中 Appium 出錯之類的：只影響這一份，記下原因接著跑下一份
            _locked_update(ctx, c_out, reason=f"執行時發生錯誤：{type(e).__name__}：{e}")
        crashed = (c_out["reason"] or "").startswith("執行時發生錯誤")
        _locked_update(ctx, c_out, status="fail" if crashed else _case_status(c_out["results"], ctx.stopped()),
                       duration=round(time.time() - started, 1))


def stop_test_script() -> dict:
    if not _test_running.is_set():
        return {"stopped": False, "message": "目前沒有執行中的測試"}
    _stop_requested.set()
    return {"stopped": True, "message": "已送出停止，正在捲動找元件的話會立刻中斷，其他動作做完這一步就停"}


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
    project_id = payload.get("project")
    out_dir = WORKSPACE.project_dir(project_id) if project_id else None
    if out_dir is not None:
        project = WORKSPACE.get_project(project_id)
        if project["platform"] != PLATFORM.name:
            return {"error": f"專案「{project_id}」是 {project['platform']}，但伺服器目前接的是 {PLATFORM.name}"}
    ctx = _new_ctx()
    screen_data = scan_screen(ctx, PLATFORM, get_driver(), screen_name,
                              describe=bool(payload.get("describe", False)),
                              scroll=bool(payload.get("scroll", True)),
                              wait_stable=True, out_dir=out_dir)
    return {"screen_data": screen_data, "usage": ctx.usage.as_dict()}


# ---------------------------------------------------------------------------
# /describe_screen：幫地圖裡已經掃描過的畫面補 LLM 用途描述（掃描時沒勾、或爬蟲
# 爬出來的都沒有）。不碰手機：用地圖存的元件資料＋存檔的截圖，跟掃描時用的是同一組
# describe_* 函式；只回傳 {unique_id: 描述}，元件 id、自訂 XPath 都不動。
# 已知限制：掃描時有「捲動收集」的話，捲到下面才抓到的元件也只能對照最頂部那張截圖。
# ---------------------------------------------------------------------------

def describe_saved_screen(payload: dict) -> dict:
    screen_name = payload.get("screen_name") or ""
    elements = payload.get("elements") or []
    if not elements:
        return {"descriptions": {}}
    platform = get_platform(payload["platform"]) if payload.get("platform") in PLATFORMS else PLATFORM

    screenshot_png, available = b"", False
    rel = payload.get("screenshot")
    if rel:
        base = (WORKSPACE.project_dir(payload["project"]) if payload.get("project") else Path(".")).resolve()
        path = (base / rel).resolve()
        if path.is_relative_to(base) and path.is_file():
            screenshot_png = path.read_bytes()
            available = not payload.get("screenshot_blocked")
    print(f"[補描述] {screen_name}：{len(elements)} 個元件，{'有' if available else '沒有'}截圖")

    native = [e for e in elements if e.get("id_source") == "native" and e.get("native_id")]
    generated = [e for e in elements if e not in native]
    ctx = RunContext()  # 不碰手機，只用來累計這次補描述的 LLM 用量
    native_desc = describe_native_elements(ctx, platform, screen_name, [
        {"native_id": e["native_id"], "class": e.get("class"), "label": e.get("label"), "value": e.get("value")}
        for e in native])
    temp_ids = [f"e{i}" for i in range(len(generated))]
    generated_desc = describe_generated_elements(ctx, platform, screen_name, screenshot_png, available, [
        {"class": e.get("class"), "label": e.get("label"), "bounds": e.get("bounds"),
         "position_bucket": (e.get("fingerprint") or {}).get("position_bucket"),
         "nearby_text": (e.get("fingerprint") or {}).get("nearby_text")}
        for e in generated], temp_ids)

    descriptions = {}
    for e in native:
        d = native_desc.get(f"{e.get('class')}::{e['native_id']}")
        if d:
            descriptions[e["unique_id"]] = d
    for tid, e in zip(temp_ids, generated):
        if generated_desc.get(tid):
            descriptions[e["unique_id"]] = generated_desc[tid]
    print(f"[補描述] {screen_name} 的 LLM 用量：{ctx.usage.summary()}")
    return {"descriptions": descriptions, "usage": ctx.usage.as_dict()}


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _request_allowed(self) -> bool:
        """
        只接受來自這支伺服器自己網頁的請求。按鈕會「真的」操作手機、花 LLM 額度，
        光綁 127.0.0.1 不夠：使用者瀏覽器開著的任何網站都能對 127.0.0.1 發請求
        （text/plain 這類不需要預檢的 POST 照樣會送到、照樣被執行，只是看不到回應），
        或用 DNS rebinding 把自己的網域指到 127.0.0.1。所以：
          - Host 必須是本機位址：擋 DNS rebinding（那種請求的 Host 是對方的網域）
          - 有 Origin 的話必須是本機網頁：擋其他網站的跨站請求
        curl、Python 腳本這類沒有 Origin 的請求照常放行。
        """
        allowed = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        if self.headers.get("Host") not in allowed:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in allowed}

    def _serve_file(self, path: str) -> bool:
        """網頁本身＋截圖這類資料夾裡的檔案；其他路徑一律 404，不會讀到資料夾外面。"""
        parts = unquote(path).lstrip("/").split("/")
        if path in WEB_PAGES:
            file = WEB_DIR / WEB_PAGES[path]
        elif len(parts) == 1 and Path(parts[0]).suffix in WEB_ASSET_SUFFIXES and not parts[0].startswith("."):
            file = WEB_DIR / parts[0]
        elif len(parts) >= 4 and parts[0] == "projects" and parts[2] in PROJECT_STATIC_DIRS:
            try:
                root = (WORKSPACE.project_dir(parts[1]) / parts[2]).resolve()
            except WorkspaceError:
                return False
            file = root.joinpath(*parts[3:]).resolve()
            if not file.is_relative_to(root):
                return False
        else:
            rel = unquote(path).lstrip("/")
            top = rel.split("/", 1)[0]
            if top not in DATA_DIRS:
                return False
            root = Path(top).resolve()
            file = Path(rel).resolve()
            if not file.is_relative_to(root):
                return False
        if not file.is_file():
            return False
        body = file.read_bytes()
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 網頁改完重新整理就要看到新版；截圖檔名會變（或帶 ?v=），不用特別處理
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_GET(self):
        if not self._request_allowed():
            self._send_json(403, {"error": f"只接受從 http://127.0.0.1:{PORT}/ 開啟的網頁"})
            return
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            self._handle_api("GET")
            return
        if path == "/ping":
            self._send_json(200, {"status": "ok", "platform": PLATFORM.name})
            return
        if path == "/test_progress":
            self._send_json(200, _progress_snapshot("test"))
            return
        if path == "/batch_progress":
            self._send_json(200, json.loads(json.dumps(_progress_snapshot("batch"), ensure_ascii=False, default=str)))
            return
        if path == "/llm_scenarios_prompt":
            self._send_json(200, {"template": SCENARIO_PROMPT_TEMPLATE, "marker": SCENARIO_PROMPT_MARKER})
            return
        if self._serve_file(path):
            return
        self._send_json(404, {"error": "not found"})

    KNOWN_PATHS = (
        "/tap", "/assert_visible", "/inspect", "/test_xpath", "/agent_xpath",
        "/llm_chat", "/run_test_script", "/stop_test_script", "/scan_screen", "/llm_scenarios",
        "/run_batch", "/check_screen", "/describe_screen",
    )
    # 純 LLM 的代理不操作裝置，不需要Appium session存活，讓使用者就算手機
    # 還沒接好也能先設計測試案例
    PATHS_NOT_REQUIRING_DRIVER = ("/llm_chat", "/llm_scenarios", "/stop_test_script", "/describe_screen")

    def do_POST(self):
        if not self._request_allowed():
            self._send_json(403, {"error": f"只接受從 http://127.0.0.1:{PORT}/ 開啟的網頁"})
            return
        if self.path.startswith("/api/"):
            self._handle_api("POST")
            return
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
            if self.path in self.PATHS_NOT_REQUIRING_DRIVER:
                result = self._dispatch(self.path, payload)
            elif _test_running.is_set():
                self._send_json(409, {"error": "測試執行中，請等測試結束或先按停止"})
                return
            else:
                with _device_lock:
                    ensure_session_alive()
                    result = self._dispatch(self.path, payload)
        except Exception as e:
            self._send_json(500, {"error": f"{type(e).__name__}: {e}"})
            return

        self._send_json(200, result)

    def do_PUT(self):
        if not self._request_allowed():
            self._send_json(403, {"error": f"只接受從 http://127.0.0.1:{PORT}/ 開啟的網頁"})
            return
        self._handle_api("PUT")

    def do_DELETE(self):
        if not self._request_allowed():
            self._send_json(403, {"error": f"只接受從 http://127.0.0.1:{PORT}/ 開啟的網頁"})
            return
        self._handle_api("DELETE")

    def _read_body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def _read_json_body(self):
        try:
            return json.loads(self._read_body() or b"null")
        except json.JSONDecodeError as e:
            raise WorkspaceError("請求內容不是合法JSON") from e

    def _handle_api(self, method: str):
        """
        workspace 專案的讀寫（建立專案、改專案設定用 workspace.py 的 create／update），
        路徑對照（<kind> 是 cases、batches 或 vars）：
          GET    /api/projects                          專案清單
          POST   /api/projects/import[?id=新代號]        匯入 zip（body 是 zip 檔本身）
          POST   /api/projects/merge[?preview=1]        zip 併進本機同代號的專案；body 是 zip，
                                                        要寫入時同名的處理方式放 query resolutions=<JSON>
          GET    /api/projects/<id>                     專案資訊＋地圖／測案／批次清單
          GET    /api/projects/<id>/export              下載 zip（不含 sensitive 的變數檔）
          GET    /api/projects/<id>/maps/<name>         讀地圖 → {map, map_version}
          POST   /api/projects/<id>/maps/<name>         新增地圖 {map?}（沒給就是空白），同名已存在回 409
          PUT    /api/projects/<id>/maps/<name>         存地圖 {map, base_version}，版本不符回 409
          POST   /api/projects/<id>/maps/<name>/rename  改名 {new_name}
          GET    /api/projects/<id>/maps/<name>/export  下載這張地圖＋用到的截圖（zip）
          POST   /api/projects/<id>/maps/<name>/import[?overwrite=1]  匯入地圖 zip（body 是 zip），同名已存在又沒 overwrite 回 409
          DELETE /api/projects/<id>/maps/<name>         刪地圖（最後一張不能刪）
          GET    /api/projects/<id>/<kind>/<name>       讀一份
          PUT    /api/projects/<id>/<kind>/<name>[?overwrite=1]  存一份（已存在又沒 overwrite 回 409）
          DELETE /api/projects/<id>/<kind>/<name>       刪一份
          POST   /api/projects/<id>/vars/<name>/rename  變數檔改名 {new_name}（批次裡的引用一起改）
        不碰手機，所以不用排 _device_lock；檔案寫入是先寫暫存檔再改名，不會寫出半份。
        """
        url = urlsplit(self.path)
        parts = [unquote(p) for p in url.path.split("/")[3:] if p]  # 去掉開頭的 /api/projects
        query = parse_qs(url.query)
        if url.path.split("/")[2:3] != ["projects"]:
            self._send_json(404, {"error": "not found"})
            return
        try:
            if method == "GET" and not parts:
                self._send_json(200, {"projects": WORKSPACE.list_projects()})
            elif method == "POST" and parts == ["import"]:
                new_id = (query.get("id") or [None])[0]
                self._send_json(200, {"project": WORKSPACE.import_zip(self._read_body(), new_id)})
            elif method == "POST" and parts == ["merge"]:
                resolutions = None
                if (query.get("preview") or ["0"])[0] != "1":
                    try:
                        resolutions = json.loads((query.get("resolutions") or ["{}"])[0])
                    except json.JSONDecodeError as e:
                        raise WorkspaceError("resolutions 不是合法JSON") from e
                self._send_json(200, WORKSPACE.merge_zip(self._read_body(), resolutions))
            elif method == "GET" and len(parts) == 1:
                self._send_json(200, WORKSPACE.summary(parts[0]))
            elif method == "GET" and len(parts) == 2 and parts[1] == "export":
                body = WORKSPACE.export_zip(parts[0])
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{parts[0]}_{time.strftime("%Y%m%d")}.zip"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif len(parts) == 4 and parts[3] == "rename" and method == "POST":
                body = self._read_json_body() or {}
                WORKSPACE.rename_item(parts[0], parts[1], parts[2], str(body.get("new_name", "")).strip())
                self._send_json(200, {"items": WORKSPACE.list_items(parts[0], parts[1])})
            elif len(parts) >= 3 and parts[1] == "maps":
                self._handle_map_api(method, parts[0], parts[2], parts[3:], query)
            elif len(parts) == 3 and method == "GET":
                self._send_json(200, {"data": WORKSPACE.read_item(*parts)})
            elif len(parts) == 3 and method == "PUT":
                overwrite = (query.get("overwrite") or ["0"])[0] == "1"
                WORKSPACE.write_item(*parts, self._read_json_body(), overwrite)
                self._send_json(200, {"items": WORKSPACE.list_items(parts[0], parts[1])})
            elif len(parts) == 3 and method == "DELETE":
                WORKSPACE.delete_item(*parts)
                self._send_json(200, {"items": WORKSPACE.list_items(parts[0], parts[1])})
            else:
                self._send_json(404, {"error": "not found"})
        except WorkspaceError as e:
            self._send_json(e.status, {"error": str(e)})
        except Exception as e:
            self._send_json(500, {"error": f"{type(e).__name__}: {e}"})

    def _handle_map_api(self, method: str, project_id: str, name: str, rest: list, query: dict):
        if method == "GET" and not rest:
            map_data, version = WORKSPACE.read_map(project_id, name)
            self._send_json(200, {"map": map_data, "map_version": version})
        elif method == "POST" and not rest:
            body = self._read_json_body() or {}
            version = WORKSPACE.create_map(project_id, name, body.get("map"))
            self._send_json(200, {"map_version": version, "maps": WORKSPACE.list_maps(project_id)})
        elif method == "PUT" and not rest:
            body = self._read_json_body() or {}
            version = WORKSPACE.write_map(project_id, name, body.get("map"), body.get("base_version"))
            self._send_json(200, {"map_version": version})
        elif method == "POST" and rest == ["rename"]:
            body = self._read_json_body() or {}
            WORKSPACE.rename_map(project_id, name, str(body.get("new_name", "")).strip())
            self._send_json(200, {"maps": WORKSPACE.list_maps(project_id)})
        elif method == "GET" and rest == ["export"]:
            body = WORKSPACE.export_map_zip(project_id, name)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}.zip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif method == "POST" and rest == ["import"]:
            overwrite = (query.get("overwrite") or ["0"])[0] == "1"
            self._send_json(200, WORKSPACE.import_map_zip(project_id, name, self._read_body(), overwrite))
        elif method == "DELETE" and not rest:
            WORKSPACE.delete_map(project_id, name)
            self._send_json(200, {"maps": WORKSPACE.list_maps(project_id)})
        else:
            self._send_json(404, {"error": "not found"})

    def _dispatch(self, path: str, payload: dict) -> dict:
        if path == "/tap":
            return {"result": run_tap(_new_ctx(), PLATFORM, get_driver(), payload)}
        if path == "/assert_visible":
            return {"result": run_assert_visible(_new_ctx(), PLATFORM, get_driver(), payload)}
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
        if path == "/run_batch":
            return run_batch(payload)
        if path == "/stop_test_script":
            return stop_test_script()
        if path == "/scan_screen":
            return do_scan_screen(payload)
        if path == "/describe_screen":
            return describe_saved_screen(payload)
        if path == "/check_screen":
            # 手機目前是不是停在某個畫面（同第0步檢查），給地圖「在上面加一層」確認點對入口用
            status, message = check_current_screen(PLATFORM, get_driver(), payload.get("screen_name") or "",
                                                   payload.get("elements") or [])
            return {"status": status, "message": message}
        if path == "/llm_scenarios":
            return llm_generate_scenarios(payload)

    def log_message(self, format, *args):
        print("[viewer_server]", format % args)


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def main():
    global PLATFORM, PORT, WORKSPACE
    parser = argparse.ArgumentParser(description="App 元件地圖／測試工具的本機橋接伺服器")
    parser.add_argument("--platform", choices=sorted(PLATFORMS), default="ios",
                        help="這次要接的裝置平台（預設 ios）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"監聽埠（預設 {DEFAULT_PORT}）")
    parser.add_argument("--workspace", default=str(DEFAULT_ROOT), help="專案資料夾（預設 ./workspace）")
    args = parser.parse_args()
    PLATFORM = get_platform(args.platform)
    PORT = args.port
    WORKSPACE = Workspace(args.workspace)

    # 被 aitest.py 或 kill 結束（SIGTERM）時跟 Ctrl+C 一樣收尾：關掉 Appium session，手機上不會留著 WDA 連線
    signal.signal(signal.SIGTERM, _raise_interrupt)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[viewer_server] 已啟動（平台：{PLATFORM.name}），只接受本機連線")
    print(f"[viewer_server] 用瀏覽器打開 http://{HOST}:{PORT}/ ，Ctrl+C 結束")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    main()
