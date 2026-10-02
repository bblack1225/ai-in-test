"""
比對同一個畫面新舊兩個版本的元件差異，判斷哪些元件是「同一個」（延續舊 unique_id）、
哪些「疑似同一個需要人工確認」、哪些是「全新元件」、哪些舊元件已經消失。

對應 PROJECT_BRIEF 決策3的相似度計分規則。網頁上原本有從這支移植的 JavaScript 展示版
（viewer.html、app_map_viewer.html 的「改版比對」分頁），目前都已暫時移除（README 第八節），
正式流程一律以這支 Python 版為準。

使用方式：
  python compare_versions.py --old_json element_map_舊版.json --new_json element_map.json \
      --screen_name cathaylife_health_screen [--test_case test_cases/cathaylife_health_screen.json]
"""

import argparse
import json
from pathlib import Path


def nearby_text_overlap_ratio(a: list, b: list) -> float:
    a = a or []
    b = b or []
    if not a and not b:
        return 0.0
    set_a, set_b = set(a), set(b)
    union = len(set_a | set_b)
    if union == 0:
        return 0.0
    return len(set_a & set_b) / union


def compute_similarity(new_el: dict, old_el: dict):
    """class 不同直接回傳 None（不是候選）；否則回傳 (score, breakdown)"""
    if new_el["class"] != old_el["class"]:
        return None

    new_fp = new_el.get("fingerprint", {}) or {}
    old_fp = old_el.get("fingerprint", {}) or {}
    breakdown = []
    score = 0

    new_label = new_fp.get("label_text", new_el.get("label", ""))
    old_label = old_fp.get("label_text", old_el.get("label", ""))
    overlap = nearby_text_overlap_ratio(new_fp.get("nearby_text"), old_fp.get("nearby_text"))

    if new_label and new_label == old_label:
        score += 40
        breakdown.append("label_text 相同 +40")
    elif overlap >= 0.6:
        score += 25
        breakdown.append(f"nearby_text 高度重疊 {overlap:.0%} +25")
    elif overlap > 0:
        pts = round(overlap * 10)
        score += pts
        breakdown.append(f"nearby_text 部分重疊 {overlap:.0%} +{pts}")

    new_bucket = new_fp.get("position_bucket", new_el.get("position_bucket"))
    old_bucket = old_fp.get("position_bucket", old_el.get("position_bucket"))
    if new_bucket and new_bucket == old_bucket:
        score += 20
        breakdown.append("position_bucket 相同 +20")

    new_sib = new_fp.get("sibling_index")
    old_sib = old_fp.get("sibling_index")
    if new_sib is not None and new_sib == old_sib:
        score += 10
        breakdown.append("sibling_index 相同 +10")

    return score, breakdown


def verdict_of(score: int) -> str:
    if score >= 70:
        return "confirmed"
    if score >= 30:
        return "needs_review"
    return "new"


def compare_screens(old_screen: dict, new_screen: dict) -> dict:
    """回傳 {"results": [...], "missing": [...]}"""
    old_elements = old_screen.get("elements", [])
    new_elements = new_screen.get("elements", [])

    old_native_by_id = {el["unique_id"]: el for el in old_elements if el["id_source"] == "native"}
    old_generated = [el for el in old_elements if el["id_source"] == "generated"]

    matched_old_ids = set()
    results = []

    for new_el in new_elements:
        if new_el["id_source"] == "native":
            # native 元件的 unique_id 是 class+native_id 組成的，沒變動就會直接相同，不需要計分
            if new_el["unique_id"] in old_native_by_id:
                matched_old_ids.add(new_el["unique_id"])
                results.append({
                    "verdict": "confirmed",
                    "new_element": new_el,
                    "old_element": old_native_by_id[new_el["unique_id"]],
                    "score": None,
                    "breakdown": ["native id 完全相同"],
                })
            else:
                results.append({"verdict": "new", "new_element": new_el, "old_element": None,
                                 "score": None, "breakdown": []})
            continue

        best = None
        for old_el in old_generated:
            sim = compute_similarity(new_el, old_el)
            if sim and (best is None or sim[0] > best[0]):
                best = (sim[0], sim[1], old_el)

        if best is None:
            results.append({"verdict": "new", "new_element": new_el, "old_element": None,
                             "score": None, "breakdown": []})
            continue

        score, breakdown, old_el = best
        verdict = verdict_of(score)
        if verdict != "new":
            matched_old_ids.add(old_el["unique_id"])
        results.append({"verdict": verdict, "new_element": new_el, "old_element": old_el,
                         "score": score, "breakdown": breakdown})

    missing = [el for el in old_elements if el["unique_id"] not in matched_old_ids]
    return {"results": results, "missing": missing}


