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
import xml.etree.ElementTree as ET
from contextlib import contextmanager

from appium.options.ios import XCUITestOptions
from appium.webdriver.common.appiumby import AppiumBy

from capture_screen_map import identity_keys_in_region, position_bucket, read_and_merge_round
from run_test_case import raise_if_stopped
from test_config import DESIRED_CAPS


# ---------------------------------------------------------------------------
# 掃描（capture_screen_map.py）用：解析、可捲動容器、捲動手勢、橫向容器收集
# ---------------------------------------------------------------------------

SCROLLABLE_CLASSES = {"XCUIElementTypeScrollView", "XCUIElementTypeTable", "XCUIElementTypeCollectionView"}

# 橫向捲動收集時，只把這兩種容器當成「可能是橫向卡片列」的候選，刻意排除
# XCUIElementTypeTable——iOS 的 Table 幾乎都是垂直清單，而且對 Table 的某一列
# 「往左滑」在 iOS 上是系統級的 swipe-to-delete / 滑動操作手勢（會叫出刪除鈕
# 等），不是捲動，硬去橫向滑很容易誤觸真實後果。橫向卡片輪播在 iOS 慣例上是
# 用 ScrollView 或 CollectionView 實作。
HORIZONTAL_SCROLL_CANDIDATE_CLASSES = {"XCUIElementTypeScrollView", "XCUIElementTypeCollectionView"}


def has_scrollable_content(xml_source: str) -> bool:
    """畫面上有沒有可捲動容器（ScrollView/Table/CollectionView），決定要不要
    走「邊捲邊抓」流程。iOS 沒有 Android 那種 scrollable="true" 屬性，只能
    用 class 類型判斷。"""
    root = ET.fromstring(xml_source)
    return any(node.tag in SCROLLABLE_CLASSES for node in root.iter())


def find_scrollable_regions(xml_source: str, classes: set = SCROLLABLE_CLASSES) -> list[dict]:
    """
    找出畫面上所有屬於 `classes` 的可捲動容器節點（class + bounds）。iOS 沒有
    Android 的 scrollable="true" 屬性可讀，只能用 class 類型判斷。

    不在這裡猜測方向——iOS accessibility tree 一樣沒有可靠的方向屬性可讀，
    交給呼叫端用「實際滑一次、看內容有沒有換一批」來判斷（見
    collect_horizontal_region 與 TROUBLESHOOTING.md 問題42 的 iOS 版落差說明：
    Android 靠 mobile: scrollGesture 的布林回傳值，iOS 只能靠內容比對，
    可靠度略低）。

    預設 `classes` 是全部可捲動類型（垂直定位重查用，見本檔案的
    _locate_by_xpath_with_scroll）；橫向卡片收集會傳
    HORIZONTAL_SCROLL_CANDIDATE_CLASSES 進來排除 Table。
    """
    root = ET.fromstring(xml_source)
    regions = []
    for node in root.iter():
        if node.tag not in classes:
            continue
        try:
            x = float(node.attrib.get("x", 0))
            y = float(node.attrib.get("y", 0))
            width = float(node.attrib.get("width", 0))
            height = float(node.attrib.get("height", 0))
        except ValueError:
            continue
        if width <= 0 or height <= 0:
            continue
        regions.append({"class": node.tag, "bounds": {"x": x, "y": y, "width": width, "height": height}})
    return regions


def swipe_within_region(driver, bounds: dict, direction: str,
                        distance_ratio: float = 0.7, duration_ms: int = 500) -> None:
    """
    在指定區域範圍內做一次滑動手勢。`direction` 是「內容捲動方向」（跟 Android
    版 scroll_step 的語意一致）：
      - "left"  → 想看到右邊還沒出現的內容，手指從右往左滑
      - "right" → 往回捲，手指從左往右滑
      - "down"/"up" → 垂直方向（給本檔案的 XPath+捲動定位共用）

    iOS 的 driver.swipe() 走 W3C pointer actions，沒有「這次有沒有真的捲到」的
    回傳值可用（這正是 iOS 版跟 Android 版的已知落差），呼叫端要自己用前後
    內容比對判斷。
    """
    b = bounds
    center_x = int(b["x"] + b["width"] / 2)
    center_y = int(b["y"] + b["height"] / 2)
    margin_x = max(int(b["width"] * 0.1), 5)
    margin_y = max(int(b["height"] * 0.1), 5)

    if direction in ("left", "right"):
        span = max(int((b["width"] - margin_x * 2) * distance_ratio), 1)
        if direction == "left":
            start_x = int(b["x"] + b["width"] - margin_x)
            end_x = start_x - span
        else:
            start_x = int(b["x"] + margin_x)
            end_x = start_x + span
        driver.swipe(start_x, center_y, end_x, center_y, duration_ms)
    else:
        span = max(int((b["height"] - margin_y * 2) * distance_ratio), 1)
        if direction == "down":
            start_y = int(b["y"] + b["height"] - margin_y)
            end_y = start_y - span
        else:
            start_y = int(b["y"] + margin_y)
            end_y = start_y + span
        driver.swipe(center_x, start_y, center_x, end_y, duration_ms)


