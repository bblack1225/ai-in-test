"""
POC: iOS App 元件地圖產生器（第三步）

功能：
  讀取 generate_test_case.py 產生的測試步驟清單，實際連線手機、透過 Appium 執行，
  並印出每個步驟的執行結果。這一步完全不用 LLM，純粹是把結構化的步驟資料
  轉換成 Appium 操作指令（機械式工作，交給固定的程式邏輯處理）。

元件定位策略：
  0. 不管 id_source 是什麼，先檢查 el["custom_xpath"]——這是使用者在
     viewer.html「重新定義 XPath」/「AI定位」功能裡人工測試、或AI生成後
     實測驗證過，確認查到唯一結果才存下來的查詢，優先於所有自動推導的
     定位方式使用
  - native 元件（有 accessibility id）：用 AppiumBy.ACCESSIBILITY_ID 定位，穩定可靠
  - generated 元件（沒有 native id）：
    1. 先試精確 XPath（class + label）；查不到就自己捲動、邊捲邊重查
       （_locate_by_xpath_with_scroll），涵蓋元件因 cell 重用暫時不在 tree
       上的情況。查到唯一一個才用（比座標更耐用，畫面稍微捲動、版面微調都
       不受影響）
    2. 查到多個（真的有歧義）時，改用 nearby_text 當錨點消歧義
       （_locate_by_anchor）——清單裡常見「每一列都有一個文字相同的
       操作按鈕」，純靠 class+label 沒辦法唯一定位，要靠同一列裡能唯一
       辨識該列的鄰近文字才行（見 TROUBLESHOOTING #36，這是先在 Android
       版發現、修正後才比照加回 iOS 版的邏輯）
    3. 都找不到、或 label 本身是空的，才退回用 bounds
       座標算出中心點，透過 `mobile: tap` 直接點擊
    4. input_text 只支援 native 元件——沒有 native id 的輸入框，代表這個
       App 本身測試友善度不足，不適合硬用座標點擊+輸入解決，建議回報給開發團隊

  找到元件之後、實際點擊/驗證之前，會先呼叫 ensure_visible()：iOS 元件即使
  捲出目前可視範圍外，Accessibility Tree 依然找得到（節點真的存在），但不在
  可視範圍內時直接點擊等同無效觸控，所以先檢查 visible 屬性，不是 true 才用
  `mobile: scroll` 帶 `toVisible: True` 捲到可互動範圍再繼續（見 TROUBLESHOOTING
  #28）。這跟 Android 版的情況不同——Android 畫面外的元件常常連節點都還沒被
  建出來，要用「邊捲邊找」而不是「找到後再捲」，見 run_test_case_android.py。

使用方式：
  python run_test_case.py --screen_name 我的頁面

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
import time
from pathlib import Path

from appium import webdriver
from appium.options.ios import XCUITestOptions
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import NoSuchElementException

from capture_screen_map import find_scrollable_regions, swipe_within_region
from test_config import APPIUM_SERVER_URL, DESIRED_CAPS

ELEMENT_MAP_PATH = Path("element_map.json")
TEST_CASES_DIR = Path("test_cases")
TEST_RESULTS_DIR = Path("test_results")


def load_test_case(screen_name: str) -> dict:
    path = TEST_CASES_DIR / f"{screen_name}.json"
    if not path.exists():
        raise SystemExit(f"找不到 {path}，請先用 generate_test_case.py 產生這個畫面的測試案例")
    return json.loads(path.read_text(encoding="utf-8"))


def load_element_lookup(screen_name: str) -> dict:
    if not ELEMENT_MAP_PATH.exists():
        raise SystemExit(f"找不到 {ELEMENT_MAP_PATH}")
    data = json.loads(ELEMENT_MAP_PATH.read_text(encoding="utf-8"))
    for screen in data["screens"]:
        if screen["screen_name"] == screen_name:
            return {el["unique_id"]: el for el in screen["elements"]}
    raise SystemExit(f"element_map.json 裡找不到畫面「{screen_name}」")


def xpath_literal(value: str) -> str:
    """把字串安全地組成 XPath 字面值，處理內含雙引號的情況"""
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    parts = value.split('"')
    return "concat(" + ', \'"\', '.join(f'"{p}"' for p in parts) + ")"


def _locate_by_xpath_with_scroll(driver, el: dict, max_rounds: int = 8):
    """
    用 class + label 查精確 XPath，查不到就自己捲動可捲動容器、每輪重新查同一條
    精確 XPath，取代原本只看當下快照、不會捲動的 _locate_by_xpath（已移除）。
    比照 Android 版 run_test_case_android.py 的同名函式移植。

    為什麼 iOS 也需要這個：iOS 的 Accessibility Tree 對「捲出畫面外」的元件
    多半還是找得到（節點仍在，見 TROUBLESHOOTING #16/#28），這種情況
    ensure_visible() 的 `mobile: scroll` `toVisible` 已經能處理。但長的
    XCUIElementTypeTable / CollectionView 有 cell 重用，離畫面夠遠的 row 會
    整個從 tree 上消失，這時精確 XPath 查 0 個、ensure_visible 也無從捲起
    （沒有 element 可以 toVisible），舊流程只能退到用 element_map.json 裡
    「掃描當下」的舊座標盲點，捲動位置一旦不同就點錯。這個函式補上這一段：
    自己捲容器、邊捲邊用精確 XPath 重查。

    跟 Android 版的差異：Android 的 scroll_step 有布林回傳值可判斷「捲不動了」；
    iOS 的 driver.swipe 沒有，改用「捲動前後 page_source 沒變」當「這個容器
    捲不動 / 已到底」的依據，可靠度略低（同一個已知落差，見
    capture_screen_map.swipe_within_region 說明）。

    查詢結果處理跟 Android 版一致：
      - 剛好 1 個 → 直接用
      - >1 個 → 真的有歧義，捲再多也不會變唯一，直接放棄交給呼叫端的錨點消歧義
      - 0 個 → 捲動重試（先用涵蓋畫面主要範圍的通用區域，再逐一試畫面上其他
        可捲動容器）
    """
    label = el.get("label", "")
    if not label:
        return None

    xpath = f'//{el["class"]}[@label={xpath_literal(label)}]'

    def _query():
        try:
            return driver.find_elements(AppiumBy.XPATH, xpath)
        except Exception as e:
            print(f"    [XPath+捲動] 查詢失敗: {xpath}  ({e})")
            return []

    matches = _query()
    print(f"    [XPath+捲動] 第0輪（不捲）: {xpath} -> 查到 {len(matches)} 個")
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return None  # 真的有歧義，交給呼叫端的錨點消歧義處理

    def _scroll_search(region_bounds, region_label):
        previous_source = None
        for round_index in range(1, max_rounds + 1):
            swipe_within_region(driver, region_bounds, "down")
            time.sleep(0.5)
            current_source = driver.page_source
            if current_source == previous_source:
                print(f"    [XPath+捲動] {region_label} 捲動前後畫面沒變，判定捲不動了，停止")
                return None
            previous_source = current_source
            found = _query()
            print(f"    [XPath+捲動] {region_label} 第{round_index}輪: 查到 {len(found)} 個")
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                return None
        return None

    size = driver.get_window_size()
    outer_area = {
        "x": int(size["width"] * 0.1),
        "y": int(size["height"] * 0.15),
        "width": int(size["width"] * 0.8),
        "height": int(size["height"] * 0.7),
    }
    matched = _scroll_search(outer_area, "主要區域")
    if matched is not None:
        return matched

    try:
        regions = find_scrollable_regions(driver.page_source)
    except Exception:
        regions = []
    for region in regions:
        matched = _scroll_search(region["bounds"], f"容器（{region['class']}）")
        if matched is not None:
            return matched

    return None


def _locate_by_anchor(driver, el: dict):
    """
    跟 Android 版（run_test_case_android.py 的 _locate_by_anchor）同樣的
    錨點消歧義邏輯，處理「清單裡每一列都有一個文字相同的操作按鈕」這種
    純 XPath 查不到唯一結果的情境（見 TROUBLESHOOTING #36）。用 fingerprint
    裡的 nearby_text（同一列裡能唯一辨識這一列的文字）當錨點，在所有符合
    class+label 的候選裡，挑跟錨點 Y 座標最接近的一個。

    跟 Android 版的關鍵差異：iOS 沒有 UiScrollable.scrollIntoView() 這種
    「邊捲邊找」的機制可用，但也不需要——iOS 元件即使捲出畫面外，
    Accessibility Tree 依然找得到（節點真的存在，見已知問題16），所以
    錨點本身直接用 XPath 查詢就查得到，不用先捲動。找到答案之後，呼叫端
    仍然會用 ensure_visible() 確保真的可以點擊/驗證。
    """
    nearby_text_list = (el.get("fingerprint") or {}).get("nearby_text") or []
    label = el.get("label", "")
    if not nearby_text_list or not label:
        return None

    anchor_literal = xpath_literal(nearby_text_list[0])
    try:
        anchors = driver.find_elements(AppiumBy.XPATH, f'//*[@label={anchor_literal}]')
    except Exception as e:
        print(f"    [錨點消歧義] 錨點查詢失敗: {e}")
        return None
    if len(anchors) != 1:
        return None
    anchor_y = anchors[0].location["y"]

    xpath = f'//{el["class"]}[@label={xpath_literal(label)}]'
    try:
        candidates = driver.find_elements(AppiumBy.XPATH, xpath)
    except Exception:
        return None
    if not candidates:
        return None

    closest = min(candidates, key=lambda c: abs(c.location["y"] - anchor_y))
    print(f"    [錨點消歧義] 用鄰近文字「{nearby_text_list[0]}」（y={anchor_y}）"
          f"從 {len(candidates)} 個同文字候選中挑出最接近的一個")
    return closest


def _locate_by_custom_xpath(driver, el: dict):
    """
    人工在 viewer.html 上用「重新定義XPath」/「AI定位」存下來的自訂查詢
    （el["custom_xpath"]），優先於所有自動推導的定位方式使用——這是人工
    測試過、或AI生成後經過實測驗證才存的，比自動邏輯更值得信任。查不到
    唯一結果時（例如畫面已經改版）不強行使用，回傳 None 讓呼叫端繼續走
    原本的自動定位鏈。跟 Android 版（run_test_case_android.py）同樣邏輯。
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


