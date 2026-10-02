"""
專案資料夾（workspace）：每支 App 的每個平台一個專案，地圖、截圖、測案、批次設定
都放在同一個資料夾，網頁（viewer_server.py）跟 CLI（crawl_app_tree.py）讀寫同一份。

    workspace/                      ← 整個不進 git（地圖、截圖含帳號金額等畫面資料）
    └── projects/
        └── mbroker-demo-ios/
            ├── project.json        ← 名稱、平台、bundle_id
            ├── map.json            ← 地圖（crawl_app_tree.py 的全名格式）
            ├── screenshots/        ← map.json 裡的 "screenshots/xxx.png" 相對於專案資料夾
            ├── cases/<名稱>.json   ← 測試案例
            └── batches/<名稱>.json ← 批次設定，測案用名稱引用、地圖固定用這個專案的

交付給不同團隊時，各自只拿自己 App 的專案（匯出成 zip，對方匯入），工具本身是同一份。

CLI：
    uv run python workspace.py list
    uv run python workspace.py create mbroker-demo-ios --platform ios --name "樹精靈 Demo iOS" \\
        --bundle_id com.cathay.securities.tw.mBroker.Demo
    # 把舊的 app_map_tree.json＋用到的截圖＋test_cases/ 搬進專案（不會刪原檔）
    uv run python workspace.py import-legacy mbroker-demo-ios --map app_map_tree.json --cases test_cases/*.json
"""

import argparse
import io
import json
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

DEFAULT_ROOT = Path("workspace")

PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# 測案／批次的名稱就是檔名：允許中文，但不能有路徑分隔字元、不能以 . 開頭
ITEM_NAME_RE = re.compile(r"^\w[\w .()（）-]{0,99}$")
PLATFORMS = ("ios", "android")
KINDS = ("cases", "batches")
# 匯入 zip 的上限，避免誤選到巨大檔案把硬碟塞滿
MAX_IMPORT_BYTES = 2 * 1024 ** 3


class WorkspaceError(Exception):
    """使用者可以看懂、可以自己修正的錯誤；status 對應 HTTP 狀態碼。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def empty_map(project: dict) -> dict:
    return {
        "app": {"platform": project["platform"], "bundle_id": project.get("bundle_id") or "",
                "crawled_at": "", "root_name": "", "max_depth": 0},
        "screens": [], "edges": [], "skipped": [],
    }


def _write_json_atomic(path: Path, data) -> None:
    """先寫暫存檔再改名：寫到一半當掉（或爬蟲同時在讀）不會讀到半份 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _version(path: Path) -> str | None:
    return str(path.stat().st_mtime_ns) if path.exists() else None


