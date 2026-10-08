"""
Android（UiAutomator2）平台：執行測案、橋接伺服器用到的 Android 專屬邏輯。

跟 iOS 版（platforms/ios.py）共用同一套測試案例格式跟 element_map.json schema，
差異只在 Appium 定位元件、下點擊指令的方式：

  - native 元件（有 resource-id）：用 AppiumBy.ID，UiAutomator2 底層會用
    resource-id 去查。因為掃描時已經先把「同畫面
    共用同一個 resource-id」的元件降級成 generated（見 TROUBLESHOOTING #26），
    這裡查到的 native 元件保證在畫面上唯一。
  - generated 元件（沒有 native id）：Android 的 accessibility tree 沒有 label
    屬性，改用 @text 或 @content-desc（label 欄位在掃描時（本檔案的 parse_candidates_raw）
    就是 content_desc 優先、沒有才用 text，所以查詢時兩個屬性都要試，用 or 一起查）。
  - 座標點擊備援：UiAutomator2 沒有 `mobile: tap`（那是 XCUITest 專用），
    要換成 `mobile: clickGesture`。
  - 畫面外的元件：iOS 元件即使捲出畫面外，Accessibility Tree 依然找得到，
    所以 iOS 是「找到後再捲到可視範圍」；Android 畫面外的元件常常連節點都還沒
    被建出來，找不到，所以要用「邊捲邊找」（UiScrollable.scrollIntoView）。
    兩邊卡住的環節不同，才需要用不同的 API，ensure_visible() 在這裡不用做事。

無 native id 元件的定位順序見 locate_generated_element()。
"""

import base64
import re
import time
import xml.etree.ElementTree as ET
from contextlib import nullcontext

from appium.options.android import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import InvalidElementStateException, NoSuchElementException

from capture_screen_map import position_bucket, read_and_merge_round
from run_test_case import raise_if_stopped, xpath_literal
from test_config import android_capabilities


# ---------------------------------------------------------------------------
# 掃描（capture_screen_map.py）用：解析、可捲動容器、捲動手勢、橫向容器收集
#
#   這裡不依 class/clickable/checkable 篩選「值不值得收錄」——只要是真實
#   節點、有可解析的座標，一律收錄，把「要不要看這個元件」的決定權交給
#   viewer 的篩選介面，掃描端不預先幫使用者決定。clickable/checkable/
#   enabled 這些原始屬性會原封不動存進結果，供 viewer 篩選用。
#   （這個決定的背景見 TROUBLESHOOTING.md 問題26/29：掃描端過早過濾已經
#   造成過真實資料遺漏，改成「先收下來，篩選交給檢視工具」更不容易漏資料）
# ---------------------------------------------------------------------------

def parse_bounds(bounds_str: str) -> dict | None:
    """把 "[x1,y1][x2,y2]" 解析成 {x, y, width, height}"""
    match = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds_str or "")
    if not match:
        return None
    x1, y1, x2, y2 = (int(v) for v in match.groups())
    return {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1}


def has_scrollable_content(xml_source: str) -> bool:
    """畫面上有沒有 scrollable="true" 的容器，決定要不要走「邊捲邊抓」流程"""
    root = ET.fromstring(xml_source)
    return any(node.attrib.get("scrollable") == "true" for node in root.iter())


def find_scrollable_regions(xml_source: str) -> list[dict]:
    """
    找出畫面上所有標記 scrollable="true" 的節點（class+bounds），包含外層的
    垂直捲動容器、也包含巢狀在裡面的橫向卡片列表（例如「推薦專區」）在內，
    不在這裡猜測方向——Android accessibility tree 沒有可靠的方向屬性可讀，
    交給呼叫端用實際捲動手勢測試「往右捲會不會動」來判斷（見問題34修正）。
    """
    root = ET.fromstring(xml_source)
    regions = []
    for node in root.iter():
        if node.attrib.get("scrollable") != "true":
            continue
        bounds = parse_bounds(node.attrib.get("bounds", ""))
        if bounds is None:
            continue
        regions.append({"class": node.attrib.get("class", ""), "bounds": bounds})
    return regions


