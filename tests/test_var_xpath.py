"""「依變數定位」（core/var_xpath.py）：從清單這一列的文字組出含變數的 XPath。
有裝 lxml 時另外實際跑一次 XPath，確認代入目前的值只查到這一列（專案沒有相依 lxml，沒裝就略過這段；
實際執行時是 Appium 在手機上查，語法一樣是 XPath 1.0）"""

import xml.etree.ElementTree as ET

import pytest

from aitest.core.var_xpath import VAR_PLACEHOLDER, build_options, fill
from aitest.platforms.android import AndroidPlatform
from aitest.platforms.ios import IOSPlatform

ANDROID = """<hierarchy>
<android.widget.FrameLayout class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
  <android.widget.EditText class="android.widget.EditText" text="0050" bounds="[700,430][930,570]"/>
  <android.widget.ListView class="android.widget.ListView" bounds="[627,573][1039,2067]">
    <android.widget.LinearLayout class="android.widget.LinearLayout" bounds="[627,573][1039,717]">
      <android.widget.ImageView class="android.widget.ImageView" bounds="[627,573][719,717]"/>
      <android.widget.LinearLayout class="android.widget.LinearLayout" bounds="[719,573][1039,717]">
        <android.widget.TextView class="android.widget.TextView" text="元大台灣50" bounds="[719,573][1005,654]"/>
        <android.widget.TextView class="android.widget.TextView" text="0050" bounds="[719,654][827,711]"/>
      </android.widget.LinearLayout>
    </android.widget.LinearLayout>
    <android.widget.LinearLayout class="android.widget.LinearLayout" bounds="[627,717][1039,942]">
      <android.widget.ImageView class="android.widget.ImageView" bounds="[627,717][719,942]"/>
      <android.widget.LinearLayout class="android.widget.LinearLayout" bounds="[719,717][1039,942]">
        <android.widget.TextView class="android.widget.TextView" text="元大台灣50估" bounds="[719,717][1039,879]"/>
        <android.widget.TextView class="android.widget.TextView" text="0050N" bounds="[719,879][861,936]"/>
      </android.widget.LinearLayout>
    </android.widget.LinearLayout>
  </android.widget.ListView>
</android.widget.FrameLayout>
</hierarchy>"""

ROW = {"class": "android.widget.LinearLayout", "bounds": {"x": 627, "y": 573, "width": 412, "height": 144}}

IOS = """<AppiumAUT><XCUIElementTypeApplication x="0" y="0" width="390" height="844">
  <XCUIElementTypeSearchField label="輸入商品代碼、名稱" value="0050" x="8" y="51" width="328" height="36"/>
  <XCUIElementTypeTable x="0" y="116" width="390" height="400">
    <XCUIElementTypeCell x="0" y="116" width="390" height="56">
      <XCUIElementTypeStaticText label="Separator" x="0" y="116" width="390" height="1"/>
      <XCUIElementTypeStaticText label="元大台灣50&#10;0050" x="40" y="120" width="200" height="40"/>
      <XCUIElementTypeButton label="下單" x="330" y="125" width="50" height="30"/>
    </XCUIElementTypeCell>
    <XCUIElementTypeCell x="0" y="172" width="390" height="56">
      <XCUIElementTypeStaticText label="愛普凱基61購02&#10;060050" x="40" y="176" width="200" height="40"/>
    </XCUIElementTypeCell>
  </XCUIElementTypeTable>
</XCUIElementTypeApplication></AppiumAUT>"""

CELL = {"class": "XCUIElementTypeCell", "bounds": {"x": 0, "y": 116, "width": 390, "height": 56}}


def _matches(source: str, xpath: str) -> list:
    """用 lxml 跑 XPath（ElementTree 不支援 contains／concat 這些函式）"""
    etree = pytest.importorskip("lxml.etree")
    return etree.fromstring(source.encode()).xpath(xpath)


def option(result, text):
    return next(o for o in result["options"] if o["text"] == text)


def test_android_code_text_gives_exact_match():
    result = build_options(AndroidPlatform(), ANDROID, ROW)
    assert result["found"]
    opt = option(result, "0050")
    assert opt["template"] == ("//android.widget.ListView/android.widget.LinearLayout"
                               "[./android.widget.LinearLayout/android.widget.TextView[@text='{{VAR}}']]")


def test_android_xpath_hits_only_that_row():
    opt = option(build_options(AndroidPlatform(), ANDROID, ROW), "0050")
    hits = _matches(ANDROID, fill(opt["template"], "0050"))
    assert len(hits) == 1 and hits[0].get("bounds") == "[627,573][1039,717]"
    # 換成第二列的代號就查到第二列，搜尋框（文字也是 0050）不會被查到
    assert [h.get("bounds") for h in _matches(ANDROID, fill(opt["template"], "0050N"))] == ["[627,717][1039,942]"]


def test_ios_multiline_label_gives_token_options():
    result = build_options(IOSPlatform(), IOS, CELL)
    texts = [o["text"] for o in result["options"]]
    assert "0050" in texts and "元大台灣50" in texts and "下單" in texts
    assert "元大台灣50\n0050" not in texts  # 有換行的整段不列，變數打不出換行
    opt = option(result, "0050")
    assert not opt["whole"]
    assert opt["template"] == ("//XCUIElementTypeTable/XCUIElementTypeCell[./XCUIElementTypeStaticText"
                               "[contains(concat(' ', normalize-space(@label), ' '), ' {{VAR}} ')]]")


def test_ios_xpath_hits_only_that_row():
    opt = option(build_options(IOSPlatform(), IOS, CELL), "0050")
    hits = _matches(IOS, fill(opt["template"], "0050"))
    assert len(hits) == 1 and hits[0].get("y") == "116"  # 060050 那列不會中


def test_row_not_on_screen():
    far = {"class": "android.widget.LinearLayout", "bounds": {"x": 0, "y": 2200, "width": 100, "height": 50}}
    result = build_options(AndroidPlatform(), ANDROID, far)
    assert not result["found"] and "找不到這一列" in result["error"]


def test_not_in_list_container():
    source = ANDROID.replace("android.widget.ListView", "android.widget.FrameLayout")
    result = build_options(AndroidPlatform(), source, ROW)
    assert not result["found"] and "清單容器" in result["error"]


def test_placeholder_kept_for_frontend():
    result = build_options(AndroidPlatform(), ANDROID, ROW)
    assert all(VAR_PLACEHOLDER in o["template"] for o in result["options"])
    ET.fromstring(ANDROID)  # 測試資料本身是合法 XML
