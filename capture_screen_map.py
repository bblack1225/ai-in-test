"""
POC: App 元件地圖產生器（第一步），iOS / Android 共用

功能：
  對「目前畫面」執行一次掃描，抓取畫面上所有可視節點（不預先用 class 白名單
  篩選，這些原始屬性會照實存起來，交給 viewer 篩選），依照有無 native id
  （iOS 的 accessibilityIdentifier／Android 的 resource-id）分流處理，產生每個
  元件的唯一 ID，並請 LLM 生成用途描述，最後把結果存成 JSON 檔案（兩個平台共用
  同一份 element_map.json、同一套 schema，用 "platform" 欄位標記來源）。

  畫面上如果偵測到可捲動容器，會自動「邊捲邊抓」：反覆讀取候選元件、往下捲
  一頁、再讀一次，直到判定已捲到底或達到輪次上限為止，把各輪抓到的元件依內容
  （不看捲動位置）去重合併成一份完整清單，不需要再手動捲動、分開存成不同
  screen_name。垂直捲動之間若偵測到巢狀的橫向卡片列，會先原地左右捲完再繼續往下。

  掃描流程兩個平台共用這一份；解析 XML、判斷可捲動容器、捲動手勢、橫向容器
  收集這些平台差異在 platforms/ios.py、platforms/android.py（主要差異：Android
  的 `mobile: scrollGesture` 會回報「這次有沒有真的捲動」，iOS 沒有等效的回傳值，
  只能用內容比對當停止依據，可靠度略低，見 TROUBLESHOOTING.md 問題42）。

使用方式：
  1. 手動操作手機導航到你要掃描的畫面
  2. 執行： python capture_screen_map.py --screen_name login_screen            （預設 iOS）
           python capture_screen_map.py --platform android --screen_name login_screen
  3. 結果會寫入 / 更新 element_map.json（同一個檔案，畫面資料會累加），
     或用 --output 寫到別的檔案（方便先備份現況再產生新版本來跑改版比對）
  4. 加上 --save_crops 時，無 id 元件的裁切截圖會存到 crops/ 資料夾，方便人工核對
     LLM 描述（預設不存：沒有任何程式讀這些小圖，viewer 是用整張截圖＋座標畫框，
     而且文字會變的元件每掃一次檔名就不同，會一直累積；清理舊檔見 cleanup_images.py）

執行前準備：
  1. iOS：Mac + Xcode，WebDriverAgent 已完成第一次簽署安裝，iPhone 用 USB 接上 Mac
     Android：Android Studio + SDK + 模擬器（或實體裝置），adb 看得到裝置
  2. 啟動 Appium Server: `appium`（需已安裝 xcuitest / uiautomator2 driver）
  3. 設定環境變數 GEMINI_API_KEY
  4. 裝置設定填在 config.toml（範本 config.example.toml）
"""

import argparse
import hashlib
import io
import json
import time
from pathlib import Path

from appium import webdriver
from google.genai import types
from PIL import Image

from test_config import APPIUM_SERVER_URL, LLM_MODEL, generate_content_with_retry

# ---------------------------------------------------------------------------
# 1. 輸出路徑設定
# ---------------------------------------------------------------------------

OUTPUT_JSON_PATH = Path("element_map.json")
CROPS_DIR = Path("crops")
SCREENSHOTS_DIR = Path("screenshots")


# ---------------------------------------------------------------------------
# 2. 抓取畫面：截圖 + Accessibility Tree
# ---------------------------------------------------------------------------

def capture_screen(driver) -> tuple[str, bytes]:
    """回傳 (page_source_xml, screenshot_png_bytes)"""
    page_source = driver.page_source
    screenshot_png = driver.get_screenshot_as_png()
    return page_source, screenshot_png


def is_screenshot_blocked(screenshot_png: bytes, sample_step: int = 17, threshold: float = 0.98) -> bool:
    """
    偵測這張截圖是不是被截圖保護（券商/銀行類 App 常見，Android 是 FLAG_SECURE）
    擋掉的純黑（或近乎單一顏色）圖。做法：等間隔取樣像素，如果同一個顏色佔比
    超過 threshold，視為被擋。用取樣而非逐像素檢查，避免大圖檢查太慢。
    """
    image = Image.open(io.BytesIO(screenshot_png)).convert("RGB")
    pixels = list(image.getdata())[::sample_step]
    if not pixels:
        return False
    most_common_count = max(pixels.count(p) for p in set(pixels))
    return (most_common_count / len(pixels)) >= threshold