def parse_candidates_raw(xml_source: str, screen_width: int, screen_height: int) -> list[dict]:
    """解析單一快照的候選元件，不做同畫面 resource-id 重複降級（那一步要等
    「邊捲邊抓」的所有輪次合併完才能做一次，見 capture_screen_map.downgrade_duplicate_native_ids）。"""
    root = ET.fromstring(xml_source)
    parent_map = {child: parent for parent in root.iter() for child in parent}

    candidates = []

    # Appium 的 uiautomator2 page_source 是用 class 名稱本身當 XML tag
    # （例如 <android.widget.FrameLayout>），不是固定的 <node> tag，
    # 所以要遍歷所有節點，用 class 屬性判斷，不能只篩特定 tag 名稱
    for node in root.iter():
        attrib = node.attrib
        if "class" not in attrib:  # 跳過 <hierarchy> 這個根節點本身，它不是真的UI元件
            continue

        # displayed=false 代表這個節點當下真的沒有被畫出來（不是「篩選掉」，
        # 是技術上沒有東西可以記錄座標/截圖裁切），這一項仍然排除
        if attrib.get("displayed", "true") == "false":
            continue

        bounds = parse_bounds(attrib.get("bounds", ""))
        if bounds is None:  # 沒有合法座標，沒辦法定位/裁切，技術上無法收錄
            continue

        resource_id = attrib.get("resource-id", "").strip()
        text = attrib.get("text", "").strip()
        content_desc = attrib.get("content-desc", "").strip()
        class_name = attrib.get("class", "")

        # label 優先用 content-desc（開發者刻意給無障礙工具看的描述），
        # 沒有的話退回用畫面顯示文字 text，呼應 iOS 版 label 的角色
        label = content_desc or text

        # resource-id 存在、且不是文字回填（原理跟 iOS 的 name!=label 判斷一樣：
        # 如果 resource-id 剛好等於顯示文字，很可能不是真正刻意設定的識別碼）
        is_real_native_id = bool(resource_id) and resource_id != text and resource_id != content_desc

        parent = parent_map.get(node)
        sibling_index = 0
        nearby_text = []
        if parent is not None:
            siblings = list(parent)
            sibling_index = siblings.index(node)
            for sib in siblings:
                if sib is node:
                    continue
                sib_text = (sib.attrib.get("content-desc") or sib.attrib.get("text") or "").strip()
                if sib_text:
                    nearby_text.append(sib_text)

        candidates.append({
            "class": class_name,
            # 這裡先暫存「候選」native_id，是否真的當作 native 要等下面統計
            # 同一個 (class, resource_id) 在這個畫面上出現幾次之後才能定案
            "native_id": resource_id if is_real_native_id else None,
            "label": label,
            "value": text,
            "bounds": bounds,
            "sibling_index": sibling_index,
            "nearby_text": nearby_text[:5],
            "position_bucket": position_bucket(
                bounds["x"], bounds["y"], bounds["width"], bounds["height"], screen_width, screen_height
            ),
            # 原始屬性照實記錄，不在這裡用來篩選，留給 viewer 篩選介面用
            "clickable": attrib.get("clickable") == "true",
            "checkable": attrib.get("checkable") == "true",
            "enabled": attrib.get("enabled") == "true",
        })

    return candidates


def scroll_step(driver, scroll_area: dict, direction: str, percent: float) -> bool:
    """
    執行一次捲動手勢，回傳這次是否真的造成畫面位移。

    `mobile: scrollGesture` 本身就會回傳布林值——這是 UiAutomator2 系統層級的
    判斷（這次手勢有沒有讓內容真的移動），不是靠比對前後兩次畫面內容去推論，
    比較權威可靠：不會被「捲不動但畫面剛好有動畫元素導致比對誤判成還在變化」
    騙到，也不會被「單一超大元件讓前後兩輪內容看起來一樣，但其實還沒捲到底」
    騙到（這是實際討論過的風險，見 TROUBLESHOOTING.md）。
    """
    return bool(driver.execute_script("mobile: scrollGesture", {
        **scroll_area, "direction": direction, "percent": percent,
    }))


def collect_horizontal_region(platform, driver, region: dict, screen_width: int, screen_height: int,
                               merged: dict, round_screenshots: list[bytes],
                               max_rounds: int = 10, scroll_percent: float = 0.75) -> bool:
    """
    針對畫面上一個巢狀的橫向捲動容器（例如「推薦專區」橫向卡片列表）原地左右
    捲動、反覆讀取合併，邏輯跟 collect_candidates_with_scroll 對整個畫面做的
    事一樣，差別只在捲動範圍限定在這個容器自己的 bounds、方向是 left/right。

    第一次往右捲動如果系統回報「沒有位移」，代表這個容器其實不能橫向捲動
    （很可能是外層那個垂直容器本身也被 scrollable="true" 標記，不是真的橫向
    清單），直接放棄、回傳 False，不繼續也不影響 merged（那一輪讀到的元件已經
    在垂直收集迴圈裡讀過，重複合併不會有副作用）。

    回傳 True 代表這確實是個橫向容器且已收集完成。
    """
    b = region["bounds"]
    # 捲動手勢的操作範圍略縮在容器邊界內，避免手勢起點/終點剛好落在邊界外
    margin_x = max(int(b["width"] * 0.05), 1)
    margin_y = max(int(b["height"] * 0.2), 1)
    scroll_area = {
        "left": b["x"] + margin_x,
        "top": b["y"] + margin_y,
        "width": max(b["width"] - margin_x * 2, 1),
        "height": max(b["height"] - margin_y * 2, 1),
    }

    read_and_merge_round(platform, driver, screen_width, screen_height, merged, round_screenshots)
    if not scroll_step(driver, scroll_area, "right", scroll_percent):
        return False  # 捲不動，這不是一個真的橫向清單

    print(f"  [橫向捲動收集] 偵測到巢狀橫向容器（{region['class']}），開始邊捲邊抓...")
    time.sleep(0.5)
    for round_index in range(1, max_rounds):
        new_count, *_ = read_and_merge_round(platform, driver, screen_width, screen_height, merged, round_screenshots)
        print(f"  [橫向捲動收集] 第{round_index + 1}輪：新增{new_count}個，"
              f"累積合併{len(merged)}個不重複元件")
        if not scroll_step(driver, scroll_area, "right", scroll_percent):
            print(f"  [橫向捲動收集] 系統回報已經無法再往右捲，判定已捲到底，停止")
            break
        time.sleep(0.5)
    else:
        print(f"  [警告] 橫向捲動收集達到輪次上限（{max_rounds}輪）仍未捲到底，"
              f"這個容器可能還有內容沒收集完整，建議人工複核")

    # 捲回最左邊，避免影響後續的垂直捲動或其他操作
    for _ in range(max_rounds):
        if not scroll_step(driver, scroll_area, "left", scroll_percent):
            break
        time.sleep(0.3)

    return True



