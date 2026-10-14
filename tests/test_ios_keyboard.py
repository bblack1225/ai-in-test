"""iOS 打完字收鍵盤（platforms/ios.dismiss_keyboard）：數字鍵盤沒有完成鍵，按工具列上的「完成」"""

from selenium.common.exceptions import WebDriverException

from aitest.platforms import ios


class FakeEl:
    def __init__(self, driver, name, y, h=44):
        self.driver, self.name = driver, name
        self.location = {"x": 0, "y": y}
        self.size = {"width": 60, "height": h}

    def click(self):
        self.driver.clicked.append(self.name)
        if self.name == "toolbar":
            self.driver.keyboard = False


class FakeDriver:
    """數字鍵盤（上緣 y=560），鍵盤上沒有完成鍵；buttons 是畫面上叫「完成」的按鈕"""

    def __init__(self, buttons):
        self.keyboard = True
        self.clicked = []
        self.buttons = [FakeEl(self, name, y) for name, y in buttons]

    def is_keyboard_shown(self):
        return self.keyboard

    def execute_script(self, script, args):
        raise WebDriverException("Did not know how to dismiss the keyboard")

    def find_elements(self, by, value):
        if value == "XCUIElementTypeKeyboard":
            return [FakeEl(self, "keyboard", 560, h=284)] if self.keyboard else []
        return list(self.buttons)


def test_taps_done_on_toolbar_above_number_pad():
    driver = FakeDriver([("page", 700 - 600), ("toolbar", 560 - 44)])
    assert ios.dismiss_keyboard(driver) is True
    assert driver.clicked == ["toolbar"]


def test_does_not_tap_done_elsewhere_on_page():
    """頁面上別的「完成」（例如送出申請）不按；收不起來就回報"""
    driver = FakeDriver([("page", 300)])
    assert ios.dismiss_keyboard(driver) is False
    assert driver.clicked == []


def test_no_keyboard_does_nothing():
    driver = FakeDriver([("toolbar", 516)])
    driver.keyboard = False
    assert ios.dismiss_keyboard(driver) is True
    assert driver.clicked == []
