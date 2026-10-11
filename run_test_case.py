"""
POC: App 測試案例執行器（第三步），iOS / Android 共用

功能：
  讀取 test_cases/{screen_name}.json 的測試步驟清單，實際連線手機、透過 Appium 執行，
  並印出每個步驟的執行結果。這一步完全不用 LLM，純粹是把結構化的步驟資料
  轉換成 Appium 操作指令（機械式工作，交給固定的程式邏輯處理）。

  執行流程（讀檔、逐步執行、timeout 重試、停止、畫面檢查、等畫面穩定）兩個平台
  共用這一份；定位元件、點擊的平台差異在 platforms/ios.py、platforms/android.py，
  這裡的函式都帶一個 platform 參數取用。

元件定位策略：
  0. 不管 id_source 是什麼，先檢查 el["custom_xpath"]——這是使用者在
     viewer「重新定義 XPath」/「AI定位」功能裡人工測試、或AI生成後
     實測驗證過，確認查到唯一結果才存下來的查詢，優先於所有自動推導的
     定位方式使用
  1. native 元件：iOS 用 accessibility id，Android 用 resource-id，穩定可靠
  2. generated 元件（沒有 native id）：class + 文字查詢、捲動重查、鄰近文字
     錨點消歧義（見 TROUBLESHOOTING #36），各平台的細節見
     platforms/*.py 的 locate_generated_element()
  3. 都找不到、或 label 本身是空的，才退回用 bounds 座標點擊
  4. input_text 定位跟 tap 一樣走 0~2，但不退回座標（打字打錯地方比點錯更難收拾）。
     定位到的元件不是輸入框時（例如顯示數量的 TextView，點了才從底部跳出輸入列），
     先點它、再對取得焦點的輸入框打字，打完送出（見 platforms/*.py 的 focus_input）

使用方式：
  python run_test_case.py --screen_name 我的頁面               （預設 iOS）
  python run_test_case.py --platform android --screen_name my_screen

安全提醒：
  這一步會「真的」操作手機、點擊真實 App 畫面。執行前務必先打開
  test_cases/{screen_name}.json 人工檢查步驟內容是否合理、有沒有機會
  觸發不可逆的真實後果（例如登入、送出表單、刪除資料等）。

  執行前會先做「畫面校驗」：用該畫面已知的 native id 元件檢查目前手機
  畫面吻不吻合，過半數對不上就會中止執行——避免手機沒有導航到正確畫面時，
  tap 的座標備援機制盲目點擊，誤觸其他畫面上的真實元件卻回報成功。
"""

import argparse
import json
import os
import re
from contextlib import contextmanager
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from appium import webdriver
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import InvalidElementStateException, InvalidSessionIdException, NoSuchElementException

# set_stop_checker、xpath_literal 這裡沒用到，留著是因為 viewer_server、crawl_app_tree 從這支 import
from common import StopRequested, raise_if_stopped
from common import set_stop_checker as set_stop_checker
from common import xpath_literal as xpath_literal
from config import APPIUM_SERVER_URL
from platforms import PLATFORMS, get_platform

ELEMENT_MAP_PATH = Path("element_map.json")
TEST_CASES_DIR = Path("test_cases")
TEST_RESULTS_DIR = Path("test_results")


def load_test_case(screen_name: str) -> dict:
    path = TEST_CASES_DIR / f"{screen_name}.json"
    if not path.exists():
        raise SystemExit(f"找不到 {path}，請先在網頁的「測案編輯」分頁產生測案，或手動建立這個檔案")
    return json.loads(path.read_text(encoding="utf-8"))


def load_element_lookup(screen_name: str) -> dict:
    if not ELEMENT_MAP_PATH.exists():
        raise SystemExit(f"找不到 {ELEMENT_MAP_PATH}")
    data = json.loads(ELEMENT_MAP_PATH.read_text(encoding="utf-8"))
    for screen in data["screens"]:
        if screen["screen_name"] == screen_name:
            return {el["unique_id"]: el for el in screen["elements"]}
    raise SystemExit(f"element_map.json 裡找不到畫面「{screen_name}」")


# 執行測案時「步驟失敗先關掉非預期彈窗再重試」的開關（viewer 的勾選框／命令列 --dismiss_dialogs），
# 預設關閉：測案本來就要操作的對話框（例如「確定買進」確認框）不該被自動關掉
_dismiss_dialogs_enabled = False


def set_dismiss_dialogs(enabled: bool) -> None:
    global _dismiss_dialogs_enabled
    _dismiss_dialogs_enabled = bool(enabled)


def try_dismiss_dialogs(platform, driver) -> list[str]:
    """開關有開才去找彈窗，回傳按過的按鈕文字；沒開或沒找到回傳空清單"""
    if not _dismiss_dialogs_enabled:
        return []
    try:
        dismissed = platform.dismiss_dialogs(driver)
    except Exception as e:
        print(f"    [關閉非預期彈窗] 檢查失敗，照原本結果繼續: {e}")
        return []
    if dismissed:
        print(f"    [關閉非預期彈窗] 按了：{'、'.join(dismissed)}")
    return dismissed


