"""
專案資料夾（workspace）：每支 App 的每個平台一個專案，地圖、截圖、測案、批次設定
都放在同一個資料夾，網頁（viewer_server.py）跟 CLI（crawl_app_tree.py）讀寫同一份。

    workspace/                      ← 整個不進 git（地圖、截圖含帳號金額等畫面資料）
    └── projects/
        └── mbroker-demo-ios/
            ├── project.json        ← 名稱、平台、bundle_id
            ├── maps/<名稱>.json    ← 地圖（crawl_app_tree.py 的全名格式），依功能分開爬可以有很多張，預設 main
            ├── backups/            ← 爬蟲覆蓋地圖前的備份 <地圖>-<時間>.json
            ├── screenshots/        ← 所有地圖共用，地圖裡的 "screenshots/xxx.png" 相對於專案資料夾
            ├── cases/<名稱>.json   ← 測試案例（不綁地圖）
            └── batches/<名稱>.json ← 批次設定，地圖、測案都用名稱引用

交付給不同團隊時，各自只拿自己 App 的專案（匯出成 zip，對方匯入），工具本身是同一份。

CLI：
    uv run python workspace.py list
    uv run python workspace.py create mbroker-demo-ios --platform ios --name "樹精靈 Demo iOS" \\
        --bundle_id com.cathay.securities.tw.mBroker.Demo
    # 改名稱、bundle id（平台不能改）
    uv run python workspace.py update mbroker-demo-ios --bundle_id com.example.app
    # 把舊的 app_map_tree.json＋用到的截圖＋test_cases/ 搬進專案（不會刪原檔），地圖存成 main
    uv run python workspace.py import-legacy mbroker-demo-ios --map app_map_tree.json --cases test_cases/*.json
    # 地圖存成別的名稱：--map_name trading

早期的專案只有一份 map.json，第一次讀到時會自動搬成 maps/main.json。
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
# 地圖／測案／批次的名稱就是檔名：允許中文，但不能有路徑分隔字元、不能以 . 開頭
ITEM_NAME_RE = re.compile(r"^\w[\w .()（）-]{0,99}$")
DEFAULT_MAP = "main"
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
        self._migrate_single_map(d)
        return d

    @staticmethod
    def _migrate_single_map(d: Path) -> None:
        """早期一個專案只有一份 map.json（含匯入的舊 zip），搬成 maps/main.json；備份搬進 backups/。"""
        legacy = d / "map.json"
        if legacy.is_file() and not (d / "maps").exists():
            (d / "maps").mkdir()
            os.replace(legacy, d / "maps" / f"{DEFAULT_MAP}.json")
        for old in d.glob("map.backup-*.json"):
            (d / "backups").mkdir(exist_ok=True)
            os.replace(old, d / "backups" / old.name.replace("map.backup-", f"{DEFAULT_MAP}-"))

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
        _write_json_atomic(d / "maps" / f"{DEFAULT_MAP}.json", empty_map(project))
        for sub in ("screenshots", *KINDS):
            (d / sub).mkdir(exist_ok=True)
        return {"id": project_id, **project}

    def update_project(self, project_id: str, name: str | None = None, bundle_id: str | None = None) -> dict:
        """改名稱、bundle_id。平台不能改：地圖是照那個平台掃出來的，換平台就不能用了。"""
        d = self._existing_dir(project_id)
        project = _read_json(d / "project.json")
        if name is not None:
            if not name.strip():
                raise WorkspaceError("名稱不能是空的")
            project["name"] = name.strip()
        if bundle_id is not None:
            project["bundle_id"] = bundle_id.strip()
        _write_json_atomic(d / "project.json", project)
        return {"id": project_id, **project}

    def summary(self, project_id: str) -> dict:
        """網頁開專案時一次拿齊：專案資訊、地圖／測案／批次清單（地圖內容另外用 read_map 拿）。"""
        return {"project": self.get_project(project_id), "maps": self.list_maps(project_id),
                "cases": self.list_items(project_id, "cases"),
                "batches": self.list_items(project_id, "batches")}

    # ---- 地圖（一個專案可以有很多張，依功能分開爬）---------------------

    def map_path(self, project_id: str, name: str = DEFAULT_MAP) -> Path:
        """地圖檔路徑（不管存不存在，給爬蟲寫新地圖用）。"""
        if not ITEM_NAME_RE.match(name or ""):
            raise WorkspaceError(f"地圖名稱「{name}」不合法：不能有 / \\ 等符號、不能以 . 開頭，最多 100 字")
        return self._existing_dir(project_id) / "maps" / f"{name}.json"

    def _existing_map(self, project_id: str, name: str) -> Path:
        path = self.map_path(project_id, name)
        if not path.is_file():
            raise WorkspaceError(f"找不到地圖「{name}」", 404)
        return path

    def list_maps(self, project_id: str) -> list[dict]:
        d = self._existing_dir(project_id) / "maps"
        out = []
        for p in sorted(d.glob("*.json")) if d.is_dir() else []:
            if p.name.startswith("."):
                continue
            entry = {"name": p.stem, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))}
            try:
                data = _read_json(p)
                entry["screens"] = len(data.get("screens") or [])
                entry["root_name"] = (data.get("app") or {}).get("root_name") or ""
            except (json.JSONDecodeError, OSError, AttributeError):
                entry["error"] = "檔案不是合法的地圖 JSON"
            out.append(entry)
        return out

    def read_map(self, project_id: str, name: str = DEFAULT_MAP) -> tuple[dict, str]:
        path = self._existing_map(project_id, name)
        return _read_json(path), _version(path)

    @staticmethod
    def _check_map(map_data) -> None:
        if not isinstance(map_data, dict) or not isinstance(map_data.get("screens"), list):
            raise WorkspaceError("地圖格式不對：找不到 screens 陣列")

    def create_map(self, project_id: str, name: str, map_data: dict | None = None) -> str:
        """新增一張地圖（map_data 沒給就是空白地圖）；同名已存在回 409，不覆蓋。"""
        path = self.map_path(project_id, name)
        if path.exists():
            raise WorkspaceError(f"地圖「{name}」已經存在", 409)
        project = self.get_project(project_id)
        if map_data is None:
            map_data = empty_map(project)
        self._check_map(map_data)
        platform = (map_data.get("app") or {}).get("platform")
        if platform and platform != project["platform"]:
            raise WorkspaceError(f"這張地圖是 {platform}，專案是 {project['platform']}，不能放進這個專案")
        map_data.setdefault("app", {}).setdefault("platform", project["platform"])
        _write_json_atomic(path, map_data)
        return _version(path)

    def write_map(self, project_id: str, name: str, map_data: dict, base_version: str | None) -> str:
        """
        base_version 是寫入方上次讀到的版本：跟目前檔案不一樣，代表中間有別人
        （另一個分頁、爬蟲）改過，直接拒絕，不默默蓋掉對方的修改。
        """
        self._check_map(map_data)
        path = self._existing_map(project_id, name)
        if base_version != _version(path):
            raise WorkspaceError(f"地圖「{name}」已經被其他地方更新過（另一個分頁或爬蟲），請重新載入後再改", 409)
        _write_json_atomic(path, map_data)
        return _version(path)

    def rename_map(self, project_id: str, name: str, new_name: str) -> None:
        """改名，專案裡存的批次設定引用到這張地圖的也一起改，不然下次開批次會找不到地圖。"""
        src = self._existing_map(project_id, name)
        dst = self.map_path(project_id, new_name)
        if dst.exists():
            raise WorkspaceError(f"地圖「{new_name}」已經存在", 409)
        os.replace(src, dst)
        for batch_path in (src.parent.parent / "batches").glob("*.json"):
            try:
                batch = _read_json(batch_path)
            except (json.JSONDecodeError, OSError):
                continue
            groups = batch.get("groups") if isinstance(batch, dict) else None
            changed = False
            for g in groups if isinstance(groups, list) else []:
                if isinstance(g, dict) and g.get("map_ref") == name:
                    g["map_ref"] = new_name
                    changed = True
            if changed:
                _write_json_atomic(batch_path, batch)

    def delete_map(self, project_id: str, name: str) -> None:
        path = self._existing_map(project_id, name)
        if len(self.list_maps(project_id)) <= 1:
            raise WorkspaceError("這是專案最後一張地圖，不能刪除")
        path.unlink()

    def backup_map(self, project_id: str, name: str) -> Path | None:
        """覆蓋地圖前備份到 backups/<地圖>-<時間>.json；地圖不存在或是空的就不用備份。"""
        path = self.map_path(project_id, name)
        if not path.is_file() or not _read_json(path).get("screens"):
            return None
        backup = path.parent.parent / "backups" / f"{name}-{time.strftime('%Y%m%d_%H%M%S')}.json"
        backup.parent.mkdir(exist_ok=True)
        shutil.copy2(path, backup)
        return backup

    # 單張地圖的 zip：maps/<名稱>.json ＋ 它用到的 screenshots/xxx.png（跟專案資料夾同樣的結構），
    # 兩個人互相更新某一張地圖的成果用，不用整個專案匯出匯入
    def export_map_zip(self, project_id: str, name: str) -> bytes:
        map_data, _ = self.read_map(project_id, name)
        d = self._existing_dir(project_id)
        shots = set()
        for s in map_data.get("screens") or []:
            rel = s.get("screenshot") if isinstance(s, dict) else None
            if isinstance(rel, str) and rel.startswith("screenshots/") and (d / rel).is_file():
                shots.add(rel)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(self.map_path(project_id, name), f"maps/{name}.json")
            for rel in sorted(shots):
                zf.write(d / rel, rel)
        return buf.getvalue()

    def import_map_zip(self, project_id: str, name: str, data: bytes, overwrite: bool = False) -> dict:
        """
        匯入 export_map_zip 產生的 zip，存成專案裡的地圖 name：截圖本機沒有的補進來，已經有的不動
        （檔名含毫秒時間，不同人拍的不會撞名）。同名地圖已存在：overwrite 才取代（先備份），不然回 409。
        """
        if len(data) > MAX_IMPORT_BYTES:
            raise WorkspaceError("檔案太大（超過 2GB）")
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise WorkspaceError("這不是 zip 檔")
        with zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            maps = [n for n in names if n.startswith("maps/") and n.endswith(".json") and n.count("/") == 1]
            if len(maps) != 1:
                raise WorkspaceError("zip 內容不對：要是「匯出目前地圖」產生的檔案（裡面有一個 maps/<名稱>.json）")
            try:
                map_data = json.loads(zf.read(maps[0]))
            except json.JSONDecodeError:
                raise WorkspaceError(f"zip 裡的 {maps[0]} 不是合法 JSON")
            self._check_map(map_data)
            path = self.map_path(project_id, name)
            backup = None
            if path.exists():
                if not overwrite:
                    raise WorkspaceError(f"地圖「{name}」已經存在", 409)
                project = self.get_project(project_id)
                platform = (map_data.get("app") or {}).get("platform")
                if platform and platform != project["platform"]:
                    raise WorkspaceError(f"這張地圖是 {platform}，專案是 {project['platform']}，不能放進這個專案")
                backup = self.backup_map(project_id, name)
                _write_json_atomic(path, map_data)
            else:
                self.create_map(project_id, name, map_data)
            shots_dir = self._existing_dir(project_id) / "screenshots"
            added = 0
            for n in names:
                folder, _, fname = n.partition("/")
                if folder != "screenshots" or not fname or "/" in fname or fname.startswith(".") or ".." in fname:
                    continue
                dest = shots_dir / fname
                if dest.exists():
                    continue
                with zf.open(n) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                added += 1
        return {"screenshots": added, "backup": str(backup) if backup else None,
                "maps": self.list_maps(project_id)}

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

    @staticmethod
    def _open_export_zip(data: bytes) -> tuple[zipfile.ZipFile, list[str], str]:
        """檢查是不是「匯出專案」產生的 zip，回傳 (zip, 檔案清單, zip 裡的專案代號)；呼叫端負責關掉 zip。"""
        if len(data) > MAX_IMPORT_BYTES:
            raise WorkspaceError("檔案太大（超過 2GB）")
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise WorkspaceError("這不是 zip 檔")
        names = [n for n in zf.namelist() if not n.endswith("/")]
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) != 1 or any("/" not in n for n in names) or not any(n.endswith("/project.json") and n.count("/") == 1 for n in names):
            zf.close()
            raise WorkspaceError("zip 內容不對：要是「匯出專案」產生的檔案（第一層是專案資料夾，裡面有 project.json）")
        if sum(i.file_size for i in zf.infolist()) > MAX_IMPORT_BYTES:
            zf.close()
            raise WorkspaceError("解壓後太大（超過 2GB）")
        return zf, names, tops.pop()

    def import_zip(self, data: bytes, project_id: str | None = None) -> dict:
        """
        zip 裡第一層是原本的專案代號；project_id 有給就改用這個代號匯入
        （例如同一份專案想留一份對照）。已經存在的專案不會被覆蓋（要併進去用 merge_zip）。
        """
        zf, names, src_id = self._open_export_zip(data)
        with zf:
            target = self.project_dir(project_id or src_id)
            if target.exists():
                raise WorkspaceError(f"專案「{target.name}」已經存在，換一個代號匯入，或先把現有的改名／移走", 409)
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
                self._migrate_single_map(staging)
                os.replace(staging, target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        return self.get_project(target.name)

    def merge_zip(self, data: bytes, resolutions: dict | None = None) -> dict:
        """
        把別人匯出的 zip 併進本機同代號的專案（兩個人分頭編不同地圖、互相同步用）：
        - 截圖：本機沒有的補進來（檔名含毫秒時間，不同人拍的不會撞名），已經有的不動
        - 地圖／測案／批次：本機沒有的直接加；同名但內容一樣的略過；同名內容不同的是「衝突」
        - 專案設定（project.json）、backups/ 用本機的，不動
        resolutions 是 None 就只預覽、不寫入；要寫入時每個衝突都要給
        {"maps/main": "mine" | "theirs" | "both"}：保留本機／用匯入的（地圖會先備份）／
        兩個都留（匯入的改名成「名稱 (匯入)」）。
        """
        zf, names, project_id = self._open_export_zip(data)
        with zf:
            mine = self.get_project(project_id)
            theirs = json.loads(zf.read(f"{project_id}/project.json"))
            if theirs.get("platform") != mine["platform"]:
                raise WorkspaceError(f"匯入的專案是 {theirs.get('platform')}，本機的「{project_id}」是 {mine['platform']}，不能合併")
            d = self._existing_dir(project_id)

            items, shots = [], []
            for n in names:
                rel = n.split("/", 1)[1]
                folder, _, fname = rel.partition("/")
                if not fname or "/" in fname or fname.startswith(".") or ".." in fname:
                    continue
                if folder == "screenshots":
                    if not (d / "screenshots" / fname).exists():
                        shots.append((n, fname))
                elif folder in ("maps", *KINDS) and fname.endswith(".json"):
                    name = fname[:-5]
                    if not ITEM_NAME_RE.match(name):
                        continue
                    content = zf.read(n)
                    try:
                        item = json.loads(content)
                    except json.JSONDecodeError:
                        raise WorkspaceError(f"zip 裡的 {rel} 不是合法 JSON")
                    local = d / folder / fname
                    if not local.exists():
                        status = "new"
                    elif local.read_bytes() == content or _read_json(local) == item:
                        status = "same"
                    else:
                        status = "conflict"
                    items.append({"kind": folder, "name": name, "status": status, "data": item})

            conflicts = [{"kind": i["kind"], "name": i["name"]} for i in items if i["status"] == "conflict"]
            report = {
                "project": mine,
                "new": [{"kind": i["kind"], "name": i["name"]} for i in items if i["status"] == "new"],
                "conflicts": conflicts,
                "screenshots": len(shots),
            }
            if resolutions is None:
                return report

            missing = [f"{c['kind']}/{c['name']}" for c in conflicts if resolutions.get(f"{c['kind']}/{c['name']}") not in ("mine", "theirs", "both")]
            if missing:
                raise WorkspaceError(f"這些同名的項目還沒選要怎麼處理：{'、'.join(missing)}")
            # 寫入前先全部檢查過，不要寫到一半才發現某一份格式不對
            for i in items:
                if i["kind"] == "maps":
                    self._check_map(i["data"])
                elif not (isinstance(i["data"], dict) and isinstance(i["data"].get("steps" if i["kind"] == "cases" else "groups"), list)):
                    raise WorkspaceError(f"zip 裡的 {i['kind']}/{i['name']}.json 格式不對")

            # 「兩個都留」的地圖改了名，匯入的批次裡引用到它的也要跟著改，不然會指到本機那張
            renames = {}
            for i in items:
                if i["status"] == "conflict" and resolutions[f"{i['kind']}/{i['name']}"] == "both":
                    new_name = self._free_name(d / i["kind"], i["name"])
                    renames[(i["kind"], i["name"])] = new_name
            for i in items:
                if i["kind"] == "batches":
                    for g in i["data"].get("groups") or []:
                        if isinstance(g, dict) and ("maps", g.get("map_ref")) in renames:
                            g["map_ref"] = renames[("maps", g["map_ref"])]

            added, replaced, renamed, backups = [], [], [], []
            for i in items:
                key = (i["kind"], i["name"])
                if i["status"] == "same" or (i["status"] == "conflict" and resolutions[f"{key[0]}/{key[1]}"] == "mine"):
                    continue
                name = renames.get(key, i["name"])
                if i["kind"] == "maps":
                    if i["status"] == "conflict" and key not in renames:
                        backup = self.backup_map(project_id, name)
                        if backup:
                            backups.append(str(backup))
                    _write_json_atomic(d / "maps" / f"{name}.json", i["data"])
                else:
                    self.write_item(project_id, i["kind"], name, i["data"], overwrite=True)
                entry = {"kind": i["kind"], "name": i["name"]}
                if key in renames:
                    renamed.append({**entry, "as": name})
                elif i["status"] == "conflict":
                    replaced.append(entry)
                else:
                    added.append(entry)
            for n, fname in shots:
                dest = d / "screenshots" / fname
                with zf.open(n) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
        return {"project": mine, "added": added, "replaced": replaced, "renamed": renamed,
                "screenshots": len(shots), "backups": backups}

    @staticmethod
    def _free_name(folder: Path, name: str) -> str:
        """「兩個都留」時匯入那份的新名稱：名稱 (匯入)、名稱 (匯入 2)…，不跟資料夾裡現有的撞名。"""
        for n in range(1, 1000):
            candidate = f"{name} (匯入{'' if n == 1 else f' {n}'})"
            if not (folder / f"{candidate}.json").exists():
                return candidate
        raise WorkspaceError(f"「{name}」的匯入複本太多了，先清掉一些")

    # ---- 舊資料搬進專案 ------------------------------------------------

    def import_legacy(self, project_id: str, map_path: Path | None, case_paths: list[Path],
                      map_name: str = DEFAULT_MAP) -> dict:
        """
        把專案根目錄的 app_map_tree.json（＋它用到的截圖）、test_cases/*.json 複製進專案，原檔不動。
        地圖存成 maps/<map_name>.json；同名地圖已經有畫面的話先備份再覆蓋。
        """
        d = self._existing_dir(project_id)
        self.map_path(project_id, map_name)  # 先檢查名稱，不合法就不要複製任何東西
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
            report["backup"] = self.backup_map(project_id, map_name)
            _write_json_atomic(self.map_path(project_id, map_name), map_data)
            report["screens"] = len(map_data["screens"])
        for p in case_paths:
            data = _read_json(p)
            self.write_item(project_id, "cases", p.stem, data, overwrite=True)
            report["cases"].append(p.stem)
        return report


def _ask_create(ws: Workspace) -> tuple[str, str, str, str]:
    """create 沒帶專案代號時，一步一步問。回傳 (project_id, name, platform, bundle_id)。"""
    print("建立新專案（直接按 Enter 用括號裡的預設值，Ctrl+C 取消）\n")
    while True:
        project_id = input("1. 專案代號（小寫英數字、- 和 _，例如 mbroker-demo-ios）：").strip()
        try:
            if ws.project_dir(project_id).exists():
                print(f"   專案「{project_id}」已經存在，換一個")
                continue
            break
        except WorkspaceError as e:
            print(f"   {e}")
    while True:
        choice = input("2. 平台  1) ios  2) android：").strip().lower()
        platform = {"1": "ios", "2": "android"}.get(choice, choice)
        if platform in PLATFORMS:
            break
        print("   請輸入 1 或 2")
    name = input(f"3. 顯示名稱（{project_id}）：").strip() or project_id
    label = "bundle id" if platform == "ios" else "package"
    bundle_id = input(f"4. {label}（可以先空白，之後用 update 補）：").strip()

    print(f"\n  專案代號：{project_id}\n  平台：{platform}\n  顯示名稱：{name}\n  {label}：{bundle_id or '（空白）'}")
    if input("\n確定建立？(Y/n)：").strip().lower() not in ("", "y", "yes"):
        sys.exit("已取消")
    return project_id, name, platform, bundle_id


def _main():
    parser = argparse.ArgumentParser(description="管理 workspace/ 底下的專案")
    parser.add_argument("--workspace", default=str(DEFAULT_ROOT), help="workspace 資料夾（預設 ./workspace）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出所有專案")
    c = sub.add_parser("create", help="建立新專案（不帶參數就一步一步問）")
    c.add_argument("project_id", nargs="?", help="專案代號，例如 mbroker-demo-ios")
    c.add_argument("--platform", choices=PLATFORMS)
    c.add_argument("--name", default="", help="顯示名稱，例如「樹精靈 Demo iOS」")
    c.add_argument("--bundle_id", default="", help="iOS bundle id／Android package")
    u = sub.add_parser("update", help="改專案名稱、bundle id（平台不能改：地圖是照那個平台掃出來的）")
    u.add_argument("project_id")
    u.add_argument("--name", default=None, help="新的顯示名稱")
    u.add_argument("--bundle_id", default=None, help="新的 bundle id／package，給空字串 \"\" 就是清掉")
    m = sub.add_parser("import-legacy", help="把舊的 app_map_tree.json、test_cases/ 複製進專案")
    m.add_argument("project_id")
    m.add_argument("--map", type=Path, default=None, help="地圖 JSON，例如 app_map_tree.json")
    m.add_argument("--cases", type=Path, nargs="*", default=[], help="測案 JSON，例如 test_cases/*.json")
    m.add_argument("--map_name", default=DEFAULT_MAP, help=f"地圖存成什麼名稱（預設 {DEFAULT_MAP}）")
    args = parser.parse_args()

    ws = Workspace(args.workspace)
    try:
        if args.cmd == "list":
            projects = ws.list_projects()
            if not projects:
                print("還沒有任何專案，用 create 建立")
            for p in projects:
                maps = "、".join(f"{m['name']}({m.get('screens', '?')})" for m in ws.list_maps(p["id"]))
                print(f"{p['id']:<28} {p['platform']:<8} {p['name']}  {p.get('bundle_id', '')}  地圖：{maps}")
        elif args.cmd == "create":
            if args.project_id is None:
                p = ws.create_project(*_ask_create(ws))
            elif args.platform is None:
                sys.exit("錯誤：要加 --platform ios 或 --platform android（或不帶參數，一步一步問）")
            else:
                p = ws.create_project(args.project_id, args.name, args.platform, args.bundle_id)
            print(f"已建立專案 {p['id']}：{ws.project_dir(p['id'])}")
        elif args.cmd == "update":
            if args.name is None and args.bundle_id is None:
                sys.exit("沒有要改的東西：加 --name 或 --bundle_id")
            p = ws.update_project(args.project_id, args.name, args.bundle_id)
            print(f"已更新 {p['id']}：名稱「{p['name']}」、bundle id「{p['bundle_id']}」、平台 {p['platform']}")
        elif args.cmd == "import-legacy":
            r = ws.import_legacy(args.project_id, args.map, args.cases, args.map_name)
            print(f"地圖：{r['screens']} 個畫面、複製 {r['screenshots']} 張截圖；測案：{len(r['cases'])} 份")
            if r.get("backup"):
                print(f"  原本的地圖「{args.map_name}」已備份到 {r['backup']}")
            if r["missing_screenshots"]:
                print(f"  [警告] 找不到 {len(r['missing_screenshots'])} 張截圖，這些畫面在網頁上不會有圖：")
                for s in r["missing_screenshots"]:
                    print(f"    {s}")
    except WorkspaceError as e:
        sys.exit(f"錯誤：{e}")
    except (KeyboardInterrupt, EOFError):
        sys.exit("\n已取消")


if __name__ == "__main__":
    _main()
