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