def with_note(outcome: str, note: str) -> str:
    """在結果文字最後的括號裡補一段說明"""
    if outcome.endswith("）"):
        return outcome[:-1] + f"；{note}）"
    return f"{outcome}（{note}）"


def with_dismiss_note(outcome: str, dismissed: list[str]) -> str:
    """在結果文字最後補上「先關掉了哪些彈窗」，網頁上看得到這一步是重試後才通過的"""
    return with_note(outcome, f"已先關掉非預期彈窗（按了「{'」「'.join(dismissed)}」）")


@contextmanager
def idle_wait_disabled(driver):
    """暫時把 waitForIdleTimeout 設 0（UiAutomator2、XCUITest 都有這個設定），結束後還原"""
    try:
        original = driver.get_settings().get("waitForIdleTimeout")
        driver.update_settings({"waitForIdleTimeout": 0})
    except Exception:
        original = None
    try:
        yield
    finally:
        if original is not None:
            try:
                driver.update_settings({"waitForIdleTimeout": original})
            except Exception:
                pass


def retry_assert(check, timeout=None, poll_interval: float = 0.5,
                 on_fail=None, on_fail_interval: float = 3.0) -> str:
    """
    assert_visible / assert_text_contains 的步驟有寫 timeout（秒）時，在期限內反覆
    執行 check()，一出現 PASS 就回報，時間到才回報最後一次的 FAIL。用在「操作後
    畫面要隔幾秒才自己刷新」的情境，例如取消委託後 3~5 秒委託狀態才變成「委託取消」
    ——wait_after_action() 只等畫面「不再變動」，刷新前畫面早就穩定了，擋不住這種延遲。

    沒寫 timeout 就只檢查一次，跟原本行為完全一樣。SKIP 代表這個元件本來就驗證
    不了，重試也沒用，直接回傳。

    on_fail：等待期間沒通過時呼叫（第一次沒通過馬上呼叫，之後每 on_fail_interval 秒一次），
    回傳這次處理掉的東西（清單）。給「自動關閉非預期彈窗」用：被彈窗擋住時不用等到
    timeout 用完才處理，關掉後繼續在剩下的時間內檢查。處理過的東西會註記在結果裡。
    """
    if not timeout:
        return check()

    deadline = time.time() + float(timeout)
    attempts = 0
    handled = []
    next_on_fail_at = 0.0

    def finish(outcome):
        return with_dismiss_note(outcome, handled) if handled else outcome

    while True:
        raise_if_stopped()
        attempts += 1
        try:
            outcome = check()
        except StopRequested:
            raise
        except Exception as e:
            # 畫面刷新中元件剛好被換掉（stale）之類的，下一輪再查
            outcome = f"FAIL（檢查時發生錯誤：{type(e).__name__}）"
        if not outcome.startswith("FAIL"):
            return finish(outcome if attempts == 1 else f"{outcome}（重試第 {attempts} 次才通過）")
        if time.time() >= deadline:
            return finish(f"{outcome}（已等待 {float(timeout):g} 秒）")
        if on_fail is not None and time.time() >= next_on_fail_at:
            handled.extend(on_fail() or [])
            next_on_fail_at = time.time() + on_fail_interval
            continue  # 處理完馬上再檢查一次，不用多睡一輪
        time.sleep(poll_interval)


def _locate_by_custom_xpath(driver, el: dict):
    """
    人工在 viewer 上用「重新定義XPath」/「AI定位」存下來的自訂查詢
    （el["custom_xpath"]），優先於所有自動推導的定位方式使用——這是人工
    測試過、或AI生成後經過實測驗證才存的，比自動邏輯更值得信任。查不到
    唯一結果時（例如畫面已經改版）不強行使用，回傳 None 讓呼叫端繼續走
    原本的自動定位鏈。
    """
    custom_xpath = el.get("custom_xpath")
    if not custom_xpath:
        return None
    try:
        matches = driver.find_elements(AppiumBy.XPATH, custom_xpath)
    except Exception as e:
        print(f"    [自訂XPath] 查詢失敗: {custom_xpath}  ({e})")
        return None
    print(f"    [自訂XPath] {custom_xpath} -> 查到 {len(matches)} 個")
    if len(matches) != 1:
        return None
    return matches[0]


_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# 這次執行選的變數檔（專案 vars/ 裡的，已經依優先順序合併好），優先於環境變數；
# sources 是選了哪些檔，找不到變數時寫進錯誤訊息
_run_vars: dict = {}
_run_vars_sources: list = []


def set_run_vars(values: dict | None, sources: list | None = None) -> None:
    global _run_vars, _run_vars_sources
    _run_vars = dict(values or {})
    _run_vars_sources = list(sources or [])


