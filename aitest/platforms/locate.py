"""
無 native id 元件（generated）的定位流程，iOS／Android 共用一份（報告 #7：原本兩個平台
各寫一套、內容幾乎一樣）。

平台物件只提供這幾樣，流程本身都在這裡：
  - 查一次：class_text_by + class_text_query()，用 class + 文字查元件
    （iOS 是 predicate，Android 是 XPath 同時比 @text、@content-desc）
  - 捲一次：scroll_once()，回傳 True（有捲動）／False（捲不動了）／None（平台沒辦法回報，
    改用捲動前後 page_source 有沒有變判斷）
  - 畫面上的可捲動容器：scrollable_regions()
  - 找錨點：find_anchor()，用鄰近文字找到唯一一個元件（Android 會邊捲邊找）
  - 最後備援：locate_fallback()（Android 的 UiScrollable.scrollIntoView；iOS 沒有）

定位順序：
  1. class + 文字精確查詢；查不到就自己捲動、每輪重查（先捲畫面主要區域，再逐一捲
     畫面上其他可捲動容器）。查到唯一一個才用；查到多個是真的有歧義，捲再多也不會
     變唯一，直接進下一層
  2. 用 fingerprint 的 nearby_text 當錨點，在所有同 class + 文字的候選裡挑 Y 座標離錨點
     最近的一個（清單裡「每列都有一個文字相同的操作按鈕」，見 TROUBLESHOOTING #36）
  3. 平台的最後備援
  4. 都找不到回傳 None，呼叫端改用座標
"""

import xml.etree.ElementTree as ET

from selenium.common.exceptions import InvalidSessionIdException, WebDriverException

from aitest.platforms.base import main_scroll_area, shrink_bounds


def find_by_class_and_text(platform, driver, class_name: str, label: str) -> list:
    return driver.find_elements(platform.class_text_by, platform.class_text_query(class_name, label))


def locate_with_scroll(ctx, platform, driver, el: dict, max_rounds: int = 8):
    """
    用 class + 文字查，查不到就自己捲動、每輪重查同一條查詢：
      - 剛好 1 個 → 直接用
      - >1 個 → 真的有歧義，捲再多也不會變唯一（繼續捲反而可能捲進更多同文字元件），
        直接放棄交給錨點消歧義
      - 0 個 → 先捲涵蓋畫面主要範圍的通用區域，捲到底還是 0 個，再對畫面上每個可捲動
        容器（巢狀在主容器裡、或跟主容器平行的獨立區塊都抓得到）用它自己的範圍重複一次

    為什麼兩個平台都要自己捲：Android 畫面外的元件常常連節點都還沒被建出來；iOS 雖然
    捲出畫面外的元件多半還找得到，但長的 Table／CollectionView 有 cell 重用，離畫面夠遠的
    row 會整個從 tree 上消失（見 TROUBLESHOOTING #16/#27/#28）。

    每一輪查的都是同一條「有比對 class」的精確查詢，不會像 UiSelector().text(X) 那樣
    完全不管 class，誤抓到文字剛好一樣但 class 不同的別的元件。
    """
    label = el.get("label", "")
    if not label:
        return None
    query = platform.class_text_query(el["class"], label)

    def _query():
        try:
            return driver.find_elements(platform.class_text_by, query)
        except InvalidSessionIdException:
            raise
        except WebDriverException as e:
            print(f"    [XPath+捲動] 查詢失敗: {query}  ({type(e).__name__})")
            return []

    matches = _query()
    print(f"    [XPath+捲動] 第0輪（不捲）: {query} -> 查到 {len(matches)} 個")
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return None  # 真的有歧義，交給呼叫端的錨點消歧義處理

    def _scroll_search(area: dict, area_name: str):
        previous_source = None
        for round_index in range(1, max_rounds + 1):
            ctx.raise_if_stopped()
            moved = platform.scroll_once(driver, area, "down")
            if moved is False:
                print(f"    [XPath+捲動] {area_name} 已經捲不動了，停止")
                return None
            if moved is None:
                # 平台沒辦法回報這次有沒有真的捲動（iOS），改用捲動前後畫面有沒有變判斷，
                # 可靠度略低（見 capture_screen_map.collect_candidates_with_scroll 的說明）
                current_source = driver.page_source
                if current_source == previous_source:
                    print(f"    [XPath+捲動] {area_name} 捲動前後畫面沒變，判定捲不動了，停止")
                    return None
                previous_source = current_source
            found = _query()
            print(f"    [XPath+捲動] {area_name} 第{round_index}輪: 查到 {len(found)} 個")
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                return None  # 真的有歧義
        return None

    size = driver.get_window_size()
    found = _scroll_search(main_scroll_area(size["width"], size["height"]), "主要區域")
    if found is not None:
        return found

    try:
        regions = platform.scrollable_regions(driver.page_source)
    except InvalidSessionIdException:
        raise
    except (WebDriverException, ET.ParseError):
        regions = []
    for region in regions:
        # 捲容器前先縮邊界，手勢不會落在容器邊緣外（原本只有 Android 這樣做）
        found = _scroll_search(shrink_bounds(region["bounds"]), f"容器（{region['class']}）")
        if found is not None:
            return found
    return None


def locate_by_anchor(platform, driver, el: dict):
    """
    目標元件本身的 class + 文字不足以唯一定位時（清單裡每一列都有一個文字一模一樣的
    操作按鈕，例如全部都叫「刪除」），改用 fingerprint 裡的 nearby_text（通常是同一列裡
    能唯一辨識這一列的文字，例如「項目 003」）當錨點：
      1. 找到錨點（平台的 find_anchor；Android 會順便把它捲進畫面）
      2. 在畫面上所有符合 class + 文字的候選裡，挑 Y 座標離錨點最近的一個（同一列的元件
         Y 座標通常很接近，比「隨便挑第一個符合的」可靠得多）
    """
    nearby_text_list = (el.get("fingerprint") or {}).get("nearby_text") or []
    label = el.get("label", "")
    if not nearby_text_list or not label:
        return None

    anchor_text = nearby_text_list[0]
    anchor = platform.find_anchor(driver, anchor_text)
    if anchor is None:
        return None
    anchor_y = anchor.location["y"]

    try:
        candidates = find_by_class_and_text(platform, driver, el["class"], label)
    except InvalidSessionIdException:
        raise
    except WebDriverException:
        return None
    if not candidates:
        return None

    closest = min(candidates, key=lambda c: abs(c.location["y"] - anchor_y))
    print(f"    [錨點消歧義] 用鄰近文字「{anchor_text}」（y={anchor_y}）"
          f"從 {len(candidates)} 個同文字候選中挑出最接近的一個")
    return closest


def locate_generated_element(ctx, platform, driver, el: dict):
    """無 native id 元件的定位，順序見模組說明。找不到回傳 None，呼叫端改用座標"""
    label = el.get("label", "")
    if not label:
        print("    [定位] label 為空，直接改用座標")
        return None

    matched = locate_with_scroll(ctx, platform, driver, el)
    source = "XPath（含捲動）"
    if matched is None:
        matched = locate_by_anchor(platform, driver, el)
        source = "鄰近文字錨點"
    if matched is None and platform.fallback_label:
        matched = platform.locate_fallback(driver, el)
        source = platform.fallback_label
    if matched is None:
        return None

    attrs = "  ".join(f"{k}={v!r}" for k, v in platform.match_preview(matched).items())
    print(f"    [命中元件屬性（{source}）] location={matched.location}  size={matched.size}  "
          f"{attrs}  enabled={matched.get_attribute('enabled')!r}")
    return matched