def parse_candidates_raw(xml_source: str, screen_width: int, screen_height: int) -> list[dict]:
    """
    解析單一快照的 Accessibility Tree，取出所有畫面上真實可見的節點，並附上
    計算 fingerprint 所需的上下文資訊（sibling_index, nearby_text）。

    不再用 class 白名單篩選「值不值得收錄」——只要節點目前可視、座標合法，
    一律收錄，clickable/checkable 這類概念 iOS 沒有對應屬性，但 enabled/
    accessible 會照實記錄，交給 viewer 篩選介面決定要看哪些，掃描端
    不預先幫使用者決定（背景見 TROUBLESHOOTING.md 問題26/29：Android版
    掃描端過早過濾曾造成真實資料遺漏，iOS版比照同一個設計方向調整）。
    """
    root = ET.fromstring(xml_source)
    parent_map = {child: parent for parent in root.iter() for child in parent}

    candidates = []

    for node in root.iter():
        attrib = node.attrib
        class_name = node.tag
        visible = attrib.get("visible", "false") == "true"

        # visible=false 代表這個節點當下真的沒有被畫出來，這一項仍然排除
        # （技術上沒有東西可以疊框/裁切，不是「篩選掉」的判斷）
        if not visible:
            continue

        name = attrib.get("name", "").strip()
        label = attrib.get("label", "").strip()
        value = attrib.get("value", "").strip()

        try:
            x = float(attrib.get("x", 0))
            y = float(attrib.get("y", 0))
            width = float(attrib.get("width", 0))
            height = float(attrib.get("height", 0))
            # 直接讀原始 index 屬性當 sibling_index——實測跟自己重新遍歷兄弟節點
            # 算出來的結果一致（2026-09-21 用真實掃描資料查證過），不用重複計算
            sibling_index = int(attrib.get("index", 0))
        except ValueError:
            continue

        # 找出同層的兄弟節點，計算 nearby_text
        parent = parent_map.get(node)
        nearby_text = []
        if parent is not None:
            siblings = list(parent)
            for sib in siblings:
                if sib is node:
                    continue
                sib_label = sib.attrib.get("label", "").strip()
                sib_name = sib.attrib.get("name", "").strip()
                text = sib_label or sib_name
                if text:
                    nearby_text.append(text)

        # name 跟 label 完全相同時，很可能不是開發者刻意設定的 accessibilityIdentifier，
        # 只是系統把顯示文字回填進 name 欄位（文案一改這個「id」就會跟著變，不穩定），
        # 視為沒有真正的 native id，改走 fingerprint 那條路
        is_real_native_id = bool(name) and name != label

        candidates.append({
            "class": class_name,
            "native_id": name if is_real_native_id else None,
            "label": label,
            "value": value,
            "bounds": {"x": x, "y": y, "width": width, "height": height},
            "sibling_index": sibling_index,
            "nearby_text": nearby_text[:5],  # 最多留5個，避免過長
            "position_bucket": position_bucket(x, y, width, height, screen_width, screen_height),
            # 原始屬性照實記錄，不在這裡用來篩選，留給 viewer.html 篩選介面用
            "enabled": attrib.get("enabled", "true") == "true",
            "accessible": attrib.get("accessible", "false") == "true",
        })

    return candidates


