"""
全自動爬蟲（樹狀版）：從目前畫面（子樹根）開始，遞迴點擊所有可互動元件、
邊爬邊掃描，產生一份「畫面樹 + 完整導覽圖」的地圖，每個畫面都能一路追回
根節點。輸出給後續「LLM 產生跨畫面測案」用。

跟舊 crawl_app.py 的差異（7 項）：
  1. 停在 App 邊界：點擊後 App 跑到背景 / context 變 WEBVIEW，就 activate_app
     拉回、把該元件標記 skipped、不遞迴。另外預先跳過 class 是 Link、或文字
     含「客服專線/撥打/0800…」或含電話號碼的元件。
  2. 被登出偵測：任何一次掃描 / 點擊後，page_source 出現「重新登入 / 已登出 /
     閒置過久 / 連線逾時 …」等字樣，就丟 CrawlAborted 中止整個爬蟲，
     **不會**把那個登出畫面存進地圖（避免像 TROUBLESHOOTING #19 那樣汙染資料）。
  3. 截圖保護降級：交給 capture_screen_map.scan_screen 內建的 is_screenshot_blocked，
     被擋掉就走純文字描述、不裁切。
  4. 模板級骨架簽章（skeleton_signature）：判斷「這兩次看到的是不是同一個畫面」
     時只看 native_id 集合 + class 種類與數量 + 選中的 tab + 導覽列標題（非數字/
     日期），忽略會變動的資料（報價、金額、時間）。台積電詳情 vs 聯發科詳情
     → 同一個 screen_uid，樹上一個節點。
  5. 常駐 chrome 排除：TabBar 只在樹根（depth 0）展開一次當 level-1 子節點；
     其他畫面掃描時 TabBar 元件照記進 elements[]，但不當爬蟲候選（否則深層
     畫面亂點 tab 會產生假的父子邊）。
  6. 樹 + 圖資料：每個畫面記 parent_screen_uid / entry_element_uid / depth /
     path（從根到當前每一跳的麵包屑），另外一份 edges[] 記完整圖
     （kind = tree / cross-link / external / noop）。
  7. 預設不啟用風險關鍵字黑名單（--safe_keywords 開啟）。除了第 1 項的 App 外
     功能，其他元件全點。

輸出：app_map_tree.json（獨立檔，不動 element_map.json）。每掃完一個畫面就
立刻整份重寫一次，中途中止也不會遺失已爬到的部分。

使用方式：
  1. 手動把 App 導航到子樹根畫面（例如某個 tab 的 landing），保持登入、螢幕解鎖
  2. python crawl_app_tree.py --root_name cathaysec_accounting --max_depth 3 --max_screens 40
     先 dry-run 看會點什麼：加 --dry_run（只掃根畫面、列候選、不點）

注意：預設 scan 時關閉「邊捲邊抓」（scroll=False），只掃每個畫面最頂部一屏，
因為爬完要立刻用 bounds 去點，捲動收集到的畫面外元件座標點不到。捲動內容的
完整收集之後再單獨處理。
"""

import argparse
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from appium import webdriver
from appium.options.ios import XCUITestOptions
from appium.webdriver.common.appiumby import AppiumBy

from capture_screen_map import scan_screen
from platforms.ios import _predicate_literal, ios_label_predicate
from test_config import APPIUM_SERVER_URL, DESIRED_CAPS

OUTPUT_PATH = Path("app_map_tree.json")

# 爬蟲會嘗試點擊的 class（其他一律不點）
CRAWL_CLASSES = {
    "XCUIElementTypeButton",
    "XCUIElementTypeCell",
    "XCUIElementTypeSegmentedControl",
}

APP_STATE_FOREGROUND = 4

# 只出現在「登出 / session 逾時」畫面的字樣（刻意不放單獨的「登出」，避免把
# 設定頁的登出按鈕誤判成已被登出）
LOGOUT_PHRASES = [
    "請重新登入", "重新登入", "已登出", "登出成功", "登入逾時", "連線逾時",
    "閒置過久", "太久沒有操作", "太久沒操作", "工作階段已逾時", "工作階段逾時",
    "帳號已於其他裝置", "帳號已在其他裝置", "錯誤代碼 003", "錯誤代碼003",
    "您已閒置", "重新登錄",
]

# 會用到「App 外功能」的線索：撥號、外部帳號等
_EXTERNAL_HINT_RE = re.compile(
    r"客服專線|服務專線|撥打|來電|電話客服|免付費|0800|市話|門號|撥號|"
    r"whatsapp|line\s*官方|官方帳號|加入好友|前往.*瀏覽器",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"0\d{1,3}[-\s]?\d{3,4}[-\s]?\d{3,4}")

