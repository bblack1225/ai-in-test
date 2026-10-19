"""主畫面找不到元件時暫時看浮動視窗（Android enableMultiWindows）：只算 App 的視窗、結束還原原本的設定"""

from selenium.common.exceptions import NoSuchElementException

from aitest.core.run_test_case import execute_step
from aitest.platforms.android import AndroidPlatform, PopupAwareDriver
from aitest.run_context import RunContext

APP = "com.example.demo"
SOURCE = (f'<hierarchy><android.widget.FrameLayout package="com.android.systemui"/>'
          f'<android.widget.FrameLayout package="{APP}"/></hierarchy>')


class FakeElement:
    def __init__(self, driver, package=APP):
        self.driver, self.package = driver, package

    def get_attribute(self, name):
        return self.package if name == "package" else None

    def click(self):
        self.driver.clicked.append(self.package)

    def is_displayed(self):
        return True


class FakeDriver:
    """popup 裡的元件只有打開 enableMultiWindows 才查得到；system 是狀態列上剛好同名的元件"""

    def __init__(self, main=0, popup=0, system=0, multi=False):
        self.settings = {"enableMultiWindows": multi}
        self.counts = (main, popup, system)
        self.updates = []
        self.clicked = []
        self.current_package = APP

    def get_settings(self):
        return dict(self.settings)

    def update_settings(self, values):
        self.updates.append(values)
        self.settings.update(values)

    def find_elements(self, by, value):
        main, popup, system = self.counts
        found = [FakeElement(self) for _ in range(main)]
        if self.settings["enableMultiWindows"]:
            found += [FakeElement(self) for _ in range(popup)]
            found += [FakeElement(self, "com.android.systemui") for _ in range(system)]
        return found

    def find_element(self, by, value):
        found = self.find_elements(by, value)
        if not found:
            raise NoSuchElementException()
        return found[0]

    @property
    def page_source(self):
        return SOURCE


def test_proxy_passes_through_until_shown():
    driver = FakeDriver(main=1, system=1, multi=False)
    proxy = PopupAwareDriver(driver)
    assert len(proxy.find_elements("xpath", "//x")) == 1
    assert proxy.page_source == SOURCE
    assert proxy.current_package == APP  # 其他屬性直接轉給 driver


def test_show_filters_system_windows_and_close_restores():
    driver = FakeDriver(popup=1, system=2)
    proxy = PopupAwareDriver(driver)
    assert proxy.show_popup_windows() is True
    assert proxy.show_popup_windows() is False  # 已經打開過，不再切換
    assert [e.package for e in proxy.find_elements("xpath", "//x")] == [APP]
    assert "systemui" not in proxy.page_source
    proxy.close()
    assert driver.settings["enableMultiWindows"] is False
    assert driver.updates == [{"enableMultiWindows": True}, {"enableMultiWindows": False}]


def test_already_on_is_left_alone():
    """錄製、跑馬燈監看本來就開著：不切換、結束也不關掉"""
    driver = FakeDriver(popup=1, multi=True)
    proxy = PopupAwareDriver(driver)
    assert proxy.show_popup_windows() is False
    proxy.close()
    assert driver.updates == [] and driver.settings["enableMultiWindows"] is True


def test_find_element_raises_when_only_system_matches():
    proxy = PopupAwareDriver(FakeDriver(system=1))
    proxy.show_popup_windows()
    try:
        proxy.find_element("xpath", "//x")
        raise AssertionError("應該找不到")
    except NoSuchElementException:
        pass


CUSTOM = {"id_source": "generated", "bounds": {}, "custom_xpath": "//x"}


def run(driver, el=CUSTOM, timeout=0):
    return execute_step(RunContext(), AndroidPlatform(), driver, {"action": "tap", "timeout": timeout}, el)


def test_tap_finds_element_in_popup_and_restores():
    driver = FakeDriver(popup=1)
    outcome = run(driver)
    assert outcome.startswith("PASS（人工自訂XPath定位") and "浮動視窗" in outcome
    assert driver.clicked == [APP]
    assert driver.settings["enableMultiWindows"] is False


def test_tap_found_on_main_screen_does_not_switch():
    driver = FakeDriver(main=1, popup=1)
    assert run(driver) == "PASS（人工自訂XPath定位）"
    assert driver.updates == []


def test_native_element_does_not_switch():
    driver = FakeDriver(popup=1)
    outcome = run(driver, {"id_source": "native", "native_id": "btn"})
    assert outcome.startswith("FAIL")
    assert driver.updates == []