# ---------------------------------------------------------------------------
# 執行測案（run_test_case.py）用
# ---------------------------------------------------------------------------

def uiautomator_string_literal(value: str) -> str:
    """把字串安全地嵌入 UiSelector 的 Java 字面值語法（跳脫反斜線與雙引號）"""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _text_xpath(class_name: str, label: str) -> str:
    literal = xpath_literal(label)
    return f'//{class_name}[@text={literal} or @content-desc={literal}]'


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

    xpath = _text_xpath(el["class"], label)

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
    try:
        candidates = driver.find_elements(AppiumBy.XPATH, _text_xpath(el["class"], label))
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
         正確解法，不能靠純 XPath 解決，見 TROUBLESHOOTING #36：純
         XPath/UiScrollable 在文字不唯一時會「默默選錯」）
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


_DASHES = str.maketrans({c: "-" for c in "－—–‐‑−"})


def _normalize_banner_text(text: str) -> str:
    """比對跑馬燈文字前先去掉空白、統一各種破折號（全形－、—、– 等），
    避免測案寫「買進-委託處理中」但畫面上是「買進－委託處理中」這種差一個字元就抓不到"""
    return re.sub(r"\s+", "", text or "").translate(_DASHES)


def _texts_in_source(xml_source: str) -> list[str]:
    """page source 裡每個節點的 text/content-desc，外加「同一個父節點底下所有子節點文字接起來」——
    跑馬燈的文字常被拆成好幾個 TextView（例如「買進」＋「-委託處理中」），單看一個節點永遠比對不到"""
    try:
        root = ET.fromstring(xml_source)
    except ET.ParseError:
        return []
    texts = []
    for node in root.iter():
        own = " ".join(v for v in (node.attrib.get("text"), node.attrib.get("content-desc")) if v)
        if own:
            texts.append(own)
        joined = "".join(child.attrib.get("text") or child.attrib.get("content-desc") or "" for child in node)
        if joined:
            texts.append(joined)
    return texts


def _window_count(xml_source: str) -> int:
    """page source 最上層有幾個視窗（開了 enableMultiWindows 才會有多個：狀態列、App、浮動層…）"""
    try:
        return len(ET.fromstring(xml_source))
    except ET.ParseError:
        return 0


def _near_miss_selector(texts: list[str]) -> str | None:
    """預期文字裡每兩個連續的中英數字當片段，組成一個 textMatches 查詢"""
    grams = []
    for t in texts:
        chars = [c for c in t if c.isalnum()]
        grams += ["".join(chars[i:i + 2]) for i in range(len(chars) - 1)]
    grams = list(dict.fromkeys(grams))
    if not grams:
        return None
    return f'new UiSelector().textMatches("(?s).*({"|".join(grams)}).*")'


# prepare_banner_watch() 提早打開 enableMultiWindows 前的原始設定，watch_banner 結束時用它還原
_pre_watch_settings = None


def prepare_banner_watch(driver) -> None:
    """
    觸發跑馬燈的那一步（送出委託）之前先打開 enableMultiWindows。

    實測（錄影對照）：有一次兩個跑馬燈在畫面上各停了 2 秒、5 秒，20 秒內抓了 26 次 page source
    卻一次都沒看到，page source 最多只有 2 個視窗（狀態列＋App），跑馬燈那個視窗整個沒被回報。
    推測是監看開始那一刻才打開 enableMultiWindows，無障礙服務重新設定還沒好、跑馬燈的視窗
    剛好在這段時間建立就沒被追蹤到；所以提早在點擊前打開，讓它有時間準備
    """
    global _pre_watch_settings
    try:
        current = driver.get_settings()
        if _pre_watch_settings is None:
            _pre_watch_settings = {k: current[k] for k in ("enableMultiWindows",) if k in current}
        driver.update_settings({"enableMultiWindows": True})
    except Exception as e:
        print(f"    [跑馬燈] 提早打開 enableMultiWindows 失敗：{type(e).__name__}")


