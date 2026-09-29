"""
POC: Android App 測試案例執行器（run_test_case.py 的移植，第三步）

跟 iOS 版（run_test_case.py）共用同一套測試案例格式（test_cases/{screen_name}.json）
跟 element_map.json schema，差異只在 Appium 定位元件、下點擊指令的方式要換成
Android/UiAutomator2 的寫法：

  - native 元件（有 resource-id）：iOS 版用 AppiumBy.ACCESSIBILITY_ID 對應
    accessibilityIdentifier；Android 版改用 AppiumBy.ID，UiAutomator2 底層會
    用 resource-id 去查。因為 capture_screen_map_android.py 已經先把「同畫面
    共用同一個 resource-id」的元件降級成 generated（見 TROUBLESHOOTING #26），
    這裡查到的 native 元件保證在畫面上唯一，不會像修正前那樣一次選到好幾個。
  - generated 元件（沒有 native id）：iOS 版 XPath 用 @label；Android 的
    accessibility tree 沒有 label 屬性，改用 @text 或 @content-desc（label
    欄位在 capture_screen_map_android.py 裡就是 content_desc 優先、沒有才用
    text，所以查詢時兩個屬性都要試，用 or 一起查）。
  - 座標點擊備援：iOS 用 `mobile: tap`；UiAutomator2 沒有這個指令，要換成
    `mobile: clickGesture`（帶 x/y 座標），這是 Appium 兩個 driver 底層手勢
    API 不同造成的，不是概念上的差異。

元件定位策略：
  0. 不管 id_source 是什麼，先檢查 el["custom_xpath"]——這是使用者在
     viewer.html「重新定義 XPath」功能裡人工測試、確認查到唯一結果後
     存下來的查詢，優先於所有自動推導的定位方式使用
  1. native 元件：AppiumBy.ID 定位，穩定可靠
  2. generated 元件：
     a. 先試目前畫面就找得到的 XPath（class + text/content-desc），
        查到唯一一個才用
     b. 查不到（0個或多個）時，改用 nearby_text 當錨點消歧義
        （_locate_by_anchor）——清單裡常見「每一列都有一個文字相同的
        操作按鈕」（例如全部叫「刪除」），純靠 class+文字沒辦法唯一
        定位，要靠同一列裡能唯一辨識該列的鄰近文字才行（見 TROUBLESHOOTING
        #36：純 XPath/UiScrollable 在文字不唯一時會「默默選錯」，不會
        報錯，是比明確失敗更危險的情況）
     c. 沒有 nearby_text 可用時，才退回 UiAutomator 原生的
        UiScrollable.scrollIntoView()——這是 Android 官方測試框架處理
        「元件在可捲動內容裡、目前捲動位置不對」的標準做法，會自動捲動
        已知的可捲動容器邊捲邊找，不像 XPath 只能查「當下這一瞬間」的
        畫面快照（元件還沒被捲到、根本沒被建出來時，XPath 永遠是 0 個
        結果，見 TROUBLESHOOTING #27/#28），但文字不唯一時可能選錯，
        只是沒有更好選項時的次佳備援
     d. 都找不到、或 label 為空，才退回座標點擊
  3. input_text 只支援 native 元件

  跟 iOS 版（run_test_case.py）的關鍵差異：iOS 元件即使捲出畫面外，
  Accessibility Tree 依然找得到（節點真的存在），只是不可互動（isHittable
  為 false），所以 iOS 版用的是「找到後再捲到可視範圍」（mobile: scroll
  toVisible）；Android 版的畫面外元件常常連節點都還沒被建出來，找不到，
  所以要用「邊捲邊找」（UiScrollable.scrollIntoView）。兩邊卡住的環節
  不同，才需要用不同的 API。

使用方式：
  python run_test_case_android.py --screen_name my_screen

安全提醒：跟 iOS 版相同，執行前務必人工檢查測試案例內容，執行前也會先做
「畫面校驗」，過半數 native 元件在目前畫面找不到就會中止執行。
"""

import argparse
import time

from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import NoSuchElementException

from capture_screen_map_android import find_scrollable_regions, scroll_step
from run_test_case import (
    ELEMENT_MAP_PATH,
    TEST_CASES_DIR,
    TEST_RESULTS_DIR,
    load_element_lookup,
    load_test_case,
    match_screen_in_source,
    raise_if_stopped,
    retry_assert,
    save_result_screenshot,
    xpath_literal,
)
from test_config import ANDROID_DESIRED_CAPS, APPIUM_SERVER_URL


