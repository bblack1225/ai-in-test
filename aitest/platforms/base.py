"""
iOS／Android 平台共用的東西：等畫面穩定、爬蟲判斷用的共用規則（對話框按鈕清單、
常駐框架範圍、「像不像資料」）、捲容器前縮邊界。

只 import 第三方套件，ios.py、android.py、locate.py 和 core/ 都可以放心使用。

例外處理的原則（報告 #10）：平台層攔 Appium 的錯誤時只攔 WebDriverException
（查不到、元件剛好消失、查詢語法不對…這類可以退回備援的），而且先把
InvalidSessionIdException 往上丟——它也是 WebDriverException 的子類，但代表連線已經
斷了，繼續走備援只會讓「斷線」看起來像「找不到元件」。連不上 Appium server 的錯誤
（urllib3 的連線錯誤）本來就不是 WebDriverException，不會被攔到。
"""

import re
import time
from contextlib import contextmanager

from selenium.common.exceptions import InvalidSessionIdException, WebDriverException

# ---------------------------------------------------------------------------
# 等畫面穩定（報告 #5：原本爬蟲、跑測案、掃描各寫一套）
# ---------------------------------------------------------------------------


def wait_until_stable(driver, signature=None, max_wait: float = 3.0, poll_interval: float = 0.4) -> bool:
    """
    輪詢 page_source，直到連續兩次的 signature(page_source) 一樣（回傳 True），或真實經過的
    時間超過 max_wait（回傳 False）。signature 沒給就比對整份 page_source。

    三個地方共用，差別只在比對什麼、等多久：
      - 爬蟲點擊後、關掉對話框後：整份 page_source，1.5～3 秒
      - 跑測案 tap／input_text 之後：版面指紋（run_test_case.wait_after_action，不看文字，
        報價跳動不影響），3 秒
      - 掃描前：候選元件簽章（capture_screen_map.wait_for_screen_stable），7.5 秒

    時間用真實經過的秒數算，不是輪詢次數：iOS 讀一次 page_source 就要 1～3 秒，用次數估計
    實際會等好幾倍。報價這類一直在變的畫面永遠不會判定穩定，靠 max_wait 界定等待時間。
    讀 page_source 失敗就不等了（回傳 False），連線斷掉（InvalidSessionIdException）照常往上丟。
    """
    previous = None
    deadline = time.monotonic() + max_wait
    while True:
        try:
            source = driver.page_source
        except InvalidSessionIdException:
            raise
        except WebDriverException:
            return False
        current = signature(source) if signature else source
        if current == previous:
            return True
        if time.monotonic() >= deadline:
            return False
        previous = current
        time.sleep(poll_interval)


@contextmanager
def no_idle_wait(driver):
    """
    跑測案期間把 waitForIdleTimeout 設 0（UiAutomator2、XCUITest 都有這個設定），結束後恢復原本設定。

    預設每個指令（讀畫面、找元件、點擊、檢查可不可見）都會先等 App 靜止，最多 10 秒。
    首頁報價這種一直在跳的畫面永遠等不到，每個指令都等滿 10 秒，一步就要幾十秒。
    跑測案時「等畫面好了沒」由我們自己控制：每一步沒通過就在 timeout 內重試
    （run_test_case.retry_until_pass），tap／輸入後等版面穩定（wait_after_action），
    不需要 Appium 這層再等一次。
    """
    try:
        original = driver.get_settings().get("waitForIdleTimeout")
        driver.update_settings({"waitForIdleTimeout": 0})
    except InvalidSessionIdException:
        raise
    except WebDriverException as e:
        print(f"    [waitForIdleTimeout] 設定失敗，照預設等待執行: {e}")
        original = None
    try:
        yield
    finally:
        if original is not None:
            try:
                driver.update_settings({"waitForIdleTimeout": original})
            except WebDriverException:
                pass  # session 已經斷了之類的，沒有要恢復的對象


# ---------------------------------------------------------------------------
# 捲動
# ---------------------------------------------------------------------------


def shrink_bounds(bounds: dict, x_ratio: float = 0.05, y_ratio: float = 0.2) -> dict:
    """捲動手勢的範圍略縮在容器邊界內，避免手勢起點／終點剛好落在邊界外（或落在旁邊的元件上）"""
    margin_x = max(int(bounds["width"] * x_ratio), 1)
    margin_y = max(int(bounds["height"] * y_ratio), 1)
    return {
        "x": bounds["x"] + margin_x,
        "y": bounds["y"] + margin_y,
        "width": max(bounds["width"] - margin_x * 2, 1),
        "height": max(bounds["height"] - margin_y * 2, 1),
    }


