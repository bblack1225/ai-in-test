"""workspace.py：專案的建立、測案讀寫、匯出／匯入／合併 zip"""

import io
import json
import zipfile

import pytest

from aitest.core.workspace import Workspace, WorkspaceError

CASE = {"test_name": "登入", "steps": [{"action": "tap", "screen_name": "登入頁"}]}


@pytest.fixture
def ws(tmp_path):
    w = Workspace(tmp_path / "workspace")
    w.create_project("demo-ios", "Demo", "ios", "tw.example.demo")
    return w


def _zip_names(data: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return set(zf.namelist())


# ---- 建立專案、測案讀寫 ----------------------------------------------------

def test_create_project_layout(ws):
    d = ws.project_dir("demo-ios")
    assert ws.get_project("demo-ios")["platform"] == "ios"
    assert (d / "maps" / "main.json").is_file()
    for sub in ("screenshots", "cases", "batches", "vars"):
        assert (d / sub).is_dir()


def test_create_project_rejects_duplicate_and_bad_platform(ws):
    with pytest.raises(WorkspaceError) as e:
        ws.create_project("demo-ios", "Demo", "ios")
    assert e.value.status == 409
    with pytest.raises(WorkspaceError):
        ws.create_project("demo-x", "Demo", "windows")


def test_case_create_edit_delete(ws):
    ws.write_item("demo-ios", "cases", "登入", CASE, overwrite=False)
    with pytest.raises(WorkspaceError) as e:
        ws.write_item("demo-ios", "cases", "登入", CASE, overwrite=False)
    assert e.value.status == 409

    edited = {**CASE, "steps": CASE["steps"] * 2}
    ws.write_item("demo-ios", "cases", "登入", edited, overwrite=True)
    assert ws.read_item("demo-ios", "cases", "登入") == edited
    assert ws.list_items("demo-ios", "cases")[0]["steps"] == 2

    ws.delete_item("demo-ios", "cases", "登入")
    assert ws.list_items("demo-ios", "cases") == []


def test_item_validation(ws):
    with pytest.raises(WorkspaceError):
        ws.write_item("demo-ios", "cases", "壞的", {"no_steps": True}, overwrite=False)
    with pytest.raises(WorkspaceError):
        ws.write_item("demo-ios", "cases", "../escape", CASE, overwrite=False)
    with pytest.raises(WorkspaceError):
        ws.write_item("demo-ios", "vars", "帳號", {"values": {"1BAD": "x"}}, overwrite=False)


def test_rename_map_updates_batch_refs(ws):
    ws.create_map("demo-ios", "登入頁")
    batch = {"groups": [
        {"map_ref": "main", "setup_map_ref": "登入頁"},
        {"map_ref": "登入頁", "setup_map_ref": None},
        {"map_ref": "main", "setup_map_ref": "其他"},
    ]}
    ws.write_item("demo-ios", "batches", "回歸", batch, overwrite=False)

    ws.rename_map("demo-ios", "登入頁", "登入")

    groups = ws.read_item("demo-ios", "batches", "回歸")["groups"]
    assert groups == [
        {"map_ref": "main", "setup_map_ref": "登入"},
        {"map_ref": "登入", "setup_map_ref": None},
        {"map_ref": "main", "setup_map_ref": "其他"},
    ]


def _case_batch():
    return {"groups": [
        {"map_ref": "main", "setup_case": {"ref": "登入"}, "cases": [{"ref": "登入", "login": "帳號A"}, {"ref": "下單"}]},
        {"map_ref": "main", "setup_case": None, "cases": [{"name": "內嵌", "case": CASE}]},
    ]}


def test_rename_case_updates_test_name_and_batch_refs(ws):
    ws.write_item("demo-ios", "cases", "登入", CASE, overwrite=False)
    ws.write_item("demo-ios", "cases", "下單", CASE, overwrite=False)
    ws.write_item("demo-ios", "batches", "回歸", _case_batch(), overwrite=False)

    ws.rename_item("demo-ios", "cases", "登入", "登入_新版")

    assert ws.read_item("demo-ios", "cases", "登入_新版") == {**CASE, "test_name": "登入_新版"}
    with pytest.raises(WorkspaceError):
        ws.read_item("demo-ios", "cases", "登入")
    g0, g1 = ws.read_item("demo-ios", "batches", "回歸")["groups"]
    assert g0["setup_case"] == {"ref": "登入_新版"}
    assert g0["cases"] == [{"ref": "登入_新版", "login": "帳號A"}, {"ref": "下單"}]
    assert g1 == _case_batch()["groups"][1]

    with pytest.raises(WorkspaceError) as e:
        ws.rename_item("demo-ios", "cases", "登入_新版", "下單")
    assert e.value.status == 409


def test_delete_case_removes_batch_refs(ws):
    ws.write_item("demo-ios", "cases", "登入", CASE, overwrite=False)
    ws.write_item("demo-ios", "batches", "回歸", _case_batch(), overwrite=False)
    ws.write_item("demo-ios", "batches", "沒用到", {"groups": [{"map_ref": "main", "cases": [{"ref": "下單"}]}]}, overwrite=False)

    assert ws.batches_using_case("demo-ios", "登入") == ["回歸"]
    ws.delete_item("demo-ios", "cases", "登入")

    g0, g1 = ws.read_item("demo-ios", "batches", "回歸")["groups"]
    assert g0["setup_case"] is None
    assert g0["cases"] == [{"ref": "下單"}]
    assert g1 == _case_batch()["groups"][1]
    assert ws.batches_using_case("demo-ios", "下單") == ["回歸", "沒用到"]


# ---- 匯出 ----------------------------------------------------------------

def test_export_skips_sensitive_vars(ws):
    ws.write_item("demo-ios", "vars", "帳號A", {"sensitive": True, "values": {"LOGIN_ID": "A123"}}, overwrite=False)
    ws.write_item("demo-ios", "vars", "股票", {"sensitive": False, "values": {"STOCK_CODE": "2330"}}, overwrite=False)
    names = _zip_names(ws.export_zip("demo-ios"))
    assert "demo-ios/vars/股票.json" in names
    assert "demo-ios/vars/帳號A.json" not in names
    assert "demo-ios/project.json" in names


# ---- 匯入 ----------------------------------------------------------------

def test_import_as_new_project(ws):
    ws.write_item("demo-ios", "cases", "登入", CASE, overwrite=False)
    data = ws.export_zip("demo-ios")

    project = ws.import_zip(data, "demo-ios-copy")
    assert project["id"] == "demo-ios-copy"
    assert ws.read_item("demo-ios-copy", "cases", "登入") == CASE


def test_import_existing_project_refused(ws):
    with pytest.raises(WorkspaceError) as e:
        ws.import_zip(ws.export_zip("demo-ios"))
    assert e.value.status == 409


def test_import_rejects_non_export_zip(ws):
    with pytest.raises(WorkspaceError):
        ws.import_zip(b"not a zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("random.txt", "hi")
    with pytest.raises(WorkspaceError):
        ws.import_zip(buf.getvalue())


def test_import_rejects_unsafe_path(ws):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("evil/project.json", json.dumps({"name": "x", "platform": "ios"}))
        zf.writestr("evil/../../escape.txt", "x")
    with pytest.raises(WorkspaceError):
        ws.import_zip(buf.getvalue())
    assert not ws.project_dir("evil").exists()


# ---- 合併 ----------------------------------------------------------------

@pytest.fixture
def theirs_zip(ws, tmp_path):
    """另一個人從同一個專案分出去、改過之後匯出的 zip"""
    ws.write_item("demo-ios", "cases", "共同", CASE, overwrite=False)
    other = Workspace(tmp_path / "other")
    other.import_zip(ws.export_zip("demo-ios"))
    other.write_item("demo-ios", "cases", "共同", {**CASE, "test_name": "他們改的"}, overwrite=True)
    other.write_item("demo-ios", "cases", "新的", CASE, overwrite=False)
    return other.export_zip("demo-ios")


def test_merge_preview_does_not_write(ws, theirs_zip):
    report = ws.merge_zip(theirs_zip)
    assert {"kind": "cases", "name": "新的"} in report["new"]
    assert report["conflicts"] == [{"kind": "cases", "name": "共同"}]
    assert [i["name"] for i in ws.list_items("demo-ios", "cases")] == ["共同"]


def test_merge_requires_resolution_for_every_conflict(ws, theirs_zip):
    with pytest.raises(WorkspaceError):
        ws.merge_zip(theirs_zip, {})


@pytest.mark.parametrize("choice, expected_test_name", [("mine", "登入"), ("theirs", "他們改的")])
def test_merge_mine_or_theirs(ws, theirs_zip, choice, expected_test_name):
    ws.merge_zip(theirs_zip, {"cases/共同": choice})
    assert ws.read_item("demo-ios", "cases", "共同")["test_name"] == expected_test_name
    assert ws.read_item("demo-ios", "cases", "新的") == CASE


def test_merge_keep_both_renames_theirs(ws, theirs_zip):
    result = ws.merge_zip(theirs_zip, {"cases/共同": "both"})
    assert result["renamed"] == [{"kind": "cases", "name": "共同", "as": "共同 (匯入)"}]
    assert ws.read_item("demo-ios", "cases", "共同")["test_name"] == "登入"
    assert ws.read_item("demo-ios", "cases", "共同 (匯入)")["test_name"] == "他們改的"


def test_merge_keep_both_map_updates_imported_batch_refs(ws, tmp_path):
    ws.create_map("demo-ios", "登入頁")
    other = Workspace(tmp_path / "other")
    other.import_zip(ws.export_zip("demo-ios"))
    map_data, version = other.read_map("demo-ios", "登入頁")
    other.write_map("demo-ios", "登入頁", {**map_data, "screens": [{"name": "他們加的"}]}, version)
    other.write_item("demo-ios", "batches", "他們的", {"groups": [
        {"map_ref": "main", "setup_map_ref": "登入頁"},
        {"map_ref": "登入頁", "setup_map_ref": "登入頁"},
        {"map_ref": "main", "setup_map_ref": None},
    ]}, overwrite=False)

    ws.merge_zip(other.export_zip("demo-ios"), {"maps/登入頁": "both"})

    groups = ws.read_item("demo-ios", "batches", "他們的")["groups"]
    assert groups == [
        {"map_ref": "main", "setup_map_ref": "登入頁 (匯入)"},
        {"map_ref": "登入頁 (匯入)", "setup_map_ref": "登入頁 (匯入)"},
        {"map_ref": "main", "setup_map_ref": None},
    ]


def test_merge_refuses_other_platform(ws, tmp_path):
    other = Workspace(tmp_path / "other")
    other.create_project("demo-ios", "Demo", "android")
    with pytest.raises(WorkspaceError):
        ws.merge_zip(other.export_zip("demo-ios"))


# ---- 執行紀錄 ----------------------------------------------------------

def _record(status="fail", batch_id=None, ts=1760000000.0):
    return {"started_ts": ts, "started_at": "2026-10-16 14:30:00", "status": status,
            "source": {"kind": "batch", "batch_id": batch_id} if batch_id else {"kind": "single"},
            "results": [{"index": 1, "outcome": "FAIL（找不到元件）"}], "case_snapshot": CASE}


def test_runs_list_and_delete(ws):
    ws.write_item("demo-ios", "cases", "登入", CASE, overwrite=False)
    first = ws.save_run("demo-ios", "登入", _record("pass"), {})
    second = ws.save_run("demo-ios", "登入", _record("fail"), {1: b"png"})
    assert first != second  # 同一毫秒也不會撞名
    [entry] = ws.list_run_cases("demo-ios")
    assert entry["count"] == 2 and entry["exists"] is True
    assert [r["id"] for r in ws.list_runs("demo-ios", "登入")] == [second, first]
    shot = ws.read_run("demo-ios", "登入", second)["results"][0]["screenshot"]
    assert ws.run_file("demo-ios", shot).is_file()

    ws.delete_runs("demo-ios", "登入", [second])
    assert not ws.run_file("demo-ios", shot).exists()
    assert [r["id"] for r in ws.list_runs("demo-ios", "登入")] == [first]
    ws.delete_case_runs("demo-ios", ["登入"])
    assert ws.list_run_cases("demo-ios") == []


def test_runs_follow_case_rename_and_delete(ws):
    ws.write_item("demo-ios", "cases", "登入", CASE, overwrite=False)
    run_id = ws.save_run("demo-ios", "登入", _record(), {1: b"png"})
    ws.rename_item("demo-ios", "cases", "登入", "登入2")
    shot = ws.read_run("demo-ios", "登入2", run_id)["results"][0]["screenshot"]
    assert shot.startswith("runs/登入2/") and ws.run_file("demo-ios", shot).is_file()
    ws.delete_item("demo-ios", "cases", "登入2")
    assert ws.list_run_cases("demo-ios") == []


def test_runs_rejects_bad_names(ws):
    with pytest.raises(WorkspaceError):
        ws.read_run("demo-ios", "登入", "../project")
    with pytest.raises(WorkspaceError):
        ws.run_file("demo-ios", "../project.json")


def test_export_zip_skips_runs(ws):
    ws.save_run("demo-ios", "登入", _record(), {1: b"png"})
    names = zipfile.ZipFile(io.BytesIO(ws.export_zip("demo-ios"))).namelist()
    assert not any("/runs/" in n for n in names)


def test_batch_report_html(ws):
    from aitest.core.run_report import render_batch_report
    ws.save_run("demo-ios", "登入", _record("fail", "b1", 1.0), {1: b"png"})
    ws.save_run("demo-ios", "其他", _record("pass", "b2", 2.0), {})
    records = ws.batch_runs("demo-ios", "b1")
    assert [r["case"] for r in records] == ["登入"]
    html = render_batch_report(ws.get_project("demo-ios"), records, lambda rel: ws.run_file("demo-ios", rel))
    assert "data:image/png;base64," in html and "找不到元件" in html


def test_run_report_html(ws):
    """單筆報告：單份執行的紀錄也能匯出，通過的也預設展開"""
    from aitest.core.run_report import render_run_report
    run_id = ws.save_run("demo-ios", "登入", _record("pass"), {})
    html = render_run_report(ws.get_project("demo-ios"), ws.read_run("demo-ios", "登入", run_id),
                             lambda rel: ws.run_file("demo-ios", rel))
    assert "單份執行" in html and "<details open>" in html
