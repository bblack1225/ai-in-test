"""
共用設定：從 config.toml 讀 Appium 連線資訊、Gemini 模型，所有腳本都從這裡取用。

- 裝置、簽署這類「這台電腦」的設定在 config.toml（不進 git，範本是 config.example.toml）
- App 的 bundle id／package 在各專案的 project.json（workspace.py），換 App 不用改設定
- Gemini API key 在 config.toml 的 [llm] api_key，或環境變數 GEMINI_API_KEY（環境變數優先）

沒有 config.toml 也能 import：只看地圖、編輯測案這類不碰手機的功能照常能用，
要連手機時（ios_capabilities()／android_capabilities()）才會說缺了什麼。
同理沒設 GEMINI_API_KEY 也能 import，用到 LLM 時（get_llm_client()）才報錯。
"""

import os
import time
import tomllib
from pathlib import Path

# 預設跟這支程式放在一起；要用別份設定檔（例如同一台電腦切換兩支手機）設環境變數 AITEST_CONFIG
CONFIG_PATH = Path(os.environ.get("AITEST_CONFIG") or Path(__file__).with_name("config.toml"))


class ConfigError(Exception):
    """設定缺漏，訊息會說要改哪個檔案的哪個欄位。"""


def _load_config() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{CONFIG_PATH} 格式錯誤：{e}") from e


CONFIG = _load_config()


def _section(name: str) -> dict:
    return CONFIG.get(name) or {}


def _require(section: str, keys: list[str]) -> dict:
    values = _section(section)
    missing = [k for k in keys if not str(values.get(k) or "").strip()]
    if missing:
        where = (f"{CONFIG_PATH} 的 [{section}]" if CONFIG_PATH.is_file()
                 else f"{CONFIG_PATH}（還沒建立：cp config.example.toml config.toml）")
        raise ConfigError(f"連線設定缺少 {', '.join(missing)}，請填在 {where}")
    return values


# ---------------------------------------------------------------------------
# Appium 連線設定
# ---------------------------------------------------------------------------

APPIUM_SERVER_URL = _section("appium").get("server_url") or "http://127.0.0.1:4723"


def ios_capabilities(app_id: str | None = None) -> dict:
    """
    app_id：連線時要打開的 App（例如爬蟲知道專案的 bundle id）。沒給就用 config 的
    launch_bundle_id；兩個都沒有就不打開任何 App，直接操作手機上目前在前景的 App。
    """
    ios = _require("ios", ["udid", "xcode_org_id", "wda_bundle_id"])
    caps = {
        "platformName": "iOS",
        "automationName": "XCUITest",
        "udid": ios["udid"],
        "xcodeOrgId": ios["xcode_org_id"],
        "xcodeSigningId": ios.get("xcode_signing_id") or "iPhone Developer",
        "updatedWDABundleId": ios["wda_bundle_id"],
        "noReset": True,
        "wdaLaunchTimeout": int(ios.get("wda_launch_timeout_ms") or 180000),
    }
    bundle_id = app_id or ios.get("launch_bundle_id")
    if bundle_id:
        caps["bundleId"] = bundle_id
    caps.update(_section("capabilities").get("ios") or {})
    return caps


def android_capabilities() -> dict:
    """Android 一律接手目前在前景的 App（不指定 appPackage），換 App 在手機上切過去就好。"""
    android = _require("android", ["udid"])
    caps = {
        "platformName": "Android",
        "automationName": "UiAutomator2",
        "udid": android["udid"],
        "noReset": True,
    }
    caps.update(_section("capabilities").get("android") or {})
    return caps


# ---------------------------------------------------------------------------
# LLM 設定 (Gemini)
# ---------------------------------------------------------------------------

# 預設寫死版本（2026-10-02 *-latest 別名實際指向的版本），避免 Google 換版本時行為／計費突然改變
LLM_MODEL = _section("llm").get("model") or "gemini-3.8-flash"

# 高頻率、範圍窄、有實測驗證迴圈把關的任務（單一元件描述、XPath生成/定位）
# 用 LLM_MODEL（Flash）就夠、也划算。像「測試情境產生」這種一次性、需要
# 綜合判斷力的任務改用這個 Pro 版本，呼叫頻率低（一次分析按鈕一次呼叫），
# 多付的成本有限，換來明顯更好的推論品質。
LLM_MODEL_PRO = _section("llm").get("model_pro") or "gemini-3.1-pro-preview"

_llm_client = None


def _api_key() -> str | None:
    # 環境變數優先：方便臨時換一把 key 測試，不用改檔案
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            or str(_section("llm").get("api_key") or "").strip() or None)


def get_llm_client():
    """第一次用到 LLM 才建立連線：沒設 API key 時，不用 LLM 的功能（看地圖、點擊、爬蟲）照常能跑。"""
    global _llm_client
    if _llm_client is None:
        api_key = _api_key()
        if not api_key:
            raise ConfigError(f"沒有設定 Gemini API key（{CONFIG_PATH} 的 [llm] api_key，或環境變數 GEMINI_API_KEY），"
                              "LLM 相關功能（描述、AI定位、測案／情境產生）無法使用（README 2.3）")
        from google import genai
        _llm_client = genai.Client(api_key=api_key)
    return _llm_client


def generate_content_with_retry(max_retries=4, base_delay_sec=5, **kwargs):
    """
    包一層重試邏輯：Gemini API 偶爾會回傳 503（伺服器端暫時性過載，
    不是我們呼叫頻率的問題），用指數退避重試幾次，減少手動重跑的次數。
    429（配額用盡）不屬於 ServerError，不會被這裡攔截重試，會直接拋出。
    """
    from google.genai.errors import ServerError

    client = get_llm_client()
    for attempt in range(1, max_retries + 1):
        try:
            return client.models.generate_content(**kwargs)
        except ServerError as e:
            if attempt == max_retries:
                raise
            wait_sec = base_delay_sec * attempt
            print(f"  [警告] Gemini API 暫時性錯誤（第{attempt}次，{e.code if hasattr(e, 'code') else ''}），"
                  f"{wait_sec}秒後重試...")
            time.sleep(wait_sec)
