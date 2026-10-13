"""core/recorder.py：錄製建樹的換頁判斷、合併、離線建立（不接手機，用假的 driver 換 page_source）"""

import io
import threading

import pytest
from PIL import Image

from aitest.core import recorder
from aitest.core.capture_screen_map import build_screen_data, parse_candidate_elements
from aitest.core.recorder import Recording, RecordingError, merge_rounds
from aitest.platforms import get_platform
from aitest.run_context import RunContext

from conftest import IOS_SCREEN


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (39, 84), (20, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


class FakeDriver:
    """page_source 是目前「手機」停的畫面，測試裡直接換掉它代表換頁"""

    def __init__(self, page_source: str):
        self.page_source = page_source

    def get_window_size(self):
        return {"width": IOS_SCREEN[0], "height": IOS_SCREEN[1]}

    def get_screenshot_as_png(self):
        return _png()


@pytest.fixture
def ios():
    return get_platform("ios")


@pytest.fixture
def rec(ios, tmp_path, ios_login, monkeypatch):
    """從 login 畫面開始的錄製；點擊一律成功，點了之後畫面換成 driver.next_source"""
    driver = FakeDriver(ios_login)

    def fake_tap(ctx, platform, d, el):
        d.tapped = el
        if getattr(d, "next_source", None) is not None:
            d.page_source = d.next_source
        return "PASS（測試）"

    monkeypatch.setattr(recorder, "run_tap", fake_tap)
    monkeypatch.setattr(recorder, "wait_after_action", lambda platform, d: None)
    r = Recording(ios, tmp_path, "proj", "main", "root_uid", "login")
    r.start(driver)
    r.driver = driver
    return r


def _index_of(rec: Recording, label: str) -> int:
    return next(i for i, c in enumerate(rec.live_candidates) if c["label"] == label)


def _a_label(rec: Recording) -> str:
    """畫面上一個有文字、而且文字只出現一次的元件"""
    labels = [c["label"] for c in rec.live_candidates if c["label"]]
    return next(label for label in labels if labels.count(label) == 1)


def test_merge_rounds_same_snapshot_twice_is_one_round(ios, ios_login):
    once = merge_rounds(ios, [(ios_login, *IOS_SCREEN)])
    twice = merge_rounds(ios, [(ios_login, *IOS_SCREEN), (ios_login, *IOS_SCREEN)])
    assert len(once) == len(twice) == len(parse_candidate_elements(ios, ios_login, *IOS_SCREEN))
    assert all(el["_round_index"] == 0 for el in twice)


def test_start_records_start_screen(rec):
    state = rec.state()
    assert state["current"] == 0 and state["steps"] == []
    assert state["live"]["candidates"]


def test_tap_to_new_screen_then_back(rec, ios, ios_policy, ios_login, monkeypatch):
    label = _a_label(rec)
    rec.driver.next_source = ios_policy
    rec.tap(RunContext(), rec.driver, _index_of(rec, label))
    state = rec.state()
    assert state["current"] == 1
    assert [(s["n"], s["parent"], s["entry_text"]) for s in state["steps"]] == [(1, 0, label)]

    # 重新抓畫面、畫面沒變：不建節點
    rec.refresh(rec.driver)
    assert rec.state()["current"] == 1 and len(rec.state()["steps"]) == 1

    # 返回到起點：認得是錄過的畫面
    def fake_back(d):
        d.page_source = ios_login
    monkeypatch.setattr(ios, "go_back", fake_back)
    message = rec.back(rec.driver)
    assert rec.current == 0 and "回到" in message


def test_tap_same_screen_is_not_a_new_node(rec):
    rec.driver.next_source = None
    message = rec.tap(RunContext(), rec.driver, _index_of(rec, _a_label(rec)))
    assert "沒有換頁" in message
    assert rec.state()["steps"] == [] and rec.current == 0


def test_force_new_creates_node_even_if_signature_same(rec):
    rec.driver.next_source = None
    rec.tap(RunContext(), rec.driver, _index_of(rec, _a_label(rec)), force_new=True)
    assert len(rec.state()["steps"]) == 1


def test_unknown_screen_after_refresh(rec, ios_policy):
    rec.driver.page_source = ios_policy  # 使用者自己在手機上換了頁
    rec.refresh(rec.driver)
    assert rec.current is None and rec.state()["live"] is None
    with pytest.raises(RecordingError):
        rec.tap(RunContext(), rec.driver, 0)


def test_merge_last_folds_step_into_parent(rec, ios_policy):
    rec.driver.next_source = ios_policy
    rec.tap(RunContext(), rec.driver, _index_of(rec, _a_label(rec)))
    rec.merge_last()
    assert rec.state()["steps"] == [] and rec.current == 0
    # 之後再看到同樣的畫面，當成起點畫面（同一頁），不會再建節點
    rec.refresh(rec.driver)
    assert rec.current == 0


def test_undo_removes_files_and_needs_refresh(rec, ios_policy):
    rec.driver.next_source = ios_policy
    rec.tap(RunContext(), rec.driver, _index_of(rec, _a_label(rec)))
    files_before = len(list(rec.dir.iterdir()))
    rec.undo()
    assert len(list(rec.dir.iterdir())) == files_before - 2
    assert rec.current is None and rec.state()["steps"] == []


def test_build_matches_scan_and_finds_entry_uid(rec, ios, ios_policy, ios_login, tmp_path):
    label = _a_label(rec)
    rec.driver.next_source = ios_policy
    rec.tap(RunContext(), rec.driver, _index_of(rec, label))

    out = tmp_path / "out"
    result = rec.build(RunContext(), out, {1: "政策總覽"}, set())
    [screen] = result["screens"]
    assert screen["parent"] == 0 and result["warnings"] == []

    # 入口 uid 就是起點畫面用掃描同一套規則算出來、文字是 label 的那個元件
    start = build_screen_data(RunContext(), ios, "login", parse_candidate_elements(ios, ios_login, *IOS_SCREEN),
                              [_png()], _png(), *IOS_SCREEN, out_dir=tmp_path / "start")
    assert screen["entry_uid"] in {el["unique_id"] for el in start["elements"] if el["label"] == label}

    # 建出來的元件跟直接掃描那個畫面一樣
    scanned = build_screen_data(RunContext(), ios, "政策總覽", parse_candidate_elements(ios, ios_policy, *IOS_SCREEN),
                                [_png()], _png(), *IOS_SCREEN, out_dir=tmp_path / "scan")
    assert screen["screen_data"]["elements"] == scanned["elements"]
    assert (out / screen["screen_data"]["screenshot"]).is_file()


def test_build_reparents_under_deleted_step(rec, ios_policy, ios_login):
    rec.driver.next_source = ios_policy
    rec.tap(RunContext(), rec.driver, _index_of(rec, _a_label(rec)))
    rec.driver.next_source = ios_login
    rec.tap(RunContext(), rec.driver, 0, force_new=True)
    assert [s["parent"] for s in rec.state()["steps"]] == [0, 1]

    result = rec.build(RunContext(), rec.dir.parent / "out", {2: "第二步"}, {1})
    [screen] = result["screens"]
    assert screen["n"] == 2 and screen["parent"] == 0
    assert any("上一步被刪掉" in w for w in result["warnings"])


def test_build_rejects_missing_or_duplicate_names(rec, ios_policy, ios_login):
    rec.driver.next_source = ios_policy
    rec.tap(RunContext(), rec.driver, _index_of(rec, _a_label(rec)))
    rec.driver.next_source = ios_login
    rec.tap(RunContext(), rec.driver, 0, force_new=True)
    with pytest.raises(RecordingError):
        rec.build(RunContext(), rec.dir.parent / "out", {1: "a"}, set())
    with pytest.raises(RecordingError):
        rec.build(RunContext(), rec.dir.parent / "out", {1: "a", 2: "a"}, set())


def test_entry_found_after_scroll(rec, ios, ios_login, ios_policy, monkeypatch):
    """捲動後才出現的元件當入口：建立時要在起點畫面合併後的元件裡找得到"""
    def fake_scroll(d, w, h, direction):
        d.page_source = ios_policy
    monkeypatch.setattr(ios, "scroll_screen", fake_scroll)
    monkeypatch.setattr(recorder.time, "sleep", lambda s: None)
    rec.swipe(rec.driver, "down")
    assert rec.live == (0, 1)
    login_labels = {c["label"] for c in merge_rounds(ios, [(ios_login, *IOS_SCREEN)])}
    label = next(c["label"] for c in rec.live_candidates
                 if c["label"] and c["label"] not in login_labels
                 and [x["label"] for x in rec.live_candidates].count(c["label"]) == 1)
    rec.driver.next_source = ios_login
    rec.tap(RunContext(), rec.driver, _index_of(rec, label), force_new=True)

    result = rec.build(RunContext(), rec.dir.parent / "out", {1: "下一頁"}, set())
    [screen] = result["screens"]
    start = merge_rounds(ios, [(ios_login, *IOS_SCREEN), (ios_policy, *IOS_SCREEN)])
    recorder.assign_unique_ids("login", start)
    assert screen["entry_uid"] == next(el["_unique_id"] for el in start if el["label"] == label)


def test_server_commit_then_describe_in_background(rec, ios_policy, monkeypatch, tmp_path):
    """/record/commit 勾了描述：先回傳建好的節點，背景補完描述後刪掉快照，伺服器可以開始下一次錄製"""
    from aitest import viewer_server

    class FakeWorkspace:
        def project_dir(self, project_id):
            return tmp_path / "project"

    rec.driver.next_source = ios_policy
    rec.tap(RunContext(), rec.driver, _index_of(rec, _a_label(rec)))
    monkeypatch.setattr(viewer_server, "WORKSPACE", FakeWorkspace())
    monkeypatch.setattr(viewer_server, "_recording", rec)
    monkeypatch.setattr(Recording, "describe", lambda self, ctx, n, name: {f"{name}::fake": "假描述"})

    result = viewer_server.record_commit({"names": {"1": "政策總覽"}, "describe": True})
    assert result["describing"] is True and [s["name"] for s in result["screens"]] == ["政策總覽"]
    assert viewer_server.recording_summary(viewer_server._recording) == {"active": False}

    for _ in range(100):
        job = viewer_server.describe_progress()
        if not job["running"]:
            break
        threading.Event().wait(0.02)
    assert job["done"] == job["total"] == 1
    assert job["results"] == [{"n": 1, "name": "政策總覽", "descriptions": {"政策總覽::fake": "假描述"}}]
    assert not rec.dir.exists()


def test_scroll_back_to_seen_position_reuses_round(rec, ios, ios_login, ios_policy, monkeypatch):
    """下滑後又上滑回頂部：跟所有輪比對，捲回看過的位置不多存一輪"""
    def fake_scroll(d, w, h, direction):
        d.page_source = ios_policy if direction == "down" else ios_login
    monkeypatch.setattr(ios, "scroll_screen", fake_scroll)
    monkeypatch.setattr(recorder.time, "sleep", lambda s: None)

    rec.swipe(rec.driver, "down")
    assert rec.live == (0, 1) and len(rec.screens[0]["rounds"]) == 2
    message = rec.swipe(rec.driver, "up")
    assert "捲回" in message
    assert rec.live == (0, 0) and len(rec.screens[0]["rounds"]) == 2
    message = rec.swipe(rec.driver, "up")
    assert "沒有變化" in message and rec.live == (0, 0)
    # 多出來的快照檔有刪掉：只剩兩輪各一組 xml＋png
    assert len(list(rec.dir.iterdir())) == 4


MULTI_WINDOW = """<hierarchy rotation="0" width="1080" height="2400">
  <android.widget.FrameLayout package="com.android.systemui" class="android.widget.FrameLayout" bounds="[0,0][1080,80]">
    <android.widget.TextView package="com.android.systemui" class="android.widget.TextView" text="2:47" bounds="[20,10][120,70]" />
  </android.widget.FrameLayout>
  <android.widget.FrameLayout package="com.demo.app" class="android.widget.FrameLayout" bounds="[0,80][1080,2400]">
    <android.widget.TextView package="com.demo.app" class="android.widget.TextView" text="自選" resource-id="com.demo.app:id/tab" clickable="true" bounds="[40,200][200,280]" />
  </android.widget.FrameLayout>
  <android.widget.FrameLayout package="com.demo.app" class="android.widget.FrameLayout" bounds="[0,400][1080,1000]">
    <android.widget.TextView package="com.demo.app" class="android.widget.TextView" text="行動自選3" clickable="true" bounds="[40,500][500,580]" />
  </android.widget.FrameLayout>
</hierarchy>"""


def test_app_windows_only_keeps_app_popup_drops_status_bar():
    from aitest.platforms.android import app_windows_only
    filtered = app_windows_only(MULTI_WINDOW, "com.demo.app")
    assert "2:47" not in filtered and "行動自選3" in filtered and "自選" in filtered
    labels = [c["label"] for c in get_platform("android").parse_candidates_raw(filtered, 1080, 2400)]
    assert "行動自選3" in labels
    # 不知道 package、或沒有屬於這支 App 的視窗：原封不動
    assert app_windows_only(MULTI_WINDOW, None) == MULTI_WINDOW
    assert app_windows_only(MULTI_WINDOW, "com.other") == MULTI_WINDOW


class FakeAndroidDriver(FakeDriver):
    current_package = "com.demo.app"
    current_activity = ".Main"

    def __init__(self, page_source):
        super().__init__(page_source)
        self.settings = {"enableMultiWindows": False}

    def get_window_size(self):
        return {"width": 1080, "height": 2400}

    def get_settings(self):
        return dict(self.settings)

    def update_settings(self, values):
        self.settings.update(values)


def test_android_recording_turns_on_multi_windows_and_restores(tmp_path, monkeypatch):
    android = get_platform("android")
    driver = FakeAndroidDriver(MULTI_WINDOW)
    r = Recording(android, tmp_path, "proj", "main", "root_uid", "行情")
    r.start(driver)
    assert driver.settings["enableMultiWindows"] is True
    assert [c["label"] for c in r.live_candidates if c["label"]] == ["自選", "行動自選3"]  # 沒有狀態列的時間
    # 定位不到（浮動層裡的元件）：改用錄製畫面上的座標點
    monkeypatch.setattr(recorder, "run_tap", lambda ctx, platform, d, el: "FAIL（找不到元件）")
    monkeypatch.setattr(recorder, "wait_after_action", lambda platform, d: None)
    tapped = []
    monkeypatch.setattr(android, "tap_by_coordinates", lambda d, bounds: tapped.append(bounds))
    message = r.tap(RunContext(), driver, _index_of(r, "行動自選3"))
    assert tapped == [{"x": 40, "y": 500, "width": 460, "height": 80}] and "座標點擊" in message
    r.finish(driver)
    assert driver.settings["enableMultiWindows"] is False