def resolve_env_placeholders(value: str) -> tuple[str, list[str]]:
    """把 "${LOGIN_PASSWORD}" 這類代號換成實際的值，回傳 (換好的值, 沒設定的變數名稱)。
    帳號密碼、股票代號這類值不寫死在測案 JSON（測案會匯出、分享、commit），執行時才讀進來：
    先找這次選的變數檔（set_run_vars），沒有再找啟動 viewer_server 的那個 shell 的環境變數"""
    missing = []

    def replace(match):
        name = match.group(1)
        if name in _run_vars:
            return _run_vars[name]
        if name not in os.environ:
            missing.append(name)
            return match.group(0)
        return os.environ[name]

    return _ENV_PLACEHOLDER.sub(replace, value or ""), missing


def missing_vars_outcome(missing: list[str]) -> str:
    chosen = "、".join(_run_vars_sources) if _run_vars_sources else "沒有選"
    return (f"FAIL（找不到變數：{'、'.join(missing)}；這次選的變數檔：{chosen}，也沒有同名的環境變數。"
            f"請在變數檔補上，或執行時多選一個有這個變數的檔）")


def _locate_input_element(platform, driver, el: dict):
    """input_text 用的定位：自訂XPath → native id → 自動定位，回傳 (元件, 定位方式)，找不到元件是 None"""
    custom = _locate_by_custom_xpath(driver, el)
    if custom is not None:
        return custom, "人工自訂XPath定位"
    if el["id_source"] == "native":
        try:
            return driver.find_element(platform.native_by, el["native_id"]), f"{platform.native_id_name} 定位"
        except NoSuchElementException:
            return None, f"找不到元件: {el['native_id']}"
    matched = platform.locate_generated_element(driver, el)
    if matched is not None:
        return matched, platform.generated_locate_label
    return None, "無 native id 且自動定位查無唯一結果，不用座標盲點輸入"


def run_input_text(platform, driver, el: dict, value: str) -> str:
    resolved, missing = resolve_env_placeholders(value)
    if missing:
        return missing_vars_outcome(missing)

    element, how = _locate_input_element(platform, driver, el)
    dismissed = []
    if element is None:
        raise_if_stopped()
        dismissed = try_dismiss_dialogs(platform, driver)
        if dismissed:
            element, how = _locate_input_element(platform, driver, el)
    if element is None:
        return with_dismiss_note(f"FAIL（{how}）", dismissed) if dismissed else f"FAIL（{how}）"

    platform.ensure_visible(driver, element)
    target, opened = platform.focus_input(driver, element)
    if target is None:
        return f"FAIL（{how}；元件不是輸入框，點了之後也沒有出現取得焦點的輸入框）"
    try:
        # 結果文字不帶輸入的值：可能是密碼
        outcome = platform.input_text(driver, target, resolved, submit=opened)
    except InvalidElementStateException as e:
        return f"FAIL（輸入框不接受輸入：{e.msg.splitlines()[0] if e.msg else type(e).__name__}）"
    outcome = with_note(outcome, how)
    if opened:
        outcome = with_note(outcome, "先點開元件、輸入到跳出來的輸入框後送出")
    return with_dismiss_note(outcome, dismissed) if dismissed else outcome


def run_tap(platform, driver, el: dict) -> str:
    custom = _locate_by_custom_xpath(driver, el)
    if custom is not None:
        platform.ensure_visible(driver, custom)
        custom.click()
        return "PASS（人工自訂XPath定位）"

    if el["id_source"] == "native":
        try:
            element = driver.find_element(platform.native_by, el["native_id"])
            platform.ensure_visible(driver, element)
            element.click()
            return f"PASS（{platform.native_id_name} 定位）"
        except NoSuchElementException:
            return f"FAIL（找不到元件: {el['native_id']}）"

    matched = platform.locate_generated_element(driver, el)
    if matched is not None:
        platform.ensure_visible(driver, matched)
        matched.click()
        return f"PASS（{platform.generated_locate_label}）"

    raise_if_stopped()  # 找元件途中被停止的話，不要再用舊座標盲點
    # 找不到元件可能是被彈窗擋住：這時用舊座標盲點會點在彈窗上、還回報 PASS，
    # 所以退回座標點擊前先關彈窗、再找一次
    dismissed = try_dismiss_dialogs(platform, driver)
    if dismissed:
        matched = platform.locate_generated_element(driver, el)
        if matched is not None:
            platform.ensure_visible(driver, matched)
            matched.click()
            return with_dismiss_note(f"PASS（{platform.generated_locate_label}）", dismissed)
        raise_if_stopped()
    platform.tap_by_coordinates(driver, el["bounds"])
    outcome = "PASS（座標點擊，備援方式）"
    return with_dismiss_note(outcome, dismissed) if dismissed else outcome