def uiautomator_string_literal(value: str) -> str:
    """把字串安全地嵌入 UiSelector 的 Java 字面值語法（跳脫反斜線與雙引號）"""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _locate_by_xpath_with_scroll(driver, el: dict, max_rounds: int = 8, scroll_percent: float = 0.75):
    """
    用 class + (text 或 content-desc) 查 XPath，查不到就自己捲動、重新查，
    取代原本純粹「只看當下快照」的 _locate_by_xpath（已移除）。

    跟 UiScrollable.scrollIntoView（_locate_by_scroll_into_view）的關鍵差異：
    這裡每一輪重新查的都是同一條「有比對 class」的精確XPath，不會像
    UiSelector().text(X) 那樣完全不管class，誤抓到文字剛好一樣但class不同
    的別的元件；而且 text/content-desc 是用 `or` 合併在同一條查詢裡一次查完，
    不會像 scrollIntoView 拆成兩次獨立呼叫、第二次卻從第一次捲完的位置
    （可能已經捲到底）繼續找，反而找不到明明存在的目標。

    捲動邏輯沿用掃描階段「邊捲邊抓」驗證過的 scroll_step()（用
    mobile: scrollGesture 的布林回傳值判斷「捲不捲得動」，不是猜固定次數）：
      1. 先在目前畫面查一次（不捲）
      2. 查到剛好1個 → 直接用
      3. 查到 >1 個 → 真的有歧義，捲再多次也不會變唯一，直接放棄交給
         呼叫端的錨點消歧義處理（繼續捲反而可能捲進更多同文字元件）
      4. 查到 0 個 → 用涵蓋畫面主要範圍的通用區域捲動重新查，直到查到
         剛好1個、查到>1個（放棄），或捲到底還是0個
      5. 通用區域捲到底還是0個 → 檢查目前畫面上有沒有其他可捲動容器
         （find_scrollable_regions，不管是巢狀在主容器裡還是跟主容器
         平行的獨立區塊都抓得到，因為是對整棵tree找scrollable=true的
         節點，不是只找特定容器底下的子節點），對每一個都重複同樣的
         捲動重試邏輯，用該容器自己的bounds當捲動範圍
    """
    label = el.get("label", "")
    if not label:
        return None

    literal = xpath_literal(label)
    xpath = f'//{el["class"]}[@text={literal} or @content-desc={literal}]'

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

    def _scroll_search(scroll_area, region_label):
        for round_index in range(1, max_rounds + 1):
            raise_if_stopped()
            if not scroll_step(driver, scroll_area, "down", scroll_percent):
                print(f"    [XPath+捲動] {region_label} 已經捲不動了，停止")
                return None
            matches = _query()
            print(f"    [XPath+捲動] {region_label} 第{round_index}輪: 查到 {len(matches)} 個")
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                return None  # 真的有歧義
        return None

    size = driver.get_window_size()
    screen_width, screen_height = size["width"], size["height"]
    outer_area = {
        "left": int(screen_width * 0.1),
        "top": int(screen_height * 0.15),
        "width": int(screen_width * 0.8),
        "height": int(screen_height * 0.7),
    }
    found = _scroll_search(outer_area, "主要區域")
    if found is not None:
        return found

    try:
        regions = find_scrollable_regions(driver.page_source)
    except Exception:
        regions = []

    for region in regions:
        b = region["bounds"]
        margin_x = max(int(b["width"] * 0.05), 1)
        margin_y = max(int(b["height"] * 0.2), 1)
        region_area = {
            "left": b["x"] + margin_x,
            "top": b["y"] + margin_y,
            "width": max(b["width"] - margin_x * 2, 1),
            "height": max(b["height"] - margin_y * 2, 1),
        }
        found = _scroll_search(region_area, f"巢狀/平行容器（{region['class']}）")
        if found is not None:
            return found

    return None


