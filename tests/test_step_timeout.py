"""每一步預設的等待（run_test_case.execute_step）：找不到元件時在 timeout 內重試，一找到就繼續"""

from contextlib import nullcontext

import pytest
from selenium.common.exceptions import NoSuchElementException

from aitest.core import run_test_case
from aitest.core.run_test_case import execute_step
from aitest.platforms.android import AndroidPlatform
from aitest.platforms.ios import IOSPlatform
from aitest.run_context import RunContext


class FakeElement:
    def __init__(self, driver):
        self.driver = driver

    def click(self):
        self.driver.clicked += 1

    def is_displayed(self):
        return True


class FakeDriver:
    """前 hidden_reads 次查詢都找不到元件（模擬畫面還在 loading），之後才出現"""

    def __init__(self, hidden_reads):
        self.hidden_reads = hidden_reads
        self.reads = 0
        self.clicked = 0
        self.coordinate_taps = 0

    def find_element(self, by, value):
        self.reads += 1
        if self.reads <= self.hidden_reads:
            raise NoSuchElementException()
        return FakeElement(self)


class FakePlatform:
    native_by = "id"
    native_id_name = "resource-id"
    generated_locate_label = "自動定位"

    def popup_windows_scope(self, driver):
        return nullcontext(driver)

    def ensure_visible(self, driver, element):
        pass

    def locate_generated_element(self, ctx, driver, el):
        try:
            return driver.find_element(None, None)
        except NoSuchElementException:
            return None

    def tap_by_coordinates(self, driver, bounds):
        driver.coordinate_taps += 1


NATIVE = {"id_source": "native", "native_id": "btn"}
GENERATED = {"id_source": "generated", "bounds": {}}


def run(step, el, driver):
    return execute_step(RunContext(), FakePlatform(), driver, step, el)


def test_tap_waits_for_element_to_appear(monkeypatch):
    monkeypatch.setattr(run_test_case, "DEFAULT_STEP_TIMEOUT", 5)
    driver = FakeDriver(hidden_reads=2)
    outcome = run({"action": "tap"}, NATIVE, driver)
    assert outcome.startswith("PASS") and "重試第 3 次" in outcome
    assert driver.clicked == 1


def test_tap_native_missing_fails_after_timeout():
    driver = FakeDriver(hidden_reads=999)
    outcome = run({"action": "tap", "timeout": 0.6}, NATIVE, driver)
    assert outcome.startswith("FAIL") and "已等待 0.6 秒" in outcome
    assert driver.reads > 1


def test_tap_coordinate_fallback_only_after_timeout():
    driver = FakeDriver(hidden_reads=999)
    outcome = run({"action": "tap", "timeout": 0.6}, GENERATED, driver)
    assert outcome.startswith("PASS（座標點擊") and "等了 0.6 秒" in outcome
    assert driver.coordinate_taps == 1 and driver.reads > 2


def test_timeout_zero_tries_once():
    driver = FakeDriver(hidden_reads=1)
    outcome = run({"action": "assert_visible", "timeout": 0}, NATIVE, driver)
    assert outcome.startswith("FAIL") and driver.reads == 1


def test_assert_visible_uses_default_timeout(monkeypatch):
    monkeypatch.setattr(run_test_case, "DEFAULT_STEP_TIMEOUT", 5)
    driver = FakeDriver(hidden_reads=1)
    assert run({"action": "assert_visible"}, NATIVE, driver).startswith("PASS")


class SettingsDriver:
    def __init__(self):
        self.settings = {"waitForIdleTimeout": 10000}

    def get_settings(self):
        return dict(self.settings)

    def update_settings(self, values):
        self.settings.update(values)


@pytest.mark.parametrize("platform", [AndroidPlatform(), IOSPlatform()], ids=["android", "ios"])
def test_run_disables_idle_wait_and_restores(platform):
    """跑測案期間不等 App 靜止（首頁報價一直跳，每個指令都會等滿 10 秒），結束後恢復"""
    driver = SettingsDriver()
    with platform.page_source_context(driver):
        assert driver.settings["waitForIdleTimeout"] == 0
    assert driver.settings["waitForIdleTimeout"] == 10000