def run_assert_visible(platform, driver, el: dict) -> str:
    custom = _locate_by_custom_xpath(driver, el)
    if custom is not None:
        platform.ensure_visible(driver, custom)
        return "PASS（人工自訂XPath定位）" if custom.is_displayed() else "FAIL（元件存在但不可見）"

    if el["id_source"] == "native":
        try:
            element = driver.find_element(platform.native_by, el["native_id"])
            platform.ensure_visible(driver, element)
            return "PASS" if element.is_displayed() else "FAIL（元件存在但不可見）"
        except NoSuchElementException:
            return "FAIL（找不到元件）"

    matched = platform.locate_generated_element(driver, el)
    if matched is not None:
        platform.ensure_visible(driver, matched)
        return f"PASS（{platform.generated_locate_label}）" if matched.is_displayed() else "FAIL（元件存在但不可見）"

    return "SKIP（無 native id 且自動定位查無唯一結果，座標無法驗證「是否可見」）"


def _read_text_by_position(platform, driver, el: dict, y_tolerance: float = 10.0):
    """
    用掃描時存的座標，讀「目前畫面同一格」的文字，不管那一格現在顯示什麼字。
    給 assert_text_contains 驗證「會變的文字」用（委託狀態、成交數量、價格），
    例如清單每列都是同一檔股票、沒有唯一文字可當錨點時，「第一列的狀態欄」只能靠位置認。

    條件：同類型元件、垂直中心差在 y_tolerance 以內、水平範圍跟掃描時有重疊
    （文字變長變短時寬度會變，只比中心點會誤判），多個符合時挑中心最近的。
    整份 page_source 只抓一次在電腦端比對，不用逐個元件問手機。
    前提是畫面停在跟掃描時一樣的捲動位置；找不到回傳 None。
    """
    bounds = el.get("bounds") or {}
    if not bounds.get("width") or not bounds.get("height"):
        return None
    try:
        root = ET.fromstring(driver.page_source)
    except Exception:
        return None

    left, right = bounds["x"], bounds["x"] + bounds["width"]
    center_x, center_y = bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2

    best, best_distance = None, None
    for node in root.iter():
        if platform.node_class(node) != el["class"]:
            continue
        b = platform.node_bounds(node)
        if b is None:
            continue
        x, y, w, h = b["x"], b["y"], b["width"], b["height"]
        if abs(y + h / 2 - center_y) > y_tolerance or x > right or x + w < left:
            continue
        distance = abs(x + w / 2 - center_x) + abs(y + h / 2 - center_y)
        if best is None or distance < best_distance:
            best, best_distance = node, distance

    if best is None:
        return None
    text = platform.node_text(best)
    print(f"    [位置定位] 掃描時座標 ({bounds['x']:g}, {bounds['y']:g}) 附近的 {el['class']}，"
          f"目前文字：「{text}」")
    return text


def run_assert_text_contains(platform, driver, el: dict, expected_text: str) -> str:
    """
    驗證元件目前顯示的文字內容有沒有包含指定字串，而不是只驗證存不存在/可不可見
    ——assert_visible 做不到這件事。給 LLM 產生的跨畫面測案用（viewer「LLM
    測案產生」分頁），例如「設定頁存了名稱後，首頁問候語有沒有真的出現這個名稱」
    這種需要檢查實際文字內容的情境。
    """
    custom = _locate_by_custom_xpath(driver, el)
    target_el, label = custom, "人工自訂XPath定位"

    if target_el is None:
        if el["id_source"] == "native":
            try:
                target_el = driver.find_element(platform.native_by, el["native_id"])
                label = f"{platform.native_id_name} 定位"
            except NoSuchElementException:
                return "FAIL（找不到元件）"
        else:
            # 沒有 native id 的元件，自動定位是靠「掃描時的文字」找，但這個 action 要驗證的
            # 正是文字有沒有變——變了就找不到，或找到別列剛好還是舊文字的元件。所以先用
            # 掃描時的座標讀同一格；位置附近沒有同類型元件才退回原本的定位（它找不到時會
            # 捲動清單，一捲動位置就不準了，所以不能放在前面）
            position_text = _read_text_by_position(platform, driver, el)
            if position_text is not None:
                if expected_text in position_text:
                    return f"PASS（位置定位，實際文字：「{position_text}」）"
                return f"FAIL（位置定位，預期文字包含「{expected_text}」，實際文字：「{position_text}」）"
            target_el = platform.locate_generated_element(driver, el)
            label = platform.generated_locate_label

    if target_el is None:
        return "SKIP（無 native id 且自動定位查無唯一結果，無法讀取文字內容）"

    platform.ensure_visible(driver, target_el)
    actual_text = platform.element_text(target_el)
    if expected_text in actual_text:
        return f"PASS（{label}，實際文字：「{actual_text}」）"
    return f"FAIL（{label}，預期文字包含「{expected_text}」，實際文字：「{actual_text}」）"