def locate_generated_element(driver, el: dict):
    """
    無 native id 元件的定位順序：
      1. 先試精確 XPath（class + label）；查不到就自己捲動可捲動容器、邊捲邊
         重查（_locate_by_xpath_with_scroll，見其說明），涵蓋「元件因 cell
         重用而暫時不在 tree 上」的情況；查到唯一一個才用
      2. 查到多個（真的有歧義）時，改用 nearby_text 當錨點消歧義
         （_locate_by_anchor，見其說明）
      3. 都找不到才回傳 None，呼叫端改用座標
    """
    label = el.get("label", "")
    if not label:
        print(f"    [定位] label 為空，直接改用座標")
        return None

    matched = _locate_by_xpath_with_scroll(driver, el)
    source = "XPath（含捲動）"
    if matched is None:
        matched = _locate_by_anchor(driver, el)
        source = "鄰近文字錨點"
    if matched is None:
        return None

    print(f"    [命中元件屬性（{source}）] location={matched.location}  size={matched.size}  "
          f"name={matched.get_attribute('name')!r}  label={matched.get_attribute('label')!r}  "
          f"value={matched.get_attribute('value')!r}  enabled={matched.get_attribute('enabled')!r}")
    return matched


def ensure_visible(driver, element):
    """
    iOS 元件即使不在目前可視範圍內，Accessibility Tree 依然找得到（節點真的
    存在，這點跟 Android 不同——Android 畫面外的元件常常根本還沒被建出來，
    見 run_test_case_android.py 的說明）。但找到不代表能點：不在可視範圍內時
    直接 .click() 在 XCTest 底層等同無效觸控。這裡在點擊/驗證前，先確認
    visible 屬性，不是 true 才捲動到可視範圍，呼叫端不用管元件原本在不在畫面上。
    """
    try:
        if element.get_attribute("visible") == "true":
            return element
        print("    [捲動至可視範圍] 元件目前不在可視範圍內，嘗試捲動...")
        driver.execute_script("mobile: scroll", {"element": element.id, "toVisible": True})
    except Exception as e:
        print(f"    [捲動至可視範圍] 檢查/捲動失敗，照原本流程繼續: {e}")
    return element


