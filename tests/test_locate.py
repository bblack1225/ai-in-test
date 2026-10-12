"""platforms/locate.py：無 native id 元件的定位流程（兩個平台共用），用假的平台物件和 driver 測"""

import pytest
from selenium.common.exceptions import InvalidSessionIdException, WebDriverException

from aitest.platforms import locate
from aitest.run_context import RunContext, StopRequested


class FakeElement:
    def __init__(self, name, y=0):
        self.name = name
        self.location = {"x": 0, "y": y}
        self.size = {"width": 10, "height": 10}

    def get_attribute(self, attr):
        return "true"


class FakeDriver:
    """find_elements 依序回傳 results 裡的結果（用完就一直回傳最後一個）；放例外的話就丟出來"""

    def __init__(self, results, page_sources=("same",)):
        self.results = list(results)
        self.page_sources = list(page_sources)
        self.queries = []

    def find_elements(self, by, query):
        self.queries.append((by, query))
        value = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(value, Exception):
            raise value
        return value

    @property
    def page_source(self):
        return self.page_sources.pop(0) if len(self.page_sources) > 1 else self.page_sources[0]

    def get_window_size(self):
        return {"width": 1000, "height": 2000}


class FakePlatform:
    class_text_by = "fake-by"
    fallback_label = "最後備援"

    def __init__(self, moves=(), regions=(), anchor=None, fallback=None):
        self.moves = list(moves)
        self.regions = list(regions)
        self.anchor = anchor
        self.fallback = fallback
        self.scrolled = []

    def class_text_query(self, class_name, label):
        return f"{class_name}={label}"

    def scroll_once(self, driver, bounds, direction):
        self.scrolled.append(bounds)
        return self.moves.pop(0) if self.moves else False

    def scrollable_regions(self, xml_source):
        return self.regions

    def find_anchor(self, driver, text):
        return self.anchor

    def locate_fallback(self, driver, el):
        return self.fallback

    def match_preview(self, m):
        return {"name": m.name}


EL = {"class": "Button", "label": "刪除", "fingerprint": {"nearby_text": ["項目 003"]}}


def test_unique_match_without_scrolling():
    target = FakeElement("target")
    platform = FakePlatform()
    driver = FakeDriver([[target]])
    assert locate.locate_generated_element(RunContext(), platform, driver, EL) is target
    assert platform.scrolled == []
    assert driver.queries == [("fake-by", "Button=刪除")]


def test_scrolls_until_found():
    target = FakeElement("target")
    platform = FakePlatform(moves=[True, True])
    driver = FakeDriver([[], [], [target]])
    assert locate.locate_generated_element(RunContext(), platform, driver, EL) is target
    # 先捲畫面主要區域（不縮邊界）
    assert platform.scrolled == [{"x": 100, "y": 300, "width": 800, "height": 1400}] * 2


def test_scrollable_regions_are_shrunk_before_scrolling():
    """主要區域捲不動，換捲畫面上的容器，捲之前先縮邊界（原本只有 Android 這樣做）"""
    platform = FakePlatform(moves=[False, False],
                            regions=[{"class": "List", "bounds": {"x": 0, "y": 0, "width": 100, "height": 100}}])
    driver = FakeDriver([[]])
    assert locate.locate_with_scroll(RunContext(), platform, driver, EL) is None
    assert platform.scrolled[1] == {"x": 5, "y": 20, "width": 90, "height": 60}


def test_platform_without_scroll_feedback_compares_page_source():
    """scroll_once 回傳 None（iOS）：捲動前後 page_source 沒變就判定捲不動"""
    platform = FakePlatform(moves=[None] * 10)
    driver = FakeDriver([[]], page_sources=["p1", "p2", "p2"])
    assert locate.locate_with_scroll(RunContext(), platform, driver, EL) is None
    assert len(platform.scrolled) == 3


def test_ambiguous_match_uses_closest_to_anchor():
    rows = [FakeElement("row1", y=100), FakeElement("row3", y=300), FakeElement("row5", y=500)]
    platform = FakePlatform(anchor=FakeElement("anchor", y=310))
    driver = FakeDriver([rows])
    assert locate.locate_generated_element(RunContext(), platform, driver, EL).name == "row3"
    assert platform.scrolled == []  # 查到多個是真的有歧義，不用捲


def test_fallback_when_nothing_else_works():
    fallback = FakeElement("fallback")
    platform = FakePlatform(fallback=fallback)
    assert locate.locate_generated_element(RunContext(), platform, FakeDriver([[]]), EL) is fallback


def test_query_error_counts_as_not_found():
    """查詢語法錯、元件剛好消失這類錯誤：當成這次沒查到，繼續走備援"""
    fallback = FakeElement("fallback")
    platform = FakePlatform(fallback=fallback)
    driver = FakeDriver([WebDriverException("bad selector")])
    assert locate.locate_generated_element(RunContext(), platform, driver, EL) is fallback


def test_session_lost_is_not_treated_as_not_found():
    """連線斷掉要往上丟，不能變成「找不到元件」再退回座標點擊（報告 #10）"""
    platform = FakePlatform(fallback=FakeElement("fallback"))
    driver = FakeDriver([InvalidSessionIdException("gone")])
    with pytest.raises(InvalidSessionIdException):
        locate.locate_generated_element(RunContext(), platform, driver, EL)


def test_empty_label_goes_straight_to_coordinates():
    platform = FakePlatform(fallback=FakeElement("fallback"))
    assert locate.locate_generated_element(RunContext(), platform, FakeDriver([[]]), {**EL, "label": ""}) is None


def test_stop_interrupts_scrolling():
    """使用者按停止：捲動搜尋中途丟出 StopRequested，不會繼續捲、也不會退回座標"""
    platform = FakePlatform(moves=[True] * 10)
    ctx = RunContext(stop_checker=lambda: len(platform.scrolled) >= 2)
    with pytest.raises(StopRequested):
        locate.locate_generated_element(ctx, platform, FakeDriver([[]]), EL)
    assert len(platform.scrolled) == 2
