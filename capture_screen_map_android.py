"""
POC: Android App 元件地圖產生器（iOS 版的移植，第一步）

功能：
  對「目前畫面」執行一次掃描，抓取畫面上所有有效節點（不預先篩選 class/
  clickable/checkable，這些原始屬性會照實存起來，交給 viewer.html 篩選），
  依照有無穩定的 resource-id 分流處理，產生每個元件的唯一 ID，並請 LLM
  生成用途描述，最後把結果存成 JSON 檔案（跟 iOS 版共用同一份
  element_map.json、同一套 JSON schema，只是多一個 "platform" 欄位標記來源）。

  畫面上如果偵測到可捲動容器（scrollable="true"），會自動「邊捲邊抓」：
  反覆讀取候選元件、往下捲一段、再讀一次，直到連續兩輪內容完全一致（判定
  已捲到底）或達到輪次上限為止，把各輪抓到的元件依內容（不看捲動位置）
  去重合併成一份完整清單，不需要再手動捲動、分開存成不同 screen_name。
  （這是因為 Android 的 accessibility tree 只反映當下實際渲染出來的內容，
  畫面外、還沒捲到的元件常常根本沒有被建出來，見 TROUBLESHOOTING.md
  問題27/28，只抓一次沒辦法拿到完整清單）

跟 iOS 版（capture_screen_map.py）的關鍵差異：
  - Android 的 XML 節點 tag 固定是 <node>，class 要讀屬性，不是 tag 本身
  - 判斷「可不可互動」直接看 clickable/checkable 屬性，不用像 iOS 那樣
    用 class 白名單用猜的
  - 穩定 id 來源是 resource-id（Android 開發習慣上涵蓋率通常比 iOS 的
    accessibilityIdentifier 高很多）
  - 座標格式是 bounds="[x1,y1][x2,y2]" 字串，需要自己解析
  - 部分金融類 App 會設定 FLAG_SECURE 防止截圖，螢幕截圖/裁切圖會是純黑色，
    這種情況下會自動偵測並改成「只靠文字線索」讓 LLM 判斷用途，不送圖

使用方式：
  1. 手動操作模擬器/手機導航到你要掃描的畫面
  2. 執行：python capture_screen_map_android.py --screen_name login_screen
  3. 結果會寫入/更新 element_map.json（同一個檔案，畫面資料會累加）

執行前準備：
  1. Android Studio + SDK + 模擬器（或實體裝置）已就緒，adb 看得到裝置
  2. 啟動 Appium Server: `appium`（需已安裝 uiautomator2 driver）
  3. pip install Appium-Python-Client google-genai pillow
  4. 設定環境變數 GEMINI_API_KEY
  5. 修改 test_config.py 的 ANDROID_DESIRED_CAPS（udid、bundleId等）
"""

import argparse
import io
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from appium import webdriver
from appium.options.android import UiAutomator2Options
from PIL import Image

from capture_screen_map import (
    OUTPUT_JSON_PATH,
    crop_element_screenshot,
    make_generated_unique_id,
    make_native_unique_id,
    position_bucket,
    save_to_json,
)
from test_config import ANDROID_DESIRED_CAPS, APPIUM_SERVER_URL, LLM_MODEL, generate_content_with_retry
from google.genai import types

CROPS_DIR = Path("crops")
SCREENSHOTS_DIR = Path("screenshots")


# ---------------------------------------------------------------------------
# 1. 座標解析：Android 的 bounds 是字串，要自己拆
# ---------------------------------------------------------------------------

def parse_bounds(bounds_str: str) -> dict | None:
    """把 "[x1,y1][x2,y2]" 解析成 {x, y, width, height}"""
    match = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds_str or "")
    if not match:
        return None
    x1, y1, x2, y2 = (int(v) for v in match.groups())
    return {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1}


# ---------------------------------------------------------------------------
# 2. 抓取畫面：截圖 + Accessibility Tree
# ---------------------------------------------------------------------------

def capture_screen(driver) -> tuple[str, bytes]:
    page_source = driver.page_source
    screenshot_png = driver.get_screenshot_as_png()
    return page_source, screenshot_png


