"""
一鍵啟動＋環境檢查。

    uv run python aitest.py start                    # 啟動 Appium（沒開的話）＋ viewer_server，打開瀏覽器
    uv run python aitest.py start cathaysec-ios      # 直接開這個專案（平台照專案設定）
    uv run python aitest.py start --platform android --no-appium
    uv run python aitest.py doctor                   # 逐項檢查環境，告訴你哪裡沒裝好、怎麼修

start：
  1. Appium 已經在跑就直接用（不會去關它）；沒在跑才自己啟動（只綁 127.0.0.1，
     log 寫到 logs/appium-<時間>.log），按 Ctrl+C 時一起關掉
  2. 啟動 viewer_server.py，等它可以連線後打開瀏覽器
  3. Ctrl+C：先關 viewer_server（讓它把 Appium session 收乾淨），再關自己啟動的 Appium

doctor 只做讀取（查版本、列裝置），不會操作手機、不會改任何設定。
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import quote, urlsplit

import test_config
from viewer_server import DEFAULT_PORT
from workspace import DEFAULT_ROOT, Workspace, WorkspaceError

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOCAL_HOSTS = ("127.0.0.1", "localhost")


# ---------------------------------------------------------------------------
# 共用小工具
# ---------------------------------------------------------------------------

def http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
            return 200 <= res.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def http_json(url: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
            return json.loads(res.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


IS_WINDOWS = os.name == "nt"


def listening_pids(port: int) -> list[str]:
    """在這個 port 上 LISTEN 的程式（同時開好幾個 Appium 會連錯，README 2.8）。"""
    if IS_WINDOWS:
        # netstat -ano 的每一行：協定  本機位址  外部位址  狀態  PID
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, errors="replace").stdout
        pids = set()
        for line in out.splitlines():
            cols = line.split()
            if len(cols) == 5 and cols[1].endswith(f":{port}") and cols[3] == "LISTENING":
                pids.add(cols[4])
        return sorted(pids)
    if not shutil.which("lsof"):
        return []
    out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                         capture_output=True, text=True).stdout
    return sorted(set(out.split()))


def who_listens_hint(port: int) -> str:
    """查是誰佔用 port 的指令，照作業系統給。"""
    return f"netstat -ano | findstr :{port}" if IS_WINDOWS else f"lsof -nP -iTCP:{port} -sTCP:LISTEN"


def kill_hint() -> str:
    return "taskkill /PID <PID> /F" if IS_WINDOWS else "kill <PID>"


def wait_until(check, timeout: float, proc: subprocess.Popen | None = None) -> bool:
    """等 check() 成立；proc 中途結束就不用等了。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return True
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(0.5)
    return check()


def tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def appium_endpoint() -> tuple[str, str, int]:
    """config.toml 的 Appium 位址拆成（狀態網址, host, port）。"""
    url = test_config.APPIUM_SERVER_URL.rstrip("/")
    parts = urlsplit(url)
    return url + "/status", parts.hostname or "127.0.0.1", parts.port or 4723


def project_choices(workspace: str) -> str:
    projects = Workspace(workspace).list_projects()
    if not projects:
        return "  （還沒有專案，用 uv run python workspace.py create 建立）"
    return "\n".join(f"  {p['id']:<24} {p['platform']:<8} {p['name']}" for p in projects)


def resolve_project(project_id: str | None, platform: str | None, workspace: str) -> tuple[str, dict | None]:
    """平台：--platform 優先，其次專案設定，都沒有就 ios。"""
    project = None
    if project_id:
        try:
            project = Workspace(workspace).get_project(project_id)
        except WorkspaceError as e:
            sys.exit(f"錯誤：{e}。可以用的專案：\n{project_choices(workspace)}")
        if platform and platform != project["platform"]:
            sys.exit(f"錯誤：專案「{project_id}」是 {project['platform']}，跟 --platform {platform} 不一致")
    return platform or (project or {}).get("platform") or "ios", project


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