def match_screen_in_source(page_source: str, elements: list, screen_name: str,
                           id_attrs: tuple, label_attrs: tuple) -> tuple[str, str]:
    """
    比對目前畫面（page_source）是不是 screen_name 這個畫面，回傳 (status, message)，
    status 是 "pass" / "fail" / "unchecked"（沒有東西可以比對，放行但提醒）。
    平台差別只在 id / 文字放在哪些屬性（id_attrs / label_attrs）。

    做法：優先用這個畫面已知的 native id 元件當指紋；畫面完全沒有 native id 時，
    改用元件文字（label）比對，並排除含數字的文字（價格、時間、筆數這類
    每次都會變的內容，拿來比對只會誤判）。過半數找得到，才視為目前在這個畫面上。
    整份 page_source 只抓一次再比對，不是每個元件各查一次，元件多時也不會拖很久。
    """
    try:
        root = ET.fromstring(page_source)
    except ET.ParseError as e:
        return "unchecked", f"畫面結構解析失敗，無法檢查「{screen_name}」（{e}）"

    present_ids, present_labels = set(), set()
    for node in root.iter():
        for attr in id_attrs:
            if node.attrib.get(attr):
                present_ids.add(node.attrib[attr])
        for attr in label_attrs:
            text = (node.attrib.get(attr) or "").strip()
            if text:
                present_labels.add(text)

    native_ids = {el["native_id"] for el in elements if el.get("id_source") == "native" and el.get("native_id")}
    if native_ids:
        expected, present, kind = native_ids, present_ids, " native 元件"
    else:
        labels = {(el.get("label") or "").strip() for el in elements}
        labels = {t for t in labels if t and not any(ch.isdigit() for ch in t)}
        if not labels:
            return "unchecked", f"「{screen_name}」沒有 native id 也沒有固定文字的元件，無法檢查畫面，請自行確認"
        expected, present, kind = labels, present_labels, "固定文字（這個畫面沒有 native id）"

    found = len(expected & present)
    ratio = found / len(expected)
    message = f"「{screen_name}」的 {len(expected)} 個{kind}中，目前畫面找到 {found} 個（{ratio:.0%}）"
    return ("pass" if ratio >= 0.5 else "fail"), message


def check_current_screen(platform, driver, screen_name: str, elements: list) -> tuple[str, str]:
    return match_screen_in_source(driver.page_source, elements, screen_name,
                                  id_attrs=platform.screen_id_attrs, label_attrs=platform.screen_label_attrs)


def verify_current_screen(platform, driver, screen_name: str, element_lookup: dict) -> bool:
    """
    執行測試步驟前的安全檢查：確認手機目前畫面看起來就是測試案例鎖定的畫面，
    避免在錯誤畫面上，tap 的座標備援機制誤觸別的畫面上的真實元件卻回報 PASS。
    比對方式見 match_screen_in_source。
    """
    status, message = check_current_screen(platform, driver, screen_name, list(element_lookup.values()))
    print(f"  [畫面校驗] {message}")
    return status != "fail"


def layout_signature(platform, page_source: str):
    """
    畫面「版面」的指紋，給 wait_after_action 判斷畫面穩定用：只看元件類型跟位置，
    不看文字（每個節點取什麼見 platforms/*.py 的 layout_key）。報價畫面的價格、
    漲跌一直在跳，用整份 page_source 比對永遠不會一致，每次 tap 都會等到上限；
    轉場、載入、彈窗這些才是要等的，它們會改變版面。
    文字延遲更新（例如取消委託後狀態才變）不在這裡等，由 assert 步驟的 timeout 處理。
    """
    try:
        root = ET.fromstring(page_source)
    except ET.ParseError:
        return page_source
    return [platform.layout_key(node) for node in root.iter()]


def wait_after_action(platform, driver, max_wait: float = 3.0, poll_interval: float = 0.4) -> None:
    """
    tap/input_text 之後改成輪詢 page_source 直到版面連續兩次一致或到時間
    上限為止，取代原本寫死的 `time.sleep(1)`——原本固定1秒的等待，遇到
    App本身有較長轉場/非同步載入（例如測試App的轉場畫面刻意設計2秒
    loading）時可能不夠，下一步容易在畫面還沒更新完就執行。時間上限抓得比
    掃描的 wait_for_screen_stable（~7.5秒）短很多，避免被本來就不會停止變化的
    動態內容（跑馬燈、loading動畫）拖慢太多測案執行時間。

    max_wait 是實際經過的秒數。iOS 讀一次 page_source 要 1~3 秒，原本只累加
    poll_interval，畫面一直在變時（報價跳動）實際會等到 8 次讀取、十幾二十秒。
    """
    previous = None
    deadline = time.time() + max_wait
    while True:
        try:
            current = layout_signature(platform, driver.page_source)
        except Exception:
            return
        if current == previous or time.time() >= deadline:
            return
        previous = current
        time.sleep(poll_interval)


def next_is_banner_check(steps: list, current_index: int) -> bool:
    """current_index 是從1起算的步驟編號；下一步是 assert_banner 時，這一步做完不能先
    wait_after_action()，否則跑馬燈會在等畫面穩定的那幾秒內消失（見 platforms/ios.py、platforms/android.py 的 watch_banner）。"""
    return current_index < len(steps) and steps[current_index].get("action") == "assert_banner"


