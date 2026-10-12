"""platforms/base.wait_until_stable：爬蟲、跑測案、掃描共用的等畫面穩定"""

import pytest
from selenium.common.exceptions import InvalidSessionIdException, WebDriverException

from aitest.platforms.base import wait_until_stable


class FakeDriver:
    """依序回傳 sources 裡的 page_source，用完就一直回傳最後一個；放例外的話就丟出來"""

    def __init__(self, *sources):
        self.sources = list(sources)
        self.reads = 0

    @property
    def page_source(self):
        self.reads += 1
        value = self.sources.pop(0) if len(self.sources) > 1 else self.sources[0]
        if isinstance(value, Exception):
            raise value
        return value


def test_stable_when_two_reads_match():
    driver = FakeDriver("a", "b", "b")
    assert wait_until_stable(driver, max_wait=5, poll_interval=0) is True
    assert driver.reads == 3


def test_signature_ignores_what_it_leaves_out():
    """只比對 signature 的結果：文字（數字）在跳，但版面一樣就算穩定"""
    driver = FakeDriver("價格 100", "價格 101")
    assert wait_until_stable(driver, lambda s: s.split()[0], max_wait=5, poll_interval=0) is True
    assert driver.reads == 2


def test_gives_up_by_real_time():
    """畫面一直在變：等到 max_wait（真實經過時間）就放棄"""
    counter = iter(range(10 ** 6))

    class Changing:
        @property
        def page_source(self):
            return str(next(counter))

    assert wait_until_stable(Changing(), max_wait=0.05, poll_interval=0.01) is False


def test_read_error_stops_waiting():
    assert wait_until_stable(FakeDriver(WebDriverException("boom")), max_wait=5, poll_interval=0) is False


def test_session_lost_propagates():
    """連線斷掉不能當成「畫面穩定了／不等了」吞掉"""
    with pytest.raises(InvalidSessionIdException):
        wait_until_stable(FakeDriver(InvalidSessionIdException("gone")), max_wait=5, poll_interval=0)
