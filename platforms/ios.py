"""
iOS（XCUITest）平台：執行測案、橋接伺服器用到的 iOS 專屬邏輯。

跟 Android 的關鍵差異：iOS 元件即使捲出目前可視範圍外，Accessibility Tree
依然找得到（節點真的存在），但不在可視範圍內時直接點擊等同無效觸控，所以
找到元件之後、實際點擊/驗證之前，會先呼叫 ensure_visible()：檢查 visible
屬性，不是 true 才用 `mobile: scroll` 帶 `toVisible: True` 捲到可互動範圍
再繼續（見 TROUBLESHOOTING #28）。Android 畫面外的元件常常連節點都還沒被
建出來，要用「邊捲邊找」而不是「找到後再捲」，見 platforms/android.py。

其他差異：native 元件用 AppiumBy.ACCESSIBILITY_ID；文字在 label/value；
XML 節點 tag 本身就是 class，座標是 x/y/width/height 數值屬性；座標點擊用
`mobile: tap`；自動定位用 iOS predicate（比 XPath 快很多，見 ios_label_predicate）。
"""

import time
from contextlib import contextmanager

from appium.options.ios import XCUITestOptions
from appium.webdriver.common.appiumby import AppiumBy

from capture_screen_map import find_scrollable_regions, swipe_within_region
from capture_screen_map import scan_screen as ios_scan_screen
from run_test_case import raise_if_stopped
from test_config import DESIRED_CAPS


@contextmanager
def skip_visible_in_page_source(driver):
    """
    執行測試期間讓 page_source 不計算 visible 屬性，結束後恢復原本設定。

    visible 不是元件現成的屬性，WDA 要逐個元件算「在不在螢幕內、有沒有被蓋住」，
    元件多的畫面（報價清單）這是讀一次 page_source 最花時間的部分之一。執行測試時
    讀 page_source 的地方（等畫面穩定、第0步畫面檢查、位置定位）都只用類型、座標、
    id、文字，用不到 visible。

    一定要恢復：掃描（capture_screen_map.parse_candidate_elements）跟爬蟲會用
    visible 決定收錄哪些元件、哪些能點，viewer_server 跟測試共用同一個連線。
    element.get_attribute("visible")（ensure_visible 用的）不受這個設定影響。
    """
    try:
        original = driver.get_settings().get("pageSourceExcludedAttributes", "")
        driver.update_settings({"pageSourceExcludedAttributes": "visible"})
    except Exception as e:
        print(f"  [提示] 無法設定跳過 visible，照原本方式讀畫面結構（{e}）")
        yield
        return
    try:
        yield
    finally:
        try:
            driver.update_settings({"pageSourceExcludedAttributes": original})
        except Exception as e:
            print(f"  [警告] 恢復 pageSourceExcludedAttributes 失敗，之後掃描前請重新連線（{e}）")