def watch_banner(driver, texts: list[str], timeout: float = 10.0, poll_interval: float = 0.1,
                 slow_source: float = 0.3) -> tuple[dict, dict, str]:
    """
    盯著畫面等跑馬燈/通知橫幅，比照 iOS 版（platforms/ios.watch_banner），回傳
    ({每段文字: 抓到它的那段文字，沒抓到是 None}, {每段文字: 第一次看到的時間}, 補充說明)。每段文字只要在
    timeout 內出現過一次就算數；PASS/FAIL 判定在 run_test_case.run_banner_group。

    Android 對應 iOS 那三點的做法：
      - 用 UiSelector 的 textContains/descriptionContains 查，查詢在手機上完成，
        不用先把整份 page source 傳回電腦（對應 iOS 的 predicate）
      - 暫時把 UiAutomator2 的 waitForIdleTimeout 設 0，不等 App 靜止
      - 不看 is_displayed()，查得到就算出現
    Android 另外要處理的：
      - 系統 Toast 不在一般的畫面結構裡，UiAutomator2 是從 accessibility 事件攔下來、
        只能用 //android.widget.Toast 這條 XPath 查到
      - App 用 PopupWindow／浮動層畫的橫幅是另一個視窗，UiAutomator2 預設只看目前
        取得焦點的視窗，所以暫時打開 enableMultiWindows
      - UiSelector 只能比對單一節點、而且是原字比對，文字被拆成好幾個 TextView 或
        破折號全半形不同就抓不到；所以每隔 source_interval 秒另外抓一次整份 page
        source 做寬鬆比對（見 _texts_in_source、_normalize_banner_text）。
      - **page source 不只是備援**：實測你好證券的「買進-委託處理中」跑馬燈畫在另一個視窗，
        UiSelector（包括 textMatches）完全查不到，只有開了 enableMultiWindows 的 page source
        看得到（它由「買進-委託處理中」「｜」「陳○玲」三個 TextView 組成，比對到的是接起來的
        那串）。跑馬燈只出現兩三秒，所以 page source 每一輪都抓、從一開始就抓；實測一次約
        0.1 秒。只有單次超過 slow_source 秒（畫面很重）才拉長間隔（耗時的 3 倍），免得
        UiAutomator2 一直忙著吐 page source、其他查詢排不進去。
        （曾經改成「前 3 秒不抓、之後每 3 秒一次」，結果第一個跑馬燈每次都錯過）
    有文字沒抓到時，會把最後一次抓到的 page source 存到 test_results/，路徑放在
    補充說明裡，用來確認跑馬燈在畫面結構裡實際長什麼樣子。
    """
    found = {t: None for t in texts}
    seen_at = {}
    wanted = {t: _normalize_banner_text(t) for t in texts}
    # 診斷用：預期文字拆成兩個字的片段（「買進-委託處理中」→ 買進、委託、處理…），監看期間另外記下
    # 「新出現、含這些片段」的文字。沒抓到時列出來，就看得到實際跳的是不是別的字（例如委託失敗）
    near_selector = _near_miss_selector(texts)
    near_baseline, near_misses = None, []
    # 診斷用：監看期間畫面上「新出現」的所有文字（不限相似）＋所有 Toast。委託被拒時 App 可能只跳
    # 「餘額不足」這類完全不含預期字眼的提示，只看相似文字會漏掉。含數字的（報價、數量）一直在跳，排除
    source_baseline, new_texts = None, []

    def note_new(labels):
        for label in labels:
            label = label.strip()
            if (label and not any(ch.isdigit() for ch in label) and label not in new_texts
                    and (source_baseline is None or label not in source_baseline)):
                new_texts.append(label)
    selectors = [f"new UiSelector().{method}({uiautomator_string_literal(t)})"
                 for t in texts for method in ("textContains", "descriptionContains")]

    def read_texts(elements):
        return [" ".join(v for v in (e.get_attribute("text"), e.get_attribute("content-desc")) if v)
                for e in elements]

    def record(labels, when):
        for label in labels:
            normalized = _normalize_banner_text(label)
            for t in texts:
                if found[t] is None and wanted[t] in normalized:
                    found[t] = label
                    seen_at[t] = when

    global _pre_watch_settings
    original = driver.get_settings()
    if _pre_watch_settings is not None:  # 點擊前就打開了，還原成打開之前的值
        original.update(_pre_watch_settings)
        _pre_watch_settings = None
    driver.update_settings({"waitForIdleTimeout": 0, "enableMultiWindows": True})
    window_refreshes, next_refresh_at = 0, None
    last_source = None
    polls, source_times = 0, []
    # 診斷用：抓到幾種不同的畫面結構、裡面最多有幾個視窗。報價頁一直跳價，正常每次都不一樣；
    # 幾乎都一樣代表讀到的畫面結構凍住了（無障礙服務沒更新），跟「跑馬燈的視窗沒被列進來」分得開
    source_hashes, max_windows = set(), 0
    try:
        started = time.time()
        deadline = started + timeout
        next_source_at = started
        while time.time() < deadline:
            polls += 1
            for selector in selectors:
                try:
                    record(read_texts(driver.find_elements(AppiumBy.ANDROID_UIAUTOMATOR, selector)), time.time())
                except Exception:
                    # 元素剛好在讀屬性時消失（stale），下一輪再查
                    pass
            try:
                toasts = read_texts(driver.find_elements(AppiumBy.XPATH, "//android.widget.Toast"))
                record(toasts, time.time())
                note_new(f"Toast：{t.strip()}" for t in toasts if t.strip())
            except Exception:
                pass
            if near_selector and not all(found.values()):
                try:
                    labels = read_texts(driver.find_elements(AppiumBy.ANDROID_UIAUTOMATOR, near_selector))
                except Exception:
                    labels = []
                if near_baseline is None:
                    near_baseline = set(labels)  # 一開始畫面上就有的（買進按鈕、看委託…）不算
                else:
                    near_misses += [l for l in labels if l and l not in near_baseline and l not in near_misses]
            if not all(found.values()) and time.time() >= next_source_at:
                try:
                    # 時間記「開始抓」的那一刻：page source 可能要抓一兩秒，記抓完的時間會讓順序判斷偏晚
                    source_started = time.time()
                    last_source = driver.page_source
                    source_times.append(time.time() - source_started)
                    source_hashes.add(hash(last_source))
                    max_windows = max(max_windows, _window_count(last_source))
                    source_texts = _texts_in_source(last_source)
                    record(source_texts, source_started)
                    if source_baseline is None:
                        source_baseline = set(t.strip() for t in source_texts)
                    else:
                        note_new(source_texts)
                except Exception:
                    pass
                last = source_times[-1] if source_times else 0
                next_source_at = time.time() + (3 * last if last > slow_source else 0)
            if all(found.values()):
                break
            # 監看超過 1 秒還只看到狀態列＋App 兩個視窗：視窗清單可能沒更新（見 prepare_banner_watch），
            # 把 enableMultiWindows 關掉再打開，逼無障礙服務重新回報視窗；之後每 2 秒一次
            now = time.time()
            if max_windows <= 2 and now - started > 1.0 and (next_refresh_at is None or now >= next_refresh_at):
                try:
                    driver.update_settings({"enableMultiWindows": False})
                    driver.update_settings({"enableMultiWindows": True})
                    window_refreshes += 1
                except Exception:
                    pass
                next_refresh_at = now + 2.0
            time.sleep(poll_interval)
    finally:
        driver.update_settings({key: original[key] for key in ("waitForIdleTimeout", "enableMultiWindows")
                                if key in original})

    note = ""
    if not all(found.values()):
        # 診斷用：快查做了幾輪、抓 page source 花多久——抓得很慢、快查輪數很少，就是跑馬燈出現時正在等 page source
        note = (f"快查 {polls} 輪，抓整頁畫面結構 {len(source_times)} 次"
                + (f"（最久 {max(source_times):.1f} 秒、內容不同的有 {len(source_hashes)} 種、"
                   f"最多 {max_windows} 個視窗）" if source_times else "")
                + (f"，重新整理視窗清單 {window_refreshes} 次" if window_refreshes else ""))
        note += ("，監看期間新出現的相似文字：" + "、".join(f"「{l.strip()}」" for l in near_misses[:8])
                 if near_misses else "，監看期間沒有新出現任何相似文字")
        note += ("，畫面上新出現過的文字（不含數字）：" + "、".join(f"「{t[:30]}」" for t in new_texts[:12])
                 if new_texts else "，畫面上沒有新出現過任何文字")
    if last_source and not all(found.values()):
        debug_path = f"test_results/banner_debug_{time.strftime('%Y%m%d_%H%M%S')}.xml"
        try:
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(last_source)
            note += f"，最後一次的畫面結構存在 {debug_path}"
        except OSError:
            pass
    return found, seen_at, note