def tap_by_coordinates(driver, bounds: dict):
    center_x = bounds["x"] + bounds["width"] / 2
    center_y = bounds["y"] + bounds["height"] / 2
    print(f"    [座標點擊] x={center_x}, y={center_y}  (來自 element_map.json 的 bounds={bounds})")
    driver.execute_script("mobile: tap", {"x": center_x, "y": center_y})


def run_input_text(driver, el: dict, value: str) -> str:
    if el["id_source"] != "native":
        return "SKIP（無 native id 的輸入框無法可靠定位，建議回報開發團隊補上 accessibilityIdentifier）"
    try:
        element = driver.find_element(AppiumBy.ACCESSIBILITY_ID, el["native_id"])
        element.send_keys(value)
        return "PASS"
    except NoSuchElementException:
        return f"FAIL（找不到元件: {el['native_id']}）"


def run_tap(driver, el: dict) -> str:
    custom = _locate_by_custom_xpath(driver, el)
    if custom is not None:
        ensure_visible(driver, custom)
        custom.click()
        return "PASS（人工自訂XPath定位）"

    if el["id_source"] == "native":
        try:
            element = driver.find_element(AppiumBy.ACCESSIBILITY_ID, el["native_id"])
            ensure_visible(driver, element)
            element.click()
            return "PASS（accessibility id 定位）"
        except NoSuchElementException:
            return f"FAIL（找不到元件: {el['native_id']}）"

    matched = locate_generated_element(driver, el)
    if matched is not None:
        ensure_visible(driver, matched)
        matched.click()
        return "PASS（XPath 定位）"

    tap_by_coordinates(driver, el["bounds"])
    return "PASS（座標點擊，備援方式）"


