"""aitest start 沒帶專案、也沒帶 --platform 時的選單（cli.pick_project）"""

import pytest

from aitest import cli
from aitest.core.workspace import Workspace


@pytest.fixture
def workspace(tmp_path):
    ws = Workspace(tmp_path / "workspace")
    ws.create_project("b-ios", "B iOS", "ios")
    ws.create_project("z-android", "Z Android", "android")
    ws.create_project("a-android", "A Android", "android")
    return str(tmp_path / "workspace")


def _answers(monkeypatch, *answers):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))


def test_enter_picks_first_android_project(monkeypatch, workspace):
    _answers(monkeypatch, "")
    assert cli.pick_project(workspace) == ("a-android", "android")


def test_android_listed_before_ios(monkeypatch, workspace):
    _answers(monkeypatch, "3")
    assert cli.pick_project(workspace) == ("b-ios", "ios")


def test_invalid_input_asks_again(monkeypatch, workspace):
    _answers(monkeypatch, "9", "abc", "2")
    assert cli.pick_project(workspace) == ("z-android", "android")


def test_zero_picks_platform_only(monkeypatch, workspace):
    _answers(monkeypatch, "0", "")
    assert cli.pick_project(workspace) == (None, "android")
    _answers(monkeypatch, "0", "0", "2")
    assert cli.pick_project(workspace) == (None, "ios")


def test_no_projects_goes_to_platform_menu(monkeypatch, tmp_path):
    _answers(monkeypatch, "2")
    assert cli.pick_project(str(tmp_path / "empty")) == (None, "ios")


def test_ctrl_c_exits(monkeypatch, workspace):
    def interrupt(prompt=""):
        raise KeyboardInterrupt
    monkeypatch.setattr("builtins.input", interrupt)
    with pytest.raises(SystemExit):
        cli.pick_project(workspace)