def _locate_by_scroll_into_view(driver, el: dict):
    """
    畫面當下找不到、但可能在可捲動容器裡尚未捲到的情況：改用 UiAutomator
    原生的 UiScrollable.scrollIntoView()，讓系統自動捲動已知的可捲動容器、
    邊捲邊找，找到符合條件（text 或 content-desc）的元件就停下來並回傳。

    這跟純 XPath 本質不同：XPath 查的是「當下這一瞬間」的畫面快照，元件如果
    還沒被捲到、根本沒被建出來，XPath 再怎麼查都是 0 個結果（已實測驗證，
    見 TROUBLESHOOTING #27/#28）；這個指令則是先捲動、確保元件真的存在於
    畫面上之後才查，呼叫端不需要自己算捲動距離或記錄掃描當下是第幾輪捲動抓到的。
    """
    label = el.get("label", "")
    if not label:
        return None

    literal = uiautomator_string_literal(label)
    for attr in ("text", "description"):
        selector = (
            f'new UiScrollable(new UiSelector().scrollable(true)).scrollIntoView('
            f'new UiSelector().{attr}({literal}))'
        )
        try:
            matched = driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, selector)
            print(f"    [UiScrollable] 捲動後用 {attr} 找到元件: {label!r}")
            return matched
        except NoSuchElementException:
            continue
        except Exception as e:
            print(f"    [UiScrollable] 查詢失敗（{attr}）: {e}")
            continue
    return None


def _locate_by_anchor(driver, el: dict):
    """
    當目標元件本身的 class+文字不足以唯一定位時（清單裡常見：每一列都有一個
    文字一模一樣的操作按鈕，例如全部都叫「刪除」），改用 fingerprint 裡的
    nearby_text（通常是同一列裡能唯一辨識這一列的文字，例如「項目 003」）
    當錨點：

    1. 先用 UiScrollable.scrollIntoView() 把錨點文字捲到畫面上（錨點文字
       通常才是真正唯一的，這一步順便解決「元件還沒捲到畫面內」的問題，
       不需要另外呼叫 scrollIntoView 兩次）
    2. 錨點現在確定可見，取得它的 Y 座標
    3. 在畫面上所有符合目標 class+文字 的候選裡，挑 Y 座標離錨點最近的一個
       （同一列的元件彼此 Y 座標通常很接近，這比「隨便挑第一個符合的」可靠
       得多——實測發現 _locate_by_scroll_into_view() 在這種情境下會默默
       回傳錯的那一個，不會報錯也不會提示，是這個函式存在的原因）
    """
    nearby_text_list = (el.get("fingerprint") or {}).get("nearby_text") or []
    label = el.get("label", "")
    if not nearby_text_list or not label:
        return None

    anchor_text = nearby_text_list[0]
    anchor_literal = uiautomator_string_literal(anchor_text)
    anchor = None
    for attr in ("text", "description"):
        selector = (
            f'new UiScrollable(new UiSelector().scrollable(true)).scrollIntoView('
            f'new UiSelector().{attr}({anchor_literal}))'
        )
        try:
            anchor = driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, selector)
            break
        except NoSuchElementException:
            continue
        except Exception as e:
            print(f"    [錨點消歧義] 錨點查詢失敗（{attr}）: {e}")
            continue
    if anchor is None:
        return None

    anchor_y = anchor.location["y"]
    literal = xpath_literal(label)
    xpath = f'//{el["class"]}[@text={literal} or @content-desc={literal}]'
    try:
        candidates = driver.find_elements(AppiumBy.XPATH, xpath)
    except Exception:
        return None
    if not candidates:
        return None

    closest = min(candidates, key=lambda c: abs(c.location["y"] - anchor_y))
    print(f"    [錨點消歧義] 用鄰近文字「{anchor_text}」（y={anchor_y}）"
          f"從 {len(candidates)} 個同文字候選中挑出最接近的一個")
    return closest


def locate_generated_element(driver, el: dict):
    """
    無 native id 元件的定位順序：
      1. 先試 XPath（class+文字），查到唯一一個就直接用；查不到才自己
         捲動＋重新查（_locate_by_xpath_with_scroll，見其說明），涵蓋
         「元件單純還沒捲到畫面上」的情況；查到>1個是真的有歧義，不會
         浪費時間再捲，直接進下一層
      2. 換用 nearby_text 當錨點消歧義（_locate_by_anchor，見其說明——
         這是清單裡「每列都有一個文字相同的操作按鈕」這種常見情境的
         正確解法，不能靠純 XPath 解決）
      3. 沒有 nearby_text 可用時，退回 UiScrollable.scrollIntoView()
         （注意：這條路徑在文字不唯一時可能選到錯的元件，只是沒有更好的
         選項時的次佳備援）
      4. 都找不到才回傳 None，呼叫端改用座標
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
        matched = _locate_by_scroll_into_view(driver, el)
        source = "UiScrollable"
    if matched is None:
        return None

    print(f"    [命中元件屬性（{source}）] location={matched.location}  size={matched.size}  "
          f"resource-id={matched.get_attribute('resource-id')!r}  "
          f"text={matched.get_attribute('text')!r}  "
          f"content-desc={matched.get_attribute('content-desc')!r}  "
          f"enabled={matched.get_attribute('enabled')!r}")
    return matched


def _locate_by_custom_xpath(driver, el: dict):
    """
    人工在 viewer.html 上用「重新定義 XPath」功能存下來的自訂查詢
    （el["custom_xpath"]），優先於所有自動推導的定位方式使用——這是
    人工已經在瀏覽器上實測確認過「查到唯一一個」才存的，比自動邏輯
    更值得信任。查不到唯一結果時（例如畫面已經改版）不強行使用，
    回傳 None 讓呼叫端繼續走原本的自動定位鏈。
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


