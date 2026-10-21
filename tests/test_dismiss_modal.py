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


def test_keeps_expected_dialog():
    """第一步畫面本來就是這個確認框：按鈕在 keep_labels 裡就整個不處理，也不改按別顆"""
    driver = FakeDriver(["取消", "確定"])
    assert android.android_dismiss_modal(driver, keep_labels={"取消", "確定"}) == []
    assert driver.clicked == []


def test_ios_keeps_expected_dialog():
    from aitest.platforms.ios import IOSPlatform

    driver = FakeIOSDriver()
    assert IOSPlatform().dismiss_dialogs(driver, keep_labels={"下次再說"}) == []
    assert driver.clicked == []


class ScreenPlatform:
    screen_id_attrs = ("resource-id",)
    screen_label_attrs = ("text",)
    screen_ignored_id_prefixes = ()


class SourceDriver:
    def __init__(self, texts):
        self.page_source = "<hierarchy>" + "".join(f'<node text="{t}" />' for t in texts) + "</hierarchy>"


CONFIRM_SCREEN = {"elements": [{"label": "確定要送出嗎？"}, {"label": "取消"}, {"label": "確定"}]}


def test_labels_if_on_screen_when_matching():
    """目前畫面就是第一步的確認框 → 回傳地圖上這個畫面的按鈕文字"""
    from aitest.core.run_test_case import labels_if_on_screen

    driver = SourceDriver(["確定要送出嗎？", "取消", "確定"])
    assert labels_if_on_screen(ScreenPlatform(), driver, "確認框", CONFIRM_SCREEN) == {"確定要送出嗎？", "取消", "確定"}


def test_labels_if_on_screen_when_not_matching():
    """目前畫面不是第一步的畫面（例如首頁上跳了 Root 警告）→ 空集合，照常關彈窗"""
    from aitest.core.run_test_case import labels_if_on_screen

    driver = SourceDriver(["偵測到 Root", "我知道了"])
    assert labels_if_on_screen(ScreenPlatform(), driver, "確認框", CONFIRM_SCREEN) == set()
    assert labels_if_on_screen(ScreenPlatform(), driver, None, None) == set()


class FakeNoticeDriver:
    """App 自己畫的公告視窗（不是系統 Alert），只有「確定」按鈕；buttons 是畫面上叫確定的按鈕數"""

    page_source = "<AppiumAUT />"

    def __init__(self, title="複委託權益公告", buttons=1):
        self.title, self.buttons = title, buttons
        self.dialog_open = True
        self.clicked = []

    def find_elements(self, by, value):
        if not self.dialog_open or "XCUIElementTypeAlert" in value:
            return []
        if "label CONTAINS" in value:
            markers = [m for m in ("公告", "提醒您", "請確認是否") if f"'{m}'" in value]
            return [FakeIOSElement(self, self.title)] if any(m in self.title for m in markers) else []
        if "'確定'" in value and "XCUIElementTypeButton" in value:
            return [FakeIOSElement(self, "確定") for _ in range(self.buttons)]
        return []


def test_ios_run_taps_single_ok_on_notice():
    from aitest.platforms.ios import IOSPlatform

    driver = FakeNoticeDriver()
    assert IOSPlatform().dismiss_dialogs(driver) == ["確定"]


def test_ios_notice_with_several_ok_not_guessed():
    from aitest.platforms.ios import IOSPlatform

    driver = FakeNoticeDriver(buttons=2)
    assert IOSPlatform().dismiss_dialogs(driver) == []
    assert driver.clicked == []


def test_ios_confirm_dialog_not_tapped():
    """「請確認是否送出」這種要使用者確認動作的視窗，只有「確定」也不按"""
    from aitest.platforms.ios import IOSPlatform

    driver = FakeNoticeDriver(title="請確認是否送出委託")
    assert IOSPlatform().dismiss_dialogs(driver) == []
    assert driver.clicked == []


def test_ios_crawler_does_not_tap_notice_ok():
    """爬蟲維持原本的規則（只在系統 Alert 上按確定）"""
    from aitest.platforms import ios

    driver = FakeNoticeDriver()
    assert ios.dismiss_modal(driver) == []
