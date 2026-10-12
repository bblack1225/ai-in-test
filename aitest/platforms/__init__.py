"""
iOS／Android 平台差異集中在這個套件：掃描（capture_screen_map.py）、執行測案（run_test_case.py）、
爬蟲（crawl_app_tree.py）、橋接伺服器（viewer_server.py）只透過平台物件取用這些差異，不自己判斷平台。

  ios.py、android.py  各平台的平台物件（IOSPlatform／AndroidPlatform）和它們的實作
  locate.py           無 native id 元件的定位流程，兩個平台共用
  base.py             等畫面穩定、捲動範圍、爬蟲共用的規則（對話框按鈕清單、常駐框架範圍…）
  scan_core.py        掃描的合併邏輯（候選元件身分 key、讀一輪畫面並合併）

這個套件只 import aitest.common、aitest.run_context、aitest.config 和第三方套件，不 import core/。
"""

from aitest.platforms.android import AndroidPlatform
from aitest.platforms.ios import IOSPlatform

PLATFORMS = {"ios": IOSPlatform, "android": AndroidPlatform}


def get_platform(name: str):
    return PLATFORMS[name]()
