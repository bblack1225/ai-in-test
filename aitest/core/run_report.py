"""
批次執行報告：把同一次批次執行留下的執行紀錄（workspace.batch_runs）輸出成一個 HTML 檔，
截圖用 base64 內嵌，不用開伺服器就能看，可以直接寄給別人或丟到共用資料夾。
內容是真實 App 畫面（帳號、金額），交出去前要留意。
"""

import base64
from html import escape
from pathlib import Path
from typing import Callable

from aitest.core.workspace import WorkspaceError

STATUS_LABEL = {"pass": "通過", "fail": "失敗", "skip": "略過"}

_CSS = """
:root { --bg:#fff; --ink:#1f2328; --soft:#656d76; --line:#d0d7de; --card:#f6f8fa;
        --pass:#1a7f37; --fail:#cf222e; --skip:#9a6700; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0d1117; --ink:#e6edf3; --soft:#9198a1; --line:#30363d; --card:#161b22;
          --pass:#3fb950; --fail:#f85149; --skip:#d29922; }
}
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px; background:var(--bg); color:var(--ink);
       font:14px/1.6 -apple-system, "PingFang TC", "Microsoft JhengHei", sans-serif; }
main { max-width: 960px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
.meta, .note { color: var(--soft); }
.note { font-size: 12px; margin: 4px 0 16px; }
.summary { display:flex; gap:12px; margin:16px 0; flex-wrap:wrap; }
.summary div { background:var(--card); border:1px solid var(--line); border-radius:8px;
               padding:8px 16px; min-width:96px; }
.summary b { display:block; font-size:22px; }
.pass { color: var(--pass); } .fail { color: var(--fail); } .skip { color: var(--skip); }
details { border:1px solid var(--line); border-radius:8px; margin:8px 0; background:var(--card); }
summary { cursor:pointer; padding:8px 12px; display:flex; gap:12px; flex-wrap:wrap; align-items:baseline; }
summary .name { font-weight:600; flex:1; min-width:160px; }
.body { padding: 0 12px 12px; }
.step { display:grid; grid-template-columns: 32px 1fr; gap:4px 8px; padding:6px 0; border-top:1px solid var(--line); }
.step .out { grid-column: 2; word-break: break-word; }
.step img { grid-column: 2; max-width: 280px; border:1px solid var(--line); border-radius:6px; }
ol { margin: 4px 0; padding-left: 20px; }
"""


def _status(status: str | None) -> str:
    return f'<span class="{escape(status or "")}">{escape(STATUS_LABEL.get(status, status or "—"))}</span>'


def _outcome_class(outcome: str) -> str:
    return "pass" if outcome.startswith("PASS") else "fail" if outcome.startswith("FAIL") else "skip"


def _image(file_of: Callable[[str], Path], rel: str) -> str:
    try:
        data = file_of(rel).read_bytes()
    except (OSError, WorkspaceError):
        return ""
    return f'<img alt="失敗當下的截圖" src="data:image/png;base64,{base64.b64encode(data).decode()}">'


