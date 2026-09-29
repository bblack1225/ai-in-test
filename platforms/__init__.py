"""
iOS／Android 平台差異集中在這個套件：執行測案（run_test_case.py）、橋接伺服器
（viewer_server.py）只透過平台物件取用這些差異，不自己判斷平台。
"""

from platforms.android import AndroidPlatform
from platforms.ios import IOSPlatform

PLATFORMS = {"ios": IOSPlatform, "android": AndroidPlatform}


def get_platform(name: str):
    return PLATFORMS[name]()
