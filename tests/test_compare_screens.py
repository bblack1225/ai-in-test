"""compare_versions.compare_screens：改版前後哪些元件是同一個"""

from compare_versions import compare_screens, compute_similarity, verdict_of


def _native(uid, cls="Button"):
    return {"unique_id": uid, "id_source": "native", "class": cls}


def _generated(uid, label, cls="Button", bucket="top_left", sibling=0, nearby=()):
    return {"unique_id": uid, "id_source": "generated", "class": cls, "label": label,
            "fingerprint": {"label_text": label, "position_bucket": bucket,
                            "sibling_index": sibling, "nearby_text": list(nearby)}}


def _verdicts(result):
    return {r["new_element"]["unique_id"]: (r["verdict"], r["old_element"] and r["old_element"]["unique_id"])
            for r in result["results"]}


def test_native_same_id_confirmed_and_new_id_new():
    result = compare_screens({"elements": [_native("Button::login")]},
                             {"elements": [_native("Button::login"), _native("Button::signup")]})
    assert _verdicts(result) == {"Button::login": ("confirmed", "Button::login"), "Button::signup": ("new", None)}
    assert result["missing"] == []


def test_generated_all_match_is_confirmed():
    """文字 +40、位置 +20、sibling +10 = 70，剛好到 confirmed 門檻"""
    old = _generated("gen::old", "下單")
    new = _generated("gen::new", "下單")
    assert compute_similarity(new, old)[0] == 70
    result = compare_screens({"elements": [old]}, {"elements": [new]})
    assert _verdicts(result) == {"gen::new": ("confirmed", "gen::old")}


def test_generated_label_changed_but_context_same_needs_review():
    old = _generated("gen::old", "買進", nearby=["台積電", "2330"])
    new = _generated("gen::new", "立即買進", nearby=["台積電", "2330"])
    score, breakdown = compute_similarity(new, old)
    assert score == 25 + 20 + 10
    assert any("nearby_text 高度重疊" in b for b in breakdown)
    assert verdict_of(score) == "needs_review"


def test_different_class_is_not_candidate():
    old = _generated("gen::old", "下單", cls="StaticText")
    new = _generated("gen::new", "下單", cls="Button")
    assert compute_similarity(new, old) is None
    result = compare_screens({"elements": [old]}, {"elements": [new]})
    assert _verdicts(result) == {"gen::new": ("new", None)}
    assert [el["unique_id"] for el in result["missing"]] == ["gen::old"]


def test_low_score_is_new_and_old_reported_missing():
    old = _generated("gen::old", "下單", bucket="top_left", sibling=0)
    new = _generated("gen::new", "完全不同", bucket="bottom_right", sibling=3)
    result = compare_screens({"elements": [old]}, {"elements": [new]})
    assert _verdicts(result)["gen::new"][0] == "new"
    assert [el["unique_id"] for el in result["missing"]] == ["gen::old"]


def test_verdict_thresholds():
    assert [verdict_of(s) for s in (70, 69, 30, 29)] == ["confirmed", "needs_review", "needs_review", "new"]