def candidate_signature(candidates: list[dict]) -> tuple:
    """把候選元件清單濃縮成一個可比較的簽章，用來判斷兩次讀取的畫面內容是否一致"""
    return tuple(sorted(
        (el["class"], el["native_id"] or el["label"], el["position_bucket"])
        for el in candidates
    ))


def wait_for_screen_stable(platform, driver, screen_width: int, screen_height: int,
                            max_polls: int = 5, poll_interval_sec: float = 1.5) -> bool:
    """
    輪詢直到連續兩次讀到的候選元件內容一致，判定畫面已經穩定
    （不再是非同步資料載入中，例如 Apple Health 資料還沒回來、佔位符還沒換成真實內容）。

    這個檢查只反覆讀 page_source 解析候選元件，不呼叫 LLM、不截圖，成本很低。

    注意：這個機制只能解決「暫時性、之後會定下來」的不穩定（例如非同步載入），
    對於「設計上就會一直變」的內容（輪播 banner、即時跳動的數字/文案）沒有效果——
    這種情況輪詢到 max_polls 還是會判定不穩定，屬於預期行為，代表這個畫面本身
    有需要人工留意的動態內容，不是輪詢邏輯有問題。
    """
    previous_signature = None
    for attempt in range(1, max_polls + 1):
        page_source = driver.page_source
        candidates = parse_candidate_elements(platform, page_source, screen_width, screen_height)
        signature = candidate_signature(candidates)

        if signature == previous_signature:
            print(f"  [畫面穩定檢查] 第{attempt}次讀取與前一次一致，判定畫面已穩定")
            return True

        previous_signature = signature
        if attempt < max_polls:
            print(f"  [畫面穩定檢查] 第{attempt}次讀取，內容與前一次不同，"
                  f"{poll_interval_sec}秒後重讀確認...")
            time.sleep(poll_interval_sec)

    print(f"  [警告] 輪詢{max_polls}次後畫面內容依然持續變化，可能有非同步載入尚未完成、"
          f"或本身就是輪播/即時更新的動態內容。這次掃描結果會照最後一次讀到的繼續產出，"
          f"但需要人工複核這次資料是否可信（見 screen_stable_at_scan 欄位）")
    return False


# ---------------------------------------------------------------------------
# 3. 解析元件：各平台把 XML 解析成候選元件（platform.parse_candidates_raw），
#    這裡處理兩邊共用的後續步驟
# ---------------------------------------------------------------------------

def position_bucket(x: float, y: float, width: float, height: float,
                     screen_width: int, screen_height: int) -> str:
    """把元件中心點換算成九宮格分區（例如 top_left, middle_center, bottom_right）"""
    center_x = x + width / 2
    center_y = y + height / 2

    col = "left" if center_x < screen_width / 3 else (
        "right" if center_x > screen_width * 2 / 3 else "center")
    row = "top" if center_y < screen_height / 3 else (
        "bottom" if center_y > screen_height * 2 / 3 else "middle")

    return f"{row}_{col}"


