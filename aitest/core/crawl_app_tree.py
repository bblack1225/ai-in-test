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

iOS / Android 共用：流程（遞迴、樹/圖資料、登出偵測、App 邊界、回復機制）只有
一份；「哪些算可點、是不是同一個畫面、怎麼關對話框、怎麼返回」這些判斷方式
兩個平台差很多，在平台物件裡（platforms/ios.py、platforms/android.py 的「爬蟲用」
那一段），用 --platform 切換。

輸出：app_map_tree.json（獨立檔，不動 element_map.json）。每掃完一個畫面就
立刻整份重寫一次，中途中止也不會遺失已爬到的部分。
加 --project <專案代號> 的話改寫進 workspace/projects/<專案>/maps/<地圖>.json（地圖名稱用
--map 指定，預設 main；依功能分開爬就各取一個名稱，例如 --map trading），截圖存到專案資料夾，
網頁開同一個專案就看得到；平台、bundle id 預設用專案設定的。同名地圖已經有畫面的話，
會先備份到 backups/<地圖>-<時間>.json 再覆蓋。

使用方式：
  1. 手動把 App 導航到子樹根畫面（例如某個 tab 的 landing），保持登入、螢幕解鎖
  2. uv run aitest-crawl --root_name foo_accounting --max_depth 3 --max_screens 40
     Android 加 --platform android（App 會用啟動當下前景的那一支）
     寫進專案：加 --project foo-ios --map accounting
     先 dry-run 看會點什麼：加 --dry_run（只掃根畫面、列候選、不點）
  3. 爬蟲期間不要開著 viewer_server.py，同一台裝置同時只能有一條 Appium 連線

