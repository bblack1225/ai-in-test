"""爬蟲的骨架簽章（platforms/ios.py、platforms/android.py）：爬蟲判斷「這個畫面有沒有看過」的依據"""

import re

from aitest.platforms.android import android_skeleton_signature
from aitest.platforms.ios import skeleton_signature

# ---- iOS ----------------------------------------------------------------

def test_same_screen_rescanned_gets_same_uid(ios_login, ios_login_rescanned):
    """同一個畫面隔一段時間重掃，XML 內容有差異，但骨架要一樣"""
    assert ios_login != ios_login_rescanned
    assert skeleton_signature(ios_login)[1] == skeleton_signature(ios_login_rescanned)[1]


def test_different_screens_get_different_uid(ios_login, ios_policy):
    assert skeleton_signature(ios_login)[1] != skeleton_signature(ios_policy)[1]


def test_ios_skeleton_content(ios_login):
    skeleton, uid = skeleton_signature(ios_login)
    assert skeleton["native_ids"] == ["bxPhone", "loginbutton", "loginidfield", "loginpassfield", "loginremeberbutton"]
    assert skeleton["class_histogram"]["XCUIElementTypeOther"] == "20+"
    assert len(uid) == 12


def test_ios_ignores_invisible_nodes(ios_login):
    original = 'name="loginbutton" label="登入" enabled="true" visible="true"'
    assert original in ios_login
    hidden = ios_login.replace(original, 'name="loginbutton" label="登入" enabled="true" visible="false"')
    assert "loginbutton" not in skeleton_signature(hidden)[0]["native_ids"]


# ---- Android ------------------------------------------------------------

ACTIVITY = ".WatchlistActivity"


def test_android_skeleton_content(android_watchlist):
    skeleton, _ = android_skeleton_signature(android_watchlist, ACTIVITY)
    assert skeleton["activity"] == ACTIVITY
    assert skeleton["nav_title"] == "自選股"
    assert skeleton["selected_tab"] == "台股"
    assert "com.example.demo:id/btn_add" in skeleton["native_ids"]
    assert "com.example.demo:id/hidden_tip" not in skeleton["native_ids"]  # displayed=false
    assert "com.example.demo:id/btn_more" not in skeleton["native_ids"]  # 文字回填


def test_android_data_changes_keep_uid(android_watchlist):
    """價格、清單項目名稱變了還是同一個畫面"""
    changed = (android_watchlist.replace('text="123.45"', 'text="130.00"')
               .replace('text="範例甲"', 'text="範例丁"'))
    original_uid = android_skeleton_signature(android_watchlist, ACTIVITY)[1]
    assert android_skeleton_signature(changed, ACTIVITY)[1] == original_uid


def test_android_shared_row_id_counted_once(android_watchlist):
    """清單多一列（同一個 resource-id）不會變成不同畫面，只要 class 數量沒跨過分桶"""
    skeleton, _ = android_skeleton_signature(android_watchlist, ACTIVITY)
    assert skeleton["native_ids"].count("com.example.demo:id/stock_row") == 1


def test_android_different_tab_or_activity_changes_uid(android_watchlist):
    base = android_skeleton_signature(android_watchlist, ACTIVITY)[1]
    assert android_skeleton_signature(android_watchlist, ".OtherActivity")[1] != base

    # 切到「美股」分頁
    us_tab = re.sub(r'(content-desc="台股"[^>]*?)selected="true"', r'\1selected="false"', android_watchlist)
    us_tab = us_tab.replace('text="台股" clickable="false" enabled="true" scrollable="false" selected="true"',
                            'text="台股" clickable="false" enabled="true" scrollable="false" selected="false"')
    us_tab = re.sub(r'(content-desc="美股"[^>]*?)selected="false"', r'\1selected="true"', us_tab)
    skeleton, uid = android_skeleton_signature(us_tab, ACTIVITY)
    assert skeleton["selected_tab"] == "美股"
    assert uid != base