def downgrade_duplicate_native_ids(platform, candidates: list[dict]) -> list[dict]:
    """
    native id 常常是「版型內部元件 id」，在清單畫面上（iOS 的 UITableView/
    UICollectionView、Android 的 RecyclerView/ListView），每一列/每一格可能共用
    同一個 id（例如每一列的「刪除」按鈕都叫同一個 id）。這種情況下這個 id 不具備
    「在這個畫面上唯一辨識一個元件」的能力，硬當 native id 用會讓多個不同元件算出
    同一個 unique_id、LLM 描述也會互相覆蓋。因此在這裡統計 (class, native_id)
    出現次數，超過 1 次的一律降級為「無 native id」，改走 fingerprint（label/位置）
    那條路去區分（見 TROUBLESHOOTING #26）。

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
        print(f"  [警告] 偵測到 {platform.native_id_name} 在同畫面重複使用（清單版型常見），"
              f"改用文字/位置指紋辨識: {sorted(downgraded)}")

    return candidates


def parse_candidate_elements(platform, xml_source: str, screen_width: int, screen_height: int) -> list[dict]:
    """單一快照解析 + 降級，沒有捲動內容的畫面用這個。"""
    return downgrade_duplicate_native_ids(platform, platform.parse_candidates_raw(xml_source, screen_width, screen_height))


def candidate_identity_key(el: dict) -> tuple:
    """
    跨捲動輪次合併去重用的識別 key，刻意不看 bounds/position_bucket/sibling_index——
    這些會隨著捲動位置改變（同一個元件捲到不同位置，畫面座標當然不一樣），
    只看不受捲動影響的內容線索。

    有 native_id 的也一併把 label 併入 key，不能只信任 (class, native_id) 就
    當作唯一——這個階段的 native_id 還沒經過 downgrade_duplicate_native_ids()
    的同畫面重複檢查（那一步要等所有輪次合併完才能做），如果同一個 id 被畫面上
    多個不同項目共用（清單版型常見，見問題26），單用 (class, native_id) 當合併
    key 會把這些不同元件誤判成「同一個在不同輪次重複出現」而錯誤合併成一筆
    ——這是 Android 實測時真的發生過的bug（83筆「項目 001」~「項目 083」被誤合併
    成1筆），併入 label 之後，同一個 id 但文字不同的項目會拿到不同的key。

    沒有 native_id 的用 class+文字+鄰近文字組合當近似身分。這不是完美的判斷
    （兩個文字內容剛好一樣的不同元件會被誤判成同一個），但跟這個專案其他地方
    接受的指紋精確度是同一個量級（見 TROUBLESHOOTING.md 問題18、24），足夠實用。
    """
    if el["native_id"]:
        return ("id", el["class"], el["native_id"], el["label"])
    return ("fp", el["class"], el["label"], tuple(el["nearby_text"]))


def identity_keys_in_region(candidates: list[dict], region_bounds: dict) -> set:
    """回傳中心點落在 region_bounds 內的候選元件的識別 key 集合，用來比較
    一次橫向滑動前後「這個容器裡露出來的是不是換了一批元件」。"""
    b = region_bounds
    x0, x1 = b["x"], b["x"] + b["width"]
    y0, y1 = b["y"], b["y"] + b["height"]
    keys = set()
    for el in candidates:
        eb = el["bounds"]
        cx = eb["x"] + eb["width"] / 2
        cy = eb["y"] + eb["height"] / 2
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            keys.add(candidate_identity_key(el))
    return keys


def region_signature(region: dict) -> tuple:
    """
    橫向捲動容器跨輪次去重用的識別 key，刻意不含 y 座標——同一個容器在整個
    畫面垂直捲動過程中，y 會隨捲到哪個位置而改變，但 x/寬/高通常不變，用這三項
    加上 class 當身分，同一容器在不同垂直輪次重複被看到時才能正確判斷「已經
    處理過」。（兩個不同橫向容器剛好同寬同高撞在一起的情況一樣接受，這種精確度
    跟專案其他地方採用的指紋精確度是同一個量級，見問題18/24）
    """
    b = region["bounds"]
    return (region["class"], b["width"], b["height"])


# ---------------------------------------------------------------------------
# 4. 邊捲邊抓
# ---------------------------------------------------------------------------

def read_and_merge_round(platform, driver, screen_width: int, screen_height: int,
                         merged: dict, round_screenshots: list) -> tuple:
    """
    讀取「目前這個當下」的畫面（不論垂直捲到哪、還是正在水平捲某個巢狀容器），
    截圖存證、解析候選元件，依內容識別合併進 merged（見 candidate_identity_key），
    回傳 (新增幾個, 這輪讀到幾個, page_source, 這輪的 candidate_signature, raw候選清單)。
    垂直收集迴圈跟橫向子容器收集迴圈（platforms/*.py 的 collect_horizontal_region）
    共用這個函式，讓兩者讀到的元件、截圖都能進同一份 merged / round_screenshots。
    """
    page_source = driver.page_source
    screenshot_png = driver.get_screenshot_as_png()
    round_index = len(round_screenshots)
    round_screenshots.append(screenshot_png)

    raw = platform.parse_candidates_raw(page_source, screen_width, screen_height)
    new_count = 0
    for el in raw:
        key = candidate_identity_key(el)
        if key not in merged:  # 以第一次出現的輪次為準，保持結果穩定、可預期
            el["_round_index"] = round_index
            merged[key] = el
            new_count += 1
    return new_count, len(raw), page_source, candidate_signature(raw), raw


def collect_candidates_with_scroll(platform, driver, screen_width: int, screen_height: int,
                                    max_rounds: int = 15) -> tuple[list[dict], list[bytes]]:
    """
    邊捲邊抓：反覆讀取候選元件、往下捲一頁、再讀一次，直到判定已經捲到底或達到
    輪次上限為止，把每一輪抓到的候選元件依內容識別（不看捲動位置）去重合併成
    一份完整清單。

    每一輪垂直捲動之間，如果畫面上出現巢狀的橫向捲動容器（例如「推薦專區」
    橫向卡片列），會先原地把它左右捲完收集完整，再繼續往下捲整個畫面（見
    TROUBLESHOOTING.md 問題34/42：一開始只處理垂直方向，橫向清單項目多到
    超出初次渲染範圍時會漏收，這裡補上）。同一個橫向容器（用 class+寬+高
    辨識，見 region_signature）只會處理一次。

    「捲到底了沒」兩個平台判斷方式不同（platform.scroll_screen 的回傳值）：
      - Android：`mobile: scrollGesture` 會回傳布林值，直接告訴我們「這次捲動
        有沒有造成畫面位移」，這是系統層級的權威判斷，回傳 False 就停
      - iOS：`mobile: scroll` 沒有對應的回傳值（回傳 None），只能退回用「連續兩輪
        內容完全一樣」當判斷依據——已知弱點：如果畫面上剛好有一個超過整個螢幕
        高度的超大元件，捲一次後畫面可能還在這個元件範圍內、內容看起來沒變，
        會被誤判成「已經到底」。這是目前 iOS 版跟 Android 版在可靠度上的已知落差

    回傳 (merged_candidates, round_screenshots)：round_screenshots 是每一輪
    當下的截圖 bytes，每個候選元件會記錄自己是在第幾輪抓到的（"_round_index"，
    內部欄位，不會寫進最終JSON），供後續生成 LLM 描述、裁切截圖時取用「那一輪」
    的畫面，而不是誤用最後一輪或第一輪的截圖去描述其他輪次抓到的元件。
    """
    merged: dict[tuple, dict] = {}
    round_screenshots: list[bytes] = []
    processed_horizontal: set = set()
    previous_signature = None

    for round_index in range(max_rounds):
        new_count, raw_len, page_source, signature, _ = read_and_merge_round(
            platform, driver, screen_width, screen_height, merged, round_screenshots
        )
        print(f"  [捲動收集] 第{round_index + 1}輪：這輪讀到{raw_len}個候選，"
              f"新增{new_count}個，累積合併{len(merged)}個不重複元件")

        # 這一輪垂直位置上，如果有還沒處理過的巢狀橫向容器，先原地把它左右捲完
        for region in platform.horizontal_scroll_candidates(page_source):
            sig = region_signature(region)
            if sig in processed_horizontal:
                continue
            processed_horizontal.add(sig)
            platform.collect_horizontal_region(
                driver, region, screen_width, screen_height, merged, round_screenshots
            )

        # 捲動沒有回報值的平台（iOS），靠「連續兩輪垂直捲動前讀到的內容完全一致」判斷到底
        if not platform.scroll_reports_movement:
            if signature == previous_signature:
                print(f"  [捲動收集] 連續兩輪內容完全一致，判定已捲到底，停止")
                break
            previous_signature = signature

        moved = platform.scroll_screen(driver, screen_width, screen_height, "down")
        if moved is False:
            print(f"  [捲動收集] 系統回報已經無法再往下捲，判定已捲到底，停止")
            break
        time.sleep(0.5)  # 給畫面一點時間反應捲動動畫/懶載入
    else:
        print(f"  [警告] 捲動收集達到輪次上限（{max_rounds}輪）仍未偵測到捲到底，"
              f"這個畫面可能還有內容沒收集完整，建議人工複核")

    # 捲動收集完後畫面停在很下面，捲回頂部，避免影響後續操作（例如接著要點擊
    # 別的元件、或換下一個畫面掃描時，手機還停留在這個畫面捲到一半的位置）。
    # 用跟收集階段同樣大小的手勢重複捲，而不是一次用超大幅度——實測發現一次性的
    # 超大幅度捲動手勢可能異常緩慢甚至卡住。
    for _ in range(max_rounds):
        if platform.scroll_screen(driver, screen_width, screen_height, "up") is False:
            break
        time.sleep(0.3)

    return list(merged.values()), round_screenshots


# ---------------------------------------------------------------------------
# 5. 產生唯一 ID
# ---------------------------------------------------------------------------

def make_native_unique_id(screen_name: str, class_name: str, native_id: str) -> str:
    # class 一併納入組合，避免同一個 native_id 出現在不同 class（例如按鈕與其內部文字子元件）時互相覆蓋
    return f"{screen_name}::native::{class_name}::{native_id}"


def make_generated_unique_id(screen_name: str, fingerprint: dict) -> str:
    """指紋內容做 hash，當作沒有 native id 的元件的唯一 ID"""
    canonical = json.dumps(fingerprint, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:8]
    return f"{screen_name}::gen::{digest}"


# ---------------------------------------------------------------------------
# 6. LLM：批次生成「有 native id」元件的用途描述
# ---------------------------------------------------------------------------

def describe_native_elements(platform, screen_name: str, elements: list[dict]) -> dict:
    """
    elements: 有 native_id 的候選元件清單
    回傳 {class::native_id: description} 的對照表
    """
    if not elements:
        return {}

    payload = [
        {
            "native_id": el["native_id"],
            "class": el["class"],
            "label": el["label"],
            "value": el["value"],
        }
        for el in elements
    ]

    prompt = f"""這是一個 {platform.display_name} App 畫面「{screen_name}」上，一批已知 {platform.native_id_name} 的 UI 元件清單。
請根據每個元件的 class、label、value，推論它的用途，用一句簡短的繁體中文描述。

輸入元件清單（JSON）：
{json.dumps(payload, ensure_ascii=False)}

同一個 native_id 有可能出現在不同 class 的元件上（例如按鈕與其內部的文字子元件），
請針對每一筆輸入分別產生對應的描述，不要合併。

請回傳一個 JSON 陣列，每個項目對應一個輸入元件，格式：
[{{"native_id": "<與輸入相同的native_id>", "class": "<與輸入相同的class>", "description": "<用途描述>"}}]

只回傳 JSON，不要有其他文字。
"""

    response = generate_content_with_retry(
        model=LLM_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
        ),
    )

    usage = getattr(response, "usage_metadata", None)
    if usage:
        print(f"  [LLM token用量／native元件] prompt={usage.prompt_token_count} "
              f"output={usage.candidates_token_count} total={usage.total_token_count}"
              f"（{len(elements)} 個元件、不含截圖，一次呼叫）")

    raw_text = (response.text or "").strip()
    try:
        result_list = json.loads(raw_text)
    except json.JSONDecodeError:
        print(f"  [警告] native元件描述生成失敗，回傳非合法JSON: {raw_text}")
        return {}

    # key 用 class::native_id 組合，避免同一個 native_id 出現在不同 class 時互相覆蓋描述
    return {
        f"{item['class']}::{item['native_id']}": item["description"]
        for item in result_list if "native_id" in item and "class" in item
    }


# ---------------------------------------------------------------------------
# 7. LLM：批次生成「沒有 native id」元件的用途描述（視覺輔助）
# ---------------------------------------------------------------------------

def describe_generated_elements(platform, screen_name: str, screenshot_png: bytes, screenshot_available: bool,
                                 elements: list[dict], temp_ids: list[str]) -> dict:
    """
    elements 與 temp_ids 一一對應，回傳 {temp_id: description} 的對照表。

    screenshot_available=False 代表這個畫面被截圖保護擋掉了（見 is_screenshot_blocked），
    改成只送文字線索、不送圖，讓 LLM 根據 class / 鄰近文字 / 九宮格位置推論。

    這裡不問 LLM「這個東西看起來像不像可以點擊」——Android 的 clickable/checkable
    是系統直接回報的訊號、iOS 的 accessible 也照實記錄了，問了只是重新猜一次，
    還會把純顯示用的文字類元件誤判掉（Android 實測發生過：卡片標題文字被判定
    「不像可互動」而整筆消失）。一律收錄、只生成 description。
    """
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

    app = f"{platform.display_name} App"
    if screenshot_available:
        prompt = f"""這是一張 {app} 畫面「{screen_name}」的截圖，附上一批「沒有 {platform.native_id_name}」
的 UI 元件清單，每個元件都附上它在畫面上的座標範圍 (bounds，單位為像素)。

請對照截圖中對應座標位置的視覺內容，加上 class 類型與鄰近文字線索，推論每個元件的用途，
用一句簡短的繁體中文描述。

元件清單（JSON）：
{json.dumps(payload, ensure_ascii=False)}

請回傳一個 JSON 陣列，每個項目對應一個輸入元件，格式：
[{{"temp_id": "<與輸入相同的temp_id>", "description": "<用途描述>"}}]

只回傳 JSON，不要有其他文字。
"""
        contents = [types.Part.from_bytes(data=screenshot_png, mime_type="image/png"), prompt]
    else:
        prompt = f"""這是一個 {app} 畫面「{screen_name}」上，一批「沒有 {platform.native_id_name}」的 UI 元件清單。
**這支 App 有截圖保護，這次沒有截圖可以參考**，請只根據 class 類型、鄰近文字、
畫面九宮格分區位置這些結構化線索，推論每個元件的用途，用一句簡短的繁體中文描述。
如果線索不足以判斷具體用途，描述可以寫「用途不明，需人工複核」，不要瞎猜。

元件清單（JSON）：
{json.dumps(payload, ensure_ascii=False)}

請回傳一個 JSON 陣列，每個項目對應一個輸入元件，格式：
[{{"temp_id": "<與輸入相同的temp_id>", "description": "<用途描述>"}}]

只回傳 JSON，不要有其他文字。
"""
        contents = prompt

    response = generate_content_with_retry(
        model=LLM_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
        ),
    )

    usage = getattr(response, "usage_metadata", None)
    if usage:
        print(f"  [LLM token用量] prompt={usage.prompt_token_count} "
              f"output={usage.candidates_token_count} total={usage.total_token_count}"
              f"（{len(elements)} 個元件、{'含' if screenshot_available else '不含'}截圖，一次呼叫）")

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
# 8. 裁切無 id 元件的截圖（方便之後人工核對）
# ---------------------------------------------------------------------------

def crop_element_screenshot(screenshot_png: bytes, bounds: dict, save_path: Path,
                             scale_x: float = 1.0, scale_y: float = 1.0) -> bool:
    """裁切成功回傳 True；元件完全在截圖可視範圍外（例如水平/垂直捲動清單裡
    還沒捲到、或已捲出畫面的項目，Accessibility Tree 仍會回報其座標，
    但當下截圖根本拍不到）則回傳 False，不寫檔。"""
    image = Image.open(io.BytesIO(screenshot_png))
    # iOS 的 bounds 是 point 座標，螢幕截圖是實際像素（Retina機型會是2x/3x），
    # 裁切前需要換算成像素座標，否則實體機上會裁到錯誤位置（Android 兩者都是像素，比例是1）
    x = bounds["x"] * scale_x
    y = bounds["y"] * scale_y
    w = bounds["width"] * scale_x
    h = bounds["height"] * scale_y
    # 留一點邊界，方便看清楚元件周圍的上下文
    padding = 10
    left = max(0, min(x - padding, image.width))
    top = max(0, min(y - padding, image.height))
    right = max(0, min(x + w + padding, image.width))
    bottom = max(0, min(y + h + padding, image.height))
    if right <= left or bottom <= top:
        return False
    cropped = image.crop((left, top, right, bottom))
    save_path.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(save_path)
    return True


# ---------------------------------------------------------------------------
# 9. 主流程：掃描一個畫面，產生完整元件地圖資料
# ---------------------------------------------------------------------------

def scan_screen(platform, driver, screen_name: str, describe: bool = True, scroll: bool = True,
                wait_stable: bool = True, page_source: str | None = None, save_crops: bool = False,
                out_dir: Path | None = None) -> dict:
    """
    掃描目前畫面，產出完整元件地圖資料。

    describe=True（預設）：呼叫 LLM 為每個元件生成用途描述。
    describe=False：跳過所有 LLM 呼叫，description 留空、description_source
    標記為 "skipped"——給 crawl_app_tree.py 這種「先要結構、之後再補描述」
    的大量掃描情境用，避免每個爬到的畫面都付一次 API 成本。

    scroll=True（預設）：偵測到可捲動內容就邊捲邊抓完整清單。
    scroll=False：只解析「目前這一屏、最頂部」的快照——給爬蟲用，因為爬完
    要立刻用這批元件的 bounds 去點擊，捲動收集到的畫面外元件座標點不到。

    wait_stable=True（預設）：掃描前輪詢等畫面穩定（最多 ~7.5s）。
    wait_stable=False：跳過——給爬蟲用，因為報價類畫面永遠不會穩定，每個畫面
    白等 7.5s 是爬蟲最大的效能瓶頸。

    page_source：呼叫端如果剛好已經抓過目前這次的 page_source，可以直接傳進來，
    省一次往返——元件多的畫面（實測 1000+ 元件）單次 page_source 可以貴到 20 秒以上，
    爬蟲每個畫面進來前通常已經為了做骨架簽章 / 登出偵測抓過一次，不重抓最划算。
    傳 None（預設）自己抓一次。

    save_crops=False（預設）：不存無 id 元件的裁切小圖，screenshot_crop 留 None。
    只有要人工打開小圖核對 LLM 描述時才需要開。

    out_dir：截圖／裁切圖存到 out_dir/screenshots、out_dir/crops（給 workspace 專案用，
    傳專案資料夾）。回傳資料裡的路徑一律是相對 out_dir 的 "screenshots/xxx.png"，
    所以地圖搬到哪個資料夾都能用。None（預設）＝目前工作目錄，跟以前一樣。
    """
    print(f"開始掃描畫面: {screen_name}")

    window_size = driver.get_window_size()
    screen_width, screen_height = window_size["width"], window_size["height"]

    is_stable = wait_for_screen_stable(platform, driver, screen_width, screen_height) if wait_stable else None

    if page_source is not None:
        screenshot_png = driver.get_screenshot_as_png()
    else:
        page_source, screenshot_png = capture_screen(driver)

    screenshot_blocked = is_screenshot_blocked(screenshot_png)
    if screenshot_blocked:
        print("  [警告] 截圖偵測為單一顏色（可能是 App 的截圖保護，Android 稱為 FLAG_SECURE），"
              "改用純文字線索判斷用途、略過裁切")

    screenshot_img = Image.open(io.BytesIO(screenshot_png))
    scale_x = screenshot_img.size[0] / screen_width
    scale_y = screenshot_img.size[1] / screen_height

    # 這張存檔用的截圖固定用「捲動前、最頂部」的畫面，當作這個畫面在 viewer
    # 裡的代表截圖（跟捲動收集過程中每一輪各自的截圖是分開的用途）
    # 檔名加上掃描時間：每份 map 的根畫面都叫 root_screen，只用畫面名稱當檔名的話，
    # 任何一次掃描都會蓋掉別份 map 的截圖，匯入舊 map 會看到最新那次掃描的圖
    base_dir = out_dir or Path(".")
    (base_dir / SCREENSHOTS_DIR).mkdir(parents=True, exist_ok=True)
    scanned_at = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
    screenshot_path = SCREENSHOTS_DIR / f"{screen_name}_{scanned_at}.png"
    screenshot_img.save(base_dir / screenshot_path)

    scroll_collected = platform.has_scrollable_content(page_source) and scroll
    if scroll_collected:
        print("  [偵測到可捲動內容] 開始邊捲邊抓，收集完整清單...")
        raw_candidates, round_screenshots = collect_candidates_with_scroll(
            platform, driver, screen_width, screen_height)
        candidates = downgrade_duplicate_native_ids(platform, raw_candidates)
    else:
        candidates = parse_candidate_elements(platform, page_source, screen_width, screen_height)
        round_screenshots = [screenshot_png]

    print(f"  找到候選元件數量: {len(candidates)}")

    native_elements = [el for el in candidates if el["native_id"]]
    generated_elements = [el for el in candidates if not el["native_id"]]
    print(f"  有native id: {len(native_elements)} 個 / 無id: {len(generated_elements)} 個")

    description_source = "llm_generated" if describe else "skipped"

    # --- 處理有 native id 的元件 ---
    native_descriptions = describe_native_elements(platform, screen_name, native_elements) if describe else {}

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
            **platform.element_flags(el),
            "description": native_descriptions.get(description_key, ""),
            "description_source": description_source,
            "status": "active",
        })

    # --- 處理沒有 native id 的元件 ---
    # 依「哪一輪捲動收集到的」分組，各自用那一輪的截圖送LLM/裁切——不同輪次的
    # 截圖畫面內容不一樣（捲到不同位置），混用會讓LLM看錯畫面、裁切裁到錯誤內容。
    # 沒有捲動收集時，所有元件都屬於第0輪（唯一一輪）。
    elements_by_round: dict[int, list[dict]] = {}
    for el in generated_elements:
        elements_by_round.setdefault(el.get("_round_index", 0), []).append(el)

    seen_unique_ids: dict[str, int] = {}
    for round_index, round_elements in elements_by_round.items():
        round_screenshot_png = round_screenshots[round_index]
        temp_ids = [f"e{round_index}_{i}" for i in range(len(round_elements))]
        generated_descriptions = describe_generated_elements(
            platform, screen_name, round_screenshot_png, not screenshot_blocked, round_elements, temp_ids
        ) if describe else {}

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

            # 指紋不夠精細時（例如同一排捲動清單裡多個佔位符文字），不同元件仍可能算出
            # 一樣的 unique_id；偵測到碰撞就加上編號後綴，保證同一次掃描內絕對不重複
            if unique_id in seen_unique_ids:
                seen_unique_ids[unique_id] += 1
                print(f"  [警告] unique_id 碰撞，加上編號後綴區隔: {unique_id}_{seen_unique_ids[unique_id]}")
                unique_id = f"{unique_id}_{seen_unique_ids[unique_id]}"
            else:
                seen_unique_ids[unique_id] = 1

            crop_saved = False
            crop_path = None
            if save_crops and not screenshot_blocked:
                crop_path = CROPS_DIR / f"{screen_name}_{unique_id.split('::')[-1]}.png"
                crop_saved = crop_element_screenshot(round_screenshot_png, el["bounds"], base_dir / crop_path, scale_x, scale_y)
                if not crop_saved:
                    print(f"  [警告] 元件超出截圖可視範圍，略過裁切: {unique_id}")

            result_elements.append({
                "unique_id": unique_id,
                "id_source": "generated",
                "native_id": None,
                "class": el["class"],
                "label": el["label"],
                "value": el["value"],
                "bounds": el["bounds"],
                **platform.element_flags(el),
                "fingerprint": fingerprint,
                "description": description,
                "description_source": description_source,
                "screenshot_crop": str(crop_path) if crop_saved else None,
                "status": "active",
            })

    screen_data = {
        "screen_name": screen_name,
        "screen_id_source": "manual",
        "platform": platform.name,
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
# 10. 儲存結果：合併寫入 JSON（同一畫面重掃會覆蓋舊資料）
# ---------------------------------------------------------------------------

def save_to_json(screen_data: dict, output_path: Path):
    if output_path.exists():
        with open(output_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"screens": []}

    # 若同名畫面已存在，直接覆蓋；否則新增
    data["screens"] = [s for s in data["screens"] if s["screen_name"] != screen_data["screen_name"]]
    data["screens"].append(screen_data)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"已儲存至 {output_path}")


# ---------------------------------------------------------------------------
# 11. 進入點
# ---------------------------------------------------------------------------

def main():
    # platforms 裡的模組會 import 這支的共用函式，放在這裡 import 避免循環
    from platforms import PLATFORMS, get_platform

    parser = argparse.ArgumentParser(description="掃描目前畫面，產生元件地圖")
    parser.add_argument("--screen_name", required=True, help="畫面名稱，例如 login_screen")
    parser.add_argument("--platform", choices=sorted(PLATFORMS), default="ios", help="裝置平台（預設 ios）")
    parser.add_argument(
        "--output", default=None,
        help="輸出檔案路徑，預設寫入 element_map.json（跟其他工具共用的預設檔）。"
             "指定成別的路徑時，同一個畫面重掃會覆蓋該檔案裡同名畫面的資料、"
             "不影響 element_map.json，方便先備份現況再產生新版本來跑改版比對。"
    )
    parser.add_argument("--save_crops", action="store_true",
                        help="另外存無 id 元件的裁切小圖到 crops/，給人工核對 LLM 描述用（預設不存）")
    args = parser.parse_args()
    platform = get_platform(args.platform)
    output_path = Path(args.output) if args.output else OUTPUT_JSON_PATH

    driver = webdriver.Remote(APPIUM_SERVER_URL, options=platform.make_options())

    try:
        screen_data = scan_screen(platform, driver, args.screen_name, save_crops=args.save_crops)
        save_to_json(screen_data, output_path)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