def run_assert_visible(driver, el: dict) -> str:
    custom = _locate_by_custom_xpath(driver, el)
    if custom is not None:
        ensure_visible(driver, custom)
        return "PASS（人工自訂XPath定位）" if custom.is_displayed() else "FAIL（元件存在但不可見）"

    if el["id_source"] == "native":
        try:
            element = driver.find_element(AppiumBy.ACCESSIBILITY_ID, el["native_id"])
            ensure_visible(driver, element)
            return "PASS" if element.is_displayed() else "FAIL（元件存在但不可見）"
        except NoSuchElementException:
            return "FAIL（找不到元件）"

    matched = locate_generated_element(driver, el)
    if matched is not None:
        ensure_visible(driver, matched)
        return "PASS（XPath 定位）" if matched.is_displayed() else "FAIL（元件存在但不可見）"

    return "SKIP（無 native id 且 XPath 查無唯一結果，座標無法驗證「是否可見」）"


def run_assert_text_contains(driver, el: dict, expected_text: str) -> str:
    """
    Android 版（run_test_case_android.py）的對應函式，這裡是 iOS 版：驗證元件
    目前顯示的文字內容有沒有包含指定字串，給 LLM 產生的跨畫面測案用（viewer.html
    「LLM 測案產生」分頁）。iOS 用 label/value 取代 Android 的 text/content-desc。
    **這次沒有 iPhone 可實機測試，只通過語法檢查**，比照本檔案其他函式已知的
    未驗證風險。
    """
    custom = _locate_by_custom_xpath(driver, el)
    target_el, label = custom, "人工自訂XPath定位"

    if target_el is None:
        if el["id_source"] == "native":
            try:
                target_el = driver.find_element(AppiumBy.ACCESSIBILITY_ID, el["native_id"])
                label = "accessibility id 定位"
            except NoSuchElementException:
                return "FAIL（找不到元件）"
        else:
            target_el = locate_generated_element(driver, el)
            label = "XPath 定位"

    if target_el is None:
        return "SKIP（無 native id 且 XPath 查無唯一結果，無法讀取文字內容）"

    ensure_visible(driver, target_el)
    actual_text = (target_el.get_attribute("label") or target_el.get_attribute("value") or "").strip()
    if expected_text in actual_text:
        return f"PASS（{label}，實際文字：「{actual_text}」）"
    return f"FAIL（{label}，預期文字包含「{expected_text}」，實際文字：「{actual_text}」）"


