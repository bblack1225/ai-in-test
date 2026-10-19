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
        if value == "XCUIElementTypeToolbar":
            return []
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


class ReturnKeyDriver(FakeDriver):
    """文字鍵盤：右下角是 search 鍵，hideKeyboard 帶到這個鍵名才收得起來"""

    def __init__(self):
        super().__init__([])
        self.keys_sent = None

    def execute_script(self, script, args):
        self.keys_sent = args["keys"]
        if "search" not in args["keys"]:
            raise WebDriverException("Did not know how to dismiss the keyboard")
        self.keyboard = False


def test_return_key_dismisses_single_line_field():
    driver = ReturnKeyDriver()
    assert ios.dismiss_keyboard(driver) is True


def test_multiline_does_not_press_return():
    """多行輸入框按 return 會換行，不帶送出鍵"""
    driver = ReturnKeyDriver()
    assert ios.dismiss_keyboard(driver, return_key=False) is False
    assert "return" not in driver.keys_sent and "search" not in driver.keys_sent


class IconButton(FakeEl):
    def __init__(self, driver, label):
        super().__init__(driver, "icon", 509)
        self.label = label

    def get_attribute(self, name):
        return self.label

    def click(self):
        self.driver.clicked.append(self.name)
        self.driver.keyboard = False


class Toolbar(FakeEl):
    def __init__(self, driver, labels, y=516):
        super().__init__(driver, "toolbar-bar", y, h=44)
        self.items = [IconButton(driver, label) for label in labels]

    def find_elements(self, by, value):
        return list(self.items)


class ToolbarDriver(FakeDriver):
    """App 在數字鍵盤（上緣 y=560）正上方加的工具列，裡面是沒有文字的收鍵盤圖示"""

    def __init__(self, labels, y=516):
        super().__init__([])
        self.toolbar = Toolbar(self, labels, y)

    def find_elements(self, by, value):
        if value == "XCUIElementTypeToolbar":
            return [self.toolbar]
        return super().find_elements(by, value)


def test_taps_single_unlabeled_icon_on_toolbar():
    driver = ToolbarDriver([None])
    assert ios.dismiss_keyboard(driver) is True
    assert driver.clicked == ["icon"]


def test_toolbar_with_several_buttons_not_guessed():
    """上一個／下一個這類好幾顆按鈕的工具列不猜"""
    driver = ToolbarDriver([None, None])
    assert ios.dismiss_keyboard(driver) is False
    assert driver.clicked == []


def test_toolbar_not_touching_keyboard_ignored():
    driver = ToolbarDriver([None], y=300)
    assert ios.dismiss_keyboard(driver) is False
    assert driver.clicked == []
