"""
清理 screenshots/、crops/ 裡已經沒有任何地圖在用的圖片

為什麼會累積：
  - screenshots/：檔名帶掃描時間，重新掃描同一個畫面後，舊截圖就沒人用了
  - crops/：檔名是用元件指紋算的，指紋包含元件上的文字，報價/委託這類文字會變
    的畫面每掃一次就產生一批新檔名。而且沒有任何程式讀這些小圖（viewer 是用
    整張截圖＋座標畫框），只是給人工核對 LLM 描述用，capture_screen_map.py
    現在預設也不再產生（要的話加 --save_crops）

怎麼判斷「還在用」：
  在專案根目錄的 .json / .html（地圖檔、viewer 內嵌的地圖）和 test_cases/ 裡
  搜尋 "screenshots/xxx.png"、"crops/xxx.png" 這種路徑，有出現就算還在用，
  不管是哪種格式的地圖。匯出到專案外面的地圖這裡看不到，用 --refs 一起帶進來，
  不然那些地圖用到的截圖會被列成可刪除。
  特別注意 app_map_viewer.html 裡正在編輯的地圖只存在頁面記憶體裡，還沒按
  「匯出目前地圖」就不在任何檔案中，它用的截圖（viewer 掃描的畫面）會被判成
  沒人用——清理前先匯出，再用 --refs 帶進來。

使用方式：
  1. 先看會刪哪些（預設只列出，不刪任何檔案）：
       uv run python cleanup_images.py
       uv run python cleanup_images.py --refs ~/Downloads/app_map_export.json
  2. 確認沒問題再加 --delete 真的刪除（直接刪除，不會進垃圾桶）：
       uv run python cleanup_images.py --delete
  3. crops/ 就算還被地圖引用也沒有程式會讀，確定不需要人工核對的話可以整批清掉：
       uv run python cleanup_images.py --all_crops --delete
  4. workspace 專案：加 --project，清的是專案資料夾裡的 screenshots/、crops/，
     引用從專案的 maps/（所有地圖）、backups/（爬蟲覆蓋前的備份）、cases/、batches/ 找；
     網頁開著專案時地圖會自動存檔，不用先匯出：
       uv run python cleanup_images.py --project foo-demo-ios
"""

import argparse
import re
from pathlib import Path

from workspace import DEFAULT_ROOT, Workspace, WorkspaceError

PROJECT_DIR = Path(__file__).resolve().parent
IMAGE_DIRS = ("screenshots", "crops")
REF_PATTERN = re.compile(r"(screenshots|crops)/([^\"'\s\\?#]+\.png)")


def reference_files(base: Path, extra: list[str]) -> list[Path]:
    files = sorted(base.glob("*.json")) + sorted(base.glob("*.html"))
    for sub in ("test_cases", "maps", "backups", "cases", "batches"):
        files += sorted((base / sub).glob("*.json"))
    for item in extra:
        path = Path(item).expanduser()
        if path.is_dir():
            files += sorted(p for p in path.rglob("*") if p.suffix in (".json", ".html"))
        elif path.is_file():
            files.append(path)
        else:
            raise SystemExit(f"--refs 找不到：{item}")
    return files


def collect_references(files: list[Path]) -> dict[str, set[str]]:
    refs = {d: set() for d in IMAGE_DIRS}
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for folder, name in REF_PATTERN.findall(text):
            refs[folder].add(name)
    return refs


def human_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{num_bytes} B"
        num_bytes /= 1024


def main():
    parser = argparse.ArgumentParser(description="清理沒有地圖在用的 screenshots/、crops/ 圖片")
    parser.add_argument("--refs", nargs="*", default=[],
                        help="專案外還在用的地圖 JSON（檔案或資料夾），裡面用到的圖片不會被刪")
    parser.add_argument("--all_crops", action="store_true",
                        help="crops/ 不管有沒有被引用都列為可刪除（沒有程式讀這些小圖）")
    parser.add_argument("--delete", action="store_true",
                        help="真的刪除（預設只列出清單）")
    parser.add_argument("--list", action="store_true", help="列出每個要刪除的檔名")
    parser.add_argument("--project", default=None, help="清 workspace 這個專案資料夾裡的圖片")
    parser.add_argument("--workspace", default=str(DEFAULT_ROOT), help="workspace 資料夾（預設 ./workspace）")
    args = parser.parse_args()

    base = PROJECT_DIR
    if args.project:
        try:
            ws = Workspace(args.workspace)
            ws.get_project(args.project)  # 確認專案存在（舊專案的 map.json 也會在這裡搬成 maps/main.json）
            base = ws.project_dir(args.project)
        except WorkspaceError as e:
            raise SystemExit(f"錯誤：{e}") from None
    files = reference_files(base, args.refs)
    refs = collect_references(files)
    print(f"檢查了 {len(files)} 個檔案裡的圖片引用")

    to_delete: list[Path] = []
    for folder in IMAGE_DIRS:
        folder_path = base / folder
        images = sorted(folder_path.glob("*.png")) if folder_path.is_dir() else []
        if folder == "crops" and args.all_crops:
            unused = images
        else:
            unused = [p for p in images if p.name not in refs[folder]]
        size = sum(p.stat().st_size for p in unused)
        print(f"  {folder}/：共 {len(images)} 張，可刪除 {len(unused)} 張（{human_size(size)}）")
        if args.list:
            for p in unused:
                print(f"    {folder}/{p.name}")
        to_delete += unused

    if not to_delete:
        print("沒有需要清理的圖片")
        return
    if not args.delete:
        print("目前只是列出，沒有刪除任何檔案；確認沒問題後加 --delete 執行刪除"
              "（--list 可以看完整檔名）")
        return

    for p in to_delete:
        p.unlink()
    print(f"已刪除 {len(to_delete)} 張圖片")


if __name__ == "__main__":
    main()