def execute_step(platform, driver, step: dict, el: dict | None) -> str:
    """
    執行一個步驟、回傳結果文字（PASS/FAIL/SKIP 開頭）。命令列（run_test_case）跟
    viewer_server 的測案執行共用，差別只在呼叫端怎麼找到 el（單一畫面 vs 每步自帶
    screen_name）。el 是 None 代表 element_map 裡找不到這個 target。
    """
    action = step.get("action")
    if action == "assert_banner":
        # 跑馬燈不在 element_map 裡，不需要 target；在測案裡執行時會先被
        # run_step_in_case 攔下來跟後面連續的 assert_banner 合併監看，這裡是單獨一步的情況
        return run_banner_group(platform, driver, [step])[0]

    if el is None:
        where = f"畫面「{step['screen_name']}」的資料" if step.get("screen_name") else "element_map.json"
        return f"FAIL（{where}裡找不到 {step.get('target')}，畫面資料可能已過期，建議重新掃描）"

    if action == "tap":
        return run_tap(platform, driver, el)
    if action == "input_text":
        return run_input_text(platform, driver, el, step.get("value", ""))
    # 有 timeout 的驗證：等待期間就順便關非預期彈窗（開關沒開時 try_dismiss_dialogs 直接回空清單）
    dismiss = lambda: try_dismiss_dialogs(platform, driver)
    if action == "assert_visible":
        return retry_assert(lambda: run_assert_visible(platform, driver, el), step.get("timeout"),
                            on_fail=dismiss if _dismiss_dialogs_enabled else None)
    if action == "assert_text_contains":
        expected, missing = resolve_env_placeholders(step.get("value", ""))
        if missing:
            return missing_vars_outcome(missing)
        return retry_assert(lambda: run_assert_text_contains(platform, driver, el, expected),
                            step.get("timeout"), on_fail=dismiss if _dismiss_dialogs_enabled else None)
    return f"FAIL（未知的 action: {action}）"


def banner_texts(step: dict) -> list[str]:
    value = step.get("value")
    texts = [value] if isinstance(value, str) else list(value or [])
    # 跑馬燈文字也可以用變數（例如 "${STOCK_CODE}"）；沒設定的照原樣留著，結果裡看得出是哪個沒換到
    return [resolve_env_placeholders(t)[0] for t in texts if t]


# 跑馬燈監看的錄影狀態：run_step_in_case 在觸發的那一步之前開錄、記下開始時間，
# run_banner_group 監看完停止（有 FAIL 才存檔），用開始時間把「第幾秒抓到什麼」對到影片上
_banner_capture = {"started": None}


def run_banner_group(platform, driver, group: list) -> list[str]:
    """
    把連續好幾個 assert_banner 步驟合併成「一次監看」，回傳每一步各自的結果文字。

    接連跳出好幾個跑馬燈時（例如「買進-委託處理中」→「委託狀態已可查詢」），
    如果一步一步分開等，前一步等比較久的話，輪到下一步時它的跑馬燈可能已經
    消失了；合併後從第一步開始就同時盯著所有步驟的文字，誰先出現都會被記下來。
    監看時間是各步 timeout 加總（跟分開執行時最久要等的時間一樣），全部抓到就提早結束。

    預設只要求「每段文字都有出現過」，不管先後。步驟加上 "in_order": true 時，
    另外要求這一步的跑馬燈比前一個 assert_banner 步驟的晚出現（或同時出現）——
    比的是「這一步最早看到的文字」跟「前一步最晚看到的文字」第一次被看到的時間。
    順序不對時前後兩步都算 FAIL：順序是兩步之間的關係，只把後一步標 FAIL 的話，
    前一步（例如「應該先跳出的那個」）看起來像是通過了，容易誤會。
    """
    if not platform.supports_banner:
        return [f"SKIP（assert_banner 在 {platform.name} 還沒實作）"] * len(group)

    texts = list(dict.fromkeys(t for step in group for t in banner_texts(step)))
    timeout = sum(step.get("timeout", 10) for step in group)
    watch_started = time.time()
    try:
        found, seen_at, note = platform.watch_banner(driver, texts, timeout) if texts else ({}, {}, "")
    finally:
        rec_started, _banner_capture["started"] = _banner_capture["started"], None
    if rec_started is not None:
        missed = not texts or not all(found.get(t) for t in texts)
        video = platform.stop_capture(
            driver, f"test_results/banner_fail_{time.strftime('%Y%m%d_%H%M%S')}.mp4" if missed else None)
        if video:
            timeline = "、".join(f"「{t}」在第 {seen_at[t] - rec_started:.1f} 秒抓到" for t in texts if t in seen_at)
            note = (note + "，" if note else "") + (
                f"錄影存在 {video}（監看從影片第 {watch_started - rec_started:.1f} 秒開始"
                + (f"；{timeline}" if timeline else "") + "）")

    outcomes = []
    order_violations = []  # (後一步的 k, 早了幾秒)
    for k, step in enumerate(group):
        step_texts = banner_texts(step)
        if not step_texts:
            outcomes.append("FAIL（assert_banner 的 value 沒有指定要等的文字）")
            continue
        missing = [t for t in step_texts if not found.get(t)]
        if missing:
            outcomes.append(f"FAIL（{timeout:g} 秒內沒看到跑馬燈文字：{'、'.join(missing)}"
                            + (f"，{note}" if note else "") + "）")
        elif step.get("in_order") and k > 0:
            outcome, early_by = _check_banner_order(group[k - 1], step_texts, found, seen_at)
            outcomes.append(outcome)
            if early_by is not None:
                order_violations.append((k, early_by))
        else:
            seen = sorted({found[t].strip() for t in step_texts})
            outcomes.append(f"PASS（跑馬燈出現，抓到的文字：{'、'.join(seen)}）")
    for k, early_by in order_violations:
        if outcomes[k - 1].startswith("PASS"):
            seen = "、".join(sorted({found[t].strip() for t in banner_texts(group[k - 1])}))
            outcomes[k - 1] = (f"FAIL（跑馬燈有出現，但比下一個跑馬燈晚 {early_by:.1f} 秒出現，順序不對"
                               f"（下一步要求在它之後出現）；抓到的文字：{seen}）")
    if len(group) > 1:
        outcomes = [o[:-1] + f"；與其他 {len(group) - 1} 個連續的 assert_banner 步驟合併監看）"
                    for o in outcomes]
    return outcomes