def collect_horizontal_region(platform, driver, region: dict, screen_width: int, screen_height: int,
                               merged: dict, round_screenshots: list,
                               max_rounds: int = 10) -> bool:
    """
    針對畫面上一個巢狀的橫向捲動容器（例如首頁「看盤金剛區」／「推薦專區」橫向
    卡片列）原地左右捲動、反覆讀取合併，邏輯跟 collect_candidates_with_scroll
    對整個畫面做的事一樣，差別只在捲動範圍限定在容器 bounds、方向是 left/right。

    跟 Android 版（platforms/android.py 的同名函式）的關鍵差異：
    Android 靠 `mobile: scrollGesture` 的布林回傳值判斷「這是不是真的橫向容器 /
    有沒有捲到底」；iOS 沒有這個回傳值，改用內容比對：
      - 「是不是真的橫向容器」：先往左滑一次，看容器範圍內露出的元件有沒有
        「換一批」（有舊的離開、也有新的進來）。如果只是多了東西、沒有東西
        離開，很可能是誤觸了 iOS 的 cell 滑動操作（例如叫出某張卡片的功能鈕），
        不是橫向捲動 → 往右滑回去嘗試復原、回傳 False。
      - 「有沒有捲到底」：連續兩輪容器範圍內內容完全一致就停。這個判斷的已知
        弱點跟垂直版一樣（見 collect_candidates_with_scroll 說明）：如果容器裡
        剛好有一張比整個容器還寬的超大卡片，可能誤判到底。

    回傳 True 代表確認是橫向容器且已收集完成；False 代表不是（或無法判斷）。
    這個判斷比 Android 版脆弱，是目前 iOS 版的已知落差，實機驗證時要重點觀察。
    """
    b = region["bounds"]

    *_, raw_before = read_and_merge_round(platform, driver, screen_width, screen_height, merged, round_screenshots)
    keys_before = identity_keys_in_region(raw_before, b)

    swipe_within_region(driver, b, "left")
    time.sleep(0.5)
    raw_after = parse_candidates_raw(driver.page_source, screen_width, screen_height)
    keys_after = identity_keys_in_region(raw_after, b)

    entered = keys_after - keys_before
    departed = keys_before - keys_after
    if not (entered and departed):
        # 沒有「換一批」的跡象：不是橫向捲動容器，或誤觸了 iOS 的 cell 滑動操作。
        # 往右滑一次嘗試復原任何被叫出來的東西，然後放棄。
        swipe_within_region(driver, b, "right")
        time.sleep(0.3)
        return False

    print(f"  [橫向捲動收集] 確認為橫向容器（{region['class']}），開始邊捲邊抓...")
    previous_region_keys = None
    for round_index in range(1, max_rounds):
        new_count, _, _, _, raw = read_and_merge_round(
            platform, driver, screen_width, screen_height, merged, round_screenshots
        )
        region_keys = frozenset(identity_keys_in_region(raw, b))
        print(f"  [橫向捲動收集] 第{round_index}輪：這輪容器內{len(region_keys)}個，"
              f"新增{new_count}個，累積合併{len(merged)}個不重複元件")
        if region_keys == previous_region_keys:
            print(f"  [橫向捲動收集] 連續兩輪容器內內容一致，判定已捲到最右邊，停止")
            break
        previous_region_keys = region_keys
        swipe_within_region(driver, b, "left")
        time.sleep(0.5)
    else:
        print(f"  [警告] 橫向捲動收集達到輪次上限（{max_rounds}輪）仍未捲到底，"
              f"這個容器可能還有內容沒收集完整，建議人工複核")

    # 捲回最左邊，避免影響後續的垂直捲動或其他操作
    previous_source = None
    for _ in range(max_rounds):
        current_source = driver.page_source
        if current_source == previous_source:
            break
        previous_source = current_source
        swipe_within_region(driver, b, "right")
        time.sleep(0.3)

    return True



# ---------------------------------------------------------------------------
# 執行測案（run_test_case.py）用
# ---------------------------------------------------------------------------

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


