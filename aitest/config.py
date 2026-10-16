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
import threading
import time
import tomllib
from pathlib import Path

# repo 根目錄（aitest/ 套件的上一層）：config.toml、web/、logs/ 都放在這裡
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 預設放在 repo 根目錄；要用別份設定檔（例如同一台電腦切換兩支手機）設環境變數 AITEST_CONFIG
CONFIG_PATH = Path(os.environ.get("AITEST_CONFIG") or PROJECT_ROOT / "config.toml")


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

# ---------------------------------------------------------------------------
# 跑測案
# ---------------------------------------------------------------------------

# 每個步驟預設的等待上限（秒）：找不到元件、驗證沒通過時在這段時間內反覆重試，一通過就
# 繼續，不會等滿。擋住「畫面突然在 loading」這種沒預期到的延遲。步驟自己寫了 timeout 的
# 以步驟為準；設 0 等於關掉（只試一次，跟以前一樣）
DEFAULT_STEP_TIMEOUT = float(_section("run").get("default_step_timeout", 10))


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


def _error_details(error) -> list:
    body = error.details if isinstance(error.details, dict) else {}
    return [d for d in (body.get("error") or {}).get("details") or [] if isinstance(d, dict)]


def _is_daily_quota(error) -> bool:
    """429 是不是「整天的額度用完」（QuotaFailure 的 quotaId 帶 PerDay）：這種等幾分鐘也不會恢復，不用重試"""
    return any("PerDay" in str(v.get("quotaId", ""))
               for d in _error_details(error) for v in d.get("violations") or [] if isinstance(v, dict))


def _retry_after_seconds(error) -> float | None:
    """429 回應的 details 裡如果有 RetryInfo（例如 "retryDelay": "33s"），回傳 Google 建議等待的秒數"""
    for item in _error_details(error):
        delay = item.get("retryDelay")
        if isinstance(delay, str) and delay.endswith("s"):
            try:
                return float(delay[:-1])
            except ValueError:
                return None
    return None


def _call_with_retry(call, max_retries=4, base_delay_sec=5, failure_detail=None):
    """
    共用的重試迴圈：call() 每次執行一次完整的 Gemini 呼叫，成功就回傳它的結果。
    重試規則見 generate_content_with_retry 的說明。
    failure_detail：失敗時呼叫，回傳的文字接在重試警告後面（串流用來記斷線前收了多久、多少字）。
    """
    import httpx
    from google.genai.errors import ClientError, ServerError

    for attempt in range(1, max_retries + 1):
        try:
            return call()
        # NetworkError 涵蓋連線失敗、讀取中斷（串流途中斷線）等；RemoteProtocolError 是伺服器沒回應就關掉
        except (httpx.NetworkError, httpx.RemoteProtocolError, httpx.TimeoutException) as e:
            if attempt == max_retries:
                raise
            wait_sec = base_delay_sec * attempt
            reason = f"連線中斷（{type(e).__name__}）"
        except ServerError as e:
            if attempt == max_retries:
                raise
            wait_sec = base_delay_sec * attempt
            reason = f"暫時性錯誤 {e.code}"
        except ClientError as e:
            if e.code != 429 or _is_daily_quota(e) or attempt == max_retries:
                raise
            wait_sec = _retry_after_seconds(e) or base_delay_sec * attempt * 2
            reason = "請求太頻繁（429）"
        detail = f"，{failure_detail()}" if failure_detail else ""
        print(f"  [警告] Gemini API {reason}（第{attempt}次）{detail}，{wait_sec:g}秒後重試...")
        time.sleep(wait_sec)


def generate_content_with_retry(usage=None, max_retries=4, base_delay_sec=5, **kwargs):
    """
    usage：這次操作的 LLMUsage（aitest.run_context），呼叫成功就把這次的用量加進去；
    同一次操作的每個呼叫都傳同一個物件，最後就是這次操作的總用量。

    包一層重試邏輯，減少手動重跑的次數：
      - 5xx（ServerError）：Gemini 伺服器端暫時性過載，等幾秒重試
      - 429 每分鐘請求數超過上限：連續掃描很多畫面時常見，等一下就能繼續。
        優先用回應裡 Google 建議的等待秒數，沒有的話等得比 5xx 久一點
      - 429 整天的額度用完：重試也不會恢復，直接拋出（見 TROUBLESHOOTING #17）
      - 連線中斷／逾時（httpx 層的 RemoteProtocolError 等）：長時間的呼叫（例如 Pro 模型）
        常被伺服器端或中間節點直接斷線，回傳 ServerError 之外的例外，重試就能過
      - 其他 4xx（格式錯、金鑰無效）重試也不會變好，直接拋出
    """
    client = get_llm_client()
    response = _call_with_retry(lambda: client.models.generate_content(**kwargs), max_retries, base_delay_sec)
    if usage is not None:
        usage.add(response)
    return response


def generate_content_stream_with_retry(usage=None, label="LLM", max_retries=4, base_delay_sec=5,
                                      progress_interval_sec=15, **kwargs) -> str:
    """
    跟 generate_content_with_retry 一樣的重試規則，但改用串流收回應：資料持續送回來，
    連線不會長時間閒置，比較不容易被中間節點（公司 proxy、VPN）切斷。
    回傳的是完整文字（收完整段才回傳）；串流途中斷線時整段從頭重試，不會回傳不完整的結果。

    label：log 開頭的標籤，看得出是哪個功能在跑。log 有：開始、每 progress_interval_sec 秒一行進度
    （字數一直沒增加就是卡住了）、完成時的耗時與字數；斷線重試時記下這次已串流幾秒、收到幾字，
    每次都斷在差不多的秒數，就是中間節點有總時間上限。
    """
    client = get_llm_client()
    # 這次嘗試的開始時間、目前收到的字數；每次重試都歸零
    progress = {"start": time.monotonic(), "chars": 0}

    def elapsed():
        return time.monotonic() - progress["start"]

    def collect():
        progress["start"], progress["chars"] = time.monotonic(), 0
        stop = threading.Event()

        def report():
            # 用另一條執行緒定時印：Pro 先思考很久才送第一段、或串流卡住時，也看得到它還在等
            while not stop.wait(progress_interval_sec):
                print(f"[{label}] 串流中…已 {elapsed():.0f} 秒，收到 {progress['chars']:,} 字")

        threading.Thread(target=report, daemon=True).start()
        try:
            parts, usage_chunk = [], None
            for chunk in client.models.generate_content_stream(**kwargs):
                if chunk.text:
                    parts.append(chunk.text)
                    progress["chars"] += len(chunk.text)
                # 用量資訊通常只在某些 chunk 帶，記下最後一個有帶的
                if chunk.usage_metadata is not None:
                    usage_chunk = chunk
            return "".join(parts), usage_chunk
        finally:
            stop.set()

    print(f"[{label}] 開始串流（{kwargs.get('model')}）...")
    total_start = time.monotonic()
    text, usage_chunk = _call_with_retry(
        collect, max_retries, base_delay_sec,
        failure_detail=lambda: f"已串流 {elapsed():.0f} 秒、收到 {progress['chars']:,} 字")
    print(f"[{label}] 串流完成，耗時 {time.monotonic() - total_start:.0f} 秒，收到 {len(text):,} 字")
    if usage is not None:
        usage.add(usage_chunk)  # 沒帶用量的串流也算一次呼叫，跟 generate_content_with_retry 一致
    return text