def is_screenshot_blocked(screenshot_png: bytes, sample_step: int = 17, threshold: float = 0.98) -> bool:
    """
    偵測這張截圖是不是被 FLAG_SECURE 擋掉的純黑（或近乎單一顏色）圖。
    做法：等間隔取樣像素，如果同一個顏色佔比超過 threshold，視為被擋。
    用取樣而非逐像素檢查，避免大圖檢查太慢。
    """
    image = Image.open(io.BytesIO(screenshot_png)).convert("RGB")
    pixels = list(image.getdata())[::sample_step]
    if not pixels:
        return False
    most_common_count = max(pixels.count(p) for p in set(pixels))
    return (most_common_count / len(pixels)) >= threshold


# ---------------------------------------------------------------------------
# 3. 解析元件：從 XML 抓出候選元件，並計算指紋所需的上下文
#
#    這裡不再依 class/clickable/checkable 篩選「值不值得收錄」——只要是真實
#    節點、有可解析的座標，一律收錄，把「要不要看這個元件」的決定權交給
#    viewer.html 的篩選介面，掃描端不預先幫使用者決定。clickable/checkable/
#    enabled 這些原始屬性會原封不動存進結果，供 viewer.html 篩選用。
#    （這個決定的背景見 TROUBLESHOOTING.md 問題26/29：掃描端過早過濾已經
#    造成過真實資料遺漏，改成「先收下來，篩選交給檢視工具」更不容易漏資料）
# ---------------------------------------------------------------------------

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


def region_signature(region: dict) -> tuple:
    """
    橫向捲動容器去重用的識別 key，刻意不含 y 座標——同一個容器在整個畫面
    垂直捲動過程中，y 會隨著捲到哪個位置而改變，但 x/寬/高通常不變，用這三項
    加上 class 當身分，同一容器在不同輪次重複被看到時才能正確判斷「已經處理過」。
    （跟兩個不同橫向容器剛好同寬同高撞在一起的情況一樣接受，這種精確度跟專案
    其他地方採用的指紋精確度是同一個量級，見問題18/24）
    """
    b = region["bounds"]
    return (region["class"], b["width"], b["height"])


def parse_candidates_raw(xml_source: str, screen_width: int, screen_height: int) -> list[dict]:
    """解析單一快照的候選元件，不做同畫面 resource-id 重複降級（那一步要等
    「邊捲邊抓」的所有輪次合併完才能做一次，見 downgrade_duplicate_native_ids）。"""
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
            # 原始屬性照實記錄，不在這裡用來篩選，留給 viewer.html 篩選介面用
            "clickable": attrib.get("clickable") == "true",
            "checkable": attrib.get("checkable") == "true",
            "enabled": attrib.get("enabled") == "true",
        })

    return candidates


def downgrade_duplicate_native_ids(candidates: list[dict]) -> list[dict]:
    """
    Android 的 resource-id 常常是「版型內部元件 id」，在 RecyclerView/ListView/
    GridView 這種清單畫面上，每一列/每一格都共用同一個 resource-id（例如清單裡
    每一項的「收藏按鈕」都叫 btnFavorite）。這種情況下 resource-id 不具備「在這
    個畫面上唯一辨識一個元件」的能力，硬當 native id 用會讓多個不同元件算出同一個
    unique_id、LLM 描述也會互相覆蓋。因此在這裡統計 (class, resource_id) 出現
    次數，超過 1 次的一律降級為「無 native id」，改走 fingerprint（label/位置）
    那條路去區分，資料本身仍然完整保留 resource_id 供之後排查用。

    這一步要在「所有輪次的候選都合併完」之後才能做一次（不能每輪各自做），
    否則同一個元件在不同輪次重複出現時會被誤判成「同畫面有多個」。
    """
    id_counts: dict[tuple[str, str], int] = {}
    for el in candidates:
        if el["native_id"]:
            key = (el["class"], el["native_id"])
            id_counts[key] = id_counts.get(key, 0) + 1

    downgraded = set()
    for el in candidates:
        if el["native_id"]:
            key = (el["class"], el["native_id"])
            if id_counts[key] > 1:
                downgraded.add(el["native_id"])
                el["native_id"] = None

    if downgraded:
        print(f"  [警告] 偵測到 resource-id 在同畫面重複使用（清單版型常見），"
              f"改用文字/位置指紋辨識: {sorted(downgraded)}")

    return candidates