def watch_banner(driver, texts: list[str], timeout: float = 10.0,
                 poll_interval: float = 0.1) -> tuple[dict, dict, str]:
    """
    盯著畫面等「只出現幾秒就消失」的跑馬燈/通知橫幅（例如下單後上方的「委託回報」），
    回傳 ({每段文字: 抓到它的那個 label，沒抓到是 None}, {每段文字: 第一次看到的時間}, 補充說明)。
    第一次看到的時間給 in_order（要求依序出現）比對先後用。
    PASS/FAIL 的判定、連續好幾個 assert_banner 步驟怎麼合併監看，在
    run_test_case.run_banner_group，這裡只負責「這段時間內看過哪些文字」。

    每段文字只要在 timeout 內「出現過一次」就算數，不用同時在畫面上——
    接連跳出好幾個跑馬燈時，前一個消失了也不會被當成沒出現。

    跟 run_assert_visible 刻意不同的三點（都是實測跑馬燈抓不到的原因）：
      - 用 iOS predicate 查，查詢在手機上完成；XPath 要先把整份 page source
        傳回電腦，一次 1~3 秒，跑馬燈早就消失了
      - 暫時把 waitForIdleTimeout 設 0：跑馬燈是動畫，預設會等 App 靜止才查詢
      - 不看 is_displayed()：實測跑馬燈上的文字 visible 都是 false（XCUITest
        對重疊/動畫中元素的誤判），只要查得到就算出現
    所以這一步必須緊接在觸發它的 tap 之後，中間不能先跑 wait_after_action()
    （它也是一直抓 page source，會把跑馬燈的時間耗掉），執行端已經處理。
    """
    found = {t: None for t in texts}
    seen_at = {}
    predicate = "type == 'XCUIElementTypeStaticText' AND (" + " OR ".join(
        f"label CONTAINS {_predicate_literal(t)}" for t in texts) + ")"

    original_idle = driver.get_settings().get("waitForIdleTimeout")
    driver.update_settings({"waitForIdleTimeout": 0})
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                labels = [e.get_attribute("label") or "" for e in
                          driver.find_elements(AppiumBy.IOS_PREDICATE, predicate)]
            except Exception:
                # 元素剛好在讀屬性時消失（stale），下一輪再查
                labels = []
            now = time.time()
            for t in texts:
                if found[t] is None:
                    found[t] = next((label for label in labels if t in label), None)
                    if found[t] is not None:
                        seen_at[t] = now
            if all(found.values()):
                break
            time.sleep(poll_interval)
    finally:
        if original_idle is not None:
            driver.update_settings({"waitForIdleTimeout": original_idle})
    return found, seen_at, ""


# 系統權限視窗才會有的按鈕（通知、定位、相機、聯絡人、追蹤…都有「不允許」）
_PERMISSION_ALERT_PREDICATE = ("type == 'XCUIElementTypeButton' AND label IN "
                               "{'不允許', 'Don’t Allow', \"Don't Allow\"}")


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

    # --- 掃描（capture_screen_map.py）---
    display_name = "iOS"
    # iOS 的 `mobile: scroll` 沒有「有沒有真的捲動」的回傳值，要靠內容比對判斷到底
    scroll_reports_movement = False

    def parse_candidates_raw(self, xml_source: str, screen_width: int, screen_height: int) -> list[dict]:
        return parse_candidates_raw(xml_source, screen_width, screen_height)

    def has_scrollable_content(self, xml_source: str) -> bool:
        return has_scrollable_content(xml_source)

    def horizontal_scroll_candidates(self, xml_source: str) -> list[dict]:
        return find_scrollable_regions(xml_source, HORIZONTAL_SCROLL_CANDIDATE_CLASSES)

    def collect_horizontal_region(self, driver, region: dict, screen_width: int, screen_height: int,
                                  merged: dict, round_screenshots: list) -> bool:
        return collect_horizontal_region(self, driver, region, screen_width, screen_height,
                                         merged, round_screenshots)

    def scroll_screen(self, driver, screen_width: int, screen_height: int, direction: str):
        """整個畫面捲一頁；沒有回傳值可判斷有沒有捲動，成功回傳 None，指令失敗回傳 False"""
        try:
            driver.execute_script("mobile: scroll", {"direction": direction})
        except Exception:
            return False
        return None

    def element_flags(self, el: dict) -> dict:
        """原始屬性照實寫進 element_map，留給 viewer 篩選用"""
        return {"enabled": el["enabled"], "accessible": el["accessible"]}

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

    def watch_banner(self, driver, texts: list[str], timeout: float = 10.0) -> tuple[dict, dict, str]:
        return watch_banner(driver, texts, timeout)

    def input_text(self, driver, element, value: str) -> str:
        element.send_keys(value)
        return "PASS"

    def dismiss_dialogs(self, driver) -> list[str]:
        """關掉擋在前面的非預期彈窗，回傳按過的按鈕文字。沿用爬蟲的 dismiss_modal（按鈕清單、
        永遠不點「關閉APP／登出」的規則都在那裡）；crawl_app_tree 會 import 這個模組，放這裡 import 避免循環"""
        from crawl_app_tree import dismiss_modal
        # 系統權限視窗不處理（使用者決定）：有些權限框的按鈕是「不允許／好」，
        # 「好」在 dismiss_modal 的確認清單裡，不擋的話會變成幫使用者按了允許
        try:
            if driver.find_elements(AppiumBy.IOS_PREDICATE, _PERMISSION_ALERT_PREDICATE):
                print("    [關閉非預期彈窗] 畫面上是系統權限視窗，不處理")
                return []
        except Exception:
            return []
        return dismiss_modal(driver)

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