# 骨架簽章裡，判斷一段文字「是不是填進版型的資料」（會隨資料變動）
_DATA_LABEL_RE = re.compile(
    r"^\s*(?:"
    r"[-+]?N?T?\$?\s?[\d,]+(?:\.\d+)?%?"       # 金額 / 數字 / 百分比
    r"|\d{1,4}[/\-.]\d{1,2}(?:[/\-.]\d{1,2})?"  # 日期
    r"|\d{1,2}:\d{2}(?::\d{2})?"                # 時間
    r"|[-–—]{1,}"                               # 佔位符 "--"
    r")\s*$"
)

# 選中的 tab：XCUITest 底下被選中的分頁按鈕 value 會是 "1"
_SELECTED_VALUES = {"1", "true", "selected"}

# 擋在前面的提示對話框，優先點這類「純關閉」語意的按鈕
MODAL_DISMISS_LABELS = [
    "我知道了", "知道了", "我已詳閱", "同意並繼續", "繼續使用", "關閉",
    "略過", "跳過", "下次再說", "稍後再說", "稍後提醒我", "稍後", "以後再說",
    "不再顯示", "取消",
]
# 找不到上面那些、且對話框看起來就是純資訊（按鈕很少）時，才點這類「確認」語意的
MODAL_ACK_ONLY_LABELS = ["確定", "確認", "好", "OK", "我瞭解"]
# 永遠不點（會關掉整個 App / 登出）
NEVER_TAP_LABELS = {
    "關閉APP", "關閉 APP", "退出APP", "退出 APP", "離開APP", "結束App",
    "結束 App", "登出", "確定登出",
}

# 底部導覽 / 頂部導覽列這類「常駐框架」：畫面最上緣 / 最下緣的可點元件，
# 不管實際 class 是不是 XCUIElementTypeTabBar（很多 App 的底部導覽是一排 Button）
CHROME_TOP_FRACTION = 0.11
CHROME_BOTTOM_FRACTION = 0.88


class CrawlAborted(Exception):
    """爬到一半被登出、或無法從 App 外拉回，中止整個爬蟲（已爬的部分保留）。"""


# ---------------------------------------------------------------------------
# 骨架簽章（#4）
# ---------------------------------------------------------------------------

def _looks_like_data(text: str) -> bool:
    if not text:
        return True
    t = text.strip()
    if _DATA_LABEL_RE.match(t):
        return True
    # 內含較長數字串（12,345.67）且整體不長 -> 多半是金額 / 數量
    if len(t) <= 16 and re.search(r"\d[\d,]{2,}(?:\.\d+)?", t):
        return True
    return False


def _histogram_bucket(n: int) -> str:
    if n <= 4:
        return str(n)
    if n <= 9:
        return "5-9"
    if n <= 19:
        return "10-19"
    return "20+"


