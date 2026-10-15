"""檢查要進 repo 的內容有沒有真實 App 識別資訊、帳號、金鑰。

要找的字不寫死在這裡（這個檔案會進 repo），而是從本機讀：
  - workspace/projects/*/project.json 的專案資料夾名稱、name、bundle_id
  - config.toml 的 UDID、Team ID、WDA bundle id、啟動 App、API key
另外用規則找 email、電話、Google API key。

用法（在 repo 根目錄）：
  python3 .claude/skills/release/scripts/check_sensitive.py              # 工作區（未 commit＋未追蹤）
  python3 .claude/skills/release/scripts/check_sensitive.py v2.3.1..HEAD # 某段 commit 範圍

只看「新增的行」；圖片等二進位檔案無法掃內容，會列出來請人工看。
找到東西時結束碼為 1。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, check=True).stdout.strip())

# 正常會出現在 repo 裡、不算外洩的 email
ALLOWED_EMAILS = {"noreply@anthropic.com"}
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "users.noreply.github.com")

PATTERNS = {
    "email": re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    "電話": re.compile(r"(?<!\d)(?:0800-?\d{3}-?\d{3}|0\d{1,2}-\d{3,4}-\d{4}|09\d{2}-?\d{3}-?\d{3})(?!\d)"),
    "Google API key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
}


def mask(value: str) -> str:
    return value if len(value) <= 6 else value[:3] + "…" + value[-2:]


def local_terms() -> list[tuple[str, str, bool]]:
    """(要找的字, 來源說明, 輸出時要不要遮)"""
    terms: list[tuple[str, str, bool]] = []
    for pj in sorted((ROOT / "workspace" / "projects").glob("*/project.json")):
        pid = pj.parent.name
        terms.append((pid, f"專案資料夾 {pid}", False))
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for key in ("name", "bundle_id"):
            if isinstance(data.get(key), str):
                terms.append((data[key], f"{pid}/project.json 的 {key}", False))

    cfg_path = ROOT / "config.toml"
    if cfg_path.exists():
        try:
            cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            cfg = {}
        for section, key, secret in [("ios", "udid", True), ("ios", "xcode_org_id", True),
                                     ("ios", "wda_bundle_id", False), ("ios", "launch_bundle_id", False),
                                     ("android", "udid", True), ("llm", "api_key", True)]:
            value = cfg.get(section, {}).get(key)
            if isinstance(value, str) and value and not value.startswith("emulator-"):
                terms.append((value, f"config.toml [{section}].{key}", secret))

    # 太短的字會到處誤判；同一個字只留一次
    seen: set[str] = set()
    result = []
    for value, src, secret in terms:
        value = value.strip()
        if len(value) >= 4 and value.lower() not in seen:
            seen.add(value.lower())
            result.append((value, src, secret))
    return result


def added_lines(rev_range: str | None) -> tuple[list[tuple[str, int, str]], list[str]]:
    """回傳 (新增的行 [(檔案, 行號, 內容)], 二進位檔案清單)"""
    diff_cmd = ["git", "diff", "--no-color", "--unified=0"]
    diff_cmd += [rev_range] if rev_range else ["HEAD"]
    diff = subprocess.run(diff_cmd, cwd=ROOT, capture_output=True, text=True, check=True).stdout

    lines: list[tuple[str, int, str]] = []
    binaries: list[str] = []
    current, lineno = "", 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            current = raw[6:] if raw.startswith("+++ b/") else ""
        elif raw.startswith("Binary files"):
            m = re.search(r"and b/(.+) differ", raw)
            if m:
                binaries.append(m.group(1))
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            lineno = int(m.group(1)) if m else 0
        elif raw.startswith("+") and current:
            lines.append((current, lineno, raw[1:]))
            lineno += 1

    if not rev_range:  # 未追蹤的新檔案 git diff 看不到，另外讀
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                                   cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
        for rel in untracked:
            try:
                text = (ROOT / rel).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                binaries.append(rel)
                continue
            lines += [(rel, i, line) for i, line in enumerate(text.splitlines(), 1)]
    return lines, binaries


def main() -> int:
    rev_range = sys.argv[1] if len(sys.argv) > 1 else None
    terms = local_terms()
    lines, binaries = added_lines(rev_range)
    this_file = str(Path(__file__).resolve().relative_to(ROOT))

    hits = []
    for path, lineno, text in lines:
        if path == this_file:
            continue
        lower = text.lower()
        for value, src, secret in terms:
            if value.lower() in lower:
                hits.append((path, lineno, src, mask(value) if secret else value))
        for label, pattern in PATTERNS.items():
            for m in pattern.finditer(text):
                found = m.group(0)
                if label == "email" and (found in ALLOWED_EMAILS or found.endswith(ALLOWED_EMAIL_DOMAINS)):
                    continue
                hits.append((path, lineno, label, mask(found) if label != "email" else found))

    scope = rev_range or "工作區（未 commit＋未追蹤）"
    print(f"檢查範圍：{scope}；新增 {len(lines)} 行；本機比對字詞 {len(terms)} 個")
    if not terms:
        print("⚠ 讀不到 workspace/projects 或 config.toml，只用通用規則檢查")
    for path, lineno, label, shown in hits:
        print(f"  ✗ {path}:{lineno}  [{label}] {shown}")
    if binaries:
        print("二進位檔案（無法掃內容，請人工看截圖裡有沒有真實 App 畫面、帳號、金額）：")
        for b in binaries:
            print(f"  ? {b}")
    if not hits:
        print("✓ 沒有找到敏感字詞")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
