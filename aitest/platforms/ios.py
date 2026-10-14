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

import hashlib
import json
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager

from appium.options.ios import XCUITestOptions
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import InvalidSessionIdException, WebDriverException

from aitest.config import CONFIG, ios_capabilities
from aitest.platforms import locate
from aitest.platforms.base import (
    CHROME_BOTTOM_FRACTION,
    CHROME_TOP_FRACTION,
    MODAL_ACK_ONLY_LABELS,
    MODAL_DISMISS_LABELS,
    MODAL_HINT_MARKERS,
    candidate_pattern,
    histogram_bucket,
    looks_like_data,
    no_idle_wait,
    wait_until_stable,
)
from aitest.platforms.scan_core import identity_keys_in_region, position_bucket, read_and_merge_round

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

    預設 `classes` 是全部可捲動類型（定位時捲容器重查用，見 platforms/locate.py 的
    locate_with_scroll）；橫向卡片收集會傳
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
            # 原始屬性照實記錄，不在這裡用來篩選，留給網頁的元件清單篩選用
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
    except InvalidSessionIdException:
        raise
    except WebDriverException as e:
        print(f"  [提示] 無法設定跳過 visible，照原本方式讀畫面結構（{e}）")
        yield
        return
    try:
        yield
    finally:
        try:
            driver.update_settings({"pageSourceExcludedAttributes": original})
        except InvalidSessionIdException:
            raise
        except WebDriverException as e:
            print(f"  [警告] 恢復 pageSourceExcludedAttributes 失敗，之後掃描前請重新連線（{e}）")