def main_scroll_area(screen_width: int, screen_height: int) -> dict:
    """涵蓋畫面主要範圍的捲動區域（避開最上方的狀態列／導覽列、最下方的 tab bar）"""
    return {
        "x": int(screen_width * 0.1),
        "y": int(screen_height * 0.15),
        "width": int(screen_width * 0.8),
        "height": int(screen_height * 0.7),
    }


# ---------------------------------------------------------------------------
# 爬蟲（crawl_app_tree.py）的共用規則：各平台的判斷在 ios.py、android.py 的「爬蟲用」那一段
# ---------------------------------------------------------------------------

# 擋在前面的提示對話框，優先點這類「純關閉」語意的按鈕
MODAL_DISMISS_LABELS = [
    "我知道了", "知道了", "我已詳閱", "同意並繼續", "繼續使用", "關閉",
    "略過", "跳過", "下次再說", "稍後再說", "稍後提醒我", "稍後", "以後再說",
    "不再顯示", "取消",
    # 行銷活動／評分邀請／推播邀請常見的婉拒按鈕
    "不用了", "暫時不要", "暫時不用", "暫不", "不了", "不，謝謝", "不了，謝謝", "殘忍拒絕",
    "Not now", "Later", "No thanks", "No, thanks", "Close",
]
# 找不到上面那些、且對話框看起來就是純資訊（按鈕很少）時，才點這類「確認」語意的
MODAL_ACK_ONLY_LABELS = ["確定", "確認", "好", "OK", "我瞭解"]
# 永遠不點（會關掉整個 App / 登出）
NEVER_TAP_LABELS = {
    "關閉APP", "關閉 APP", "退出APP", "退出 APP", "離開APP", "結束App",
    "結束 App", "登出", "確定登出",
}

# 出現這些字樣才值得花時間去查一次關閉鈕（效能：絕大多數 tap 後沒有 modal，
# 快篩要挑「畫面內容不太可能自然出現」的詞——刻意不放 MODAL_DISMISS_LABELS /
# MODAL_ACK_ONLY_LABELS 整包進來，那裡面「確定/取消/更新/版本」這種單詞在資料量大
# 的真實畫面上（實測 1000+ 元件的報價/清單頁）到處都會出現，快篩形同虛設）
MODAL_HINT_MARKERS = (
    "XCUIElementTypeAlert", "連線中斷", "偵測到系統", "請確認是否", "提醒您",
    "溫馨提醒", "重要通知", "我知道了", "繼續使用", "我已詳閱", "同意並繼續",
    "公告",
)

# 底部導覽 / 頂部導覽列這類「常駐框架」：畫面最上緣 / 最下緣的可點元件，
# 不管實際 class 是不是 XCUIElementTypeTabBar（很多 App 的底部導覽是一排 Button）
CHROME_TOP_FRACTION = 0.11
CHROME_BOTTOM_FRACTION = 0.88

# 骨架簽章裡，判斷一段文字「是不是填進版型的資料」（會隨資料變動）
_DATA_LABEL_RE = re.compile(
    r"^\s*(?:"
    r"[-+]?N?T?\$?\s?[\d,]+(?:\.\d+)?%?"       # 金額 / 數字 / 百分比
    r"|\d{1,4}[/\-.]\d{1,2}(?:[/\-.]\d{1,2})?"  # 日期
    r"|\d{1,2}:\d{2}(?::\d{2})?"                # 時間
    r"|[-–—]{1,}"                               # 佔位符 "--"
    r")\s*$"
)


def looks_like_data(text: str) -> bool:
    if not text:
        return True
    t = text.strip()
    if _DATA_LABEL_RE.match(t):
        return True
    # 內含較長數字串（12,345.67）且整體不長 -> 多半是金額 / 數量
    if len(t) <= 16 and re.search(r"\d[\d,]{2,}(?:\.\d+)?", t):
        return True
    return False


def histogram_bucket(n: int) -> str:
    if n <= 4:
        return str(n)
    if n <= 9:
        return "5-9"
    if n <= 19:
        return "10-19"
    return "20+"


_INDEX_SUFFIX_RE = re.compile(r"(?:[_\-]?\d+)+$")


def candidate_pattern(el: dict) -> str:
    """
    把清單類元件的 native_id 去掉結尾的索引數字，得到「同一批」的 pattern，
    例如 MBKGridQuote_QuoteDataCell_0_12 -> MBKGridQuote_QuoteDataCell。
    用於同模板取樣（見 crawl_app_tree.crawl_screen）。沒有 native_id 的回傳 ""（不取樣）。
    """
    nid = (el.get("native_id") or "").strip()
    if not nid:
        return ""
    stripped = _INDEX_SUFFIX_RE.sub("", nid)
    return stripped if stripped and stripped != nid else ""
