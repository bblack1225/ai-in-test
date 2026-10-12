"""
掃描畫面時 capture_screen_map.py 和 platforms/*.py 共用的函式：候選元件的身分 key、
九宮格分區、讀一輪畫面並合併進結果。

這支不 import 專案內任何其他模組（platform 物件由呼叫端當參數傳進來），
讓 platforms/ 不用反過來 import capture_screen_map.py。
"""


def candidate_signature(candidates: list[dict]) -> tuple:
    """把候選元件清單濃縮成一個可比較的簽章，用來判斷兩次讀取的畫面內容是否一致"""
    return tuple(sorted(
        (el["class"], el["native_id"] or el["label"], el["position_bucket"])
        for el in candidates
    ))


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