def predicate_literal(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def ios_label_predicate(class_name: str, label: str) -> str:
    """自動定位用的 iOS predicate，跟 `//{class}[@label=...]` 這條 XPath 意思一樣。
    XPath 在 iOS 上每查一次都要先把整份畫面結構序列化傳回來（一次 1~3 秒），
    predicate 直接在手機上比對，所以自動定位一律用這個；使用者存的 custom_xpath
    需要表達結構關係（第幾個 Cell 裡的第幾個元件），predicate 做不到，維持 XPath。"""
    return f"type == {predicate_literal(class_name)} AND label == {predicate_literal(label)}"


def find_anchor(driver, text: str):
    """
    錨點消歧義（platforms/locate.py 的 locate_by_anchor）用：找文字是 text 的唯一一個元件。
    iOS 不用先捲動——元件即使捲出畫面外，Accessibility Tree 依然找得到（見已知問題16），
    找到答案之後呼叫端仍然會用 ensure_visible() 確保真的可以點擊/驗證。
    """
    try:
        anchors = driver.find_elements(AppiumBy.IOS_PREDICATE, f"label == {predicate_literal(text)}")
    except InvalidSessionIdException:
        raise
    except WebDriverException as e:
        print(f"    [錨點消歧義] 錨點查詢失敗: {type(e).__name__}")
        return None
    return anchors[0] if len(anchors) == 1 else None


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
    except InvalidSessionIdException:
        raise
    except WebDriverException as e:
        print(f"    [捲動至可視範圍] 檢查/捲動失敗，照原本流程繼續: {e}")
    return element


_INPUT_TYPES = ("XCUIElementTypeTextField", "XCUIElementTypeSecureTextField",
                "XCUIElementTypeTextView", "XCUIElementTypeSearchField")


def focus_input(ctx, driver, element, timeout: float = 3.0):
    """
    回傳 (真正能打字的輸入框, 是不是點開後才出現的)。元件本身是（或包著）輸入框就直接用；
    不是的話先點一下，再等鍵盤焦點落在某個輸入框上。點了也等不到就回傳 (None, True)
    """
    if element.get_attribute("type") in _INPUT_TYPES:
        return element, False
    inner = element.find_elements(AppiumBy.XPATH, " | ".join(f".//{t}" for t in _INPUT_TYPES))
    if inner:
        return inner[0], False
    element.click()
    deadline = time.time() + timeout
    while True:
        ctx.raise_if_stopped()
        try:
            active = driver.switch_to.active_element
            if active is not None and active.get_attribute("type") in _INPUT_TYPES:
                print(f"    [輸入] 元件不是輸入框，點開後找到取得焦點的輸入框 name={active.get_attribute('name')!r}")
                return active, True
        except InvalidSessionIdException:
            raise
        except WebDriverException:
            pass
        if time.time() >= deadline:
            return None, True
        time.sleep(0.3)


_KEYBOARD_DONE_LABELS = ["完成", "Done"]


def dismiss_keyboard(driver) -> bool:
    """
    打完字收鍵盤，回傳鍵盤是不是收起來了。Android 打完會收，iOS 不收的話鍵盤會蓋住
    下一步要點的按鈕（例如輸入數量後的「下一步」）。
      1. 鍵盤上的「完成」鍵（文字鍵盤的 return 鍵常被改成完成）
      2. 數字鍵盤沒有完成鍵，App 常在鍵盤正上方加一條工具列放「完成」按鈕——它不在鍵盤裡，
         第 1 步找不到。只按緊貼鍵盤上緣的那顆，不會誤按頁面上別的「完成」（例如送出申請）
    """
    def shown() -> bool:
        try:
            return driver.is_keyboard_shown()
        except InvalidSessionIdException:
            raise
        except WebDriverException:
            return False

    if not shown():
        return True
    try:
        driver.execute_script("mobile: hideKeyboard", {"keys": _KEYBOARD_DONE_LABELS})
    except InvalidSessionIdException:
        raise
    except WebDriverException:
        pass  # 鍵盤上沒有這些鍵（數字鍵盤），換下一個方法
    if not shown():
        return True
    try:
        keyboards = driver.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeKeyboard")
        if keyboards:
            keyboard_top = keyboards[0].location["y"]
            labels = ", ".join(predicate_literal(label) for label in _KEYBOARD_DONE_LABELS)
            for button in driver.find_elements(
                    AppiumBy.IOS_PREDICATE, f"type == 'XCUIElementTypeButton' AND label IN {{{labels}}}"):
                bottom = button.location["y"] + button.size["height"]
                if keyboard_top - 60 <= bottom <= keyboard_top + 5:
                    button.click()
                    break
    except InvalidSessionIdException:
        raise
    except WebDriverException as e:
        print(f"    [輸入] 找鍵盤工具列的完成鈕失敗: {e}")
    return not shown()


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
        f"label CONTAINS {predicate_literal(t)}" for t in texts) + ")"

    original_idle = driver.get_settings().get("waitForIdleTimeout")
    driver.update_settings({"waitForIdleTimeout": 0})
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                labels = [e.get_attribute("label") or "" for e in
                          driver.find_elements(AppiumBy.IOS_PREDICATE, predicate)]
            except InvalidSessionIdException:
                raise
            except WebDriverException:
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


# ---------------------------------------------------------------------------
# 爬蟲（crawl_app_tree.py）用：哪些算可點、是不是同一個畫面、怎麼關對話框、怎麼返回
# ---------------------------------------------------------------------------

# 爬蟲會嘗試點擊的 class（其他一律不點）
CRAWL_CLASSES = {
    "XCUIElementTypeButton",
    "XCUIElementTypeCell",
    "XCUIElementTypeSegmentedControl",
}

# 選中的 tab：XCUITest 底下被選中的分頁按鈕 value 會是 "1"
_SELECTED_VALUES = {"1", "true", "selected"}