def tap_by_coordinates(driver, bounds: dict):
    center_x = bounds["x"] + bounds["width"] / 2
    center_y = bounds["y"] + bounds["height"] / 2
    print(f"    [座標點擊] x={center_x}, y={center_y}  (來自 element_map.json 的 bounds={bounds})")
    # UiAutomator2 沒有 `mobile: tap`（那是 XCUITest 專用），座標點擊要用 clickGesture
    driver.execute_script("mobile: clickGesture", {"x": center_x, "y": center_y})


def run_input_text(driver, el: dict, value: str) -> str:
    if el["id_source"] != "native":
        return "SKIP（無 native id 的輸入框無法可靠定位，建議回報開發團隊補上 resource-id）"
    try:
        element = driver.find_element(AppiumBy.ID, el["native_id"])
        element.send_keys(value)
        return "PASS"
    except NoSuchElementException:
        return f"FAIL（找不到元件: {el['native_id']}）"


def run_tap(driver, el: dict) -> str:
    custom = _locate_by_custom_xpath(driver, el)
    if custom is not None:
        custom.click()
        return "PASS（人工自訂XPath定位）"

    if el["id_source"] == "native":
        try:
            driver.find_element(AppiumBy.ID, el["native_id"]).click()
            return "PASS（resource-id 定位）"
        except NoSuchElementException:
            return f"FAIL（找不到元件: {el['native_id']}）"

    matched = locate_generated_element(driver, el)
    if matched is not None:
        matched.click()
        return "PASS（XPath / UiScrollable 定位）"

    raise_if_stopped()  # 找元件途中被停止的話，不要再用舊座標盲點
    tap_by_coordinates(driver, el["bounds"])
    return "PASS（座標點擊，備援方式）"


def run_assert_visible(driver, el: dict) -> str:
    custom = _locate_by_custom_xpath(driver, el)
    if custom is not None:
        return "PASS（人工自訂XPath定位）" if custom.is_displayed() else "FAIL（元件存在但不可見）"

    if el["id_source"] == "native":
        try:
            element = driver.find_element(AppiumBy.ID, el["native_id"])
            return "PASS" if element.is_displayed() else "FAIL（元件存在但不可見）"
        except NoSuchElementException:
            return "FAIL（找不到元件）"

    matched = locate_generated_element(driver, el)
    if matched is not None:
        return "PASS（XPath / UiScrollable 定位）" if matched.is_displayed() else "FAIL（元件存在但不可見）"

    return "SKIP（無 native id，XPath 及 UiScrollable 皆查無唯一結果，座標無法驗證「是否可見」）"


def run_assert_text_contains(driver, el: dict, expected_text: str) -> str:
    """
    驗證元件目前顯示的文字內容有沒有包含指定字串，而不是只驗證存不存在/可不可見
    ——assert_visible 做不到這件事。給 LLM 產生的跨畫面測案用（viewer.html「LLM
    測案產生」分頁），例如「設定頁存了名稱後，首頁問候語有沒有真的出現這個名稱」
    這種需要檢查實際文字內容的情境。定位優先序跟其他 run_* 函式一致（自訂XPath→
    native id→XPath/錨點/scrollIntoView）。
    """
    custom = _locate_by_custom_xpath(driver, el)
    target_el, label = custom, "人工自訂XPath定位"

    if target_el is None:
        if el["id_source"] == "native":
            try:
                target_el = driver.find_element(AppiumBy.ID, el["native_id"])
                label = "resource-id 定位"
            except NoSuchElementException:
                return "FAIL（找不到元件）"
        else:
            target_el = locate_generated_element(driver, el)
            label = "XPath / UiScrollable 定位"

    if target_el is None:
        return "SKIP（無 native id，XPath 及 UiScrollable 皆查無唯一結果，無法讀取文字內容）"

    actual_text = (target_el.get_attribute("text") or target_el.get_attribute("content-desc") or "").strip()
    if expected_text in actual_text:
        return f"PASS（{label}，實際文字：「{actual_text}」）"
    return f"FAIL（{label}，預期文字包含「{expected_text}」，實際文字：「{actual_text}」）"


