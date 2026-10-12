"""platforms/*.py 的 page_source 解析：候選元件、native id 判斷、可捲動容器"""

from conftest import ANDROID_SCREEN, IOS_SCREEN

from aitest.core.capture_screen_map import downgrade_duplicate_native_ids, parse_candidate_elements
from aitest.platforms import android, get_platform, ios


def _by_native_id(candidates):
    return {el["native_id"]: el for el in candidates if el["native_id"]}


# ---- iOS ----------------------------------------------------------------

def test_ios_login_candidate_count(ios_login):
    assert len(ios.parse_candidates_raw(ios_login, *IOS_SCREEN)) == 52


def test_ios_login_native_ids(ios_login):
    ids = _by_native_id(ios.parse_candidates_raw(ios_login, *IOS_SCREEN))
    assert set(ids) == {"loginremeberbutton", "loginidfield", "loginpassfield", "loginbutton", "bxPhone"}
    assert ids["loginbutton"]["class"] == "XCUIElementTypeButton"
    assert ids["loginbutton"]["label"] == "登入"


def test_ios_name_equal_to_label_is_not_native_id(ios_login):
    """name 只是文字回填（name == label）的不能當 native id（TROUBLESHOOTING #14）"""
    candidates = ios.parse_candidates_raw(ios_login, *IOS_SCREEN)
    forgot = [el for el in candidates if el["label"] == "忘記密碼"]
    assert forgot and all(el["native_id"] is None for el in forgot)


def test_ios_candidate_fields(ios_login):
    el = _by_native_id(ios.parse_candidates_raw(ios_login, *IOS_SCREEN))["loginbutton"]
    assert set(el["bounds"]) == {"x", "y", "width", "height"}
    assert el["position_bucket"] in {f"{r}_{c}" for r in ("top", "middle", "bottom")
                                     for c in ("left", "center", "right")}
    assert isinstance(el["nearby_text"], list)


def test_ios_has_scrollable_content(ios_login):
    assert ios.has_scrollable_content(ios_login) is True


def test_ios_policy_candidate_count(ios_policy):
    assert len(ios.parse_candidates_raw(ios_policy, *IOS_SCREEN)) == 101


# ---- Android ------------------------------------------------------------

def test_android_skips_hidden_nodes(android_watchlist):
    labels = {el["label"] for el in android.parse_candidates_raw(android_watchlist, *ANDROID_SCREEN)}
    assert "隱藏的提示" not in labels


def test_android_candidate_count(android_watchlist):
    # 33 個有 class 的節點，扣掉 1 個 displayed=false
    assert len(android.parse_candidates_raw(android_watchlist, *ANDROID_SCREEN)) == 32


def test_android_resource_id_is_native_id(android_watchlist):
    ids = _by_native_id(android.parse_candidates_raw(android_watchlist, *ANDROID_SCREEN))
    assert ids["com.example.demo:id/btn_add"]["position_bucket"] == "bottom_center"


def test_android_resource_id_equal_to_content_desc_is_not_native_id(android_watchlist):
    """resource-id 剛好等於 content-desc 是文字回填，不能當 native id"""
    candidates = android.parse_candidates_raw(android_watchlist, *ANDROID_SCREEN)
    more = next(el for el in candidates if el["class"] == "android.widget.ImageButton")
    assert more["native_id"] is None


def test_android_label_prefers_content_desc(android_watchlist):
    """content-desc 是開發者刻意給無障礙工具的描述，優先於畫面文字 text；text 另外存在 value"""
    ids = _by_native_id(android.parse_candidates_raw(android_watchlist, *ANDROID_SCREEN))
    assert ids["com.example.demo:id/btn_add"]["label"] == "新增自選股票"
    assert ids["com.example.demo:id/btn_add"]["value"] == "新增自選"


def test_android_label_falls_back_to_text(android_watchlist):
    ids = _by_native_id(android.parse_candidates_raw(android_watchlist, *ANDROID_SCREEN))
    assert ids["com.example.demo:id/btn_delete"]["label"] == "刪除"


def test_android_nearby_text_from_siblings(android_watchlist):
    candidates = android.parse_candidates_raw(android_watchlist, *ANDROID_SCREEN)
    row_a_delete = next(el for el in candidates if el["label"] == "刪除" and "範例甲" in el["nearby_text"])
    assert row_a_delete["nearby_text"] == ["範例甲", "123.45"]


def test_android_nearby_text_skips_hidden_siblings(android_watchlist):
    """displayed=false 的兄弟節點（「隱藏的提示」）不算鄰近文字"""
    ids = _by_native_id(android.parse_candidates_raw(android_watchlist, *ANDROID_SCREEN))
    # 「新增自選」的兄弟節點裡只有隱藏的提示有文字，原本會被當成鄰近文字
    assert ids["com.example.demo:id/btn_add"]["nearby_text"] == []


def test_android_scrollable_regions(android_watchlist):
    assert android.has_scrollable_content(android_watchlist) is True
    regions = android.find_scrollable_regions(android_watchlist)
    assert [r["bounds"]["height"] for r in regions] == [300, 1260]  # 橫向推薦列、垂直自選清單


def test_parse_bounds():
    assert android.parse_bounds("[42,820][500,880]") == {"x": 42, "y": 820, "width": 458, "height": 60}
    assert android.parse_bounds("") is None


# ---- 兩平台共用的降級 ------------------------------------------------------

def test_duplicate_resource_id_downgraded(android_watchlist):
    """清單每一列共用的 resource-id 不能當 native id，會讓不同元件算出同一個 unique_id（TROUBLESHOOTING #26）"""
    platform = get_platform("android")
    candidates = parse_candidate_elements(platform, android_watchlist, *ANDROID_SCREEN)
    ids = _by_native_id(candidates)
    for shared in ("stock_name", "stock_price", "btn_delete", "stock_row", "banner_image"):
        assert f"com.example.demo:id/{shared}" not in ids
    assert "com.example.demo:id/btn_add" in ids


def test_downgrade_keeps_unique_ids():
    platform = get_platform("ios")
    candidates = [{"class": "Button", "native_id": "a"}, {"class": "Button", "native_id": "b"},
                  {"class": "Button", "native_id": "b"}, {"class": "Text", "native_id": "a"}]
    result = downgrade_duplicate_native_ids(platform, candidates)
    assert [el["native_id"] for el in result] == ["a", None, None, "a"]