def skeleton_signature(page_source: str) -> tuple[dict, str]:
    """
    把畫面濃縮成「模板級」骨架 + 一個穩定 hash（screen_uid），爬蟲判斷「這個畫面有沒有看過」用。

    只取這幾類穩定資訊：
      - native_ids：真正的 accessibilityIdentifier（name 存在、name != label、
        且看起來不像資料）
      - class_histogram：每種 class 出現幾個（分桶：0-4 精確，之後 5-9 / 10-19 / 20+）
      - selected_tab：TabBar 裡目前選中的分頁文字
      - nav_title：導覽列標題（非數字 / 日期時才採用）

    只看 visible=true 的節點——爬蟲每次都是「剛導航到、停在最頂部」的一致狀態，
    這個假設成立；如果之後改成爬到一半也重算簽章，要重新評估。
    """
    root = ET.fromstring(page_source)

    native_ids: set[str] = set()
    class_counts: dict[str, int] = {}
    nav_title = None
    selected_tab = None

    for node in root.iter():
        tag = node.tag
        if not tag.startswith("XCUIElementType"):
            continue
        attrib = node.attrib
        if attrib.get("visible", "false") != "true":
            continue

        class_counts[tag] = class_counts.get(tag, 0) + 1

        name = (attrib.get("name") or "").strip()
        label = (attrib.get("label") or "").strip()
        if name and name != label and not looks_like_data(name):
            native_ids.add(name)

        if tag == "XCUIElementTypeNavigationBar":
            title = name or label
            if title and not looks_like_data(title):
                nav_title = title

    for tabbar in root.iter("XCUIElementTypeTabBar"):
        for btn in tabbar.iter("XCUIElementTypeButton"):
            if btn.attrib.get("value") in _SELECTED_VALUES or btn.attrib.get("selected") == "true":
                selected_tab = (btn.attrib.get("label") or btn.attrib.get("name") or "").strip()
                break

    skeleton = {
        "native_ids": sorted(native_ids),
        "class_histogram": {k: histogram_bucket(v) for k, v in sorted(class_counts.items())},
        "selected_tab": selected_tab,
        "nav_title": nav_title,
    }
    canonical = json.dumps(skeleton, sort_keys=True, ensure_ascii=False)
    screen_uid = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
    return skeleton, screen_uid


def _element_identifier(a: dict) -> str:
    """跟 capture_screen_map 產出的識別字串一致：name 是真正的 id 時用 name，
    否則用 label（這樣才比對得到 scan_screen 產出的 candidate）。"""
    name = (a.get("name") or "").strip()
    label = (a.get("label") or "").strip()
    return name if (name and name != label) else label


def interactive_statictext_keys(page_source: str) -> set[tuple[str, str]]:
    """
    找出「看起來可點、但 class 是 StaticText」的元件的 (class, identifier)。

    很多 App（含你好Demo）的分頁列 / 選單項目是 accessible=true 的 StaticText
    包在 eOther 裡，不是 Button/Cell，naive 的 Button/Cell click-crawl 會整個漏掉。
    判定條件：
      - visible=true 且 accessible=true（是獨立的無障礙元素）
      - 文字不像資料（排除報價、百分比、金額、日期）
      - 沒有 Button/Cell 祖先（有的話它只是那個元件的子文字，點父元件就好）
    """
    root = ET.fromstring(page_source)
    parent = {c: p for p in root.iter() for c in p}
    keys: set[tuple[str, str]] = set()
    for node in root.iter():
        if node.tag != "XCUIElementTypeStaticText":
            continue
        a = node.attrib
        if a.get("visible") != "true" or a.get("accessible") != "true":
            continue
        ident = _element_identifier(a)
        if not ident or looks_like_data(ident):
            continue
        p = parent.get(node)
        skip = False
        while p is not None:
            if p.tag in ("XCUIElementTypeButton", "XCUIElementTypeCell"):
                skip = True
                break
            p = parent.get(p)
        if not skip:
            keys.add((node.tag, ident))
    return keys


def persistent_chrome_keys(page_source: str, screen_height: float) -> set[tuple[str, str]]:
    """
    「常駐框架」元件的 (class, label/name)：畫面最上緣 / 最下緣的可點元件（頂部
    導覽列、底部 tab bar / 導覽 Button 列），加上 TabBar 底下的所有元件。

    不管實際 class 是不是 XCUIElementTypeTabBar——你好Demo 這類 App 的底部
    導覽是一排 name 開頭「btn 」的 Button，不是標準 TabBar。用來把常駐框架從
    非根層畫面（或整支子樹）的爬蟲候選裡排掉。
    """
    root = ET.fromstring(page_source)
    keys: set[tuple[str, str]] = set()
    chrome_classes = CRAWL_CLASSES | {"XCUIElementTypeStaticText"}

    for tabbar in root.iter("XCUIElementTypeTabBar"):
        for node in tabbar.iter():
            ident = _element_identifier(node.attrib)
            if node.tag.startswith("XCUIElementType") and ident:
                keys.add((node.tag, ident))

    for node in root.iter():
        if node.tag not in chrome_classes:
            continue
        a = node.attrib
        ident = _element_identifier(a)
        if not ident:
            continue
        try:
            cy = float(a.get("y", 0)) + float(a.get("height", 0)) / 2
        except ValueError:
            continue
        if cy <= screen_height * CHROME_TOP_FRACTION or cy >= screen_height * CHROME_BOTTOM_FRACTION:
            keys.add((node.tag, ident))
    return keys


