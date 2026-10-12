"""crawl_app_tree.crawl_screen：子畫面中止爬蟲（例如偵測到登出）時，例外不能被 finally 吞掉（報告 #1）"""

import pytest

from aitest.core import crawl_app_tree


class FakeAnalysis:
    def is_candidate(self, el):
        return True

    def text(self, el):
        return el["label"]

    def pattern(self, el):
        return ""


class FakePlatform:
    """點擊前在根畫面，點擊後到子畫面"""

    def __init__(self):
        self.tapped = False

    def dismiss_modal(self, driver, page_source=None):
        return []

    def skeleton(self, driver, page_source):
        return {}, "child" if self.tapped else "root"

    def chrome_keys(self, page_source, screen_height):
        return set()

    def analyze(self, page_source):
        return FakeAnalysis()


class FakeDriver:
    page_source = "<root />"


def _screen_data(name):
    return {
        "last_scanned": "", "screenshot": None, "screen_stable_at_scan": None,
        "screen_width": 1000, "screen_height": 2000,
        "elements": [{"unique_id": f"{name}::btn", "class": "Button", "label": "下一頁", "native_id": None,
                      "bounds": {"x": 0, "y": 1000, "width": 100, "height": 100}}],
    }


def test_abort_in_child_screen_propagates(monkeypatch):
    platform = FakePlatform()
    recovered = []

    def fake_scan(platform_, driver, name, **kwargs):
        if platform.tapped:
            raise crawl_app_tree.CrawlAborted("子畫面偵測到登出")
        return _screen_data(name)

    def fake_tap(platform_, driver, el, screen_height):
        platform.tapped = True
        return True

    def fake_recover(platform_, driver, target_uid, **kwargs):
        recovered.append(target_uid)
        return False  # 回不去：舊版 finally 裡的 break 會在這裡把例外吞掉

    monkeypatch.setattr(crawl_app_tree, "scan_screen", fake_scan)
    monkeypatch.setattr(crawl_app_tree, "tap_candidate", fake_tap)
    monkeypatch.setattr(crawl_app_tree, "recover_to", fake_recover)
    monkeypatch.setattr(crawl_app_tree, "wait_until_stable", lambda *a, **k: True)
    monkeypatch.setattr(crawl_app_tree, "went_outside_app", lambda *a, **k: None)
    monkeypatch.setattr(crawl_app_tree, "save_map", lambda *a, **k: None)

    cfg = {"max_screens": 10, "max_depth": 3, "describe": False, "include_nav": True, "safe_keywords": False,
           "dry_run": False, "sample_per_pattern": 3, "bundle_id": "app", "meta": {}}
    state = {"screens": {}, "edges": [], "skipped": [], "visit_order": []}

    with pytest.raises(crawl_app_tree.CrawlAborted):
        crawl_app_tree.crawl_screen(platform, FakeDriver(), "root", None, None, [], 0, cfg, state)
    assert recovered == ["root"]  # 往上丟之前還是有試著回到原畫面
    assert list(state["screens"]) == ["root"]