def _check_banner_order(previous_step: dict, step_texts: list[str], found: dict,
                        seen_at: dict) -> tuple[str, float | None]:
    """in_order 的步驟：確認這一步的文字不早於前一步的文字出現（前提是這一步的文字都抓到了）。
    回傳 (結果文字, 順序不對時早了幾秒，其他情況 None)，讓呼叫端把前一步也標 FAIL"""
    seen = "、".join(sorted({found[t].strip() for t in step_texts}))
    prev_texts = banner_texts(previous_step)
    if not prev_texts or not all(t in seen_at for t in prev_texts):
        return f"FAIL（跑馬燈有出現，但前一個跑馬燈沒抓到，無法確認先後順序；抓到的文字：{seen}）", None
    this_first = min(seen_at[t] for t in step_texts)
    prev_last = max(seen_at[t] for t in prev_texts)
    if this_first < prev_last:
        early_by = prev_last - this_first
        return (f"FAIL（跑馬燈有出現，但比前一個跑馬燈早 {early_by:.1f} 秒出現，"
                f"順序不對；抓到的文字：{seen}）"), early_by
    return f"PASS（跑馬燈依序出現，比前一個晚 {this_first - prev_last:.1f} 秒；抓到的文字：{seen}）", None


def execute_step_dismissing_dialogs(platform, driver, step: dict, el: dict | None) -> str:
    """
    執行一步；失敗時（而且開關有開）檢查是不是被非預期彈窗擋住，有的話關掉再重試一次。
    只在失敗時才檢查：正常情況不多花時間，也不會誤關測案本來就要操作的對話框。
    el 是 None（地圖裡找不到 target）是資料問題，關彈窗也沒用，不重試。
    """
    outcome = _execute_step_safely(platform, driver, step, el)
    if not outcome.startswith("FAIL") or el is None:
        return outcome
    dismissed = try_dismiss_dialogs(platform, driver)
    if not dismissed:
        return outcome
    return with_dismiss_note(_execute_step_safely(platform, driver, step, el), dismissed)


def _execute_step_safely(platform, driver, step: dict, el: dict | None) -> str:
    """一步裡發生沒預料到的錯誤（Appium 回報元件狀態不對、連線抖動…）只讓這一步 FAIL，
    不要整份測案中斷——批次執行時一份測案中斷，後面的測案也跟著跑不了。
    停止請求跟 Appium session 整個斷掉這類「繼續跑也沒意義」的錯誤照常往外丟"""
    try:
        return execute_step(platform, driver, step, el)
    except StopRequested:
        raise
    except InvalidSessionIdException:
        raise
    except Exception as e:
        first_line = (getattr(e, "msg", None) or str(e) or "").splitlines()
        return f"FAIL（執行時發生錯誤：{type(e).__name__}{'：' + first_line[0] if first_line else ''}）"


