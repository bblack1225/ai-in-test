"""
測試共用的 fixture。

page_source 樣本：
  - docs/reference_data/：真實 iOS App 掃描存下來的 XML（已去識別化）
  - tests/fixtures/：照真實結構手寫的假資料（目前是 Android）
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DATA = ROOT / "docs" / "reference_data"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

IOS_SCREEN = (390, 844)
ANDROID_SCREEN = (1080, 2400)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture
def ios_login() -> str:
    return _read(REFERENCE_DATA / "raw_accessibility_tree_login_screen.xml")


@pytest.fixture
def ios_login_rescanned() -> str:
    """同一個登入畫面隔一段時間重新掃描的版本（內容有差異）"""
    return _read(REFERENCE_DATA / "raw_accessibility_tree_login_screen_20260817.xml")


@pytest.fixture
def ios_policy() -> str:
    return _read(REFERENCE_DATA / "raw_accessibility_tree_foolife_policy_overview.xml")


@pytest.fixture
def android_watchlist() -> str:
    return _read(FIXTURES / "android_watchlist.xml")