def find_affected_test_steps(comparison: dict, test_case: dict) -> dict:
    """
    交叉比對測試腳本，回傳 {"ok": [...], "needs_review": [...], "broken": [...]}
    ok：步驟目標元件被 confirmed 沿用，腳本可以繼續用
    needs_review：目標元件比對結果是 needs_review，建議人工確認新對應正不正確
    broken：目標元件已消失、或在舊資料裡根本找不到，腳本這幾步大概率已失效
    """
    old_to_verdict = {}
    for r in comparison["results"]:
        if r["old_element"]:
            old_to_verdict[r["old_element"]["unique_id"]] = r["verdict"]
    for el in comparison["missing"]:
        old_to_verdict[el["unique_id"]] = "missing"

    ok, needs_review, broken = [], [], []
    for i, step in enumerate(test_case.get("steps", []), 1):
        target = step.get("target")
        verdict = old_to_verdict.get(target)
        entry = {"step_index": i, "step": step, "verdict": verdict}
        if verdict == "confirmed":
            ok.append(entry)
        elif verdict == "needs_review":
            needs_review.append(entry)
        else:
            broken.append(entry)

    return {"ok": ok, "needs_review": needs_review, "broken": broken}


def print_comparison_summary(screen_name: str, comparison: dict):
    counts = {"confirmed": 0, "needs_review": 0, "new": 0}
    for r in comparison["results"]:
        counts[r["verdict"]] += 1
    print(f"畫面「{screen_name}」比對結果：")
    print(f"  確定同一元件（沿用舊ID）: {counts['confirmed']}")
    print(f"  疑似同一元件，需人工確認: {counts['needs_review']}")
    print(f"  全新元件: {counts['new']}")
    print(f"  舊元件已消失: {len(comparison['missing'])}")
    for r in comparison["results"]:
        if r["verdict"] == "needs_review":
            print(f"    [需確認] {r['old_element']['unique_id']} -> {r['new_element']['unique_id']}"
                  f"（分數{r['score']}: {'、'.join(r['breakdown'])}）")


def print_affected_test_steps(test_case_path: str, affected: dict):
    print(f"\n測試腳本「{test_case_path}」影響評估：")
    print(f"  可繼續使用: {len(affected['ok'])} 步驟")
    print(f"  建議人工確認: {len(affected['needs_review'])} 步驟")
    for entry in affected["needs_review"]:
        print(f"    第{entry['step_index']}步 [{entry['step']['action']}] {entry['step']['target']}")
    print(f"  可能已失效: {len(affected['broken'])} 步驟")
    for entry in affected["broken"]:
        print(f"    第{entry['step_index']}步 [{entry['step']['action']}] {entry['step']['target']}")


def main():
    parser = argparse.ArgumentParser(description="比對同一畫面新舊兩個版本的元件差異")
    parser.add_argument("--old_json", required=True)
    parser.add_argument("--new_json", required=True)
    parser.add_argument("--screen_name", required=True)
    parser.add_argument("--test_case", help="選填，該畫面對應的 test_cases/*.json 路徑")
    args = parser.parse_args()

    old_data = json.loads(Path(args.old_json).read_text(encoding="utf-8"))
    new_data = json.loads(Path(args.new_json).read_text(encoding="utf-8"))

    old_screen = next((s for s in old_data["screens"] if s["screen_name"] == args.screen_name), None)
    new_screen = next((s for s in new_data["screens"] if s["screen_name"] == args.screen_name), None)
    if not old_screen or not new_screen:
        raise SystemExit(f"畫面「{args.screen_name}」在新舊其中一份資料裡找不到")

    comparison = compare_screens(old_screen, new_screen)
    print_comparison_summary(args.screen_name, comparison)

    if args.test_case:
        test_case = json.loads(Path(args.test_case).read_text(encoding="utf-8"))
        affected = find_affected_test_steps(comparison, test_case)
        print_affected_test_steps(args.test_case, affected)


if __name__ == "__main__":
    main()
