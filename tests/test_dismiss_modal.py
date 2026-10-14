"""關掉非預期彈窗：只按「純關閉／婉拒」語意的按鈕（platforms/android.py 的 android_dismiss_modal）"""

from aitest.platforms import android


class FakeButton:
    def __init__(self, driver, text):
        self.driver, self.text = driver, text

    def get_attribute(self, name):
        return self.text if name == "text" else None

    def click(self):
        self.driver.clicked.append(self.text)
        self.driver.dialog_open = False


class FakeDriver:
    """畫面上有一個對話框，按鈕是 buttons；查詢的 XPath 有比對到按鈕文字才回傳它"""

    page_source = "<hierarchy />"

    def __init__(self, buttons):
        self.buttons = buttons
        self.dialog_open = True
        self.clicked = []

    def find_elements(self, by, xpath):
        if not self.dialog_open:
            return []
        return [FakeButton(self, t) for t in self.buttons if f'@text="{t}"' in xpath]


def test_biometric_prompt_declined():
    """登入後跳出「利用生物辨識登入」：按「暫時不用」，不按「立刻啟用」"""
    driver = FakeDriver(["立刻啟用", "暫時不用"])
    assert android.android_dismiss_modal(driver) == ["暫時不用"]
    assert driver.clicked == ["暫時不用"]


def test_never_taps_logout():
    driver = FakeDriver(["登出"])
    assert android.android_dismiss_modal(driver) == []


class FakeIOSElement:
    def __init__(self, driver, label):
        self.driver, self.label = driver, label

    def get_attribute(self, name):
        return self.label if name == "label" else None

    def click(self):
        self.driver.clicked.append(self.label)
        self.driver.dialog_open = False


class FakeIOSDriver:
    """App 自己畫的彈窗：「下次再說」是 StaticText，不是 Button"""

    page_source = "<AppiumAUT />"

    def __init__(self):
        self.dialog_open = True
        self.clicked = []

    def find_elements(self, by, value):
        if not self.dialog_open or "XCUIElementTypeButton" in value or "XCUIElementTypeAlert" in value:
            return []
        return [FakeIOSElement(self, "下次再說")] if "'下次再說'" in value else []


def test_ios_run_taps_text_close_button():
    """跑測案時關彈窗不限 Button 類型（跟 Android 一樣）"""
    from aitest.platforms.ios import IOSPlatform

    driver = FakeIOSDriver()
    assert IOSPlatform().dismiss_dialogs(driver) == ["下次再說"]


def test_ios_crawler_only_taps_buttons():
    """爬蟲每一頁都會檢查，維持只找 Button"""
    from aitest.platforms import ios

    driver = FakeIOSDriver()
    assert ios.dismiss_modal(driver) == []
    assert driver.clicked == []