def run_step_in_case(platform, driver, steps: list, i: int, el: dict | None, banner_outcomes: dict) -> str:
    """
    在一份測案裡執行第 i 步（從1起算）。遇到 assert_banner 時，把它跟後面連續的
    assert_banner 一起交給 run_banner_group 合併監看，後面那幾步的結果先存在
    banner_outcomes（呼叫端整份測案共用同一個 dict），輪到它們時直接取出，
    所以每一步在結果清單裡還是各自一列。
    """
    if i in banner_outcomes:
        return banner_outcomes.pop(i)
    step = steps[i - 1]
    if step.get("action") != "assert_banner":
        if not (platform.supports_banner and next_is_banner_check(steps, i)):
            return execute_step_dismissing_dialogs(platform, driver, step, el)
        # 下一步要驗跑馬燈：這一步（通常是送出委託的 tap）也不能等 App 靜止。UiAutomator2／XCUITest
        # 點擊後預設會等畫面靜止才回傳（最多 waitForIdleTimeout，預設 10 秒），報價一直跳的
        # 下單頁永遠不會靜止，點擊要等好幾秒才回來，第一個跑馬燈就在這段時間出現又消失了
        # 跑馬燈偶爾漏看、又不好重現：從觸發的這一步之前就開始錄影，監看完沒抓到才留檔
        platform.prepare_banner_watch(driver)
        if platform.start_capture(driver):
            time.sleep(0.5)  # 等錄影程式真的開始錄，免得錄不到按下去那一刻
            _banner_capture["started"] = time.time()
        started = time.time()
        with idle_wait_disabled(driver):
            outcome = execute_step_dismissing_dialogs(platform, driver, step, el)
        elapsed = time.time() - started
        if elapsed > 1.5:
            outcome = with_note(outcome, f"這一步花了 {elapsed:.1f} 秒，跑馬燈可能在這段時間就出現了")
        return outcome
    end = i
    while end < len(steps) and steps[end].get("action") == "assert_banner":
        end += 1
    outcomes = run_banner_group(platform, driver, steps[i - 1:end])
    banner_outcomes.update({i + k: o for k, o in enumerate(outcomes) if k > 0})
    return outcomes[0]


def should_wait_after(platform, steps: list, current_index: int) -> bool:
    """tap/input_text 之後要等畫面穩定，除非下一步是要趁跑馬燈還在時驗證的 assert_banner"""
    action = steps[current_index - 1].get("action")
    if action not in ("tap", "input_text"):
        return False
    return not (platform.supports_banner and next_is_banner_check(steps, current_index))


def run_test_case(platform, driver, test_case: dict, element_lookup: dict):
    results = []
    steps = test_case["steps"]
    banner_outcomes = {}
    for i, step in enumerate(steps, 1):
        outcome = run_step_in_case(platform, driver, steps, i, element_lookup.get(step.get("target")),
                                   banner_outcomes)
        results.append((i, step, outcome))
        print(f"  {i}. [{step.get('action')}] {step.get('target') or step.get('value')} -> {outcome}")
        if should_wait_after(platform, steps, i):
            wait_after_action(platform, driver)
    return results


def save_result_screenshot(driver, screen_name: str):
    TEST_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TEST_RESULTS_DIR / f"{screen_name}_after.png"
    with open(path, "wb") as f:
        f.write(driver.get_screenshot_as_png())
    print(f"執行後截圖存到 {path}")


def main():
    parser = argparse.ArgumentParser(description="執行 test_cases/{screen_name}.json 的測試案例")
    parser.add_argument("--screen_name", required=True)
    parser.add_argument("--platform", choices=sorted(PLATFORMS), default="ios", help="裝置平台（預設 ios）")
    parser.add_argument("--dismiss_dialogs", action="store_true",
                        help="步驟失敗時先關掉擋在前面的非預期彈窗（行銷/評分/推播邀請…）再重試一次")
    args = parser.parse_args()
    platform = get_platform(args.platform)
    set_dismiss_dialogs(args.dismiss_dialogs)

    test_case = load_test_case(args.screen_name)
    element_lookup = load_element_lookup(args.screen_name)

    print(f"執行畫面「{args.screen_name}」的測試案例（{platform.name}），共 {len(test_case['steps'])} 個步驟")

    driver = webdriver.Remote(APPIUM_SERVER_URL, options=platform.make_options())

    try:
        with platform.page_source_context(driver):
            on_screen = verify_current_screen(platform, driver, args.screen_name, element_lookup)
            if not on_screen and try_dismiss_dialogs(platform, driver):
                # 畫面對不上可能只是被非預期彈窗蓋住，關掉後再檢查一次
                on_screen = verify_current_screen(platform, driver, args.screen_name, element_lookup)
            if not on_screen:
                save_result_screenshot(driver, args.screen_name)
                raise SystemExit(
                    f"手機目前畫面看起來不是「{args.screen_name}」，為避免座標點擊備援機制誤觸其他畫面上的"
                    f"真實元件，中止執行。請手動導航回正確畫面後再重新執行（已存一張當下截圖到 "
                    f"test_results/{args.screen_name}_after.png 方便你確認目前實際在哪個畫面，"
                    f"但若這支 App 有 FLAG_SECURE 保護，這張截圖可能是純黑色，無法用來確認畫面）。"
                )
            results = run_test_case(platform, driver, test_case, element_lookup)
            save_result_screenshot(driver, args.screen_name)
    finally:
        driver.quit()

    passed = sum(1 for _, _, outcome in results if outcome.startswith("PASS"))
    failed = sum(1 for _, _, outcome in results if outcome.startswith("FAIL"))
    skipped = sum(1 for _, _, outcome in results if outcome.startswith("SKIP"))
    print(f"\n總結：{len(results)} 步驟，PASS {passed} / FAIL {failed} / SKIP {skipped}")


if __name__ == "__main__":
    main()