def _record_html(r: dict, file_of: Callable[[str], Path], open_: bool = False) -> str:
    """open_：預設展開（單筆報告用）；批次報告只預設展開沒通過的"""
    snapshot = r.get("case_snapshot") or {}
    results = r.get("results") or []
    passed = sum(str(x.get("outcome", "")).startswith("PASS") for x in results)
    total = len(snapshot.get("steps") or results)
    meta = [f"{passed}/{total} 步通過"]
    if r.get("duration") is not None:
        meta.append(f"{r['duration']} 秒")
    if r.get("login"):
        meta.append(f"登入：{escape(r['login'])}")
    if r.get("vars"):
        meta.append(f"變數：{escape('、'.join(r['vars']))}")
    parts = [f'<details{" open" if open_ or r.get("status") != "pass" else ""}><summary>{_status(r.get("status"))}'
             f'<span class="name">{escape(snapshot.get("test_name") or r.get("case") or "")}</span>'
             f'<span class="meta">{"・".join(meta)}</span></summary><div class="body">']
    if r.get("reason"):
        parts.append(f'<p class="fail">{escape(r["reason"])}</p>')
    if r.get("navigation"):
        parts.append("<b>登入／回到起始畫面</b><ol>" +
                     "".join(f"<li>{escape(str(line))}</li>" for line in r["navigation"]) + "</ol>")
    if r.get("precheck"):
        parts.append(f"<p><b>第0步：</b>{escape(str(r['precheck'].get('message', '')))}</p>")
    for x in results:
        outcome = str(x.get("outcome", ""))
        label = " ".join(str(v) for v in (x.get("action"), x.get("screen_name"), x.get("note")) if v)
        parts.append(f'<div class="step"><span>{escape(str(x.get("index", "")))}</span><span>{escape(label)}</span>'
                     f'<span class="out {_outcome_class(outcome)}">{escape(outcome)}</span>'
                     f'{_image(file_of, x["screenshot"]) if x.get("screenshot") else ""}</div>')
    parts.append("</div></details>")
    return "".join(parts)


def _page(title: str, meta: list[str], note: str, body: str) -> str:
    return (f'<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(title)}</title><style>{_CSS}</style></head><body><main>"
            f"<h1>{escape(title)}</h1><div class=\"meta\">{'・'.join(m for m in meta if m)}</div>"
            f'<div class="note">{note}內含 App 實際畫面，分享前請確認對象。</div>{body}</main></body></html>')


def _project_meta(project: dict, record: dict) -> list[str]:
    meta = [escape(project.get("name") or project.get("id") or ""), escape(record.get("platform") or "")]
    if (record.get("source") or {}).get("app_id"):
        meta.append(f"App：{escape(record['source']['app_id'])}")
    return meta


def render_batch_report(project: dict, records: list[dict], file_of: Callable[[str], Path]) -> str:
    """records 是同一次批次執行的紀錄（依開始時間排）；file_of 把紀錄裡的截圖相對路徑換成實際檔案"""
    source = records[0].get("source") or {}
    title = f"{source.get('batch_name') or '批次執行'}・{records[0].get('started_at', '')}"
    counts = {k: sum(r.get("status") == k for r in records) for k in STATUS_LABEL}
    seconds = sum(r.get("duration") or 0 for r in records)
    meta = _project_meta(project, records[0])
    if any(r.get("stopped") for r in records):
        meta.append("中途手動停止")
    meta.append(f"共 {len(records)} 份測案・{round(seconds)} 秒")
    groups: dict[str, list[dict]] = {}
    for r in records:
        groups.setdefault((r.get("source") or {}).get("map_name") or "", []).append(r)
    body = "".join(
        (f"<h2>{escape(map_name)}</h2>" if map_name else "") + "".join(_record_html(r, file_of) for r in rs)
        for map_name, rs in groups.items())
    summary = "".join(f'<div class="{k}"><b>{counts[k]}</b>{label}</div>' for k, label in STATUS_LABEL.items())
    return _page(title, meta, "只含用專案測案加入批次的結果。", f'<div class="summary">{summary}</div>{body}')


def render_run_report(project: dict, record: dict, file_of: Callable[[str], Path]) -> str:
    """一份測案的一次執行（單份執行，或批次裡的其中一份）"""
    name = (record.get("case_snapshot") or {}).get("test_name") or record.get("case") or ""
    source = record.get("source") or {}
    meta = _project_meta(project, record)
    if source.get("kind") == "batch":
        meta.append(f"批次「{escape(source.get('batch_name') or '未存檔的批次')}」")
    else:
        meta.append("單份執行")
    if source.get("map_name"):
        meta.append(f"地圖：{escape(source['map_name'])}")
    return _page(f"{name}・{record.get('started_at', '')}", meta, "", _record_html(record, file_of, open_=True))