def skeleton_signature(page_source: str) -> tuple[dict, str]:
    """
    把畫面濃縮成「模板級」骨架 + 一個穩定 hash（screen_uid）。

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
        if name and name != label and not _looks_like_data(name):
            native_ids.add(name)

        if tag == "XCUIElementTypeNavigationBar":
            title = name or label
            if title and not _looks_like_data(title):
                nav_title = title

    for tabbar in root.iter("XCUIElementTypeTabBar"):
        for btn in tabbar.iter("XCUIElementTypeButton"):
            if btn.attrib.get("value") in _SELECTED_VALUES or btn.attrib.get("selected") == "true":
                selected_tab = (btn.attrib.get("label") or btn.attrib.get("name") or "").strip()
                break

    skeleton = {
        "native_ids": sorted(native_ids),
        "class_histogram": {k: _histogram_bucket(v) for k, v in sorted(class_counts.items())},
        "selected_tab": selected_tab,
        "nav_title": nav_title,
    }
    canonical = json.dumps(skeleton, sort_keys=True, ensure_ascii=False)
    screen_uid = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
    return skeleton, screen_uid


# ---------------------------------------------------------------------------
# 安全 / 邊界判斷（#1 #2 #5）
# ---------------------------------------------------------------------------

def detect_logged_out(page_source: str) -> str | None:
    for phrase in LOGOUT_PHRASES:
        if phrase in page_source:
            return phrase
    return None


def looks_external(el: dict) -> bool:
    if el.get("class") == "XCUIElementTypeLink":
        return True
    text = f"{el.get('label', '')} {el.get('value', '')} {el.get('description', '')}"
    return bool(_EXTERNAL_HINT_RE.search(text) or _PHONE_RE.search(text))


def went_outside_app(driver, bundle_id: str) -> str | None:
    try:
        state = driver.query_app_state(bundle_id)
        if state != APP_STATE_FOREGROUND:
            return f"app_state={state}"
    except Exception:
        pass
    try:
        ctx = driver.current_context
        if ctx and ctx != "NATIVE_APP":
            return f"context={ctx}"
    except Exception:
        pass
    return None


def _element_identifier(a: dict) -> str:
    """跟 capture_screen_map 產出的識別字串一致：name 是真正的 id 時用 name，
    否則用 label（這樣才比對得到 scan_screen 產出的 candidate）。"""
    name = (a.get("name") or "").strip()
    label = (a.get("label") or "").strip()
    return name if (name and name != label) else label


_INDEX_SUFFIX_RE = re.compile(r"(?:[_\-]?\d+)+$")


def _candidate_pattern(el: dict) -> str:
    """
    把清單類元件的 native_id 去掉結尾的索引數字，得到「同一批」的 pattern，
    例如 MBKGridQuote_QuoteDataCell_0_12 -> MBKGridQuote_QuoteDataCell。
    用於同模板取樣（見 crawl_screen）。沒有 native_id 的回傳 ""（不取樣）。
    """
    nid = (el.get("native_id") or "").strip()
    if not nid:
        return ""
    stripped = _INDEX_SUFFIX_RE.sub("", nid)
    return stripped if stripped and stripped != nid else ""


def interactive_statictext_keys(page_source: str) -> set[tuple[str, str]]:
    """
    找出「看起來可點、但 class 是 StaticText」的元件的 (class, identifier)。

    很多 App（含樹精靈Demo）的分頁列 / 選單項目是 accessible=true 的 StaticText
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
        if not ident or _looks_like_data(ident):
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

    不管實際 class 是不是 XCUIElementTypeTabBar——樹精靈Demo 這類 App 的底部
    導覽是一排 name 開頭「btn 」的 Button，不是標準 TabBar。用來把常駐框架從
    非根層畫面（或整支子樹）的爬蟲候選裡排掉（#5）。
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


# 出現這些字樣才值得花時間去查一次關閉鈕（效能：絕大多數 tap 後沒有 modal，
# 快篩要挑「畫面內容不太可能自然出現」的詞——刻意不放 MODAL_DISMISS_LABELS /
# MODAL_ACK_ONLY_LABELS 整包進來，那裡面「確定/取消/更新/版本」這種單詞在資料量大
# 的真實畫面上（實測 1000+ 元件的報價/清單頁）到處都會出現，快篩形同虛設）
_MODAL_HINT_MARKERS = (
    "XCUIElementTypeAlert", "連線中斷", "偵測到系統", "請確認是否", "提醒您",
    "溫馨提醒", "重要通知", "我知道了", "繼續使用", "我已詳閱", "同意並繼續",
    "公告",
)


