"""scan_core.py：九宮格分區、跨捲動輪次的身分 key、讀一輪畫面並合併"""

from aitest.platforms.scan_core import (
    candidate_identity_key,
    candidate_signature,
    identity_keys_in_region,
    position_bucket,
    read_and_merge_round,
)


def _el(label, native_id=None, cls="Button", nearby=(), bounds=None, bucket="middle_center"):
    return {"class": cls, "native_id": native_id, "label": label, "nearby_text": list(nearby),
            "bounds": bounds or {"x": 0, "y": 0, "width": 10, "height": 10}, "position_bucket": bucket}


def test_position_bucket_corners_and_center():
    assert position_bucket(0, 0, 10, 10, 390, 844) == "top_left"
    assert position_bucket(180, 400, 30, 40, 390, 844) == "middle_center"
    assert position_bucket(380, 830, 10, 10, 390, 844) == "bottom_right"


def test_identity_key_ignores_position():
    a = _el("刪除", nearby=["範例甲"], bounds={"x": 0, "y": 100, "width": 10, "height": 10})
    b = _el("刪除", nearby=["範例甲"], bounds={"x": 0, "y": 900, "width": 10, "height": 10}, bucket="bottom_center")
    assert candidate_identity_key(a) == candidate_identity_key(b)


def test_identity_key_native_id_includes_label():
    """同一個 id 但文字不同的清單項目不能合併成一筆（TROUBLESHOOTING #32）"""
    assert candidate_identity_key(_el("項目 001", "row")) != candidate_identity_key(_el("項目 002", "row"))


def test_candidate_signature_order_independent():
    a, b = _el("甲"), _el("乙", bucket="top_left")
    assert candidate_signature([a, b]) == candidate_signature([b, a])
    assert candidate_signature([a]) != candidate_signature([a, b])


def test_identity_keys_in_region_uses_center_point():
    region = {"x": 0, "y": 0, "width": 100, "height": 100}
    inside = _el("內", bounds={"x": 40, "y": 40, "width": 20, "height": 20})
    straddling = _el("跨", bounds={"x": 90, "y": 0, "width": 40, "height": 10})  # 中心點 x=110，在外面
    keys = identity_keys_in_region([inside, straddling], region)
    assert keys == {candidate_identity_key(inside)}


class _FakeDriver:
    def __init__(self, sources):
        self._sources = list(sources)

    @property
    def page_source(self):
        return self._sources.pop(0)

    def get_screenshot_as_png(self):
        return b"png"


class _FakePlatform:
    """parse_candidates_raw 直接把 page_source 當成候選清單的代號"""

    def __init__(self, rounds):
        self.rounds = rounds

    def parse_candidates_raw(self, page_source, w, h):
        return [dict(el) for el in self.rounds[page_source]]


def test_read_and_merge_round_dedupes_across_rounds():
    platform = _FakePlatform({"r1": [_el("甲"), _el("乙")], "r2": [_el("乙"), _el("丙")]})
    driver = _FakeDriver(["r1", "r2"])
    merged, shots = {}, []

    new1, raw1, src1, _, _ = read_and_merge_round(platform, driver, 390, 844, merged, shots)
    new2, raw2, src2, _, raw = read_and_merge_round(platform, driver, 390, 844, merged, shots)

    assert (new1, raw1, src1) == (2, 2, "r1")
    assert (new2, raw2, src2) == (1, 2, "r2")
    assert len(shots) == 2
    assert sorted(el["label"] for el in merged.values()) == ["丙", "乙", "甲"]
    # 以第一次出現的輪次為準
    assert merged[candidate_identity_key(_el("乙"))]["_round_index"] == 0
    assert merged[candidate_identity_key(_el("丙"))]["_round_index"] == 1
    assert [el["label"] for el in raw] == ["乙", "丙"]