class Workspace:
    def __init__(self, root: Path | str = DEFAULT_ROOT):
        self.root = Path(root)
        self.projects_dir = self.root / "projects"

    # ---- 專案 ----------------------------------------------------------

    def project_dir(self, project_id: str) -> Path:
        if not PROJECT_ID_RE.match(project_id or ""):
            raise WorkspaceError(f"專案代號「{project_id}」不合法：只能用小寫英數字、- 和 _，例如 mbroker-demo-ios")
        return self.projects_dir / project_id

    def _existing_dir(self, project_id: str) -> Path:
        d = self.project_dir(project_id)
        if not (d / "project.json").is_file():
            raise WorkspaceError(f"找不到專案「{project_id}」", 404)
        return d

    def list_projects(self) -> list[dict]:
        if not self.projects_dir.is_dir():
            return []
        out = []
        for d in sorted(self.projects_dir.iterdir()):
            if (d / "project.json").is_file():
                out.append({"id": d.name, **_read_json(d / "project.json")})
        return out

    def get_project(self, project_id: str) -> dict:
        d = self._existing_dir(project_id)
        return {"id": project_id, **_read_json(d / "project.json")}

    def create_project(self, project_id: str, name: str, platform: str, bundle_id: str = "") -> dict:
        d = self.project_dir(project_id)
        if d.exists():
            raise WorkspaceError(f"專案「{project_id}」已經存在", 409)
        if platform not in PLATFORMS:
            raise WorkspaceError(f"平台要是 {' 或 '.join(PLATFORMS)}")
        project = {"name": name or project_id, "platform": platform, "bundle_id": bundle_id or "",
                   "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        _write_json_atomic(d / "project.json", project)
        _write_json_atomic(d / "map.json", empty_map(project))
        for sub in ("screenshots", *KINDS):
            (d / sub).mkdir(exist_ok=True)
        return {"id": project_id, **project}

    def summary(self, project_id: str) -> dict:
        """網頁開專案時一次拿齊：專案資訊、地圖（含版本）、測案／批次清單。"""
        project = self.get_project(project_id)
        map_data, version = self.read_map(project_id)
        return {"project": project, "map": map_data, "map_version": version,
                "cases": self.list_items(project_id, "cases"),
                "batches": self.list_items(project_id, "batches")}

    # ---- 地圖 ----------------------------------------------------------

    def map_path(self, project_id: str) -> Path:
        return self._existing_dir(project_id) / "map.json"

    def read_map(self, project_id: str) -> tuple[dict, str | None]:
        path = self.map_path(project_id)
        if not path.exists():
            return empty_map(self.get_project(project_id)), None
        return _read_json(path), _version(path)

    def write_map(self, project_id: str, map_data: dict, base_version: str | None) -> str:
        """
        base_version 是寫入方上次讀到的版本：跟目前檔案不一樣，代表中間有別人
        （另一個分頁、爬蟲）改過，直接拒絕，不默默蓋掉對方的修改。
        """
        if not isinstance(map_data, dict) or not isinstance(map_data.get("screens"), list):
            raise WorkspaceError("地圖格式不對：找不到 screens 陣列")
        path = self.map_path(project_id)
        current = _version(path)
        if current is not None and base_version != current:
            raise WorkspaceError("地圖已經被其他地方更新過（另一個分頁或爬蟲），請重新載入專案後再改", 409)
        _write_json_atomic(path, map_data)
        return _version(path)

    # ---- 測案／批次 ----------------------------------------------------

    def _item_path(self, project_id: str, kind: str, name: str) -> Path:
        if kind not in KINDS:
            raise WorkspaceError(f"不支援的類型：{kind}", 404)
        if not ITEM_NAME_RE.match(name or ""):
            raise WorkspaceError(f"名稱「{name}」不合法：不能有 / \\ 等符號、不能以 . 開頭，最多 100 字")
        return self._existing_dir(project_id) / kind / f"{name}.json"

    def list_items(self, project_id: str, kind: str) -> list[dict]:
        d = self._existing_dir(project_id) / kind
        if not d.is_dir():
            return []
        items = []
        for p in sorted(d.glob("*.json")):
            if p.name.startswith("."):
                continue
            entry = {"name": p.stem, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))}
            if kind == "cases":
                try:
                    data = _read_json(p)
                    entry["test_name"] = data.get("test_name")
                    entry["steps"] = len(data.get("steps") or [])
                except (json.JSONDecodeError, OSError, AttributeError):
                    entry["error"] = "檔案不是合法的測案 JSON"
            items.append(entry)
        return items

    def read_item(self, project_id: str, kind: str, name: str):
        path = self._item_path(project_id, kind, name)
        if not path.is_file():
            raise WorkspaceError(f"找不到「{name}」", 404)
        return _read_json(path)

    def write_item(self, project_id: str, kind: str, name: str, data, overwrite: bool) -> None:
        if kind == "cases" and not (isinstance(data, dict) and isinstance(data.get("steps"), list)):
            raise WorkspaceError("這不是測試案例（找不到 steps 陣列）")
        if kind == "batches" and not (isinstance(data, dict) and isinstance(data.get("groups"), list)):
            raise WorkspaceError("這不是批次設定（找不到 groups 陣列）")
        path = self._item_path(project_id, kind, name)
        if path.exists() and not overwrite:
            raise WorkspaceError(f"「{name}」已經存在", 409)
        _write_json_atomic(path, data)

    def delete_item(self, project_id: str, kind: str, name: str) -> None:
        path = self._item_path(project_id, kind, name)
        if not path.is_file():
            raise WorkspaceError(f"找不到「{name}」", 404)
        path.unlink()

    # ---- 匯出／匯入 zip（交付給其他團隊用）-----------------------------

    def export_zip(self, project_id: str) -> bytes:
        d = self._existing_dir(project_id)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(d.rglob("*")):
                if p.is_file() and not p.name.startswith("."):
                    zf.write(p, f"{project_id}/{p.relative_to(d).as_posix()}")
        return buf.getvalue()

    def import_zip(self, data: bytes, project_id: str | None = None) -> dict:
        """
        zip 裡第一層是原本的專案代號；project_id 有給就改用這個代號匯入
        （例如同一份專案想留一份對照）。已經存在的專案不會被覆蓋。
        """
        if len(data) > MAX_IMPORT_BYTES:
            raise WorkspaceError("檔案太大（超過 2GB）")
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise WorkspaceError("這不是 zip 檔")
        with zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            tops = {n.split("/", 1)[0] for n in names}
            if len(tops) != 1 or any("/" not in n for n in names) or not any(n.endswith("/project.json") and n.count("/") == 1 for n in names):
                raise WorkspaceError("zip 內容不對：要是「匯出專案」產生的檔案（第一層是專案資料夾，裡面有 project.json）")
            src_id = tops.pop()
            target = self.project_dir(project_id or src_id)
            if target.exists():
                raise WorkspaceError(f"專案「{target.name}」已經存在，換一個代號匯入，或先把現有的改名／移走", 409)
            total = sum(i.file_size for i in zf.infolist())
            if total > MAX_IMPORT_BYTES:
                raise WorkspaceError("解壓後太大（超過 2GB）")
            staging = target.with_name(f".{target.name}.importing")
            if staging.exists():
                shutil.rmtree(staging)
            try:
                for n in names:
                    rel = Path(n.split("/", 1)[1])
                    if rel.is_absolute() or ".." in rel.parts:
                        raise WorkspaceError(f"zip 裡有不安全的路徑：{n}")
                    dest = staging / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(n) as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
                for sub in ("screenshots", *KINDS):
                    (staging / sub).mkdir(exist_ok=True)
                os.replace(staging, target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        return self.get_project(target.name)

    # ---- 舊資料搬進專案 ------------------------------------------------

    def import_legacy(self, project_id: str, map_path: Path | None, case_paths: list[Path]) -> dict:
        """把專案根目錄的 app_map_tree.json（＋它用到的截圖）、test_cases/*.json 複製進專案，原檔不動。"""
        d = self._existing_dir(project_id)
        report = {"screens": 0, "screenshots": 0, "missing_screenshots": [], "cases": []}
        if map_path:
            map_data = _read_json(map_path)
            if not isinstance(map_data.get("screens"), list):
                raise WorkspaceError(f"{map_path} 不是地圖（找不到 screens 陣列）")
            base = map_path.resolve().parent
            for s in map_data["screens"]:
                shot = s.get("screenshot")
                if not shot:
                    continue
                src = (base / shot).resolve()
                if not src.is_file():
                    report["missing_screenshots"].append(shot)
                    continue
                rel = Path("screenshots") / src.name
                (d / "screenshots").mkdir(exist_ok=True)
                shutil.copy2(src, d / rel)
                s["screenshot"] = rel.as_posix()
                report["screenshots"] += 1
            project = self.get_project(project_id)
            map_data.setdefault("app", {}).setdefault("platform", project["platform"])
            _write_json_atomic(d / "map.json", map_data)
            report["screens"] = len(map_data["screens"])
        for p in case_paths:
            data = _read_json(p)
            self.write_item(project_id, "cases", p.stem, data, overwrite=True)
            report["cases"].append(p.stem)
        return report


def _main():
    parser = argparse.ArgumentParser(description="管理 workspace/ 底下的專案")
    parser.add_argument("--workspace", default=str(DEFAULT_ROOT), help="workspace 資料夾（預設 ./workspace）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出所有專案")
    c = sub.add_parser("create", help="建立新專案")
    c.add_argument("project_id", help="專案代號，例如 mbroker-demo-ios")
    c.add_argument("--platform", choices=PLATFORMS, required=True)
    c.add_argument("--name", default="", help="顯示名稱，例如「樹精靈 Demo iOS」")
    c.add_argument("--bundle_id", default="", help="iOS bundle id／Android package")
    m = sub.add_parser("import-legacy", help="把舊的 app_map_tree.json、test_cases/ 複製進專案")
    m.add_argument("project_id")
    m.add_argument("--map", type=Path, default=None, help="地圖 JSON，例如 app_map_tree.json")
    m.add_argument("--cases", type=Path, nargs="*", default=[], help="測案 JSON，例如 test_cases/*.json")
    args = parser.parse_args()

    ws = Workspace(args.workspace)
    try:
        if args.cmd == "list":
            projects = ws.list_projects()
            if not projects:
                print("還沒有任何專案，用 create 建立")
            for p in projects:
                print(f"{p['id']:<28} {p['platform']:<8} {p['name']}  {p.get('bundle_id', '')}")
        elif args.cmd == "create":
            p = ws.create_project(args.project_id, args.name, args.platform, args.bundle_id)
            print(f"已建立專案 {p['id']}：{ws.project_dir(p['id'])}")
        elif args.cmd == "import-legacy":
            r = ws.import_legacy(args.project_id, args.map, args.cases)
            print(f"地圖：{r['screens']} 個畫面、複製 {r['screenshots']} 張截圖；測案：{len(r['cases'])} 份")
            if r["missing_screenshots"]:
                print(f"  [警告] 找不到 {len(r['missing_screenshots'])} 張截圖，這些畫面在網頁上不會有圖：")
                for s in r["missing_screenshots"]:
                    print(f"    {s}")
    except WorkspaceError as e:
        sys.exit(f"錯誤：{e}")


if __name__ == "__main__":
    _main()
