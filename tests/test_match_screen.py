"""run_test_case.match_screen_in_source：執行前的畫面檢查（手機是不是停在測案的起始畫面）"""

from run_test_case import match_screen_in_source

IOS_ATTRS = {"id_attrs": ("name",), "label_attrs": ("label",)}
ANDROID_ATTRS = {"id_attrs": ("resource-id",), "label_attrs": ("content-desc", "text")}


def _native(*ids):
    return [{"id_source": "native", "native_id": i} for i in ids]


def _generated(*labels):
    return [{"id_source": "generated", "native_id": None, "label": t} for t in labels]


def test_login_screen_matches_itself(ios_login):
    status, _ = match_screen_in_source(ios_login, _native("loginbutton", "loginidfield"), "登入", **IOS_ATTRS)
    assert status == "pass"


def test_half_found_is_pass(ios_login):
    status, message = match_screen_in_source(ios_login, _native("loginbutton", "doesNotExist"), "登入", **IOS_ATTRS)
    assert status == "pass"
    assert "50%" in message


def test_less_than_half_is_fail(ios_login):
    elements = _native("loginbutton", "doesNotExist1", "doesNotExist2")
    status, _ = match_screen_in_source(ios_login, elements, "登入", **IOS_ATTRS)
    assert status == "fail"


def test_wrong_screen_is_fail(ios_policy):
    status, _ = match_screen_in_source(ios_policy, _native("loginbutton", "loginidfield"), "登入", **IOS_ATTRS)
    assert status == "fail"


def test_falls_back_to_labels_without_native_ids(ios_login):
    status, message = match_screen_in_source(ios_login, _generated("忘記密碼", "不登入先逛逛"), "登入", **IOS_ATTRS)
    assert status == "pass"
    assert "固定文字" in message


def test_labels_with_digits_are_ignored(ios_login):
    """價格、時間、電話這類含數字的文字每次都可能變，不拿來比對"""
    elements = _generated("忘記密碼", "02-1234-5678", "不存在的文字 1", "不存在的文字 2")
    status, message = match_screen_in_source(ios_login, elements, "登入", **IOS_ATTRS)
    # 只剩「忘記密碼」一個能比，找到了 -> 100%
    assert status == "pass"
    assert "1 個" in message


def test_nothing_to_check_is_unchecked(ios_login):
    status, _ = match_screen_in_source(ios_login, _generated("123", ""), "登入", **IOS_ATTRS)
    assert status == "unchecked"


def test_unparsable_source_is_unchecked():
    status, _ = match_screen_in_source("<not xml", _native("x"), "登入", **IOS_ATTRS)
    assert status == "unchecked"


def test_android_attrs(android_watchlist):
    elements = _native("com.example.demo:id/btn_add", "com.example.demo:id/search_input")
    status, _ = match_screen_in_source(android_watchlist, elements, "自選", **ANDROID_ATTRS)
    assert status == "pass"
    status, _ = match_screen_in_source(android_watchlist, _generated("新增自選", "自選股"), "自選", **ANDROID_ATTRS)
    assert status == "pass"
