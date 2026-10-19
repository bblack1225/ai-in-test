"""自訂XPath 裡的 ${KEY}：執行時換成變數值；查不到時不退回自動定位或座標"""

from contextlib import nullcontext

from aitest.core.run_test_case import execute_step
from aitest.run_context import RunContext


class FakeElement:
    def __init__(self, driver):
        self.driver = driver

    def click(self):
        self.driver.clicked += 1

    def is_displayed(self):
        return True


class FakeDriver:
    """只有 xpath 等於 match_xpath 時查得到一個元件"""

    def __init__(self, match_xpath):
        self.match_xpath = match_xpath
        self.queried = []
        self.clicked = 0
        self.coordinate_taps = 0

    def find_elements(self, by, value):
        self.queried.append(value)
        return [FakeElement(self)] if value == self.match_xpath else []


class FakePlatform:
    native_by = "id"
    native_id_name = "resource-id"
    generated_locate_label = "自動定位"

    def __init__(self):
        self.auto_located = 0

    def popup_windows_scope(self, driver):
        return nullcontext(driver)

    def ensure_visible(self, driver, element):
        pass

    def locate_generated_element(self, ctx, driver, el):
        self.auto_located += 1
        return FakeElement(driver)  # 自動定位找得到錄製時的那一筆

    def tap_by_coordinates(self, driver, bounds):
        driver.coordinate_taps += 1


EL = {"id_source": "generated", "bounds": {},
      "custom_xpath": "//*[@resource-id='list']//*[contains(@text,'${STOCK_CODE}')]"}


def ctx_with(**values):
    ctx = RunContext()
    ctx.set_vars(values, ["股票"])
    return ctx


def test_tap_resolves_vars_in_custom_xpath():
    driver = FakeDriver("//*[@resource-id='list']//*[contains(@text,'2330')]")
    outcome = execute_step(ctx_with(STOCK_CODE="2330"), FakePlatform(), driver, {"action": "tap", "timeout": 0}, EL)
    assert outcome == "PASS（人工自訂XPath定位）"
    assert driver.clicked == 1


def test_tap_with_vars_no_match_does_not_fall_back():
    driver, platform = FakeDriver("never"), FakePlatform()
    for timeout in (0, 0.3):
        outcome = execute_step(ctx_with(STOCK_CODE="2330"), platform, driver, {"action": "tap", "timeout": timeout}, EL)
        assert outcome.startswith("FAIL") and "含變數" in outcome
    assert driver.clicked == 0 and driver.coordinate_taps == 0 and platform.auto_located == 0


def test_missing_var_in_custom_xpath_fails_clearly(monkeypatch):
    monkeypatch.delenv("STOCK_CODE", raising=False)
    driver = FakeDriver("never")
    outcome = execute_step(RunContext(), FakePlatform(), driver, {"action": "tap", "timeout": 0}, EL)
    assert outcome.startswith("FAIL") and "STOCK_CODE" in outcome
    assert driver.queried == [] and driver.coordinate_taps == 0


def test_custom_xpath_without_vars_still_falls_back():
    el = {**EL, "custom_xpath": "//*[@text='0050']"}
    driver, platform = FakeDriver("never"), FakePlatform()
    outcome = execute_step(RunContext(), platform, driver, {"action": "tap", "timeout": 0}, el)
    assert outcome == "PASS（自動定位）"
    assert platform.auto_located == 1