def parse_candidate_elements(xml_source: str, screen_width: int, screen_height: int) -> list[dict]:
    """單一快照解析 + 降級，沒有捲動內容的畫面用這個（等同於原本行為）。"""
    return downgrade_duplicate_native_ids(parse_candidates_raw(xml_source, screen_width, screen_height))


def candidate_signature(candidates: list[dict]) -> tuple:
    return tuple(sorted(
        (el["class"], el["native_id"] or el["label"], el["position_bucket"])
        for el in candidates
    ))


def wait_for_screen_stable(driver, screen_width: int, screen_height: int,
                            max_polls: int = 5, poll_interval_sec: float = 1.5) -> bool:
    """跟 iOS 版同樣的輪詢穩定邏輯（見 capture_screen_map.py 的說明），Android版獨立一份
    是因為要呼叫 Android 專屬的 parse_candidate_elements。"""
    previous_signature = None
    for attempt in range(1, max_polls + 1):
        page_source = driver.page_source
        candidates = parse_candidate_elements(page_source, screen_width, screen_height)
        signature = candidate_signature(candidates)

        if signature == previous_signature:
            print(f"  [畫面穩定檢查] 第{attempt}次讀取與前一次一致，判定畫面已穩定")
            return True

        previous_signature = signature
        if attempt < max_polls:
            print(f"  [畫面穩定檢查] 第{attempt}次讀取，內容與前一次不同，"
                  f"{poll_interval_sec}秒後重讀確認...")
            time.sleep(poll_interval_sec)

    print(f"  [警告] 輪詢{max_polls}次後畫面內容依然持續變化，需要人工複核這次資料是否可信")
    return False


def candidate_identity_key(el: dict) -> tuple:
    """
    跨捲動輪次合併去重用的識別 key，刻意不看 bounds/position_bucket/sibling_index——
    這些會隨著捲動位置改變（同一個元件捲到不同位置，畫面座標當然不一樣），
    只看不受捲動影響的內容線索。

    有 native_id 的也一併把 label 併入 key，不能只信任 (class, native_id) 就
    當作唯一——這個階段的 native_id 還沒經過 downgrade_duplicate_native_ids()
    的同畫面重複檢查（那一步要等所有輪次合併完才能做），如果同一個 resource-id
    被畫面上多個不同項目共用（清單版型常見，見問題26），單用 (class, native_id)
    當合併key會把這些不同元件誤判成「同一個在不同輪次重複出現」而錯誤合併成一筆
    ——這是實測時真的發生過的bug（83筆「項目 001」~「項目 083」被誤合併成1筆），
    併入 label 之後，同一個resource-id但文字不同的項目會拿到不同的key，才不會
    互相吃掉；同一個元件在重疊輪次裡重複出現時，label通常不變，還是能正確合併。

    沒有 native_id 的用 class+文字+鄰近文字組合當近似身分。這不是完美的判斷
    （兩個文字內容剛好一樣的不同元件會被誤判成同一個），但跟這個專案其他地方
    接受的指紋精確度是同一個量級（見 TROUBLESHOOTING.md 問題18、24），足夠實用。
    """
    if el["native_id"]:
        return ("id", el["class"], el["native_id"], el["label"])
    return ("fp", el["class"], el["label"], tuple(el["nearby_text"]))


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


def _read_and_merge_round(driver, screen_width: int, screen_height: int,
                           merged: dict, round_screenshots: list[bytes]) -> tuple[int, str]:
    """
    讀取「目前這個當下」的畫面（不論是垂直捲動到哪個位置、還是正在水平捲動某個
    巢狀容器），截圖存證、解析候選元件，依內容識別合併進 merged（見
    candidate_identity_key），回傳 (這一輪新增了幾個元件, 這次讀到的page_source)。
    垂直收集迴圈跟橫向子容器收集迴圈共用這個函式，讓兩者讀到的元件、截圖都能
    進同一份 merged/round_screenshots，_round_index 的追蹤機制不用另外特化。
    """
    page_source = driver.page_source
    screenshot_png = driver.get_screenshot_as_png()
    round_index = len(round_screenshots)
    round_screenshots.append(screenshot_png)

    raw = parse_candidates_raw(page_source, screen_width, screen_height)
    new_count = 0
    for el in raw:
        key = candidate_identity_key(el)
        if key not in merged:  # 以第一次出現的輪次為準，保持結果穩定、可預期
            el["_round_index"] = round_index
            merged[key] = el
            new_count += 1
    return new_count, page_source


