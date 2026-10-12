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


class FakeMenu:
    """代替方向鍵選單：依序回傳預先決定好的選擇，並記下每次顯示的選項"""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.shown = []

    def __call__(self, message, titles):
        self.shown.append(titles)
        return self.answers.pop(0)


def test_android_projects_listed_first(workspace):
    menu = FakeMenu(0)
    assert cli.pick_project(workspace, menu) == ("a-android", "android")
    assert [t.split()[0] for t in menu.shown[0]] == ["a-android", "z-android", "b-ios", "不開專案，只選平台"]


def test_pick_ios_project(workspace):
    assert cli.pick_project(workspace, FakeMenu(2)) == ("b-ios", "ios")


def test_platform_only(workspace):
    menu = FakeMenu(3, 0)
    assert cli.pick_project(workspace, menu) == (None, "android")
    assert menu.shown[1] == ["android", "ios"]
    assert cli.pick_project(workspace, FakeMenu(3, 1)) == (None, "ios")


def test_no_projects_goes_to_platform_menu(tmp_path):
    menu = FakeMenu(1)
    assert cli.pick_project(str(tmp_path / "empty"), menu) == (None, "ios")
    assert menu.shown == [["android", "ios"]]


@pytest.mark.parametrize("answers", [(None,), (3, None)])
def test_ctrl_c_exits(workspace, answers):
    with pytest.raises(SystemExit):
        cli.pick_project(workspace, FakeMenu(*answers))
