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

無 native id 元件的定位流程兩個平台共用（platforms/locate.py），這裡只提供查詢方式、
捲動手勢、錨點查詢和最後備援（UiScrollable.scrollIntoView）。
"""

import base64
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET

from appium.options.android import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import (
    InvalidElementStateException,
    InvalidSessionIdException,
    NoSuchElementException,
    WebDriverException,
)

from aitest.common import xpath_literal
from aitest.config import android_capabilities
from aitest.platforms import locate
from aitest.platforms.base import (
    CHROME_BOTTOM_FRACTION,
    CHROME_TOP_FRACTION,
    MODAL_ACK_ONLY_LABELS,
    MODAL_DISMISS_LABELS,
    MODAL_HINT_MARKERS,
    NEVER_TAP_LABELS,
    candidate_pattern,
    histogram_bucket,
    looks_like_data,
    main_scroll_area,
    no_idle_wait,
    shrink_bounds,
    wait_until_stable,
)
from aitest.platforms.scan_core import position_bucket, read_and_merge_round

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
                # 隱藏的兄弟節點（displayed=false）畫面上看不到，不算鄰近文字：它的文字可能
                # 之後才出現、或根本不會出現，算進來會讓錨點消歧義拿一段看不到的字去找
                if sib is node or sib.attrib.get("displayed", "true") == "false":
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


def scroll_area(bounds: dict) -> dict:
    """{x, y, width, height} 換成 `mobile: scrollGesture` 要的 {left, top, width, height}"""
    return {"left": bounds["x"], "top": bounds["y"], "width": bounds["width"], "height": bounds["height"]}


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
    # 捲動手勢的操作範圍略縮在容器邊界內，避免手勢起點/終點剛好落在邊界外
    area = scroll_area(shrink_bounds(region["bounds"]))

    read_and_merge_round(platform, driver, screen_width, screen_height, merged, round_screenshots)
    if not scroll_step(driver, area, "right", scroll_percent):
        return False  # 捲不動，這不是一個真的橫向清單

    print(f"  [橫向捲動收集] 偵測到巢狀橫向容器（{region['class']}），開始邊捲邊抓...")
    time.sleep(0.5)
    for round_index in range(1, max_rounds):
        new_count, *_ = read_and_merge_round(platform, driver, screen_width, screen_height, merged, round_screenshots)
        print(f"  [橫向捲動收集] 第{round_index + 1}輪：新增{new_count}個，"
              f"累積合併{len(merged)}個不重複元件")
        if not scroll_step(driver, area, "right", scroll_percent):
            print(f"  [橫向捲動收集] 系統回報已經無法再往右捲，判定已捲到底，停止")
            break
        time.sleep(0.5)
    else:
        print(f"  [警告] 橫向捲動收集達到輪次上限（{max_rounds}輪）仍未捲到底，"
              f"這個容器可能還有內容沒收集完整，建議人工複核")

    # 捲回最左邊，避免影響後續的垂直捲動或其他操作
    for _ in range(max_rounds):
        if not scroll_step(driver, area, "left", scroll_percent):
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


def text_xpath(class_name: str, label: str) -> str:
    """class + 文字的 XPath：label 在掃描時是 content-desc 優先、沒有才用 text，所以兩個屬性用 or 一起查"""
    literal = xpath_literal(label)
    return f'//{class_name}[@text={literal} or @content-desc={literal}]'


def _scroll_into_view(driver, text: str, log_tag: str):
    """
    用 UiAutomator 原生的 UiScrollable.scrollIntoView() 讓系統自動捲動已知的可捲動容器、
    邊捲邊找，找到文字是 text（先比 text，再比 content-desc）的元件就停下來並回傳。

    這跟 XPath 本質不同：XPath 查的是「當下這一瞬間」的畫面快照，元件還沒被捲到、根本沒
    被建出來時，XPath 再怎麼查都是 0 個結果（已實測驗證，見 TROUBLESHOOTING #27/#28）；
    這個指令則是先捲動、確保元件真的存在於畫面上之後才查。
    """
    literal = uiautomator_string_literal(text)
    for attr in ("text", "description"):
        selector = (
            f'new UiScrollable(new UiSelector().scrollable(true)).scrollIntoView('
            f'new UiSelector().{attr}({literal}))'
        )
        try:
            matched = driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, selector)
            return matched, attr
        except NoSuchElementException:
            continue
        except InvalidSessionIdException:
            raise
        except WebDriverException as e:
            print(f"    [{log_tag}] 查詢失敗（{attr}）: {type(e).__name__}")
            continue
    return None, None


def find_anchor(driver, text: str):
    """
    錨點消歧義（platforms/locate.py 的 locate_by_anchor）用：用 scrollIntoView 把錨點文字捲到
    畫面上再回傳。錨點文字通常才是真正唯一的，這一步順便解決「元件還沒捲到畫面內」的問題。
    """
    anchor, _ = _scroll_into_view(driver, text, "錨點消歧義")
    return anchor


def locate_by_scroll_into_view(driver, el: dict):
    """
    定位的最後備援：直接用 scrollIntoView 找文字跟 label 一樣的元件。
    注意：這條路徑完全不比對 class，文字不唯一時可能選到錯的元件（實測發現清單裡
    「每列都有一個刪除鈕」時會默默回傳錯的那一個），只是沒有更好的選項時的次佳備援。
    """
    label = el.get("label", "")
    if not label:
        return None
    matched, attr = _scroll_into_view(driver, label, "UiScrollable")
    if matched is not None:
        print(f"    [UiScrollable] 捲動後用 {attr} 找到元件: {label!r}")
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


def app_windows_only(page_source: str, package: str | None) -> str:
    """
    開了 enableMultiWindows 的 page source 最上層是好幾個視窗（狀態列、App、App 的浮動層、
    鍵盤、導覽列…）。只留 package 是這支 App 的視窗：App 自己開的 PopupWindow（例如展開的
    下拉選單）要留，系統的狀態列、鍵盤不要（時間、通知圖示一直變，會讓換頁判斷誤判）。
    找不到任何屬於這支 App 的視窗（或不知道 package）就原封不動回傳。
    """
    if not package:
        return page_source
    try:
        root = ET.fromstring(page_source)
    except ET.ParseError:
        return page_source
    windows = list(root)
    keep = [w for w in windows if w.attrib.get("package") == package]
    if not keep or len(keep) == len(windows):
        return page_source
    for w in windows:
        if w not in keep:
            root.remove(w)
    return ET.tostring(root, encoding="unicode")


# prepare_banner_watch() 提早打開 enableMultiWindows 前的原始設定存在 ctx.platform_state 的這個 key，
# watch_banner 結束時用它還原（平台物件是共用的，同一次執行的狀態要放在 ctx）
_PRE_WATCH_SETTINGS = "android_pre_watch_settings"


def prepare_banner_watch(ctx, driver) -> None:
    """
    觸發跑馬燈的那一步（送出委託）之前先打開 enableMultiWindows。

    實測（錄影對照）：有一次兩個跑馬燈在畫面上各停了 2 秒、5 秒，20 秒內抓了 26 次 page source
    卻一次都沒看到，page source 最多只有 2 個視窗（狀態列＋App），跑馬燈那個視窗整個沒被回報。
    推測是監看開始那一刻才打開 enableMultiWindows，無障礙服務重新設定還沒好、跑馬燈的視窗
    剛好在這段時間建立就沒被追蹤到；所以提早在點擊前打開，讓它有時間準備
    """
    try:
        current = driver.get_settings()
        ctx.platform_state.setdefault(_PRE_WATCH_SETTINGS,
                                      {k: current[k] for k in ("enableMultiWindows",) if k in current})
        driver.update_settings({"enableMultiWindows": True})
    except InvalidSessionIdException:
        raise
    except WebDriverException as e:
        print(f"    [跑馬燈] 提早打開 enableMultiWindows 失敗：{type(e).__name__}")


def watch_banner(ctx, driver, texts: list[str], timeout: float = 10.0, poll_interval: float = 0.1,
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

    original = driver.get_settings()
    pre_watch = ctx.platform_state.pop(_PRE_WATCH_SETTINGS, None)
    if pre_watch is not None:  # 點擊前就打開了，還原成打開之前的值
        original.update(pre_watch)
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
                except InvalidSessionIdException:
                    raise
                except WebDriverException:
                    # 元素剛好在讀屬性時消失（stale），下一輪再查
                    pass
            try:
                toasts = read_texts(driver.find_elements(AppiumBy.XPATH, "//android.widget.Toast"))
                record(toasts, time.time())
                note_new(f"Toast：{t.strip()}" for t in toasts if t.strip())
            except InvalidSessionIdException:
                raise
            except WebDriverException:
                pass
            if near_selector and not all(found.values()):
                try:
                    labels = read_texts(driver.find_elements(AppiumBy.ANDROID_UIAUTOMATOR, near_selector))
                except InvalidSessionIdException:
                    raise
                except WebDriverException:
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
                except InvalidSessionIdException:
                    raise
                except WebDriverException:
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
                except InvalidSessionIdException:
                    raise
                except WebDriverException:
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
    except InvalidSessionIdException:
        raise
    except WebDriverException:
        inner = []
    return inner[0] if inner else element


def focus_input(ctx, driver, element, timeout: float = 3.0):
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
        ctx.raise_if_stopped()
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
    except InvalidSessionIdException:
        raise
    except WebDriverException as e:
        print(f"    [輸入] 鍵盤「完成」送出失敗，改按 Enter: {e}")
        driver.press_keycode(66)  # KEYCODE_ENTER


# 模擬按鍵的間隔：完全不等的話，輸入框有在打字時做處理（轉大寫、格式化、遮罩）的 App
# 會漏掉中間的字（實際遇過 A123456789 打成 A12789）；比對不符重打時再放慢
_KEY_INTERVAL = 0.15
_SLOW_KEY_INTERVAL = 0.25
_MASK_CHARS = set("*•●·")


def _type_by_keycodes(ctx, driver, target, value: str, interval: float) -> None:
    target.click()
    time.sleep(0.3)  # 等輸入框取得焦點、鍵盤出現
    for c in value:
        ctx.raise_if_stopped()
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
    except InvalidSessionIdException:
        raise
    except WebDriverException as e:
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


def input_text(ctx, driver, element, value: str, submit: bool = False) -> str:
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
        _type_by_keycodes(ctx, driver, target, value, _KEY_INTERVAL)
        outcome = "PASS（輸入框不接受直接設定文字，改用模擬按鍵逐字輸入）"

    actual = _typed_mismatch(target, value)
    if actual is not None:
        if unsupported:
            return f"FAIL（輸入後欄位內容是「{actual}」跟要輸入的不一樣，有特殊字元沒辦法改用按鍵重打）"
        print(f"    [輸入] 輸入後內容跟預期不一樣（長度 {len(actual)} / {len(value)}），清掉放慢重打一次")
        _clear_text(driver, target)
        _type_by_keycodes(ctx, driver, target, value, _SLOW_KEY_INTERVAL)
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
        except InvalidSessionIdException:
            raise
        except WebDriverException as e:
            print(f"    [輸入] 收鍵盤失敗，照原本流程繼續: {e}")
    return outcome


# ---------------------------------------------------------------------------
# 爬蟲（crawl_app_tree.py）用：哪些算可點、是不是同一個畫面、怎麼關對話框
#
#   跟 iOS 最大的差別：
#     - 可不可以點直接看 clickable 屬性（系統回報），不用像 iOS 用 class 白名單猜
#     - 有系統返回鍵，go_back 不用猜導覽列按鈕
#     - 骨架多一個 current_activity（不同 Activity 一定是不同畫面；但很多 App 整支
#       只有一個 Activity、靠 Fragment 切畫面，所以只是其中一個特徵，不單獨用）
#     - 可點的常常是容器（LinearLayout 包一個 TextView），文字在子元件上
#     - 清單每一列共用同一個 resource-id（掃描時會被降級成沒有 native id），
#       同模板取樣要改用「畫面上重複出現的 resource-id」當 pattern
# ---------------------------------------------------------------------------

_TAB_HINTS = ("TabLayout", "BottomNavigationView", "NavigationBarView", "TabWidget")
_TOOLBAR_HINTS = ("Toolbar", "ActionBar")
# 系統 AlertDialog 會有的 id（自訂對話框沒有，另外看視窗有沒有蓋滿畫面）
_DIALOG_IDS = ("android:id/alertTitle", "android:id/message", "android:id/button1", "android:id/button2")


def _node_text(a: dict) -> str:
    """跟掃描時的 label 一樣：content-desc 優先，沒有才用 text"""
    return (a.get("content-desc") or a.get("text") or "").strip()


def _native_id(a: dict) -> str:
    """跟掃描時判斷 native id 一樣：resource-id 等於顯示文字的不算（文字回填，不穩定）"""
    rid = (a.get("resource-id") or "").strip()
    if rid and rid != (a.get("text") or "").strip() and rid != (a.get("content-desc") or "").strip():
        return rid
    return ""


def _ui_nodes(root) -> list:
    """真正的 UI 節點：跳過 <hierarchy> 根節點、displayed=false 的節點"""
    return [n for n in root.iter()
            if n.attrib.get("class") not in (None, "", "hierarchy") and n.attrib.get("displayed") != "false"]


def _has_ancestor_class(node, parent_map: dict, hints: tuple) -> bool:
    """node 自己或祖先的 class 名稱含有 hints 其中之一"""
    while node is not None:
        if any(h in node.attrib.get("class", "") for h in hints):
            return True
        node = parent_map.get(node)
    return False


def _first_text(node) -> str:
    return next((t for t in (_node_text(n.attrib) for n in node.iter()) if t), "")


def android_skeleton_signature(page_source: str, activity: str | None) -> tuple[dict, str]:
    """
    iOS 版 skeleton_signature（platforms/ios.py）的 Android 對應：一樣只取穩定資訊、
    忽略資料，多一個 activity。
      - native_ids：真正的 resource-id（不是文字回填、不像資料）；清單每列共用的
        同一個 id 在集合裡只算一次，資料筆數不同不會變成不同畫面
      - class_histogram：同 iOS 的分桶
      - selected_tab：分頁容器（TabLayout / BottomNavigationView…）裡 selected=true
        的項目文字；沒有標準分頁容器的 App（自訂 view）退回用任何 selected=true 的項目
      - nav_title：Toolbar / ActionBar 裡的標題文字；沒有的話退回用畫面上方
        resource-id 含 title 的 TextView
    """
    root = ET.fromstring(page_source)
    parent_map = {c: p for p in root.iter() for c in p}
    try:
        screen_height = float(root.attrib.get("height", 0))
    except ValueError:
        screen_height = 0

    native_ids: set[str] = set()
    class_counts: dict[str, int] = {}
    nav_title = title_fallback = None
    selected_tab = selected_fallback = None

    for node in _ui_nodes(root):
        a = node.attrib
        cls = a["class"]
        class_counts[cls] = class_counts.get(cls, 0) + 1

        rid = _native_id(a)
        if rid and not looks_like_data(rid):
            native_ids.add(rid)

        text = _node_text(a)
        if cls.endswith("TextView") and text and not looks_like_data(text):
            if nav_title is None and _has_ancestor_class(node, parent_map, _TOOLBAR_HINTS):
                nav_title = text
            if title_fallback is None and "title" in (a.get("resource-id") or "").lower():
                b = parse_bounds(a.get("bounds", ""))
                if b and (not screen_height or b["y"] < screen_height * 0.2):
                    title_fallback = text

        if a.get("selected") == "true":
            label = _first_text(node)
            if label and not looks_like_data(label):
                if selected_tab is None and _has_ancestor_class(node, parent_map, _TAB_HINTS):
                    selected_tab = label
                if selected_fallback is None:
                    selected_fallback = label

    skeleton = {
        "activity": activity,
        "native_ids": sorted(native_ids),
        "class_histogram": {k: histogram_bucket(v) for k, v in sorted(class_counts.items())},
        "selected_tab": selected_tab or selected_fallback,
        "nav_title": nav_title or title_fallback,
    }
    canonical = json.dumps(skeleton, sort_keys=True, ensure_ascii=False)
    screen_uid = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
    return skeleton, screen_uid


def android_chrome_keys(page_source: str, screen_height: float) -> set[tuple[str, str]]:
    """iOS 版 persistent_chrome_keys 的 Android 對應：分頁容器（TabLayout /
    BottomNavigationView…）裡的元件，加上畫面最上緣 / 最下緣的可點元件。
    key 是 (class, native id 或自己的文字)，跟 crawl_screen 裡 el_ident 的算法一致。"""
    root = ET.fromstring(page_source)
    parent_map = {c: p for p in root.iter() for c in p}
    keys: set[tuple[str, str]] = set()
    for node in _ui_nodes(root):
        a = node.attrib
        ident = _native_id(a) or _node_text(a)
        if not ident:
            continue
        if _has_ancestor_class(node, parent_map, _TAB_HINTS):
            keys.add((a["class"], ident))
            continue
        if a.get("clickable") != "true":
            continue
        b = parse_bounds(a.get("bounds", ""))
        if b is None:
            continue
        cy = b["y"] + b["height"] / 2
        if cy <= screen_height * CHROME_TOP_FRACTION or cy >= screen_height * CHROME_BOTTOM_FRACTION:
            keys.add((a["class"], ident))
    return keys


def _is_dialog(page_source: str) -> bool:
    """畫面上是不是對話框：系統 AlertDialog 的 id，或最上層視窗沒有蓋滿畫面
    （自訂對話框，例如「主機無法連線」那種置中的框）"""
    if any(i in page_source for i in _DIALOG_IDS):
        return True
    try:
        root = ET.fromstring(page_source)
        screen_area = float(root.attrib["width"]) * float(root.attrib["height"])
    except (ET.ParseError, KeyError, ValueError):
        return False
    window = next((n for n in root if n.attrib.get("class") not in (None, "", "hierarchy")), None)
    b = parse_bounds(window.attrib.get("bounds", "")) if window is not None else None
    return bool(b and screen_area and b["width"] * b["height"] < screen_area * 0.85)


def _find_by_texts(driver, texts: list[str]) -> list:
    conds = " or ".join(f"@text={xpath_literal(t)} or @content-desc={xpath_literal(t)}" for t in texts)
    try:
        return driver.find_elements(AppiumBy.XPATH, f"//*[{conds}]")
    except InvalidSessionIdException:
        raise
    except WebDriverException:
        return []


def android_dismiss_modal(driver, max_rounds: int = 3, page_source: str | None = None,
                          keep_labels=()) -> list[str]:
    """iOS 版 dismiss_modal 的 Android 對應：先找「純關閉」語意的按鈕；找不到、
    且畫面確定是對話框時，才點「確定 / OK」這類確認語意的。永遠不點 NEVER_TAP_LABELS。
    要按的按鈕在 keep_labels 裡就整個不處理（測案預期的視窗，見 iOS 版說明）。
    快篩：沒有任何 modal 線索、也不是對話框，就不做任何裝置查詢。"""
    if (page_source is not None and not any(m in page_source for m in MODAL_HINT_MARKERS)
            and not _is_dialog(page_source)):
        return []

    dismissed: list[str] = []
    for _ in range(max_rounds):
        matches = _find_by_texts(driver, MODAL_DISMISS_LABELS)
        if not matches:
            try:
                is_dialog = _is_dialog(driver.page_source)
            except InvalidSessionIdException:
                raise
            except WebDriverException:
                is_dialog = False
            if is_dialog:
                matches = _find_by_texts(driver, MODAL_ACK_ONLY_LABELS)
        if not matches:
            break
        element = matches[0]
        try:
            target = ((element.get_attribute("text") or element.get_attribute("content-desc")) or "").strip()
            if target in NEVER_TAP_LABELS:
                break
            if target in keep_labels:
                print(f"    [關閉非預期彈窗] 「{target}」是第一步畫面上的按鈕，視窗是預期的，不處理")
                break
            element.click()
        except InvalidSessionIdException:
            raise
        except WebDriverException:
            break
        dismissed.append(target or "?")
        wait_until_stable(driver)
    return dismissed


class AndroidScreenAnalysis:
    """一個畫面的 page_source 解析一次，給 crawl_app_tree.crawl_screen 判斷每個元件用"""

    def __init__(self, page_source: str):
        root = ET.fromstring(page_source)
        self._by_bounds: dict[tuple, object] = {}
        self._rid_counts: dict[tuple, int] = {}
        for node in _ui_nodes(root):
            a = node.attrib
            b = parse_bounds(a.get("bounds", ""))
            if b is None:
                continue
            self._by_bounds.setdefault((a["class"], b["x"], b["y"], b["width"], b["height"]), node)
            rid = a.get("resource-id")
            if rid:
                self._rid_counts[(a["class"], rid)] = self._rid_counts.get((a["class"], rid), 0) + 1

    def _node(self, el: dict):
        b = el["bounds"]
        return self._by_bounds.get((el["class"], b["x"], b["y"], b["width"], b["height"]))

    def is_candidate(self, el: dict) -> bool:
        return bool(el.get("clickable"))

    def text(self, el: dict) -> str:
        """元件自己沒有文字時（可點的容器），用子孫的文字代表它"""
        if el.get("label"):
            return el["label"]
        node = self._node(el)
        if node is None:
            return ""
        texts = [t for t in (_node_text(n.attrib) for n in node.iter()) if t]
        return " ".join(dict.fromkeys(texts))[:60]

    def pattern(self, el: dict) -> str:
        """有編號的 native id 照 iOS 的方式去掉編號；清單每列共用的 resource-id
        （掃描時已降級成沒有 native id）就用那個 id 本身當 pattern"""
        pat = candidate_pattern(el)
        if pat or el.get("native_id"):
            return pat
        node = self._node(el)
        rid = node.attrib.get("resource-id") if node is not None else ""
        if rid and self._rid_counts.get((el["class"], rid), 0) > 1:
            return rid
        return ""


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
        return self.scroll_once(driver, main_scroll_area(screen_width, screen_height), direction)

    def element_flags(self, el: dict) -> dict:
        """原始屬性照實寫進 element_map，留給 viewer 篩選用"""
        return {"clickable": el["clickable"], "checkable": el["checkable"], "enabled": el["enabled"]}

    def page_source_context(self, driver):
        """跑測案期間（單份、批次、命令列）套用的設定，見 no_idle_wait"""
        return no_idle_wait(driver)

    # --- 無 native id 元件的定位（流程在 platforms/locate.py）---
    class_text_by = AppiumBy.XPATH
    fallback_label = "UiScrollable"

    def class_text_query(self, class_name: str, label: str) -> str:
        return text_xpath(class_name, label)

    def find_by_class_and_text(self, driver, class_name: str, label: str) -> list:
        return locate.find_by_class_and_text(self, driver, class_name, label)

    def scroll_once(self, driver, bounds: dict, direction: str) -> bool:
        """回傳系統回報的「這次有沒有真的捲動」"""
        return scroll_step(driver, scroll_area(bounds), direction, self.scroll_percent)

    def scrollable_regions(self, xml_source: str) -> list[dict]:
        return find_scrollable_regions(xml_source)

    def find_anchor(self, driver, text: str):
        return find_anchor(driver, text)

    def count_text(self, driver, text: str) -> int:
        """畫面上文字是 text 的元件有幾個（無文字元件的錨點要唯一才能用；find_anchor 捲到第一個就回傳，不檢查唯一）"""
        return len(_find_by_texts(driver, [text]))

    def locate_fallback(self, driver, el: dict):
        return locate_by_scroll_into_view(driver, el)

    def locate_generated_element(self, ctx, driver, el: dict):
        return locate.locate_generated_element(ctx, self, driver, el)

    def ensure_visible(self, driver, element):
        # 定位時已經用「邊捲邊找」把元件捲進畫面，找到的就是可互動的（見模組說明）
        return element

    def tap_by_coordinates(self, driver, bounds: dict):
        center_x = bounds["x"] + bounds["width"] / 2
        center_y = bounds["y"] + bounds["height"] / 2
        print(f"    [座標點擊] x={center_x}, y={center_y}  (來自 element_map.json 的 bounds={bounds})")
        driver.execute_script("mobile: clickGesture", {"x": center_x, "y": center_y})

    def watch_banner(self, ctx, driver, texts: list[str], timeout: float = 10.0) -> tuple[dict, dict, str]:
        return watch_banner(ctx, driver, texts, timeout)

    def prepare_banner_watch(self, ctx, driver) -> None:
        prepare_banner_watch(ctx, driver)

    def focus_input(self, ctx, driver, element):
        return focus_input(ctx, driver, element)

    def input_text(self, ctx, driver, element, value: str, submit: bool = False) -> str:
        return input_text(ctx, driver, element, value, submit)

    # --- 跑馬燈監看的錄影（除錯用，見 run_test_case.run_banner_group）---
    def start_capture(self, driver) -> bool:
        """開始錄手機畫面（UiAutomator2 內建，底層是 adb screenrecord，不用另外安裝）"""
        try:
            driver.start_recording_screen(timeLimit=180, bitRate=4000000, forceRestart=True)
            return True
        except InvalidSessionIdException:
            raise
        except WebDriverException as e:
            print(f"    [錄影] 開始錄影失敗，這次不錄：{type(e).__name__}：{e}")
            return False

    def stop_capture(self, driver, save_path: str | None) -> str | None:
        """停止錄影；save_path 有給才存成 mp4（PASS 時不留檔），回傳存好的路徑"""
        try:
            data = driver.stop_recording_screen()
        except InvalidSessionIdException:
            raise
        except WebDriverException as e:
            print(f"    [錄影] 停止錄影失敗：{type(e).__name__}：{e}")
            return None
        if not save_path or not data:
            return None
        with open(save_path, "wb") as f:
            f.write(base64.b64decode(data))
        return save_path

    # --- 批次執行的導航（navigator.py）、爬蟲 ---
    def go_back(self, driver) -> None:
        driver.back()

    def app_id(self, driver) -> str | None:
        """capabilities 沒指定 appPackage（接上已經開著的 App），用當下前景的 App"""
        return driver.current_package

    # --- 錄製建樹（core/recorder.py）---
    def begin_recording(self, driver) -> dict:
        """
        錄製期間打開 enableMultiWindows：App 自己開的浮動層（PopupWindow，例如分頁列右邊展開的
        群組選單）是另一個視窗，預設的 page source 只有取得焦點的那個視窗，看不到也點不到。
        回傳這次錄製的平台狀態：原始設定（end_recording 還原用，開著的話掃描會把狀態列也收進去）、
        App 的 package（recording_source 過濾視窗用）
        """
        current = driver.get_settings()
        driver.update_settings({"enableMultiWindows": True})
        return {"settings": {"enableMultiWindows": bool(current.get("enableMultiWindows", False))},
                "package": driver.current_package}

    def end_recording(self, driver, state: dict | None) -> None:
        if state:
            driver.update_settings(state["settings"])

    def recording_source(self, page_source: str, state: dict | None) -> str:
        return app_windows_only(page_source, (state or {}).get("package"))

    def dismiss_dialogs(self, driver, keep_labels=()) -> list[str]:
        """關掉擋在前面的非預期彈窗，回傳按過的按鈕文字。沿用爬蟲的 android_dismiss_modal（按鈕清單、
        永遠不點「關閉APP／登出」的規則都在那裡）；keep_labels 見 android_dismiss_modal"""
        # 系統權限視窗不處理（使用者決定）：權限框是系統的 permissioncontroller／packageinstaller
        # 跳出來的，出現時目前前景的 package 就是它
        try:
            package = driver.current_package or ""
        except InvalidSessionIdException:
            raise
        except WebDriverException:
            return []
        if "permissioncontroller" in package or "packageinstaller" in package:
            print(f"    [關閉非預期彈窗] 畫面上是系統權限視窗（{package}），不處理")
            return []
        return android_dismiss_modal(driver, keep_labels=keep_labels)

    def element_text(self, element) -> str:
        return (element.get_attribute("text") or element.get_attribute("content-desc") or "").strip()

    # --- 爬蟲（crawl_app_tree.py）---
    def skeleton(self, driver, page_source: str) -> tuple[dict, str]:
        try:
            activity = driver.current_activity
        except InvalidSessionIdException:
            raise
        except WebDriverException:
            activity = None
        return android_skeleton_signature(page_source, activity)

    def analyze(self, page_source: str) -> AndroidScreenAnalysis:
        return AndroidScreenAnalysis(page_source)

    def chrome_keys(self, page_source: str, screen_height: float) -> set:
        return android_chrome_keys(page_source, screen_height)

    def dismiss_modal(self, driver, max_rounds: int = 3, page_source: str | None = None) -> list[str]:
        return android_dismiss_modal(driver, max_rounds, page_source)

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