def _predicate_literal(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def run_assert_banner(driver, expected, timeout: float = 10.0, poll_interval: float = 0.1) -> str:
    """
    驗證「只出現幾秒就消失」的跑馬燈/通知橫幅（例如下單後上方的「委託回報」）。

    expected 可以是字串或字串陣列，每個字串都要出現在畫面上某個 StaticText 裡才算 PASS，
    例如 ["委託回報", "2882 國泰金"]。不需要 element_map 裡的元件。

    跟 run_assert_visible 刻意不同的三點（都是實測跑馬燈抓不到的原因）：
      - 用 iOS predicate 查，查詢在手機上完成；XPath 要先把整份 page source
        傳回電腦，一次 1~3 秒，跑馬燈早就消失了
      - 暫時把 waitForIdleTimeout 設 0：跑馬燈是動畫，預設會等 App 靜止才查詢
      - 不看 is_displayed()：實測跑馬燈上的文字 visible 都是 false（XCUITest
        對重疊/動畫中元素的誤判），只要查得到就算出現
    所以這一步必須緊接在觸發它的 tap 之後，中間不能先跑 wait_after_action()
    （它也是一直抓 page source，會把跑馬燈的時間耗掉），執行端已經處理。
    """
    texts = [expected] if isinstance(expected, str) else list(expected or [])
    texts = [t for t in texts if t]
    if not texts:
        return "FAIL（assert_banner 的 value 沒有指定要等的文字）"

    predicate = "type == 'XCUIElementTypeStaticText' AND (" + " OR ".join(
        f"label CONTAINS {_predicate_literal(t)}" for t in texts) + ")"

    original_idle = driver.get_settings().get("waitForIdleTimeout")
    driver.update_settings({"waitForIdleTimeout": 0})
    try:
        seen_labels = set()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                labels = [e.get_attribute("label") or "" for e in
                          driver.find_elements(AppiumBy.IOS_PREDICATE, predicate)]
            except Exception:
                # 元素剛好在讀屬性時消失（stale），下一輪再查
                labels = []
            seen_labels.update(labels)
            if all(any(t in label for label in seen_labels) for t in texts):
                return f"PASS（跑馬燈出現，抓到的文字：{'、'.join(sorted(seen_labels))}）"
            time.sleep(poll_interval)
    finally:
        if original_idle is not None:
            driver.update_settings({"waitForIdleTimeout": original_idle})

    missing = [t for t in texts if not any(t in label for label in seen_labels)]
    return f"FAIL（{timeout:g} 秒內沒看到跑馬燈文字：{'、'.join(missing)}）"


def verify_current_screen(driver, screen_name: str, element_lookup: dict) -> bool:
    """
    執行測試步驟前的安全檢查：確認手機目前畫面看起來就是測試案例鎖定的畫面，
    避免在錯誤畫面上，tap 的座標備援機制誤觸別的畫面上的真實元件卻回報 PASS。
    做法：用這個畫面已知的 native id 元件當指紋比對，檢查目前畫面上找得到幾個。
    """
    native_ids = [el["native_id"] for el in element_lookup.values() if el["id_source"] == "native"]
    if not native_ids:
        print("  [警告] 這個畫面沒有任何 native id 元件可以校驗，跳過畫面比對，風險請自行留意")
        return True

    found = sum(1 for nid in native_ids if driver.find_elements(AppiumBy.ACCESSIBILITY_ID, nid))
    ratio = found / len(native_ids)
    print(f"  [畫面校驗] 「{screen_name}」的 {len(native_ids)} 個 native 元件中，"
          f"目前畫面找到 {found} 個（{ratio:.0%}）")
    return ratio >= 0.5  # 過半數 native 元件存在，才視為目前真的在這個畫面上


def wait_after_action(driver, max_wait: float = 3.0, poll_interval: float = 0.4) -> None:
    """
    Android 版（run_test_case_android.py）的對應函式，這裡是 iOS 版：
    tap/input_text 之後改成輪詢 page_source 直到內容連續兩次一致或到時間
    上限為止，取代原本寫死的 `time.sleep(1)`——原本固定1秒的等待，遇到
    App本身有較長轉場/非同步載入（例如測試App的轉場畫面刻意設計2秒
    loading）時可能不夠，下一步容易在畫面還沒更新完就執行。
    """
    previous = None
    elapsed = 0.0
    while elapsed < max_wait:
        try:
            current = driver.page_source
        except Exception:
            return
        if current == previous:
            return
        previous = current
        time.sleep(poll_interval)
        elapsed += poll_interval


def next_is_banner_check(steps: list, current_index: int) -> bool:
    """current_index 是從1起算的步驟編號；下一步是 assert_banner 時，這一步做完不能先
    wait_after_action()，否則跑馬燈會在等畫面穩定的那幾秒內消失（見 run_assert_banner）。"""
    return current_index < len(steps) and steps[current_index].get("action") == "assert_banner"


def run_test_case(driver, test_case: dict, element_lookup: dict):
    results = []
    steps = test_case["steps"]
    for i, step in enumerate(steps, 1):
        action = step["action"]
        target = step.get("target")

        if action == "assert_banner":
            outcome = run_assert_banner(driver, step.get("value"), step.get("timeout", 10))
            results.append((i, step, outcome))
            print(f"  {i}. [{action}] {step.get('value')} -> {outcome}")
            continue

        el = element_lookup.get(target)

        if el is None:
            results.append((i, step, f"FAIL（element_map.json 裡找不到 {target}，畫面資料可能已過期，建議重新掃描）"))
            continue

        if action == "input_text":
            outcome = run_input_text(driver, el, step.get("value", ""))
        elif action == "tap":
            outcome = run_tap(driver, el)
        elif action == "assert_visible":
            outcome = run_assert_visible(driver, el)
        elif action == "assert_text_contains":
            outcome = run_assert_text_contains(driver, el, step.get("value", ""))
        else:
            outcome = f"FAIL（未知的 action: {action}）"

        results.append((i, step, outcome))
        print(f"  {i}. [{action}] {target} -> {outcome}")

        if action in ("tap", "input_text") and not next_is_banner_check(steps, i):
            wait_after_action(driver)

    return results


def save_result_screenshot(driver, screen_name: str):
    TEST_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TEST_RESULTS_DIR / f"{screen_name}_after.png"
    with open(path, "wb") as f:
        f.write(driver.get_screenshot_as_png())
    print(f"執行後截圖存到 {path}")


def main():
    parser = argparse.ArgumentParser(description="執行 generate_test_case.py 產生的測試案例")
    parser.add_argument("--screen_name", required=True)
    args = parser.parse_args()

    test_case = load_test_case(args.screen_name)
    element_lookup = load_element_lookup(args.screen_name)

    print(f"執行畫面「{args.screen_name}」的測試案例，共 {len(test_case['steps'])} 個步驟")

    options = XCUITestOptions().load_capabilities(DESIRED_CAPS)
    driver = webdriver.Remote(APPIUM_SERVER_URL, options=options)

    try:
        if not verify_current_screen(driver, args.screen_name, element_lookup):
            save_result_screenshot(driver, args.screen_name)
            raise SystemExit(
                f"手機目前畫面看起來不是「{args.screen_name}」，為避免座標點擊備援機制誤觸其他畫面上的"
                f"真實元件，中止執行。請手動導航回正確畫面後再重新執行（已存一張當下截圖到 "
                f"test_results/{args.screen_name}_after.png 方便你確認目前實際在哪個畫面）。"
            )
        results = run_test_case(driver, test_case, element_lookup)
        save_result_screenshot(driver, args.screen_name)
    finally:
        driver.quit()

    passed = sum(1 for _, _, outcome in results if outcome.startswith("PASS"))
    failed = sum(1 for _, _, outcome in results if outcome.startswith("FAIL"))
    skipped = sum(1 for _, _, outcome in results if outcome.startswith("SKIP"))
    print(f"\n總結：{len(results)} 步驟，PASS {passed} / FAIL {failed} / SKIP {skipped}")


if __name__ == "__main__":
    main()
