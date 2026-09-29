"""
Android（UiAutomator2）平台：執行測案、橋接伺服器用到的 Android 專屬邏輯。

跟 iOS 版（platforms/ios.py）共用同一套測試案例格式跟 element_map.json schema，
差異只在 Appium 定位元件、下點擊指令的方式：

  - native 元件（有 resource-id）：用 AppiumBy.ID，UiAutomator2 底層會用
    resource-id 去查。因為 capture_screen_map_android.py 已經先把「同畫面
    共用同一個 resource-id」的元件降級成 generated（見 TROUBLESHOOTING #26），
    這裡查到的 native 元件保證在畫面上唯一。
  - generated 元件（沒有 native id）：Android 的 accessibility tree 沒有 label
    屬性，改用 @text 或 @content-desc（label 欄位在 capture_screen_map_android.py
    裡就是 content_desc 優先、沒有才用 text，所以查詢時兩個屬性都要試，用 or 一起查）。
  - 座標點擊備援：UiAutomator2 沒有 `mobile: tap`（那是 XCUITest 專用），
    要換成 `mobile: clickGesture`。
  - 畫面外的元件：iOS 元件即使捲出畫面外，Accessibility Tree 依然找得到，
    所以 iOS 是「找到後再捲到可視範圍」；Android 畫面外的元件常常連節點都還沒
    被建出來，找不到，所以要用「邊捲邊找」（UiScrollable.scrollIntoView）。
    兩邊卡住的環節不同，才需要用不同的 API，ensure_visible() 在這裡不用做事。

無 native id 元件的定位順序見 locate_generated_element()。
"""

from contextlib import nullcontext

from appium.options.android import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import NoSuchElementException

from capture_screen_map_android import find_scrollable_regions, parse_bounds, scroll_step
from capture_screen_map_android import scan_screen as android_scan_screen
from run_test_case import raise_if_stopped, xpath_literal
from test_config import ANDROID_DESIRED_CAPS


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


class AndroidPlatform:
    name = "android"
    native_by = AppiumBy.ID
    native_id_name = "resource-id"
    generated_locate_label = "XPath / UiScrollable 定位"
    supports_banner = False
    screen_id_attrs = ("resource-id",)
    screen_label_attrs = ("text", "content-desc")

    xpath_intro = ("你是 Android UI 自動化測試專家。以下是透過 Appium UiAutomator2 取得的\n"
                   "畫面 Accessibility Tree（XML 格式，可以用 XPath 查詢，概念上跟網頁 DOM 類似）。")
    xpath_class_field = "class"
    xpath_text_field = "文字內容（text/content-desc）"
    xpath_nearby_desc = "同層兄弟節點的文字"
    xpath_attr_rule = "Android 的元件屬性用 @text 或 @content-desc 查文字內容"
    xpath_bad_example = "//{class}[.//text=X]"

    def make_options(self):
        return UiAutomator2Options().load_capabilities(ANDROID_DESIRED_CAPS)

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

    def assert_banner(self, driver, expected, timeout: float = 10.0) -> str:
        raise NotImplementedError("assert_banner 目前只有 iOS 版")

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

    def scan(self, driver, screen_name: str, describe: bool, scroll: bool) -> dict:
        return android_scan_screen(driver, screen_name, describe=describe, scroll=scroll, wait_stable=True)