def collect_horizontal_region(driver, region: dict, screen_width: int, screen_height: int,
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

    _read_and_merge_round(driver, screen_width, screen_height, merged, round_screenshots)
    if not scroll_step(driver, scroll_area, "right", scroll_percent):
        return False  # 捲不動，這不是一個真的橫向清單

    print(f"  [橫向捲動收集] 偵測到巢狀橫向容器（{region['class']}），開始邊捲邊抓...")
    time.sleep(0.5)
    for round_index in range(1, max_rounds):
        new_count, _ = _read_and_merge_round(driver, screen_width, screen_height, merged, round_screenshots)
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


def collect_candidates_with_scroll(driver, screen_width: int, screen_height: int,
                                    max_rounds: int = 15, scroll_percent: float = 0.75
                                    ) -> tuple[list[dict], list[bytes]]:
    """
    邊捲邊抓：反覆讀取候選元件、往下捲一段、再讀一次，直到系統回報「這次捲動
    沒有造成任何位移」（判定已經捲到底）或達到輪次上限為止，把每一輪抓到的
    候選元件依內容識別（不看捲動位置）去重合併成一份完整清單。

    scroll_percent 刻意設在 1.0 以下（每次捲動畫面高度的75%），讓相鄰兩輪之間
    有重疊區域，提高跨輪次去重比對的可靠度。

    每一輪垂直捲動之間，如果畫面上出現巢狀的橫向捲動容器（例如「推薦專區」
    橫向卡片列表），會先原地把它左右捲完收集完整，再繼續往下捲整個畫面
    （見問題34：一開始只處理垂直方向，橫向清單項目多到超出初次渲染範圍時
    會漏收，這裡補上）。同一個橫向容器（用 class+寬+高辨識，見
    region_signature）只會處理一次，不會因為它在多輪垂直捲動裡重複出現而
    重複橫向捲動。

    回傳 (merged_candidates, round_screenshots)：round_screenshots 是每一輪
    當下的截圖 bytes，每個候選元件會記錄自己是在第幾輪抓到的（"_round_index"，
    內部欄位，不會寫進最終JSON），供後續生成 LLM 描述、裁切截圖時取用「那一輪」
    的畫面，而不是誤用最後一輪或第一輪的截圖去描述其他輪次抓到的元件。
    """
    merged: dict[tuple, dict] = {}
    round_screenshots: list[bytes] = []
    processed_horizontal: set = set()

    scroll_area = {
        "left": int(screen_width * 0.1),
        "top": int(screen_height * 0.15),
        "width": int(screen_width * 0.8),
        "height": int(screen_height * 0.7),
    }

    for round_index in range(max_rounds):
        new_count, page_source = _read_and_merge_round(
            driver, screen_width, screen_height, merged, round_screenshots
        )
        print(f"  [捲動收集] 第{round_index + 1}輪：新增{new_count}個，"
              f"累積合併{len(merged)}個不重複元件")

        for region in find_scrollable_regions(page_source):
            sig = region_signature(region)
            if sig in processed_horizontal:
                continue
            processed_horizontal.add(sig)
            collect_horizontal_region(
                driver, region, screen_width, screen_height, merged, round_screenshots
            )

        scrolled = scroll_step(driver, scroll_area, "down", scroll_percent)
        if not scrolled:
            print(f"  [捲動收集] 系統回報已經無法再往下捲，判定已捲到底，停止")
            break
        time.sleep(0.5)  # 給畫面一點時間反應捲動動畫/懶載入
    else:
        print(f"  [警告] 捲動收集達到輪次上限（{max_rounds}輪）仍未捲到底，"
              f"這個畫面可能還有內容沒收集完整，建議人工複核")

    # 捲動收集完後畫面停在很下面，捲回頂部，避免影響後續操作（例如接著要點擊
    # 別的元件、或換下一個畫面掃描時，手機還停留在這個畫面捲到一半的位置）。
    # 用跟收集階段同樣大小的手勢重複捲，而不是一次用超大 percent——今天實測
    # 發現一次性的超大幅度捲動手勢可能異常緩慢甚至卡住，改成跟收集階段一樣
    # 正常大小、用同一個回傳值判斷，捲到系統回報「捲不動了」為止。
    for _ in range(max_rounds):
        if not scroll_step(driver, scroll_area, "up", scroll_percent):
            break
        time.sleep(0.3)

    return list(merged.values()), round_screenshots


# ---------------------------------------------------------------------------
# 4. LLM：批次生成「有 native id」元件的用途描述
# ---------------------------------------------------------------------------

def describe_native_elements(screen_name: str, elements: list[dict]) -> dict:
    if not elements:
        return {}

    payload = [
        {"native_id": el["native_id"], "class": el["class"], "label": el["label"], "value": el["value"]}
        for el in elements
    ]

    prompt = f"""這是一個 Android App 畫面「{screen_name}」上，一批已知 resource-id 的 UI 元件清單。
請根據每個元件的 class、label、value，推論它的用途，用一句簡短的繁體中文描述。

輸入元件清單（JSON）：
{json.dumps(payload, ensure_ascii=False)}

請回傳一個 JSON 陣列，每個項目對應一個輸入元件，格式：
[{{"native_id": "<與輸入相同的native_id>", "class": "<與輸入相同的class>", "description": "<用途描述>"}}]

只回傳 JSON，不要有其他文字。
"""

    response = generate_content_with_retry(
        model=LLM_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json"),
    )

    raw_text = (response.text or "").strip()
    try:
        result_list = json.loads(raw_text)
    except json.JSONDecodeError:
        print(f"  [警告] native元件描述生成失敗，回傳非合法JSON: {raw_text}")
        return {}

    return {
        f"{item['class']}::{item['native_id']}": item["description"]
        for item in result_list if "native_id" in item and "class" in item
    }


# ---------------------------------------------------------------------------
# 5. LLM：批次生成「沒有 native id」元件的用途描述
#    （截圖可用就圖+文字一起送；FLAG_SECURE 擋掉截圖時只送文字）
# ---------------------------------------------------------------------------

def describe_generated_elements(screen_name: str, screenshot_png: bytes, screenshot_available: bool,
                                 elements: list[dict], temp_ids: list[str]) -> dict:
    if not elements:
        return {}

    payload = [
        {
            "temp_id": tid,
            "class": el["class"],
            "label": el["label"] or "(無文字)",
            "bounds": el["bounds"],
            "position_bucket": el["position_bucket"],
            "nearby_text": el["nearby_text"],
        }
        for tid, el in zip(temp_ids, elements)
    ]

    # 這批元件已經是通過 is_candidate() 篩選過的候選（clickable/checkable 或
    # ALWAYS_KEEP_CLASSES 文字類型），Android 的 clickable/checkable 屬性本身就是
    # 系統直接回報的可靠訊號，不像 iOS 得靠 class 白名單用猜的，所以這裡不再另外
    # 問 LLM「這個東西看起來像不像可以點擊」——問了也只是重新猜一次系統早就告訴我們
    # 的答案，還會把 ALWAYS_KEEP_CLASSES 這種「不管可不可點擊都要留著」的文字類元件
    # 誤判掉（實測發生過：純顯示用的卡片標題文字被判定「不像可互動」而整筆消失）。
    # 一律收錄、只生成 description。
    if screenshot_available:
        prompt = f"""這是一張 Android App 畫面「{screen_name}」的截圖，附上一批「沒有 resource-id」
的 UI 元件清單，每個元件都附上它在畫面上的座標範圍 (bounds，單位為像素)。

請對照截圖中對應座標位置的視覺內容，加上 class 類型與鄰近文字線索，推論每個元件的用途，
用一句簡短的繁體中文描述。

元件清單（JSON）：
{json.dumps(payload, ensure_ascii=False)}

請回傳一個 JSON 陣列，格式：
[{{"temp_id": "<與輸入相同的temp_id>", "description": "<用途描述>"}}]

只回傳 JSON，不要有其他文字。
"""
        contents = [types.Part.from_bytes(data=screenshot_png, mime_type="image/png"), prompt]
    else:
        prompt = f"""這是一個 Android App 畫面「{screen_name}」上，一批「沒有 resource-id」的 UI 元件清單。
**這支 App 有螢幕保護機制，這次沒有截圖可以參考**，請只根據 class 類型、鄰近文字、
畫面九宮格分區位置這些結構化線索，推論每個元件的用途，用一句簡短的繁體中文描述。
如果線索不足以判斷具體用途，描述可以寫「用途不明，需人工複核」，不要瞎猜。

元件清單（JSON）：
{json.dumps(payload, ensure_ascii=False)}

請回傳一個 JSON 陣列，格式：
[{{"temp_id": "<與輸入相同的temp_id>", "description": "<用途描述>"}}]

只回傳 JSON，不要有其他文字。
"""
        contents = prompt

    response = generate_content_with_retry(
        model=LLM_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json"),
    )

    raw_text = (response.text or "").strip()
    try:
        result_list = json.loads(raw_text)
    except json.JSONDecodeError:
        print(f"  [警告] 無id元件描述生成失敗，回傳非合法JSON: {raw_text}")
        return {}

    return {
        item["temp_id"]: item.get("description", "")
        for item in result_list if "temp_id" in item
    }


# ---------------------------------------------------------------------------
# 6. 主流程：掃描一個畫面，產生完整元件地圖資料
# ---------------------------------------------------------------------------

def scan_screen(driver, screen_name: str) -> dict:
    print(f"開始掃描畫面: {screen_name}")

    window_size = driver.get_window_size()
    screen_width, screen_height = window_size["width"], window_size["height"]

    is_stable = wait_for_screen_stable(driver, screen_width, screen_height)

    page_source, screenshot_png = capture_screen(driver)
    screenshot_img = Image.open(io.BytesIO(screenshot_png))
    scale_x = screenshot_img.size[0] / screen_width
    scale_y = screenshot_img.size[1] / screen_height

    screenshot_blocked = is_screenshot_blocked(screenshot_png)
    if screenshot_blocked:
        print("  [警告] 截圖偵測為單一顏色（可能是 FLAG_SECURE 保護），改用純文字線索判斷用途")

    # 這張存檔用的截圖固定用「捲動前、最頂部」的畫面，當作這個畫面在 viewer.html
    # 裡的代表截圖（跟捲動收集過程中每一輪各自的截圖是分開的用途）
    # 檔名加上掃描時間：每份 map 的根畫面都叫 root_screen，只用畫面名稱當檔名的話，
    # 任何一次掃描都會蓋掉別份 map 的截圖，匯入舊 map 會看到最新那次掃描的圖
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    scanned_at = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
    screenshot_path = SCREENSHOTS_DIR / f"{screen_name}_{scanned_at}.png"
    screenshot_img.save(screenshot_path)

    scroll_collected = has_scrollable_content(page_source)
    if scroll_collected:
        print("  [偵測到可捲動內容] 開始邊捲邊抓，收集完整清單...")
        raw_candidates, round_screenshots = collect_candidates_with_scroll(driver, screen_width, screen_height)
        candidates = downgrade_duplicate_native_ids(raw_candidates)
    else:
        candidates = parse_candidate_elements(page_source, screen_width, screen_height)
        round_screenshots = [screenshot_png]

    print(f"  找到候選元件數量: {len(candidates)}")

    native_elements = [el for el in candidates if el["native_id"]]
    generated_elements = [el for el in candidates if not el["native_id"]]
    print(f"  有native id: {len(native_elements)} 個 / 無id: {len(generated_elements)} 個")

    native_descriptions = describe_native_elements(screen_name, native_elements)

    result_elements = []
    for el in native_elements:
        unique_id = make_native_unique_id(screen_name, el["class"], el["native_id"])
        description_key = f"{el['class']}::{el['native_id']}"
        result_elements.append({
            "unique_id": unique_id,
            "id_source": "native",
            "native_id": el["native_id"],
            "class": el["class"],
            "label": el["label"],
            "value": el["value"],
            "bounds": el["bounds"],
            "clickable": el["clickable"],
            "checkable": el["checkable"],
            "enabled": el["enabled"],
            "description": native_descriptions.get(description_key, ""),
            "description_source": "llm_generated",
            "status": "active",
        })

    # 依「哪一輪捲動收集到的」分組，各自用那一輪的截圖送LLM/裁切——不同輪次的
    # 截圖畫面內容不一樣（捲到不同位置），混用會讓LLM看錯畫面、裁切裁到錯誤內容。
    # 沒有捲動收集時，所有元件都屬於第0輪（唯一一輪），行為等同原本邏輯。
    elements_by_round: dict[int, list[dict]] = {}
    for el in generated_elements:
        elements_by_round.setdefault(el.get("_round_index", 0), []).append(el)

    seen_unique_ids: dict[str, int] = {}
    for round_index, round_elements in elements_by_round.items():
        round_screenshot_png = round_screenshots[round_index]
        temp_ids = [f"e{round_index}_{i}" for i in range(len(round_elements))]
        generated_descriptions = describe_generated_elements(
            screen_name, round_screenshot_png, not screenshot_blocked, round_elements, temp_ids
        )

        for tid, el in zip(temp_ids, round_elements):
            description = generated_descriptions.get(tid, "")

            fingerprint = {
                "class": el["class"],
                "label_text": el["label"],
                "position_bucket": el["position_bucket"],
                "sibling_index": el["sibling_index"],
                "nearby_text": el["nearby_text"],
            }
            unique_id = make_generated_unique_id(screen_name, fingerprint)

            if unique_id in seen_unique_ids:
                seen_unique_ids[unique_id] += 1
                print(f"  [警告] unique_id 碰撞，加上編號後綴區隔: {unique_id}_{seen_unique_ids[unique_id]}")
                unique_id = f"{unique_id}_{seen_unique_ids[unique_id]}"
            else:
                seen_unique_ids[unique_id] = 1

            crop_path = None
            if not screenshot_blocked:
                crop_path = CROPS_DIR / f"{screen_name}_{unique_id.split('::')[-1]}.png"
                crop_saved = crop_element_screenshot(round_screenshot_png, el["bounds"], crop_path, scale_x, scale_y)
                if not crop_saved:
                    print(f"  [警告] 元件超出截圖可視範圍，略過裁切: {unique_id}")
                    crop_path = None

            result_elements.append({
                "unique_id": unique_id,
                "id_source": "generated",
                "native_id": None,
                "class": el["class"],
                "label": el["label"],
                "value": el["value"],
                "bounds": el["bounds"],
                "clickable": el["clickable"],
                "checkable": el["checkable"],
                "enabled": el["enabled"],
                "fingerprint": fingerprint,
                "description": description,
                "description_source": "llm_generated",
                "screenshot_crop": str(crop_path) if crop_path else None,
                "status": "active",
            })

    screen_data = {
        "screen_name": screen_name,
        "screen_id_source": "manual",
        "platform": "android",
        "last_scanned": time.strftime("%Y-%m-%d %H:%M:%S"),
        "screen_width": screen_width,
        "screen_height": screen_height,
        "screenshot": str(screenshot_path),
        "screenshot_blocked": screenshot_blocked,
        "screen_stable_at_scan": is_stable,
        "scroll_collected": scroll_collected,
        "scroll_rounds": len(round_screenshots),
        "elements": result_elements,
    }

    print(f"  完成，共產出 {len(result_elements)} 筆元件資料"
          + ("" if is_stable else "（⚠️ 掃描當下畫面未確認穩定，建議人工複核）"))
    return screen_data


# ---------------------------------------------------------------------------
# 7. 進入點
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="掃描目前 Android 畫面，產生元件地圖")
    parser.add_argument("--screen_name", required=True, help="畫面名稱，例如 login_screen")
    parser.add_argument(
        "--output", default=None,
        help="輸出檔案路徑，預設寫入 element_map.json（跟其他工具共用的預設檔）。"
             "指定成別的路徑時，同一個畫面重掃會覆蓋該檔案裡同名畫面的資料、"
             "不影響 element_map.json，方便先備份現況再產生新版本來跑改版比對。"
    )
    args = parser.parse_args()
    output_path = Path(args.output) if args.output else OUTPUT_JSON_PATH

    options = UiAutomator2Options().load_capabilities(ANDROID_DESIRED_CAPS)
    driver = webdriver.Remote(APPIUM_SERVER_URL, options=options)

    try:
        screen_data = scan_screen(driver, args.screen_name)
        save_to_json(screen_data, output_path)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