# 模擬按鍵輸入用的 Android keycode（KeyEvent.KEYCODE_*）。只收帳號密碼常見的字元，
# 中文這類沒有對應按鍵的字元打不出來，遇到就回報 FAIL
_SHIFT = 1  # KeyEvent.META_SHIFT_ON
_KEYCODES = {**{str(d): 7 + d for d in range(10)},
             **{c: 29 + i for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")},
             **{c.upper(): (29 + i, _SHIFT) for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")},
             " ": 62, ".": 56, ",": 55, "-": 69, "=": 70, "/": 76, "@": 77, "+": 81,
             ";": 74, "'": 75, "[": 71, "]": 72, "\\": 73, "`": 68, "*": 17, "#": 18,
             "_": (69, _SHIFT), "!": (8, _SHIFT), "$": (11, _SHIFT), "%": (12, _SHIFT),
             "^": (13, _SHIFT), "&": (14, _SHIFT), "(": (16, _SHIFT), ")": (7, _SHIFT),
             "?": (76, _SHIFT), ":": (74, _SHIFT), "~": (68, _SHIFT)}


def _find_edit_text(element):
    """抓到的是外層容器、旁邊的標籤時，往下找真正的 EditText；本身就是 EditText 就用它"""
    if (element.get_attribute("class") or "").endswith("EditText"):
        return element
    try:
        inner = element.find_elements(AppiumBy.XPATH, ".//android.widget.EditText")
    except Exception:
        inner = []
    return inner[0] if inner else element


def focus_input(driver, element, timeout: float = 3.0):
    """
    回傳 (真正能打字的輸入框, 是不是點開後才出現的)。元件本身是（或包著）EditText 就直接用；
    不是的話（例如顯示股數的 TextView，點了才從底部跳出輸入列）先點一下，再等取得焦點的
    EditText 出現。點了也等不到就回傳 (None, True)
    """
    target = _find_edit_text(element)
    if (target.get_attribute("class") or "").endswith("EditText"):
        return target, False
    element.click()
    deadline = time.time() + timeout
    while True:
        raise_if_stopped()
        focused = driver.find_elements(AppiumBy.XPATH, "//android.widget.EditText[@focused='true']")
        if focused:
            print(f"    [輸入] 元件不是輸入框，點開後找到取得焦點的輸入框 resource-id="
                  f"{focused[0].get_attribute('resource-id')!r}")
            return focused[0], True
        if time.time() >= deadline:
            return None, True
        time.sleep(0.3)


def _submit_input(driver) -> None:
    """按鍵盤的「完成」送出，讓點開後才跳出的輸入列把值寫回畫面並收起來"""
    try:
        driver.execute_script("mobile: performEditorAction", {"action": "done"})
    except Exception as e:
        print(f"    [輸入] 鍵盤「完成」送出失敗，改按 Enter: {e}")
        driver.press_keycode(66)  # KEYCODE_ENTER


# 模擬按鍵的間隔：完全不等的話，輸入框有在打字時做處理（轉大寫、格式化、遮罩）的 App
# 會漏掉中間的字（實際遇過 A123456789 打成 A12789）；比對不符重打時再放慢
_KEY_INTERVAL = 0.15
_SLOW_KEY_INTERVAL = 0.25
_MASK_CHARS = set("*•●·")


def _type_by_keycodes(driver, target, value: str, interval: float) -> None:
    target.click()
    time.sleep(0.3)  # 等輸入框取得焦點、鍵盤出現
    for c in value:
        raise_if_stopped()
        code = _KEYCODES[c]
        keycode, meta = code if isinstance(code, tuple) else (code, 0)
        driver.press_keycode(keycode, metastate=meta or None)
        time.sleep(interval)


def _typed_mismatch(target, value: str) -> str | None:
    """讀回輸入框內容跟要輸入的比對：不一樣回傳實際內容，一樣或沒辦法比對（密碼欄、
    顯示時有遮罩）回傳 None"""
    time.sleep(0.3)  # App 處理輸入（格式化、轉大寫）要一點時間
    try:
        if target.get_attribute("password") == "true":
            return None
        actual = target.get_attribute("text") or ""
    except Exception as e:
        print(f"    [輸入] 讀不回輸入框內容，不比對: {e}")
        return None
    if actual == value or _MASK_CHARS & set(actual):
        return None
    return actual


def _clear_text(driver, target) -> None:
    try:
        target.clear()
        return
    except InvalidElementStateException:
        pass
    # 連清除都被擋：游標移到最後再一直按刪除
    target.click()
    length = len(target.get_attribute("text") or "") + 5
    driver.press_keycode(123)  # KEYCODE_MOVE_END
    for _ in range(length):
        driver.press_keycode(67)  # KEYCODE_DEL


def input_text(driver, element, value: str, submit: bool = False) -> str:
    """
    輸入文字。帳號密碼欄位常常擋掉 UiAutomator2 預設的「直接設定文字」（ACTION_SET_TEXT，
    錯誤是 InvalidElementStateException: Cannot set the element to ...），但還是接受
    一個鍵一個鍵的按鍵事件，所以被擋時改成先點一下取得焦點、再逐字模擬按鍵。
    App 自己畫的安全鍵盤（數字／亂序鍵盤）連按鍵事件都不收的話，這裡也打不進去，會回報 FAIL。
    打完讀回內容比對（密碼欄、有遮罩的沒辦法比，略過），不符就清掉放慢重打一次，還是不符才 FAIL。
    submit=True 時打完按鍵盤的「完成」送出（點開後才跳出的輸入列要送出才會寫回畫面）。
    """
    target = _find_edit_text(element)
    unsupported = sorted({c for c in value if c not in _KEYCODES})
    by_keys = False
    try:
        target.send_keys(value)
        outcome = "PASS"
    except InvalidElementStateException:
        by_keys = True
        if unsupported:
            return f"FAIL（輸入框不接受直接設定文字，改用模擬按鍵也打不出這些字元：{''.join(unsupported)}）"
        _type_by_keycodes(driver, target, value, _KEY_INTERVAL)
        outcome = "PASS（輸入框不接受直接設定文字，改用模擬按鍵逐字輸入）"

    actual = _typed_mismatch(target, value)
    if actual is not None:
        if unsupported:
            return f"FAIL（輸入後欄位內容是「{actual}」跟要輸入的不一樣，有特殊字元沒辦法改用按鍵重打）"
        print(f"    [輸入] 輸入後內容跟預期不一樣（長度 {len(actual)} / {len(value)}），清掉放慢重打一次")
        _clear_text(driver, target)
        _type_by_keycodes(driver, target, value, _SLOW_KEY_INTERVAL)
        by_keys = True
        actual = _typed_mismatch(target, value)
        if actual is not None:
            return f"FAIL（放慢重打後欄位內容還是「{actual}」，跟要輸入的不一樣，可能是 App 會改寫輸入或欄位有長度限制）"
        outcome = "PASS（第一次輸入後內容不對，已清掉放慢逐字重打）"

    if submit:
        _submit_input(driver)
    elif by_keys:
        # 收鍵盤只是收尾（避免擋到下一個欄位），有些 App 的鍵盤收不起來（WebDriverException：
        # The software keyboard cannot be hidden），字已經打好了，不能因為這個讓整步 FAIL
        try:
            if driver.is_keyboard_shown():
                driver.hide_keyboard()
        except Exception as e:
            print(f"    [輸入] 收鍵盤失敗，照原本流程繼續: {e}")
    return outcome


class AndroidPlatform:
    name = "android"
    native_by = AppiumBy.ID
    native_id_name = "resource-id"
    generated_locate_label = "XPath / UiScrollable 定位"
    supports_banner = True
    screen_id_attrs = ("resource-id",)
    screen_label_attrs = ("text", "content-desc")

    xpath_intro = ("你是 Android UI 自動化測試專家。以下是透過 Appium UiAutomator2 取得的\n"
                   "畫面 Accessibility Tree（XML 格式，可以用 XPath 查詢，概念上跟網頁 DOM 類似）。")
    xpath_class_field = "class"
    xpath_text_field = "文字內容（text/content-desc）"
    xpath_nearby_desc = "同層兄弟節點的文字"
    xpath_attr_rule = "Android 的元件屬性用 @text 或 @content-desc 查文字內容"
    xpath_bad_example = "//{class}[.//text=X]"

    def make_options(self, app_id: str | None = None):
        """Android 不指定 App（app_id 不用），接手目前在前景的 App"""
        return UiAutomator2Options().load_capabilities(android_capabilities())

    # --- 掃描（capture_screen_map.py）---
    display_name = "Android"
    # `mobile: scrollGesture` 會回報這次有沒有真的捲動，捲不動就是到底了
    scroll_reports_movement = True
    # 每次捲動畫面高度的75%，刻意設在 1.0 以下讓相鄰兩輪之間有重疊區域，
    # 提高跨輪次去重比對的可靠度
    scroll_percent = 0.75

    def parse_candidates_raw(self, xml_source: str, screen_width: int, screen_height: int) -> list[dict]:
        return parse_candidates_raw(xml_source, screen_width, screen_height)

    def has_scrollable_content(self, xml_source: str) -> bool:
        return has_scrollable_content(xml_source)

    def horizontal_scroll_candidates(self, xml_source: str) -> list[dict]:
        # Android 沒有可靠的方向屬性，每個 scrollable 容器都試著往右捲看看（見 collect_horizontal_region）
        return find_scrollable_regions(xml_source)

    def collect_horizontal_region(self, driver, region: dict, screen_width: int, screen_height: int,
                                  merged: dict, round_screenshots: list) -> bool:
        return collect_horizontal_region(self, driver, region, screen_width, screen_height,
                                         merged, round_screenshots)

    def scroll_screen(self, driver, screen_width: int, screen_height: int, direction: str) -> bool:
        """整個畫面捲一段，回傳系統回報的「這次有沒有真的捲動」"""
        scroll_area = {
            "left": int(screen_width * 0.1),
            "top": int(screen_height * 0.15),
            "width": int(screen_width * 0.8),
            "height": int(screen_height * 0.7),
        }
        return scroll_step(driver, scroll_area, direction, self.scroll_percent)

    def element_flags(self, el: dict) -> dict:
        """原始屬性照實寫進 element_map，留給 viewer 篩選用"""
        return {"clickable": el["clickable"], "checkable": el["checkable"], "enabled": el["enabled"]}

    def page_source_context(self, driver):
        return nullcontext()

    def locate_generated_element(self, driver, el: dict):
        return locate_generated_element(driver, el)

    def ensure_visible(self, driver, element):
        # 定位時已經用「邊捲邊找」把元件捲進畫面，找到的就是可互動的（見模組說明）
        return element

    def tap_by_coordinates(self, driver, bounds: dict):
        center_x = bounds["x"] + bounds["width"] / 2
        center_y = bounds["y"] + bounds["height"] / 2
        print(f"    [座標點擊] x={center_x}, y={center_y}  (來自 element_map.json 的 bounds={bounds})")
        driver.execute_script("mobile: clickGesture", {"x": center_x, "y": center_y})

    def watch_banner(self, driver, texts: list[str], timeout: float = 10.0) -> tuple[dict, dict, str]:
        return watch_banner(driver, texts, timeout)

    def prepare_banner_watch(self, driver) -> None:
        prepare_banner_watch(driver)

    def focus_input(self, driver, element):
        return focus_input(driver, element)

    def input_text(self, driver, element, value: str, submit: bool = False) -> str:
        return input_text(driver, element, value, submit)

    # --- 跑馬燈監看的錄影（除錯用，見 run_test_case.run_banner_group）---
    def start_capture(self, driver) -> bool:
        """開始錄手機畫面（UiAutomator2 內建，底層是 adb screenrecord，不用另外安裝）"""
        try:
            driver.start_recording_screen(timeLimit=180, bitRate=4000000, forceRestart=True)
            return True
        except Exception as e:
            print(f"    [錄影] 開始錄影失敗，這次不錄：{type(e).__name__}：{e}")
            return False

    def stop_capture(self, driver, save_path: str | None) -> str | None:
        """停止錄影；save_path 有給才存成 mp4（PASS 時不留檔），回傳存好的路徑"""
        try:
            data = driver.stop_recording_screen()
        except Exception as e:
            print(f"    [錄影] 停止錄影失敗：{type(e).__name__}：{e}")
            return None
        if not save_path or not data:
            return None
        with open(save_path, "wb") as f:
            f.write(base64.b64decode(data))
        return save_path

    # --- 批次執行的導航（navigator.py）---
    def go_back(self, driver) -> None:
        driver.back()

    def app_id(self, driver) -> str | None:
        """capabilities 沒指定 appPackage（接上已經開著的 App），用當下前景的 App"""
        return driver.current_package

    def dismiss_dialogs(self, driver) -> list[str]:
        """關掉擋在前面的非預期彈窗，回傳按過的按鈕文字。沿用爬蟲的 android_dismiss_modal（按鈕清單、
        永遠不點「關閉APP／登出」的規則都在那裡）；crawl_app_tree 會 import 這個模組，放這裡 import 避免循環"""
        from crawl_app_tree import android_dismiss_modal
        # 系統權限視窗不處理（使用者決定）：權限框是系統的 permissioncontroller／packageinstaller
        # 跳出來的，出現時目前前景的 package 就是它
        try:
            package = driver.current_package or ""
        except Exception:
            return []
        if "permissioncontroller" in package or "packageinstaller" in package:
            print(f"    [關閉非預期彈窗] 畫面上是系統權限視窗（{package}），不處理")
            return []
        return android_dismiss_modal(driver)

    def element_text(self, element) -> str:
        return (element.get_attribute("text") or element.get_attribute("content-desc") or "").strip()

    # --- page_source XML 節點 ---
    def node_class(self, node) -> str:
        return node.attrib.get("class", "")

    def node_bounds(self, node) -> dict | None:
        return parse_bounds(node.attrib.get("bounds", ""))

    def node_text(self, node) -> str:
        return (node.attrib.get("text") or node.attrib.get("content-desc") or "").strip()

    def layout_key(self, node) -> tuple:
        """比照 iOS：文字元件只看 y 跟高度，數字變長變短時寬度會跟著變"""
        cls = node.attrib.get("class", "")
        bounds = parse_bounds(node.attrib.get("bounds", ""))
        if cls.endswith("TextView") and bounds is not None:
            return (cls, bounds["y"], bounds["height"])
        return (cls, node.attrib.get("bounds"))

    # --- 橋接伺服器（viewer_server.py）---
    def ancestor_info(self, parent) -> dict | None:
        pa = parent.attrib
        if "class" not in pa:
            return None
        return {
            "class": pa.get("class", ""),
            "resource_id": pa.get("resource-id", ""),
            "text": pa.get("text", ""),
            "content_desc": pa.get("content-desc", ""),
        }

    def match_preview(self, m) -> dict:
        return {
            "resource_id": m.get_attribute("resource-id"),
            "text": m.get_attribute("text"),
            "content_desc": m.get_attribute("content-desc"),
        }