def dismiss_modal(driver, max_rounds: int = 3, page_source: str | None = None) -> list[str]:
    """
    偵測並關掉擋在前面的提示對話框（報價連線中斷、root 偵測、行銷推播、
    「我知道了」公告…）。回傳被關掉的按鈕文字清單（記進輸出供人工複核）。

    先點「純關閉」語意的按鈕（我知道了 / 關閉 / 略過…）；找不到、且畫面上
    出現 XCUIElementTypeAlert 時，才點「確定 / OK」這類確認語意的。永遠不點
    NEVER_TAP_LABELS（關閉APP…）。

    效能（兩層）：
      1. 傳 page_source 進來時，先用字串快篩，沒有任何 modal 線索就直接 return，
         不做任何 WDA 呼叫
      2. 就算快篩沒篩掉，找按鈕也是**一條 predicate 把整批候選文字用 IN 查一次**
         （`type == 'XCUIElementTypeButton' AND label IN {X, Y, …}`），不是列舉畫面上
         所有 Button 再逐一 get_attribute("label")——元件數大的畫面後者可能要
         上百次 WDA 往返，前者固定 1-2 次
    """
    if page_source is not None and not any(m in page_source for m in _MODAL_HINT_MARKERS):
        return []

    dismiss_predicate = "type == 'XCUIElementTypeButton' AND label IN {" + ", ".join(
        _predicate_literal(l) for l in MODAL_DISMISS_LABELS) + "}"

    dismissed: list[str] = []
    for _ in range(max_rounds):
        try:
            matches = driver.find_elements(AppiumBy.IOS_PREDICATE, dismiss_predicate)
        except Exception:
            matches = []
        element, target = (matches[0], None) if matches else (None, None)
        if element is not None:
            try:
                target = (element.get_attribute("label") or "").strip()
            except Exception:
                element = None

        if element is None:
            # 找不到「純關閉」類，只有真的出現 XCUIElementTypeAlert 時才進一步
            # 查「確定/OK」這類確認語意的——避免在一般畫面上把「確定」誤點掉
            try:
                has_alert = bool(driver.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeAlert"))
            except Exception:
                has_alert = False
            if has_alert:
                ack_predicate = "type == 'XCUIElementTypeButton' AND label IN {" + ", ".join(
                    _predicate_literal(l) for l in MODAL_ACK_ONLY_LABELS) + "}"
                try:
                    ack_matches = driver.find_elements(AppiumBy.IOS_PREDICATE, ack_predicate)
                except Exception:
                    ack_matches = []
                if ack_matches:
                    element = ack_matches[0]
                    try:
                        target = (element.get_attribute("label") or "").strip()
                    except Exception:
                        element = None
        if element is None:
            break
        try:
            element.click()
            dismissed.append(target or "?")
            wait_stable(driver)
        except Exception:
            break
    return dismissed


# ---------------------------------------------------------------------------
# 操作：等待穩定 / 點擊 / 返回
# ---------------------------------------------------------------------------

def wait_stable(driver, max_wait: float = 3.0, poll: float = 0.4) -> None:
    """輪詢直到連續兩次 page_source 一致，或耗掉 max_wait（用真實流逝時間量測，
    不是用輪詢次數估計——page_source 在元件多的畫面上本身就可能要好幾秒，
    用次數估計會讓實際等待時間遠超 max_wait）。

    報價/即時數據類畫面的內容本質上一直在變，這裡永遠不會提早判定穩定，
    只會等滿 max_wait 才放棄——這是預期行為，靠 max_wait 本身把等待時間界定住。
    """
    previous = None
    start = time.monotonic()
    while time.monotonic() - start < max_wait:
        try:
            current = driver.page_source
        except Exception:
            return
        if current == previous:
            return
        previous = current
        time.sleep(poll)


def tap_candidate(driver, el: dict, screen_height: float) -> bool:
    """重新定位再點（避免用 scan 當下的舊座標）；定位不到才退回座標，且僅當
    座標落在畫面內。回傳是否真的點了。"""
    if el.get("native_id"):
        try:
            driver.find_element(AppiumBy.ACCESSIBILITY_ID, el["native_id"]).click()
            return True
        except Exception:
            pass

    label = el.get("label", "")
    if label:
        try:
            matches = driver.find_elements(AppiumBy.IOS_PREDICATE, ios_label_predicate(el["class"], label))
            if len(matches) == 1:
                matches[0].click()
                return True
        except Exception:
            pass

    b = el["bounds"]
    center_y = b["y"] + b["height"] / 2
    if 0 <= center_y <= screen_height:
        driver.execute_script(
            "mobile: tap", {"x": b["x"] + b["width"] / 2, "y": center_y}
        )
        return True
    return False


def go_back(driver) -> None:
    """依序試：導覽列返回鍵 → 左上角按鈕 → 原生 back → 從左緣往右滑。"""
    try:
        for nav_bar in driver.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeNavigationBar"):
            buttons = nav_bar.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeButton")
            if buttons:
                buttons[0].click()
                return
    except Exception:
        pass
    try:
        for btn in driver.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeButton"):
            loc = btn.location
            if loc["x"] < 80 and loc["y"] < 120:
                btn.click()
                return
    except Exception:
        pass
    try:
        driver.back()
        return
    except Exception:
        pass
    try:
        size = driver.get_window_size()
        driver.swipe(1, size["height"] // 2, int(size["width"] * 0.9), size["height"] // 2, 300)
    except Exception:
        pass


def _find_element_by_uid(state: dict, screen_uid: str, element_uid: str) -> dict | None:
    screen = state["screens"].get(screen_uid)
    if not screen:
        return None
    for el in screen["elements"]:
        if el["unique_id"] == element_uid:
            return el
    return None


def _find_root_noop_anchors(state: dict, root_uid: str) -> list[dict]:
    """從根畫面「點了骨架沒變」的候選（noop edge）裡挑出元件，當復原錨點。

    這些元件已經被爬蟲自己驗證過屬於某種「持續存在、點了頂多維持原地」的
    控制項（分頁條/顯示模式切換鈕這類常見情況），不是憑空猜的，也不是
    針對特定App寫死的規則——是從這次爬蟲自己記錄的資料反推出來的，換一支
    App一樣適用，不需要為每支App另外調整。
    """
    anchors = []
    for edge in state["edges"]:
        if edge["from"] == root_uid and edge["kind"] == "noop":
            el = _find_element_by_uid(state, root_uid, edge["via"])
            if el:
                anchors.append(el)
    return anchors


def reset_and_replay(driver, path: list, state: dict, root_uid: str) -> bool:
    """go_back() 猜測性重試都失敗時的最後手段：不從當下（可能已經被 go_back
    誤觸帶到不知道哪裡去的）位置繼續猜，而是先確認真的回到根畫面、骨架
    對得上，才依照 path[] 記錄的順序重放一次「當初怎麼一路點進來的」。

    回根畫面本身也不用猜：優先試根畫面自己記錄過的「自我循環」候選
    （_find_root_noop_anchors），這些已經被驗證過屬於持續存在的控制項，
    比 go_back() 亂猜的「導覽列第一顆按鈕」更可能在陌生畫面上還按得到、
    按了也不會把狀況搞更糟。連根畫面都回不去就老實承認失敗，不會拿
    當下不確定的畫面狀態硬套用舊座標亂點、越點越遠。
    """
    if not path:
        return False
    screen_h = driver.get_window_size()["height"]

    src = driver.page_source
    _, current = skeleton_signature(src)
    if current != root_uid:
        print("    [重放路徑] 目前不在根畫面，試著用根畫面的『自我循環』候選當錨點拉回去")
        for anchor in _find_root_noop_anchors(state, root_uid):
            tap_candidate(driver, anchor, screen_h)
            wait_stable(driver, max_wait=1.5)
            src = driver.page_source
            _, current = skeleton_signature(src)
            if current == root_uid:
                print(f"    [重放路徑] 用錨點「{anchor.get('label') or anchor.get('native_id')}」成功拉回根畫面")
                break

    if current != root_uid:
        print(f"    [重放路徑] 錨點都試過了還是回不去根畫面（目前在 {current}），放棄")
        return False

    for i, step in enumerate(path):
        el = _find_element_by_uid(state, step["screen_uid"], step["element_uid"])
        if el is None:
            print(f"    [重放路徑] 第{i + 1}步的元件資料找不到，放棄：{step['element_uid']}")
            return False
        if not tap_candidate(driver, el, screen_h):
            print(f"    [重放路徑] 第{i + 1}步點擊失敗，放棄：{step['element_uid']}")
            return False
        wait_stable(driver, max_wait=1.5)
    return True


def recover_to(driver, target_uid: str, tries: int = 3,
                path: list | None = None, state: dict | None = None) -> bool:
    """盡量把畫面弄回 target_uid 對應的畫面。先用 go_back() 猜幾次比較快；
    都失敗才觸發 reset_and_replay（需要呼叫端傳 path/state 才能用這個
    最後手段，沒傳就維持原本「猜到底」的行為）。"""
    for i in range(tries):
        src = driver.page_source
        dismiss_modal(driver, page_source=src)
        _, current = skeleton_signature(src)
        if current == target_uid:
            return True
        go_back(driver)
        wait_stable(driver, max_wait=1.5)
    src = driver.page_source
    _, current = skeleton_signature(src)
    if current == target_uid:
        return True
    if path and state is not None:
        print("    [recover] go_back 重試都失敗，改用「回根畫面＋重放已知路徑」最後手段")
        root_uid = path[0]["screen_uid"]
        if reset_and_replay(driver, path, state, root_uid):
            src = driver.page_source
            _, current = skeleton_signature(src)
            return current == target_uid
    return False


# ---------------------------------------------------------------------------
# 輸出
# ---------------------------------------------------------------------------

def save_map(state: dict, meta: dict) -> None:
    payload = {
        "app": meta,
        "screens": [state["screens"][uid] for uid in state["visit_order"]],
        "edges": state["edges"],
        "skipped": state["skipped"],
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _screen_name(skeleton: dict, suggested: str, existing_names: set[str]) -> str:
    base = skeleton.get("nav_title") or skeleton.get("selected_tab") or suggested
    name = re.sub(r"\s+", "_", base.strip()) or suggested
    if name in existing_names:
        n = sum(1 for x in existing_names if x == name or x.startswith(name + "_")) + 1
        name = f"{name}_{n}"
    return name


# ---------------------------------------------------------------------------
# 遞迴爬
# ---------------------------------------------------------------------------

def crawl_screen(driver, suggested_name: str, parent_uid: str | None, entry_el: dict | None,
                  path: list, depth: int, cfg: dict, state: dict) -> str:
    src = driver.page_source
    dismissed = dismiss_modal(driver, page_source=src)
    if dismissed:
        print(f"  [關掉對話框] {dismissed}")
        state["skipped"].append({"screen_uid": None, "reason": "dismissed_modal", "buttons": dismissed})
        src = driver.page_source

    logout = detect_logged_out(src)
    if logout:
        raise CrawlAborted(f"深度 {depth}：page_source 出現「{logout}」，判定已被登出")

    skeleton, uid = skeleton_signature(src)

    via_uid = entry_el["unique_id"] if entry_el else None
    via_label = (entry_el or {}).get("label", "")

    if uid in state["screens"]:
        if parent_uid is not None:
            state["edges"].append({
                "from": parent_uid, "via": via_uid, "to": uid,
                "kind": "cross-link", "label": via_label,
            })
        print(f"    -> 已爬過的畫面 uid={uid}（記 cross-link，不重掃）")
        return uid

    if len(state["screens"]) >= cfg["max_screens"]:
        state["skipped"].append({"reason": "max_screens", "at_parent": parent_uid, "via": via_uid})
        return uid

    existing_names = {r["screen_name"] for r in state["screens"].values()}
    name = _screen_name(skeleton, suggested_name, existing_names)

    print(f"\n[爬蟲] 掃描「{name}」 uid={uid} depth={depth} "
          f"（已收錄 {len(state['screens'])} / {cfg['max_screens']}）")
    screen_data = scan_screen(driver, name, describe=cfg["describe"], scroll=False, wait_stable=False,
                               page_source=src)

    record = {
        "screen_uid": uid,
        "screen_name": name,
        "parent_screen_uid": parent_uid,
        "entry_element_uid": via_uid,
        "depth": depth,
        "path": path,
        "skeleton": skeleton,
        "last_scanned": screen_data["last_scanned"],
        "screenshot": screen_data["screenshot"],
        "screenshot_blocked": screen_data.get("screenshot_blocked", False),
        "screen_stable_at_scan": screen_data["screen_stable_at_scan"],
        "screen_width": screen_data["screen_width"],
        "screen_height": screen_data["screen_height"],
        "elements": screen_data["elements"],
    }
    state["screens"][uid] = record
    state["visit_order"].append(uid)
    if parent_uid is not None:
        state["edges"].append({
            "from": parent_uid, "via": via_uid, "to": uid, "kind": "tree", "label": via_label,
        })
    save_map(state, cfg["meta"])

    if depth >= cfg["max_depth"]:
        print(f"  已達最大深度 {cfg['max_depth']}，這個分支不再往下")
        return uid

    screen_h = screen_data["screen_height"]
    # 常駐框架（頂部導覽列 / 底部 tab bar）預設在所有深度都排除；
    # --include_nav 且 depth==0 時保留，讓底部導覽當 level-1 子節點（整支 App 爬）
    exclude_chrome = not (cfg["include_nav"] and depth == 0)
    chrome_keys = persistent_chrome_keys(src, screen_h) if exclude_chrome else set()

    # class 是 Button/Cell/SegmentedControl，或是「看起來可點的 StaticText」（分頁列 /
    # 選單項目常見，見 interactive_statictext_keys）
    interactive_st = interactive_statictext_keys(src)

    seen_candidate_keys: set = set()
    candidates = []
    for el in screen_data["elements"]:
        el_ident = el.get("native_id") or el.get("label", "")
        if el["class"] in CRAWL_CLASSES:
            pass
        elif (el["class"], el_ident) in interactive_st:
            pass
        else:
            continue
        b = el["bounds"]
        cy = b["y"] + b["height"] / 2
        # 常駐框架排除：先看位置（畫面最上/最下緣，抓得到沒有 identifier 的
        # icon 列 / nav cell），再看具名比對（處理稍微不在精確區間的）
        if exclude_chrome and (
            cy <= screen_h * CHROME_TOP_FRACTION
            or cy >= screen_h * CHROME_BOTTOM_FRACTION
            or (el["class"], el_ident) in chrome_keys
        ):
            state["skipped"].append({
                "screen_uid": uid, "element_uid": el["unique_id"], "reason": "chrome",
                "label": el_ident,
            })
            continue
        if el.get("label", "").strip() in NEVER_TAP_LABELS or (el.get("native_id") or "").strip() in NEVER_TAP_LABELS:
            state["skipped"].append({
                "screen_uid": uid, "element_uid": el["unique_id"], "reason": "never_tap",
                "label": el.get("label", ""),
            })
            continue
        # 去重：XCUITest tree 偶爾把同一個元件回報兩次（同 class+label+座標）
        dedup_key = (el["class"], el.get("label", ""), round(b["x"]), round(b["y"]),
                     round(b["width"]), round(b["height"]))
        if dedup_key in seen_candidate_keys:
            continue
        seen_candidate_keys.add(dedup_key)
        if looks_external(el):
            state["skipped"].append({
                "screen_uid": uid, "element_uid": el["unique_id"], "reason": "external_hint",
                "label": el.get("label", ""),
            })
            continue
        if cfg["safe_keywords"] and _hits_risky_keyword(el):
            state["skipped"].append({
                "screen_uid": uid, "element_uid": el["unique_id"], "reason": "risky_keyword",
                "label": el.get("label", ""),
            })
            continue
        center_y = el["bounds"]["y"] + el["bounds"]["height"] / 2
        if not (0 <= center_y <= screen_h):
            state["skipped"].append({
                "screen_uid": uid, "element_uid": el["unique_id"], "reason": "offscreen",
                "label": el.get("label", ""),
            })
            continue
        candidates.append(el)

    print(f"  {len(candidates)} 個候選可點"
          + ("（dry-run：只列不點）" if cfg["dry_run"] else ""))

    if cfg["dry_run"]:
        for i, el in enumerate(candidates):
            print(f"    [{i + 1}] {el['class']}  「{el.get('label') or el.get('description') or el['unique_id']}」")
        return uid

    pattern_children: dict[str, set] = {}   # 同 name pattern -> 導到哪些 child_uid
    pattern_tries: dict[str, int] = {}

    for i, el in enumerate(candidates):
        if len(state["screens"]) >= cfg["max_screens"]:
            print("  已達 max_screens 上限，停止繼續探索")
            break

        label = el.get("label") or el.get("description") or el.get("native_id") or el["unique_id"]

        # 同模板取樣：連續 N 個同 pattern（如 QuoteDataCell_0_0/_1_0/…）都導到同一個
        # 畫面，剩下的就不逐一點了（帳號被登出/風控前，也少戳幾下）
        pat = _candidate_pattern(el)
        if pat and pattern_tries.get(pat, 0) >= cfg["sample_per_pattern"] and len(pattern_children.get(pat, set())) <= 1:
            state["skipped"].append({
                "screen_uid": uid, "element_uid": el["unique_id"], "reason": "pattern_sampled",
                "label": label, "pattern": pat,
            })
            continue

        print(f"  [{i + 1}/{len(candidates)}] 點擊「{label}」")

        if not tap_candidate(driver, el, screen_h):
            state["skipped"].append({
                "screen_uid": uid, "element_uid": el["unique_id"], "reason": "tap_failed",
                "label": el.get("label", ""),
            })
            continue
        wait_stable(driver, max_wait=1.5)

        after_src = driver.page_source
        modal_after = dismiss_modal(driver, page_source=after_src)
        if modal_after:
            print(f"    [點擊後關掉對話框] {modal_after}")
            after_src = driver.page_source

        outside = went_outside_app(driver, cfg["bundle_id"])
        if outside:
            print(f"    [跑到 App 外] {outside}，activate_app 拉回、跳過這個元件")
            try:
                driver.activate_app(cfg["bundle_id"])
            except Exception:
                pass
            wait_stable(driver, max_wait=1.5)
            state["edges"].append({
                "from": uid, "via": el["unique_id"], "to": None, "kind": "external",
                "label": el.get("label", ""),
            })
            state["skipped"].append({
                "screen_uid": uid, "element_uid": el["unique_id"],
                "reason": f"went_outside:{outside}", "label": el.get("label", ""),
            })
            if not recover_to(driver, uid, path=path, state=state):
                print("    [警告] 拉回後無法確認回到本畫面，放棄這個畫面剩餘候選")
                break
            continue

        logout = detect_logged_out(after_src)
        if logout:
            raise CrawlAborted(f"點擊「{label}」後 page_source 出現「{logout}」，判定已被登出")

        _, child_uid = skeleton_signature(after_src)
        pattern_tries[pat] = pattern_tries.get(pat, 0) + 1 if pat else 0
        if pat:
            pattern_children.setdefault(pat, set()).add(child_uid)
        if child_uid == uid:
            state["edges"].append({
                "from": uid, "via": el["unique_id"], "to": uid, "kind": "noop",
                "label": el.get("label", ""),
            })
            print("    [點擊後畫面骨架沒變] 記 noop，繼續下一個候選")
            continue

        child_path = path + [{"screen_uid": uid, "element_uid": el["unique_id"]}]
        child_suggested = f"auto_{name}_{i + 1}"
        try:
            crawl_screen(driver, child_suggested, uid, el, child_path, depth + 1, cfg, state)
        finally:
            if not recover_to(driver, uid, path=path, state=state):
                print(f"    [警告] 無法確認回到「{name}」，放棄這個畫面剩餘候選")
                save_map(state, cfg["meta"])
                break
        save_map(state, cfg["meta"])

    return uid


# 舊 crawl_app.py 的風險關鍵字（--safe_keywords 才啟用）
_RISKY_KEYWORDS = [
    "登出", "退出", "刪除", "確認", "送出", "付款", "繳費", "驗證", "提交",
    "清除", "解除", "停用", "取消", "解約", "重設", "重置", "匯款", "轉帳",
    "投保", "申請", "購買", "下單", "簽署", "同意", "買進", "賣出", "改價",
    "刪單", "圈存", "生物辨識", "指紋", "Face",
]


def _hits_risky_keyword(el: dict) -> bool:
    text = f"{el.get('description', '')} {el.get('label', '')}"
    return any(k in text for k in _RISKY_KEYWORDS)


def crawl(driver, cfg: dict) -> dict:
    state = {"screens": {}, "edges": [], "skipped": [], "visit_order": []}
    try:
        crawl_screen(driver, cfg["root_name"], None, None, [], 0, cfg, state)
        print(f"\n[爬蟲] 完成，共收錄 {len(state['screens'])} 個不重複畫面")
    except CrawlAborted as e:
        print(f"\n[爬蟲中止] {e}")
        print(f"  已收錄 {len(state['screens'])} 個畫面，資料已存到 {OUTPUT_PATH}")
        state["aborted_reason"] = str(e)
    save_map(state, cfg["meta"])
    for uid in state["visit_order"]:
        r = state["screens"][uid]
        print(f"  {'  ' * r['depth']}- {r['screen_name']} (depth {r['depth']}, {len(r['elements'])} 元件)")
    return state


def main():
    parser = argparse.ArgumentParser(description="全自動爬蟲（樹狀版）：從目前畫面探索子樹")
    parser.add_argument("--root_name", required=True,
                         help="子樹根畫面的建議名稱（有導覽列標題的話會優先用標題）")
    parser.add_argument("--max_depth", type=int, default=3)
    parser.add_argument("--max_screens", type=int, default=40)
    parser.add_argument("--describe", action="store_true",
                         help="每個畫面都呼叫 LLM 生成元件用途描述（預設關閉，先要結構）")
    parser.add_argument("--safe_keywords", action="store_true",
                         help="啟用風險關鍵字黑名單（預設關閉，除 App 外功能外全點）")
    parser.add_argument("--dry_run", action="store_true",
                         help="只掃根畫面、列出會點哪些候選，不真的點")
    parser.add_argument("--include_nav", action="store_true",
                         help="根畫面保留底部導覽列當 level-1 子節點（整支 App 爬）；"
                              "預設把頂部/底部常駐框架在所有深度都排除（子樹爬）")
    parser.add_argument("--sample_per_pattern", type=int, default=3,
                         help="同 name pattern（如 QuoteDataCell_*）點過 N 個都導到同一畫面，"
                              "就跳過其餘同 pattern 的候選（預設 3；設 0 關閉取樣）")
    parser.add_argument("--bundle_id", default=DESIRED_CAPS.get("bundleId"))
    args = parser.parse_args()

    cfg = {
        "root_name": args.root_name,
        "max_depth": args.max_depth,
        "max_screens": args.max_screens,
        "describe": args.describe,
        "safe_keywords": args.safe_keywords,
        "dry_run": args.dry_run,
        "include_nav": args.include_nav,
        "sample_per_pattern": args.sample_per_pattern if args.sample_per_pattern > 0 else 10 ** 9,
        "bundle_id": args.bundle_id,
        "meta": {
            "bundle_id": args.bundle_id,
            "crawled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "root_name": args.root_name,
            "max_depth": args.max_depth,
            "max_screens": args.max_screens,
            "describe": args.describe,
            "safe_keywords": args.safe_keywords,
            "include_nav": args.include_nav,
            "sample_per_pattern": args.sample_per_pattern,
        },
    }

    options = XCUITestOptions().load_capabilities(DESIRED_CAPS)
    driver = webdriver.Remote(APPIUM_SERVER_URL, options=options)
    try:
        crawl(driver, cfg)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
