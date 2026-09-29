"""
共用設定：Appium 連線資訊 + Gemini LLM 設定

capture_screen_map.py、generate_test_case.py、run_test_case.py 三支腳本共用，
換裝置/App 只需要改這裡一個地方。
"""

import os
import time

from google import genai
from google.genai.errors import ServerError

# ---------------------------------------------------------------------------
# Appium 連線設定 (iOS)
# ---------------------------------------------------------------------------

DESIRED_CAPS = {
    "platformName": "iOS",
    "automationName": "XCUITest",
    "udid": "00008110-0019098C34A8201E",
    "xcodeOrgId": "5Q9SABC4U6",
    "xcodeSigningId": "iPhone Developer",
    "bundleId": "com.cathay.securities.tw.mBroker.Demo",
    "noReset": True,
    "updatedWDABundleId": "com.blakeliu.WebDriverAgentRunner",
    "wdaLaunchTimeout": 180000,
}

APPIUM_SERVER_URL = "http://127.0.0.1:4723"

# ---------------------------------------------------------------------------
# Appium 連線設定 (Android)
# ---------------------------------------------------------------------------

ANDROID_DESIRED_CAPS = {
    "platformName": "Android",
    "automationName": "UiAutomator2",
    "udid": "59031FDCQ001N1",
    "noReset": True,
}

# ---------------------------------------------------------------------------
# LLM 設定 (Gemini)
# ---------------------------------------------------------------------------

llm_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
LLM_MODEL = "gemini-flash-latest"

# 高頻率、範圍窄、有實測驗證迴圈把關的任務（單一元件描述、XPath生成/定位）
# 用 LLM_MODEL（Flash）就夠、也划算。像「測試情境產生」這種一次性、需要
# 綜合判斷力的任務改用這個 Pro 版本，呼叫頻率低（一次分析按鈕一次呼叫），
# 多付的成本有限，換來明顯更好的推論品質。
LLM_MODEL_PRO = "gemini-pro-latest"


def generate_content_with_retry(max_retries=4, base_delay_sec=5, **kwargs):
    """
    包一層重試邏輯：Gemini API 偶爾會回傳 503（伺服器端暫時性過載，
    不是我們呼叫頻率的問題），用指數退避重試幾次，減少手動重跑的次數。
    429（配額用盡）不屬於 ServerError，不會被這裡攔截重試，會直接拋出。
    """
    for attempt in range(1, max_retries + 1):
        try:
            return llm_client.models.generate_content(**kwargs)
        except ServerError as e:
            if attempt == max_retries:
                raise
            wait_sec = base_delay_sec * attempt
            print(f"  [警告] Gemini API 暫時性錯誤（第{attempt}次，{e.code if hasattr(e, 'code') else ''}），"
                  f"{wait_sec}秒後重試...")
            time.sleep(wait_sec)