def start_appium(host: str, port: int) -> tuple[subprocess.Popen, Path]:
    appium = shutil.which("appium")
    if not appium:
        sys.exit("錯誤：找不到 appium 指令（npm install -g appium，README 2.1）。"
                 "只想看地圖、編輯測案的話加 --no-appium")
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"appium-{time.strftime('%Y%m%d_%H%M%S')}.log"
    log = open(log_path, "w")
    # 自己一個 process group：終端機按 Ctrl+C 時不會跟著馬上收到訊號，
    # 讓 viewer_server 先關、把 session 收乾淨，再由這裡關 Appium
    proc = subprocess.Popen([appium, "--address", host, "--port", str(port)],
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    return proc, log_path


def stop(proc: subprocess.Popen | None, name: str, sig=signal.SIGTERM, timeout: float = 10) -> None:
    """SIGTERM：Appium 會正常收尾；viewer_server 收到 SIGTERM 跟 Ctrl+C 一樣會先關掉 Appium session。"""
    if proc is None or proc.poll() is not None:
        return
    print(f"[aitest] 關閉 {name}...")
    try:
        proc.send_signal(sig)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def cmd_start(args) -> None:
    # 關掉終端機視窗（SIGHUP）或被 kill（SIGTERM）時也要走 Ctrl+C 的收尾，不然 Appium 會被留在背景。
    # Windows 沒有 SIGHUP，只註冊有的
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is not None:
            signal.signal(sig, _raise_interrupt)
    # 輸出導到檔案時也要即時看得到（例如用 > log 記錄）
    sys.stdout.reconfigure(line_buffering=True)
    platform, project = resolve_project(args.project, args.platform, args.workspace)
    viewer_url = f"http://127.0.0.1:{args.port}/" + (f"?project={quote(args.project)}" if args.project else "")

    # ---- viewer_server 是不是已經開著 ----
    ping = http_json(f"http://127.0.0.1:{args.port}/ping")
    if ping is not None:
        if ping.get("platform") != platform:
            sys.exit(f"錯誤：port {args.port} 已經有 viewer_server 在跑（平台 {ping.get('platform')}），"
                     f"跟這次要的 {platform} 不一樣。先在那個視窗按 Ctrl+C 關掉它再重跑")
        print(f"[aitest] viewer_server 已經在跑（{platform}），不用再啟動：{viewer_url}")
        if not args.no_browser:
            webbrowser.open(viewer_url)
        return
    if listening_pids(args.port):
        sys.exit(f"錯誤：port {args.port} 被其他程式佔用（{who_listens_hint(args.port)} 看是誰），"
                 "關掉它或用 --port 換一個")

    # ---- Appium ----
    appium_proc, appium_log = None, None
    status_url, host, port = appium_endpoint()
    if args.no_appium:
        print("[aitest] 不啟動 Appium（--no-appium）：可以看地圖、編輯測案，操作手機的功能會失敗")
    elif http_ok(status_url):
        pids = listening_pids(port)
        print(f"[aitest] Appium 已經在跑（{test_config.APPIUM_SERVER_URL}），直接使用")
        if len(pids) > 1:
            print(f"[aitest] [警告] port {port} 上有 {len(pids)} 個程式在聽（PID {', '.join(pids)}），"
                  f"可能同時開了好幾個 Appium，Python 會連到錯的那個（README 2.8）。只留一個，多的用 {kill_hint()} 關掉")
    elif host not in LOCAL_HOSTS:
        sys.exit(f"錯誤：config.toml 的 Appium 位址 {test_config.APPIUM_SERVER_URL} 不是本機，"
                 "連不上也不能幫你啟動，請先確認那台的 Appium 有開")
    else:
        appium_proc, appium_log = start_appium(host, port)
        print(f"[aitest] 啟動 Appium（log：{appium_log.relative_to(ROOT)}）...")
        if not wait_until(lambda: http_ok(status_url), 60, appium_proc):
            stop(appium_proc, "Appium")
            sys.exit(f"錯誤：Appium 沒有成功啟動，log 最後幾行：\n{tail(appium_log)}")
        print(f"[aitest] Appium 已啟動：{test_config.APPIUM_SERVER_URL}")

    # ---- viewer_server ----
    cmd = [sys.executable, str(ROOT / "viewer_server.py"), "--platform", platform,
           "--port", str(args.port), "--workspace", args.workspace]
    # 跟 Appium 一樣自己一個 process group：Ctrl+C 只會送到這裡，再由 stop() 送「一次」SIGINT 給它，
    # 不然它會收到兩次，第二次可能打斷它結束時收 Appium session 的動作
    server_proc = subprocess.Popen(cmd, cwd=ROOT, start_new_session=True)
    try:
        if not wait_until(lambda: http_ok(f"http://127.0.0.1:{args.port}/ping"), 30, server_proc):
            raise SystemExit("錯誤：viewer_server 沒有成功啟動（看上面的錯誤訊息）")
        print(f"[aitest] 準備好了：{viewer_url}" + ("" if args.no_browser else "（正在打開瀏覽器）"))
        if project:
            print(f"[aitest] 專案：{project['name']}（{project['platform']}）")
        else:
            print(f"[aitest] 沒有指定專案，網頁會開上次用的那個。下次可以直接指定：aitest.py start <專案代號>\n"
                  f"{project_choices(args.workspace)}")
        print("[aitest] 按 Ctrl+C 結束" + ("（會一起關掉這次啟動的 Appium）" if appium_proc else ""))
        if not args.no_browser:
            webbrowser.open(viewer_url)
        while True:
            if server_proc.poll() is not None:
                print("[aitest] viewer_server 已結束")
                break
            if appium_proc is not None and appium_proc.poll() is not None:
                print(f"[aitest] [警告] Appium 意外結束了，log 最後幾行：\n{tail(appium_log)}")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print()
    finally:
        stop(server_proc, "viewer_server")
        stop(appium_proc, "Appium")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

class Report:
    def __init__(self):
        self.failed = 0
        self.warned = 0

    def section(self, title: str) -> None:
        print(f"\n■ {title}")

    def ok(self, msg: str) -> None:
        print(f"  ✅ {msg}")

    def warn(self, msg: str, fix: str = "") -> None:
        self.warned += 1
        print(f"  ⚠️  {msg}" + (f"\n      → {fix}" if fix else ""))

    def info(self, msg: str) -> None:
        print(f"  ℹ️  {msg}")

    def fail(self, msg: str, fix: str = "") -> None:
        self.failed += 1
        print(f"  ❌ {msg}" + (f"\n      → {fix}" if fix else ""))


def run(cmd: list[str], timeout: float = 20) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def check_python(r: Report) -> None:
    r.section("Python 環境")
    v = sys.version_info
    if v >= (3, 12):
        r.ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        r.fail(f"Python {v.major}.{v.minor}，需要 3.12 以上", "用 uv run python ... 執行（README 2.2）")
    missing = []
    for mod in ("appium", "selenium", "PIL", "google.genai"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        r.fail(f"缺少套件：{', '.join(missing)}", "uv sync")
    else:
        r.ok("套件都裝好了（appium、selenium、pillow、google-genai）")


def check_config(r: Report, platforms: list[str]) -> None:
    r.section("設定（config.toml）")
    if not test_config.CONFIG_PATH.is_file():
        r.fail(f"找不到 {test_config.CONFIG_PATH.name}", "cp config.example.toml config.toml，再填裝置資訊（README 2.6）")
    else:
        r.ok(f"讀到 {test_config.CONFIG_PATH}")
    for p in platforms:
        try:
            caps = test_config.ios_capabilities() if p == "ios" else test_config.android_capabilities()
            r.ok(f"{p} 連線設定完整（udid {caps['udid']}）")
        except test_config.ConfigError as e:
            r.fail(f"{p} 連線設定：{e}")
    if test_config._api_key():
        r.ok(f"Gemini API key 已設定（模型 {test_config.LLM_MODEL}／{test_config.LLM_MODEL_PRO}）")
    else:
        r.warn("沒有 Gemini API key：LLM 相關功能（描述、AI 定位、測案／情境產生）不能用，其他照常",
               "config.toml 的 [llm] api_key，或環境變數 GEMINI_API_KEY（README 2.3）")
    models = (test_config.LLM_MODEL, test_config.LLM_MODEL_PRO)
    if any(m.endswith("-latest") for m in models):
        r.warn("Gemini 模型是會自動換版本的別名（*-latest）", "在 config.toml 的 [llm] 改成固定版本號（範本見 config.example.toml）")
    for m in models:
        if "preview" in m:
            r.warn(f"{m} 是預覽版，Google 可能提前下架或改變行為",
                   "出現找不到模型的錯誤時，在 config.toml 的 [llm] 換成當下的正式版（README 六、第 4 點）")


def check_appium(r: Report, platforms: list[str]) -> None:
    r.section("Appium")
    if not shutil.which("node"):
        r.fail("找不到 node", "用 nvm 或 Homebrew 安裝 Node.js（README 2.1）")
    appium = shutil.which("appium")
    if not appium:
        r.fail("找不到 appium 指令", "npm install -g appium（README 2.1）")
        return
    res = run([appium, "--version"])
    r.ok(f"Appium {res.stdout.strip().splitlines()[-1] if res and res.stdout.strip() else '（版本不明）'}")
    res = run([appium, "driver", "list", "--installed", "--json"], timeout=60)
    installed = {}
    if res and res.returncode == 0:
        try:
            installed = json.loads(res.stdout[res.stdout.index("{"):])
        except ValueError:
            installed = {}
    need = {"ios": "xcuitest", "android": "uiautomator2"}
    for p in platforms:
        name = need[p]
        if name in installed:
            r.ok(f"{name} driver {installed[name].get('version', '')}")
        else:
            r.fail(f"沒有安裝 {name} driver（{p} 需要）", f"appium driver install {name}")
    status_url, host, port = appium_endpoint()
    pids = listening_pids(port)
    if http_ok(status_url):
        r.ok(f"Appium 正在執行（{test_config.APPIUM_SERVER_URL}）")
        if len(pids) > 1:
            r.fail(f"port {port} 上有 {len(pids)} 個程式在聽（PID {', '.join(pids)}），可能同時開了好幾個 Appium",
                   f"只留一個，多的用 {kill_hint()} 關掉（README 2.8）")
    else:
        r.info("Appium 目前沒在跑（aitest.py start 會自動啟動）")


def check_ios(r: Report) -> None:
    r.section("iOS 裝置")
    res = run(["xcodebuild", "-version"])
    if res and res.returncode == 0:
        r.ok(res.stdout.strip().splitlines()[0])
    else:
        r.fail("找不到 Xcode（xcodebuild）", "App Store 安裝 Xcode，裝完 sudo xcodebuild -license accept（README 2.1）")
        return
    res = run(["security", "find-identity", "-v", "-p", "codesigning"])
    if res and "Apple Development" in res.stdout:
        r.ok("有 Apple Development 簽署憑證")
    else:
        r.fail("沒有 Apple Development 簽署憑證", "Xcode → Settings → Accounts 登入 Apple ID（README 2.5）")
    udid = str((test_config.CONFIG.get("ios") or {}).get("udid") or "")
    res = run(["xcrun", "devicectl", "list", "devices"], timeout=30)
    if not res or res.returncode != 0:
        r.warn("查不到裝置清單（xcrun devicectl 失敗）", "確認 Xcode 版本，或手動用 Xcode 的 Devices 視窗看")
        return
    line = next((l for l in res.stdout.splitlines() if udid and udid in l), None)
    if not udid:
        r.fail("config.toml 沒填 [ios] udid", "xcrun xctrace list devices 查 UDID（README 2.4）")
    elif line is None:
        r.fail(f"Mac 看不到手機 {udid}", "USB 接上、解鎖、按「信任這台電腦」（README 2.4）")
    elif "unavailable" in line:
        r.fail(f"手機 {udid} 狀態是 unavailable", "重新插拔 USB、解鎖手機，或確認開發者模式有開（README 2.4）")
    else:
        r.ok(f"手機已連接（{udid}）")
    r.info("WDA 簽署有沒有過期 doctor 看不出來；連線報 xcodebuild code 65／0xe8008011 時照 README 2.5 用 Xcode ⌘U 重簽")


def check_android(r: Report) -> None:
    r.section("Android 裝置")
    adb = shutil.which("adb")
    if not adb:
        r.fail("找不到 adb", "安裝 Android SDK platform-tools，並把它加進 PATH（README 2.9）")
        return
    udid = str((test_config.CONFIG.get("android") or {}).get("udid") or "")
    res = run([adb, "devices"])
    devices = {}
    for line in (res.stdout.splitlines()[1:] if res else []):
        parts = line.split()
        if len(parts) >= 2:
            devices[parts[0]] = parts[1]
    if udid in devices and devices[udid] == "device":
        r.ok(f"裝置已連接（{udid}）")
    elif udid in devices:
        r.fail(f"裝置 {udid} 狀態是 {devices[udid]}", "unauthorized 的話在手機上允許 USB 偵錯")
    else:
        found = "、".join(devices) or "沒有任何裝置"
        r.fail(f"adb 看不到 {udid or '（config.toml 沒填 [android] udid）'}（目前：{found}）",
               "開模擬器或接上手機，config.toml 的 [android] udid 填 adb devices 看到的代號")


def check_workspace(r: Report, workspace: str, port: int) -> None:
    r.section("專案與網頁")
    ws = Workspace(workspace)
    projects = ws.list_projects()
    if projects:
        for p in projects:
            maps = ws.list_maps(p["id"])
            note = "" if p.get("bundle_id") else "（沒填 bundle id，兩支以上 App 時建議用 workspace.py update 補上）"
            r.ok(f"專案 {p['id']}：{p['name']}，{p['platform']}，{len(maps)} 張地圖{note}")
    else:
        r.warn(f"{workspace}/ 底下還沒有專案", "uv run python workspace.py create <代號> --platform ios --name <名稱>（README 3.3）")
    ping = http_json(f"http://127.0.0.1:{port}/ping")
    if ping is not None:
        r.ok(f"viewer_server 正在執行（{ping.get('platform')}）：http://127.0.0.1:{port}/")
    elif listening_pids(port):
        r.fail(f"port {port} 被其他程式佔用", f"{who_listens_hint(port)} 看是誰，或 aitest.py start --port 換一個")
    else:
        r.ok(f"port {port} 空著，可以啟動 viewer_server")


def cmd_doctor(args) -> None:
    platforms = [args.platform] if args.platform else ["ios", "android"]
    r = Report()
    check_python(r)
    check_config(r, platforms)
    check_appium(r, platforms)
    if "ios" in platforms:
        check_ios(r)
    if "android" in platforms:
        check_android(r)
    check_workspace(r, args.workspace, args.port)
    print()
    if r.failed:
        print(f"有 {r.failed} 項要處理（❌），{r.warned} 項提醒（⚠️）")
        if not args.platform:
            print("只用其中一個平台的話，加 --platform ios 或 --platform android，就不會檢查另一個")
        sys.exit(1)
    print("環境都沒問題" + (f"（{r.warned} 項提醒）" if r.warned else "") + "，可以 uv run python aitest.py start")


def main():
    parser = argparse.ArgumentParser(description="一鍵啟動（start）＋環境檢查（doctor）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="啟動 Appium（沒開的話）＋ viewer_server，打開瀏覽器")
    s.add_argument("project", nargs="?", default=None,
                   help="直接開這個專案（平台照專案設定），例如 cathaysec-ios；有哪些用 workspace.py list 查")
    s.add_argument("--platform", choices=("ios", "android"), default=None, help="預設：專案的平台，沒指定專案就是 ios")
    s.add_argument("--no-appium", action="store_true", help="不啟動 Appium（只看地圖、編輯測案）")
    s.add_argument("--no-browser", action="store_true", help="不自動打開瀏覽器")
    d = sub.add_parser("doctor", help="逐項檢查環境（只讀取，不操作手機）")
    d.add_argument("--platform", choices=("ios", "android"), default=None, help="只檢查這個平台（預設兩個都查）")
    for p in (s, d):
        p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"viewer_server 的 port（預設 {DEFAULT_PORT}）")
        p.add_argument("--workspace", default=str(DEFAULT_ROOT), help="專案資料夾（預設 ./workspace）")
    args = parser.parse_args()
    os.chdir(ROOT)  # 截圖、workspace 這些相對路徑都以專案資料夾為準
    {"start": cmd_start, "doctor": cmd_doctor}[args.cmd](args)


if __name__ == "__main__":
    main()