def check_current_screen(driver, screen_name: str, elements: list) -> tuple[str, str]:
    """跟 iOS 版邏輯相同（見 run_test_case.match_screen_in_source），id 在 resource-id，
    文字在 text / content-desc。"""
    return match_screen_in_source(driver.page_source, elements, screen_name,
                                  id_attrs=("resource-id",), label_attrs=("text", "content-desc"))


def verify_current_screen(driver, screen_name: str, element_lookup: dict) -> bool:
    status, message = check_current_screen(driver, screen_name, list(element_lookup.values()))
    print(f"  [畫面校驗] {message}")
    return status != "fail"


def wait_after_action(driver, max_wait: float = 3.0, poll_interval: float = 0.4) -> None:
    """
    tap/input_text 之後不用寫死固定秒數等待，改成輪詢 page_source 直到內容
    連續兩次一致（判定畫面已經穩定）或到時間上限為止——跟
    `capture_screen_map_android.py` 的 `wait_for_screen_stable()` 是同樣精神
    （見該函式說明），差別只在這裡是「執行測案」用，時間上限抓得比「掃描」
    短很多（3秒 vs 掃描的~7.5秒），避免被本來就不會停止變化的動態內容
    （跑馬燈、loading動畫）拖慢太多測案執行時間。

    這個改動是為了修正一個真實存在的風險：原本寫死 `time.sleep(1)`，但
    測試App本身就有轉場畫面刻意設計2秒的loading（`TRANSITION_LOADING_MS`），
    已經超過1秒，如果測案剛好點到會觸發轉場/非同步載入的按鈕，下一步
    可能在畫面還沒完成時就執行，容易誤判失敗或驗證到還沒更新的內容。
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


def run_test_case(driver, test_case: dict, element_lookup: dict):
    results = []
    for i, step in enumerate(test_case["steps"], 1):
        action = step["action"]
        target = step["target"]
        el = element_lookup.get(target)

        if el is None:
            results.append((i, step, f"FAIL（element_map.json 裡找不到 {target}，畫面資料可能已過期，建議重新掃描）"))
            continue

        if action == "input_text":
            outcome = run_input_text(driver, el, step.get("value", ""))
        elif action == "tap":
            outcome = run_tap(driver, el)
        elif action == "assert_visible":
            outcome = retry_assert(lambda: run_assert_visible(driver, el), step.get("timeout"))
        elif action == "assert_text_contains":
            outcome = retry_assert(lambda: run_assert_text_contains(driver, el, step.get("value", "")),
                                   step.get("timeout"))
        else:
            outcome = f"FAIL（未知的 action: {action}）"

        results.append((i, step, outcome))
        print(f"  {i}. [{action}] {target} -> {outcome}")

        if action in ("tap", "input_text"):
            wait_after_action(driver)

    return results


def main():
    parser = argparse.ArgumentParser(description="執行 generate_test_case.py 產生的測試案例（Android）")
    parser.add_argument("--screen_name", required=True)
    args = parser.parse_args()

    test_case = load_test_case(args.screen_name)
    element_lookup = load_element_lookup(args.screen_name)

    print(f"執行畫面「{args.screen_name}」的測試案例，共 {len(test_case['steps'])} 個步驟")

    options = UiAutomator2Options().load_capabilities(ANDROID_DESIRED_CAPS)
    driver = webdriver.Remote(APPIUM_SERVER_URL, options=options)

    try:
        if not verify_current_screen(driver, args.screen_name, element_lookup):
            save_result_screenshot(driver, args.screen_name)
            raise SystemExit(
                f"手機目前畫面看起來不是「{args.screen_name}」，為避免座標點擊備援機制誤觸其他畫面上的"
                f"真實元件，中止執行。請手動導航回正確畫面後再重新執行（已存一張當下截圖到 "
                f"test_results/{args.screen_name}_after.png 方便你確認目前實際在哪個畫面，"
                f"但若這支 App 有 FLAG_SECURE 保護，這張截圖可能是純黑色，無法用來確認畫面）。"
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
