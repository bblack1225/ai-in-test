"""viewer_server 的測案／批次執行：每次執行一份 RunContext，進度、變數不會被別次執行蓋掉"""

import threading

import pytest

from aitest import viewer_server
from aitest.platforms import get_platform


class FakeDriver:
    page_source = "<hierarchy />"

    def get_settings(self):
        return {}

    def update_settings(self, values):
        pass

    def get_screenshot_as_png(self):
        return b"png"


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(viewer_server, "PLATFORM", get_platform("android"))
    monkeypatch.setattr(viewer_server, "get_driver", lambda *a, **k: FakeDriver())
    monkeypatch.setattr(viewer_server, "_latest_runs", {"test": None, "batch": None})
    return viewer_server


# target 在地圖裡找不到：這一步直接 FAIL，不會碰到手機
STEPS = [{"action": "assert_visible", "screen_name": "home", "target": "home::missing"},
         {"action": "assert_visible", "screen_name": "home", "target": "home::missing2"}]


def test_run_test_script_progress(server):
    result = server.run_test_script({"element_map": {"screens": [{"screen_name": "home", "elements": []}]},
                                     "test_case": {"steps": STEPS}, "check_screen": False})
    assert [r["outcome"][:4] for r in result["results"]] == ["FAIL", "FAIL"]
    progress = server._progress_snapshot("test")
    assert progress["running"] is False
    assert progress["total"] == 2
    assert [r["index"] for r in progress["results"]] == [1, 2]
    assert progress["usage"]["calls"] == 0


def test_progress_while_running(server, monkeypatch):
    """執行中輪詢：看得到目前第幾步、已完成的結果"""
    reached_step_2, release = threading.Event(), threading.Event()
    original = server.run_step_in_case

    def slow_step(ctx, platform, driver, steps, i, el, banner_outcomes, screen_elements=()):
        if i == 2:
            reached_step_2.set()
            release.wait(5)
        return original(ctx, platform, driver, steps, i, el, banner_outcomes, screen_elements)

    monkeypatch.setattr(server, "run_step_in_case", slow_step)
    worker = threading.Thread(target=server.run_test_script, args=({
        "element_map": {"screens": []}, "test_case": {"steps": STEPS}, "check_screen": False},))
    worker.start()
    assert reached_step_2.wait(5)
    progress = server._progress_snapshot("test")
    release.set()
    worker.join(5)
    assert progress["running"] is True and progress["current"] == 2
    assert len(progress["results"]) == 1


def test_batch_current_not_overwritten_by_steps(server, monkeypatch):
    """批次的 current 是「第幾組第幾份」，執行步驟時不能被改成步驟編號（原本共用全域進度時會）"""
    seen = []
    original = server._run_steps

    def spy(ctx, *args, **kwargs):
        original(ctx, *args, **kwargs)
        seen.append(ctx.progress_snapshot()["current"])

    monkeypatch.setattr(server, "_run_steps", spy)
    result = server.run_batch({
        "app_id": "app",
        "groups": [{"map_name": "m", "map": {"screens": []},
                    "cases": [{"name": "c1", "case": {"steps": STEPS}}]}],
        "options": {"auto_navigate": False},
    })
    assert seen == [{"group": 0, "case": 0}]
    assert result["groups"][0]["cases"][0]["status"] in ("fail", "skip")
    assert server._progress_snapshot("batch")["running"] is False


@pytest.fixture
def ws(server, monkeypatch, tmp_path):
    from aitest.core.workspace import Workspace
    w = Workspace(tmp_path / "workspace")
    w.create_project("demo", "Demo", "android")
    w.write_item("demo", "cases", "登入", {"test_name": "登入", "steps": STEPS}, overwrite=False)
    monkeypatch.setattr(server, "WORKSPACE", w)
    return w


def test_single_run_saved_for_project_case(server, ws):
    """選的是專案測案：留一筆紀錄，失敗的步驟有截圖"""
    result = server.run_test_script({"element_map": {"screens": []}, "test_case": {"steps": STEPS},
                                     "check_screen": False, "project": "demo", "case_ref": "登入"})
    assert result["run_saved"] is True
    [run] = ws.list_runs("demo", "登入")
    assert run["status"] == "fail" and run["source"] == {"kind": "single"}
    assert run["steps"] == {"pass": 0, "fail": 2, "skip": 0, "total": 2}
    full = ws.read_run("demo", "登入", run["id"])
    assert full["case_snapshot"]["steps"] == STEPS
    assert ws.run_file("demo", full["results"][0]["screenshot"]).read_bytes() == b"png"


def test_single_run_not_saved_without_case_ref(server, ws):
    result = server.run_test_script({"element_map": {"screens": []}, "test_case": {"steps": STEPS},
                                     "check_screen": False, "project": "demo"})
    assert result["run_saved"] is False
    assert ws.list_run_cases("demo") == []


def test_batch_saves_only_project_cases(server, ws):
    result = server.run_batch({
        "app_id": "app", "project": "demo", "batch_name": "每日",
        "groups": [{"map_name": "m", "map": {"screens": []},
                    "cases": [{"name": "登入", "ref": "登入", "case": {"steps": STEPS}},
                              {"name": "臨時", "case": {"steps": STEPS}}]}],
        "options": {"auto_navigate": False},
    })
    [entry] = ws.list_run_cases("demo")
    assert entry["case"] == "登入" and entry["recent"] == [result["groups"][0]["cases"][0]["status"]]
    source = entry["last"]["source"]
    assert source["kind"] == "batch" and source["batch_name"] == "每日" and source["batch_id"] == result["batch_id"]
    [record] = ws.batch_runs("demo", result["batch_id"])
    assert record["case"] == "登入"