def _first_button_label(driver, labels: list[str], any_type: bool = False):
    """畫面上第一個文字在 labels 裡的按鈕，回傳 (元件, 文字)；沒有回傳 (None, None)。
    any_type=True：Button 找不到再放寬到任何類型——App 自己畫的彈窗，「下次再說」常常
    只是一段文字（StaticText／Other）不是 Button。Android 版本來就不分類型"""
    label_in = "label IN {" + ", ".join(predicate_literal(label) for label in labels) + "}"
    predicates = [f"type == 'XCUIElementTypeButton' AND {label_in}"] + ([label_in] if any_type else [])
    for predicate in predicates:
        try:
            matches = driver.find_elements(AppiumBy.IOS_PREDICATE, predicate)
            if matches:
                return matches[0], (matches[0].get_attribute("label") or "").strip()
        except InvalidSessionIdException:
            raise
        except WebDriverException:
            pass
    return None, None


def dismiss_modal(driver, max_rounds: int = 3, page_source: str | None = None, any_type: bool = False) -> list[str]:
    """
    偵測並關掉擋在前面的提示對話框（報價連線中斷、root 偵測、行銷推播、
    「我知道了」公告…）。回傳被關掉的按鈕文字清單（記進輸出供人工複核）。

    先點「純關閉」語意的按鈕（我知道了 / 關閉 / 略過…）；找不到、且畫面上
    出現 XCUIElementTypeAlert 時，才點「確定 / OK」這類確認語意的。永遠不點
    NEVER_TAP_LABELS（關閉APP…，兩份按鈕清單裡本來就沒有）。

    效能（兩層）：
      1. 傳 page_source 進來時，先用字串快篩，沒有任何 modal 線索就直接 return，
         不做任何 WDA 呼叫
      2. 就算快篩沒篩掉，找按鈕也是**一條 predicate 把整批候選文字用 IN 查一次**
         （`type == 'XCUIElementTypeButton' AND label IN {X, Y, …}`），不是列舉畫面上
         所有 Button 再逐一 get_attribute("label")——元件數大的畫面後者可能要
         上百次 WDA 往返，前者固定 1-2 次

    any_type：關閉按鈕不限 Button 類型（見 _first_button_label）。跑測案時用（步驟失敗才會
    檢查）；爬蟲每一頁都會檢查，維持只找 Button，免得誤點一般畫面上剛好叫「取消」「關閉」的文字
    """
    if page_source is not None and not any(m in page_source for m in MODAL_HINT_MARKERS):
        return []

    dismissed: list[str] = []
    for _ in range(max_rounds):
        element, target = _first_button_label(driver, MODAL_DISMISS_LABELS, any_type)
        if element is None:
            # 找不到「純關閉」類，只有真的出現 XCUIElementTypeAlert 時才進一步
            # 查「確定/OK」這類確認語意的——避免在一般畫面上把「確定」誤點掉
            try:
                has_alert = bool(driver.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeAlert"))
            except InvalidSessionIdException:
                raise
            except WebDriverException:
                has_alert = False
            if has_alert:
                element, target = _first_button_label(driver, MODAL_ACK_ONLY_LABELS)
        if element is None:
            break
        try:
            element.click()
        except InvalidSessionIdException:
            raise
        except WebDriverException:
            break
        dismissed.append(target or "?")
        wait_until_stable(driver)
    return dismissed


def go_back(driver) -> None:
    """依序試：導覽列返回鍵 → 左上角按鈕 → 原生 back → 從左緣往右滑。
    iOS 沒有系統返回鍵，只能猜；每一種失敗（找不到、點不到）就換下一種"""
    try:
        for nav_bar in driver.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeNavigationBar"):
            buttons = nav_bar.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeButton")
            if buttons:
                buttons[0].click()
                return
    except InvalidSessionIdException:
        raise
    except WebDriverException:
        pass
    try:
        for btn in driver.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeButton"):
            loc = btn.location
            if loc["x"] < 80 and loc["y"] < 120:
                btn.click()
                return
    except InvalidSessionIdException:
        raise
    except WebDriverException:
        pass
    try:
        driver.back()
        return
    except InvalidSessionIdException:
        raise
    except WebDriverException:
        pass
    try:
        size = driver.get_window_size()
        driver.swipe(1, size["height"] // 2, int(size["width"] * 0.9), size["height"] // 2, 300)
    except InvalidSessionIdException:
        raise
    except WebDriverException:
        pass


class IOSScreenAnalysis:
    """一個畫面的 page_source 解析一次，給 crawl_app_tree.crawl_screen 判斷每個元件用"""

    def __init__(self, page_source: str):
        # 「看起來可點的 StaticText」（分頁列 / 選單項目常見，見 interactive_statictext_keys）
        self._interactive_st = interactive_statictext_keys(page_source)

    def is_candidate(self, el: dict) -> bool:
        el_ident = el.get("native_id") or el.get("label", "")
        return el["class"] in CRAWL_CLASSES or (el["class"], el_ident) in self._interactive_st

    def text(self, el: dict) -> str:
        return el.get("label", "")

    def pattern(self, el: dict) -> str:
        return candidate_pattern(el)


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

    def make_options(self, app_id: str | None = None):
        """app_id：連線時順便打開這支 App；沒給就照 config.toml（預設不打開，操作前景的 App）"""
        return XCUITestOptions().load_capabilities(ios_capabilities(app_id))

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
        except InvalidSessionIdException:
            raise
        except WebDriverException:
            return False
        return None

    def element_flags(self, el: dict) -> dict:
        """原始屬性照實寫進 element_map，留給 viewer 篩選用"""
        return {"enabled": el["enabled"], "accessible": el["accessible"]}

    @contextmanager
    def page_source_context(self, driver):
        """跑測案期間（單份、批次、命令列）套用的設定，見 skip_visible_in_page_source、no_idle_wait"""
        with skip_visible_in_page_source(driver), no_idle_wait(driver):
            yield

    # --- 無 native id 元件的定位（流程在 platforms/locate.py）---
    class_text_by = AppiumBy.IOS_PREDICATE
    fallback_label = None  # 沒有最後備援

    def class_text_query(self, class_name: str, label: str) -> str:
        return ios_label_predicate(class_name, label)

    def find_by_class_and_text(self, driver, class_name: str, label: str) -> list:
        return locate.find_by_class_and_text(self, driver, class_name, label)

    def scroll_once(self, driver, bounds: dict, direction: str) -> None:
        """iOS 的 swipe 沒有「有沒有真的捲動」的回傳值，回傳 None 讓呼叫端比對前後畫面"""
        swipe_within_region(driver, bounds, direction)
        time.sleep(0.5)
        return None

    def scrollable_regions(self, xml_source: str) -> list[dict]:
        return find_scrollable_regions(xml_source)

    def find_anchor(self, driver, text: str):
        return find_anchor(driver, text)

    def count_text(self, driver, text: str) -> int:
        """畫面上文字是 text 的元件有幾個（無文字元件的錨點要唯一才能用）"""
        return len(driver.find_elements(AppiumBy.IOS_PREDICATE, f"label == {predicate_literal(text)}"))

    def locate_fallback(self, driver, el: dict):
        return None

    def locate_generated_element(self, ctx, driver, el: dict):
        return locate.locate_generated_element(ctx, self, driver, el)

    def ensure_visible(self, driver, element):
        return ensure_visible(driver, element)

    def tap_by_coordinates(self, driver, bounds: dict):
        center_x = bounds["x"] + bounds["width"] / 2
        center_y = bounds["y"] + bounds["height"] / 2
        print(f"    [座標點擊] x={center_x}, y={center_y}  (來自 element_map.json 的 bounds={bounds})")
        driver.execute_script("mobile: tap", {"x": center_x, "y": center_y})

    def watch_banner(self, ctx, driver, texts: list[str], timeout: float = 10.0) -> tuple[dict, dict, str]:
        return watch_banner(driver, texts, timeout)

    def focus_input(self, ctx, driver, element):
        return focus_input(ctx, driver, element)

    def input_text(self, ctx, driver, element, value: str, submit: bool = False) -> str:
        element.send_keys(value)
        # 字已經打好了，鍵盤收不起來不讓整步 FAIL，但註記出來：下一步被鍵盤擋住時看得出原因
        if not dismiss_keyboard(driver):
            print("    [輸入] 鍵盤收不起來，照原本流程繼續")
            return "PASS（鍵盤收不起來，可能會擋住下一步）"
        return "PASS"

    def prepare_banner_watch(self, ctx, driver) -> None:
        pass  # iOS 的 predicate 查詢看得到所有視窗，不用事先準備

    def start_capture(self, driver) -> bool:
        return False  # XCUITest 錄影需要電腦裝 ffmpeg，目前沒用到 iOS，先不錄

    def stop_capture(self, driver, save_path: str | None) -> str | None:
        return None

    # --- 錄製建樹（core/recorder.py）：iOS 的 page source 本來就包含 App 所有視窗，不用特別處理 ---
    def begin_recording(self, driver) -> None:
        return None

    def end_recording(self, driver, state) -> None:
        return None

    def recording_source(self, page_source: str, state) -> str:
        return page_source

    # --- 批次執行的導航（navigator.py）、爬蟲 ---
    def go_back(self, driver) -> None:
        """iOS 沒有系統返回鍵，用猜的（導覽列返回鍵 → 左上角按鈕 → 原生 back → 左緣右滑）"""
        go_back(driver)

    def app_id(self, driver) -> str | None:
        """呼叫端（批次執行）沒給專案的 bundle id 時的備案：config 指定的 App，沒有就用當下前景的 App"""
        launch = (CONFIG.get("ios") or {}).get("launch_bundle_id")
        if launch:
            return launch
        try:
            info = driver.execute_script("mobile: activeAppInfo")
        except InvalidSessionIdException:
            raise
        except WebDriverException:
            return None
        return (info or {}).get("bundleId")

    def dismiss_dialogs(self, driver) -> list[str]:
        """關掉擋在前面的非預期彈窗，回傳按過的按鈕文字。沿用爬蟲的 dismiss_modal（按鈕清單、
        永遠不點「關閉APP／登出」的規則都在那裡）"""
        # 系統權限視窗不處理（使用者決定）：有些權限框的按鈕是「不允許／好」，
        # 「好」在 dismiss_modal 的確認清單裡，不擋的話會變成幫使用者按了允許
        try:
            if driver.find_elements(AppiumBy.IOS_PREDICATE, _PERMISSION_ALERT_PREDICATE):
                print("    [關閉非預期彈窗] 畫面上是系統權限視窗，不處理")
                return []
        except InvalidSessionIdException:
            raise
        except WebDriverException:
            return []
        return dismiss_modal(driver, any_type=True)

    def element_text(self, element) -> str:
        return (element.get_attribute("label") or element.get_attribute("value") or "").strip()

    # --- 爬蟲（crawl_app_tree.py）---
    def skeleton(self, driver, page_source: str) -> tuple[dict, str]:
        return skeleton_signature(page_source)

    def analyze(self, page_source: str) -> IOSScreenAnalysis:
        return IOSScreenAnalysis(page_source)

    def chrome_keys(self, page_source: str, screen_height: float) -> set:
        return persistent_chrome_keys(page_source, screen_height)

    def dismiss_modal(self, driver, max_rounds: int = 3, page_source: str | None = None) -> list[str]:
        return dismiss_modal(driver, max_rounds, page_source)

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