def _predicate_literal(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def ios_label_predicate(class_name: str, label: str) -> str:
    """自動定位用的 iOS predicate，跟 `//{class}[@label=...]` 這條 XPath 意思一樣。
    XPath 在 iOS 上每查一次都要先把整份畫面結構序列化傳回來（一次 1~3 秒），
    predicate 直接在手機上比對，所以自動定位一律用這個；使用者存的 custom_xpath
    需要表達結構關係（第幾個 Cell 裡的第幾個元件），predicate 做不到，維持 XPath。"""
    return f"type == {_predicate_literal(class_name)} AND label == {_predicate_literal(label)}"


def _locate_by_xpath_with_scroll(driver, el: dict, max_rounds: int = 8):
    """
    用 class + label 查精確 XPath，查不到就自己捲動可捲動容器、每輪重新查同一條
    精確 XPath，取代原本只看當下快照、不會捲動的 _locate_by_xpath（已移除）。
    比照 Android 版（platforms/android.py）的同名函式移植。

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

    predicate = ios_label_predicate(el["class"], label)

    def _query():
        try:
            return driver.find_elements(AppiumBy.IOS_PREDICATE, predicate)
        except Exception as e:
            print(f"    [XPath+捲動] 查詢失敗: {predicate}  ({e})")
            return []

    matches = _query()
    print(f"    [XPath+捲動] 第0輪（不捲）: {predicate} -> 查到 {len(matches)} 個")
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return None  # 真的有歧義，交給呼叫端的錨點消歧義處理

    def _scroll_search(region_bounds, region_label):
        previous_source = None
        for round_index in range(1, max_rounds + 1):
            raise_if_stopped()
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
    跟 Android 版（platforms/android.py 的 _locate_by_anchor）同樣的
    錨點消歧義邏輯，處理「清單裡每一列都有一個文字相同的操作按鈕」這種
    純 XPath 查不到唯一結果的情境（見 TROUBLESHOOTING #36）。用 fingerprint
    裡的 nearby_text（同一列裡能唯一辨識這一列的文字）當錨點，在所有符合
    class+label 的候選裡，挑跟錨點 Y 座標最接近的一個。

    跟 Android 版的關鍵差異：iOS 沒有 UiScrollable.scrollIntoView() 這種
    「邊捲邊找」的機制可用，但也不需要——iOS 元件即使捲出畫面外，
    Accessibility Tree 依然找得到（節點真的存在，見已知問題16），所以
    錨點本身直接查詢就查得到，不用先捲動。找到答案之後，呼叫端
    仍然會用 ensure_visible() 確保真的可以點擊/驗證。
    """
    nearby_text_list = (el.get("fingerprint") or {}).get("nearby_text") or []
    label = el.get("label", "")
    if not nearby_text_list or not label:
        return None

    try:
        anchors = driver.find_elements(AppiumBy.IOS_PREDICATE,
                                       f"label == {_predicate_literal(nearby_text_list[0])}")
    except Exception as e:
        print(f"    [錨點消歧義] 錨點查詢失敗: {e}")
        return None
    if len(anchors) != 1:
        return None
    anchor_y = anchors[0].location["y"]

    try:
        candidates = driver.find_elements(AppiumBy.IOS_PREDICATE, ios_label_predicate(el["class"], label))
    except Exception:
        return None
    if not candidates:
        return None

    closest = min(candidates, key=lambda c: abs(c.location["y"] - anchor_y))
    print(f"    [錨點消歧義] 用鄰近文字「{nearby_text_list[0]}」（y={anchor_y}）"
          f"從 {len(candidates)} 個同文字候選中挑出最接近的一個")
    return closest


def locate_generated_element(driver, el: dict):
    """
    無 native id 元件的定位順序：
      1. 先試精確查詢（class + label）；查不到就自己捲動可捲動容器、邊捲邊
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
    見 platforms/android.py 的說明）。但找到不代表能點：不在可視範圍內時
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


class IOSPlatform:
    name = "ios"
    native_by = AppiumBy.ACCESSIBILITY_ID
    native_id_name = "accessibility id"
    generated_locate_label = "XPath 定位"
    supports_banner = True
    # 第0步畫面檢查（run_test_case.match_screen_in_source）：id / 文字放在哪些屬性
    screen_id_attrs = ("name",)
    screen_label_attrs = ("label",)

    # /agent_xpath prompt 裡的平台用語（見 viewer_server._build_agent_xpath_prompt）
    xpath_intro = ("你是 iOS UI 自動化測試專家。以下是透過 Appium XCUITest 取得的\n"
                   "畫面 Accessibility Tree（XML 格式，可以用 XPath 查詢；注意 iOS 這裡 XML 的\n"
                   "tag 名稱本身就是元件的 class，例如 <XCUIElementTypeButton>，不是像網頁那樣\n"
                   "用固定的 tag 加 class 屬性）。")
    xpath_class_field = "class（同時也是XML tag名稱）"
    xpath_text_field = "文字內容（label）"
    xpath_nearby_desc = "同層兄弟節點的 label"
    xpath_attr_rule = ("iOS 的元件屬性用 @label 查文字內容（不是 @text 或 @content-desc，\n"
                       "   那是 Android 的屬性名稱）")
    xpath_bad_example = "//{class}[.//@label=X]"

    def make_options(self):
        return XCUITestOptions().load_capabilities(DESIRED_CAPS)

    def page_source_context(self, driver):
        return skip_visible_in_page_source(driver)

    def locate_generated_element(self, driver, el: dict):
        return locate_generated_element(driver, el)

    def ensure_visible(self, driver, element):
        return ensure_visible(driver, element)

    def tap_by_coordinates(self, driver, bounds: dict):
        center_x = bounds["x"] + bounds["width"] / 2
        center_y = bounds["y"] + bounds["height"] / 2
        print(f"    [座標點擊] x={center_x}, y={center_y}  (來自 element_map.json 的 bounds={bounds})")
        driver.execute_script("mobile: tap", {"x": center_x, "y": center_y})

    def assert_banner(self, driver, expected, timeout: float = 10.0) -> str:
        return run_assert_banner(driver, expected, timeout)

    def element_text(self, element) -> str:
        return (element.get_attribute("label") or element.get_attribute("value") or "").strip()

    # --- page_source XML 節點 ---
    def node_class(self, node) -> str:
        return node.tag

    def node_bounds(self, node) -> dict | None:
        try:
            return {k: float(node.attrib[k]) for k in ("x", "y", "width", "height")}
        except (KeyError, ValueError):
            return None

    def node_text(self, node) -> str:
        return (node.attrib.get("label") or node.attrib.get("value") or "").strip()

    def layout_key(self, node) -> tuple:
        """wait_after_action 判斷版面穩定用（見 run_test_case.layout_signature）：
        文字元件只看 y 跟高度，數字變長變短時寬度跟 x（靠右對齊）會跟著變"""
        a = node.attrib
        if node.tag == "XCUIElementTypeStaticText":
            return (node.tag, a.get("y"), a.get("height"))
        return (node.tag, a.get("x"), a.get("y"), a.get("width"), a.get("height"))

    # --- 橋接伺服器（viewer_server.py）---
    def ancestor_info(self, parent) -> dict | None:
        pa = parent.attrib
        return {"class": parent.tag, "name": pa.get("name", ""), "label": pa.get("label", "")}

    def match_preview(self, m) -> dict:
        return {
            "name": m.get_attribute("name"),
            "label": m.get_attribute("label"),
            "value": m.get_attribute("value"),
        }

    def scan(self, driver, screen_name: str, describe: bool, scroll: bool) -> dict:
        return ios_scan_screen(driver, screen_name, describe=describe, scroll=scroll, wait_stable=True)