注意：預設 scan 時關閉「邊捲邊抓」（scroll=False），只掃每個畫面最頂部一屏，
因為爬完要立刻用 bounds 去點，捲動收集到的畫面外元件座標點不到。捲動內容的
完整收集之後再單獨處理。
"""

import argparse
import re
import time
from pathlib import Path

from appium import webdriver
from selenium.common.exceptions import InvalidSessionIdException, WebDriverException

from aitest.config import APPIUM_SERVER_URL
from aitest.core.capture_screen_map import scan_screen
from aitest.core.workspace import DEFAULT_MAP, DEFAULT_ROOT, Workspace, WorkspaceError, _write_json_atomic
from aitest.platforms import PLATFORMS, get_platform
from aitest.platforms.base import CHROME_BOTTOM_FRACTION, CHROME_TOP_FRACTION, NEVER_TAP_LABELS, wait_until_stable

OUTPUT_PATH = Path("app_map_tree.json")

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


class CrawlAborted(Exception):
    """爬到一半被登出、或無法從 App 外拉回，中止整個爬蟲（已爬的部分保留）。"""


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
    except InvalidSessionIdException:
        raise
    except WebDriverException:
        pass
    try:
        ctx = driver.current_context
        if ctx and ctx != "NATIVE_APP":
            return f"context={ctx}"
    except InvalidSessionIdException:
        raise
    except WebDriverException:
        pass
    return None


# ---------------------------------------------------------------------------
# 操作：等待穩定 / 點擊 / 返回
# ---------------------------------------------------------------------------

def tap_candidate(platform, driver, el: dict, screen_height: float) -> bool:
    """重新定位再點（避免用 scan 當下的舊座標）；定位不到才退回座標，且僅當
    座標落在畫面內。回傳是否真的點了。不捲動找元件（跟 run_test_case 不同）：
    爬蟲是剛掃完就點，元件就在畫面上，捲動反而會讓之後的座標全部失準。"""
    if el.get("native_id"):
        try:
            driver.find_element(platform.native_by, el["native_id"]).click()
            return True
        except InvalidSessionIdException:
            raise
        except WebDriverException:
            pass

    label = el.get("label", "")
    if label:
        try:
            matches = platform.find_by_class_and_text(driver, el["class"], label)
            if len(matches) == 1:
                matches[0].click()
                return True
        except InvalidSessionIdException:
            raise
        except WebDriverException:
            pass

    b = el["bounds"]
    center_y = b["y"] + b["height"] / 2
    if 0 <= center_y <= screen_height:
        platform.tap_by_coordinates(driver, b)
        return True
    return False


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


def reset_and_replay(platform, driver, path: list, state: dict, root_uid: str) -> bool:
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
    _, current = platform.skeleton(driver, src)
    if current != root_uid:
        print("    [重放路徑] 目前不在根畫面，試著用根畫面的『自我循環』候選當錨點拉回去")
        for anchor in _find_root_noop_anchors(state, root_uid):
            tap_candidate(platform, driver, anchor, screen_h)
            wait_until_stable(driver, max_wait=1.5)
            src = driver.page_source
            _, current = platform.skeleton(driver, src)
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
        if not tap_candidate(platform, driver, el, screen_h):
            print(f"    [重放路徑] 第{i + 1}步點擊失敗，放棄：{step['element_uid']}")
            return False
        wait_until_stable(driver, max_wait=1.5)
    return True


def recover_to(platform, driver, target_uid: str, tries: int = 3,
                path: list | None = None, state: dict | None = None) -> bool:
    """盡量把畫面弄回 target_uid 對應的畫面。先用 go_back() 猜幾次比較快；
    都失敗才觸發 reset_and_replay（需要呼叫端傳 path/state 才能用這個
    最後手段，沒傳就維持原本「猜到底」的行為）。"""
    for i in range(tries):
        src = driver.page_source
        platform.dismiss_modal(driver, page_source=src)
        _, current = platform.skeleton(driver, src)
        if current == target_uid:
            return True
        platform.go_back(driver)
        wait_until_stable(driver, max_wait=1.5)
    src = driver.page_source
    _, current = platform.skeleton(driver, src)
    if current == target_uid:
        return True
    if path and state is not None:
        print("    [recover] go_back 重試都失敗，改用「回根畫面＋重放已知路徑」最後手段")
        root_uid = path[0]["screen_uid"]
        if reset_and_replay(platform, driver, path, state, root_uid):
            src = driver.page_source
            _, current = platform.skeleton(driver, src)
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
    # 先寫暫存檔再改名：網頁可能剛好在讀這張地圖，不會讀到寫一半的 JSON
    _write_json_atomic(OUTPUT_PATH, payload)


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

def crawl_screen(platform, driver, suggested_name: str, parent_uid: str | None, entry_el: dict | None,
                  path: list, depth: int, cfg: dict, state: dict) -> str:
    src = driver.page_source
    dismissed = platform.dismiss_modal(driver, page_source=src)
    if dismissed:
        print(f"  [關掉對話框] {dismissed}")
        state["skipped"].append({"screen_uid": None, "reason": "dismissed_modal", "buttons": dismissed})
        src = driver.page_source

    logout = detect_logged_out(src)
    if logout:
        raise CrawlAborted(f"深度 {depth}：page_source 出現「{logout}」，判定已被登出")

    skeleton, uid = platform.skeleton(driver, src)

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
    screen_data = scan_screen(platform, driver, name, describe=cfg["describe"], scroll=False, wait_stable=False,
                               page_source=src, out_dir=cfg.get("out_dir"))

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
    chrome_keys = platform.chrome_keys(src, screen_h) if exclude_chrome else set()

    # 哪些元件算可點、元件的顯示文字、同模板 pattern：平台判斷方式不同（見
    # platforms/ios.py 的 IOSScreenAnalysis、platforms/android.py 的 AndroidScreenAnalysis）
    analysis = platform.analyze(src)

    seen_candidate_keys: set = set()
    candidates = []
    for el in screen_data["elements"]:
        el_ident = el.get("native_id") or el.get("label", "")
        if not analysis.is_candidate(el):
            continue
        el_text = analysis.text(el)
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
        if el_text.strip() in NEVER_TAP_LABELS or (el.get("native_id") or "").strip() in NEVER_TAP_LABELS:
            state["skipped"].append({
                "screen_uid": uid, "element_uid": el["unique_id"], "reason": "never_tap",
                "label": el_text,
            })
            continue
        # 去重：XCUITest tree 偶爾把同一個元件回報兩次（同 class+label+座標）
        dedup_key = (el["class"], el.get("label", ""), round(b["x"]), round(b["y"]),
                     round(b["width"]), round(b["height"]))
        if dedup_key in seen_candidate_keys:
            continue
        seen_candidate_keys.add(dedup_key)
        # el_text：iOS 就是元件自己的 label；Android 可點的常常是容器，文字在子元件上，
        # 要把子孫文字一起算進來，不然「登出」「撥打客服」這種包在容器裡的會漏判
        text_el = {**el, "label": el_text}
        if looks_external(text_el):
            state["skipped"].append({
                "screen_uid": uid, "element_uid": el["unique_id"], "reason": "external_hint",
                "label": el_text,
            })
            continue
        if cfg["safe_keywords"] and _hits_risky_keyword(text_el):
            state["skipped"].append({
                "screen_uid": uid, "element_uid": el["unique_id"], "reason": "risky_keyword",
                "label": el_text,
            })
            continue
        center_y = el["bounds"]["y"] + el["bounds"]["height"] / 2
        if not (0 <= center_y <= screen_h):
            state["skipped"].append({
                "screen_uid": uid, "element_uid": el["unique_id"], "reason": "offscreen",
                "label": el_text,
            })
            continue
        candidates.append(text_el)

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
        pat = analysis.pattern(el)
        if pat and pattern_tries.get(pat, 0) >= cfg["sample_per_pattern"] and len(pattern_children.get(pat, set())) <= 1:
            state["skipped"].append({
                "screen_uid": uid, "element_uid": el["unique_id"], "reason": "pattern_sampled",
                "label": label, "pattern": pat,
            })
            continue

        print(f"  [{i + 1}/{len(candidates)}] 點擊「{label}」")

        if not tap_candidate(platform, driver, el, screen_h):
            state["skipped"].append({
                "screen_uid": uid, "element_uid": el["unique_id"], "reason": "tap_failed",
                "label": el.get("label", ""),
            })
            continue
        wait_until_stable(driver, max_wait=1.5)

        after_src = driver.page_source
        modal_after = platform.dismiss_modal(driver, page_source=after_src)
        if modal_after:
            print(f"    [點擊後關掉對話框] {modal_after}")
            after_src = driver.page_source

        outside = went_outside_app(driver, cfg["bundle_id"])
        if outside:
            print(f"    [跑到 App 外] {outside}，activate_app 拉回、跳過這個元件")
            try:
                driver.activate_app(cfg["bundle_id"])
            except InvalidSessionIdException:
                raise
            except WebDriverException:
                pass
            wait_until_stable(driver, max_wait=1.5)
            state["edges"].append({
                "from": uid, "via": el["unique_id"], "to": None, "kind": "external",
                "label": el.get("label", ""),
            })
            state["skipped"].append({
                "screen_uid": uid, "element_uid": el["unique_id"],
                "reason": f"went_outside:{outside}", "label": el.get("label", ""),
            })
            if not recover_to(platform, driver, uid, path=path, state=state):
                print("    [警告] 拉回後無法確認回到本畫面，放棄這個畫面剩餘候選")
                break
            continue

        logout = detect_logged_out(after_src)
        if logout:
            raise CrawlAborted(f"點擊「{label}」後 page_source 出現「{logout}」，判定已被登出")

        _, child_uid = platform.skeleton(driver, after_src)
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
        # break 不能寫在 finally 裡：finally 裡的 break 會吃掉正在往上傳的例外，
        # 子畫面丟出的 CrawlAborted（例如偵測到登出）會被吞掉、爬蟲繼續點下去
        try:
            crawl_screen(platform, driver, child_suggested, uid, el, child_path, depth + 1, cfg, state)
        finally:
            recovered = recover_to(platform, driver, uid, path=path, state=state)
            if not recovered:
                print(f"    [警告] 無法確認回到「{name}」，放棄這個畫面剩餘候選")
                save_map(state, cfg["meta"])
        if not recovered:
            break
        save_map(state, cfg["meta"])

    return uid


# 舊 crawl_app.py 的風險關鍵字（--safe_keywords 才啟用）
_RISKY_KEYWORDS = [
    "登出", "退出", "離開", "刪除", "確認", "送出", "付款", "繳費", "驗證", "提交",
    "清除", "解除", "停用", "取消", "解約", "重設", "重置", "匯款", "轉帳",
    "投保", "申請", "購買", "下單", "簽署", "同意", "買進", "賣出", "改價",
    "刪單", "圈存", "生物辨識", "指紋", "Face",
]


def _hits_risky_keyword(el: dict) -> bool:
    text = f"{el.get('description', '')} {el.get('label', '')}"
    return any(k in text for k in _RISKY_KEYWORDS)


def crawl(platform, driver, cfg: dict) -> dict:
    state = {"screens": {}, "edges": [], "skipped": [], "visit_order": []}
    try:
        crawl_screen(platform, driver, cfg["root_name"], None, None, [], 0, cfg, state)
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
    parser.add_argument("--platform", choices=sorted(PLATFORMS), default=None,
                         help="裝置平台（預設：有 --project 用專案的平台，否則 ios）")
    parser.add_argument("--bundle_id", default=None,
                         help="App 的 bundle id（iOS）／package（Android），用來判斷有沒有跑出 App；"
                              "預設用專案設定的；都沒有的話用啟動當下前景的 App（iOS 會先看 config.toml 的 launch_bundle_id）")
    parser.add_argument("--project", default=None,
                         help="寫進 workspace 的這個專案（maps/<地圖>.json＋截圖），不寫 app_map_tree.json")
    parser.add_argument("--map", default=DEFAULT_MAP,
                         help=f"搭配 --project：寫進專案的哪張地圖（預設 {DEFAULT_MAP}；新名稱就新增一張）")
    parser.add_argument("--workspace", default=str(DEFAULT_ROOT), help="workspace 資料夾（預設 ./workspace）")
    args = parser.parse_args()

    global OUTPUT_PATH
    out_dir = None
    project_bundle_id = None
    if args.project:
        ws = Workspace(args.workspace)
        try:
            project = ws.get_project(args.project)
            OUTPUT_PATH = ws.map_path(args.project, args.map)
        except WorkspaceError as e:
            raise SystemExit(f"錯誤：{e}（專案要先用 aitest-workspace create 建立）") from None
        if args.platform and args.platform != project["platform"]:
            raise SystemExit(f"錯誤：專案「{args.project}」是 {project['platform']}，跟 --platform {args.platform} 不一致")
        args.platform = project["platform"]
        project_bundle_id = project.get("bundle_id") or None
        out_dir = ws.project_dir(args.project)
        backup = ws.backup_map(args.project, args.map)
        if backup:
            print(f"[爬蟲] 地圖「{args.map}」原本的內容已備份到 {backup}")
        print(f"[爬蟲] 寫進專案「{args.project}」的地圖「{args.map}」：{OUTPUT_PATH}")
    args.platform = args.platform or "ios"

    platform = get_platform(args.platform)
    bundle_id = args.bundle_id or project_bundle_id
    # 知道要爬哪支 App 的話，iOS 連線時順便把它打開
    driver = webdriver.Remote(APPIUM_SERVER_URL, options=platform.make_options(app_id=bundle_id))
    bundle_id = bundle_id or platform.app_id(driver)
    print(f"[爬蟲] 平台：{platform.name}，App：{bundle_id}")

    cfg = {
        "root_name": args.root_name,
        "max_depth": args.max_depth,
        "max_screens": args.max_screens,
        "describe": args.describe,
        "safe_keywords": args.safe_keywords,
        "dry_run": args.dry_run,
        "include_nav": args.include_nav,
        "sample_per_pattern": args.sample_per_pattern if args.sample_per_pattern > 0 else 10 ** 9,
        "bundle_id": bundle_id,
        "out_dir": out_dir,
        "meta": {
            # app_map_viewer.html 讀這個欄位判斷地圖是哪個平台
            "platform": platform.name,
            "bundle_id": bundle_id,
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

    try:
        crawl(platform, driver, cfg)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
