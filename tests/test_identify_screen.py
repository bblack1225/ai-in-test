"""批次自動導航的畫面辨識（core/navigator.identify_screen）"""

from aitest.core.navigator import MapIndex, identify_screen
from aitest.platforms.ios import IOSPlatform

TAB_BAR = ["ic24PxGreyQuickNormalNsp", "ic24PxGreyRegularinvestNormalNsp", "ic24PxColorfulOrder",
           "ic24PxGreyAccountingNormalNsp"]
QUOTE_CELLS = [f"MBKMetroQuote_QuoteDataCell_{r}_{c}" for r in range(3) for c in range(2)]


def native(ids):
    return [{"id_source": "native", "native_id": i} for i in ids]


# 首頁：搜尋鈕＋報價格子＋底部導覽列（「我的」沒選取）；「我的」：只有底部導覽列（「行情」沒選取）
MAP = MapIndex({"screens": [
    {"screen_uid": "home", "screen_name": "root_screen",
     "elements": native(["navi_search_button", *QUOTE_CELLS, *TAB_BAR, "ic24PxGreyMyNormalNsp"])},
    {"screen_uid": "mine", "screen_name": "我的", "parent_screen_uid": "home",
     "elements": native(["ic24PxGreyPriceNormaNsp", *TAB_BAR])},
]})


def page(ids):
    return "<AppiumAUT>" + "".join(f'<XCUIElementTypeOther name="{i}" label=""/>' for i in ids) + "</AppiumAUT>"


def test_home_before_quotes_load_is_not_mistaken_for_mine():
    """剛登入報價還沒載入：首頁少了報價格子，不能輸給只剩導覽列 id 的「我的」"""
    current = page(["navi_search_button", *TAB_BAR, "ic24PxGreyMyNormalNsp", "ic24PxGreyPriceNormaNsp"])
    assert identify_screen(IOSPlatform(), current, MAP)[0]["name"] == "root_screen"


def test_home_with_quotes_loaded():
    current = page(["navi_search_button", *QUOTE_CELLS, *TAB_BAR, "ic24PxGreyMyNormalNsp"])
    assert identify_screen(IOSPlatform(), current, MAP)[0]["name"] == "root_screen"


def test_really_on_mine():
    current = page(["ic24PxGreyPriceNormaNsp", *TAB_BAR])
    assert identify_screen(IOSPlatform(), current, MAP)[0]["name"] == "我的"


def test_android_status_bar_ids_are_ignored():
    """地圖掃描時混進狀態列 id、現在畫面抓不到：不能因此拉低比例、認不出這一頁"""
    from aitest.platforms.android import AndroidPlatform

    status_bar = [f"com.android.systemui:id/{n}" for n in ("clock", "battery", "wifi_signal", "status_bar")]
    app_ids = ["com.example.demo:id/gridView", "com.example.demo:id/toolbar"]
    index = MapIndex({"screens": [
        {"screen_uid": "home", "screen_name": "root_screen", "elements": native([*app_ids, *status_bar])},
    ]})
    current = "<hierarchy>" + "".join(f'<node resource-id="{i}" text=""/>' for i in app_ids) + "</hierarchy>"
    assert identify_screen(AndroidPlatform(), current, index)[0]["name"] == "root_screen"
