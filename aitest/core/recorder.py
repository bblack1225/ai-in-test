"""
錄製建樹：使用者在網頁上點手機截圖上的元件框，由程式代點，換頁就記成一個新節點；
按停止、確認好名稱之後，才一次把所有節點建出來（見 docs 的「錄製建樹功能開發文件」）。

跟「手動加節點＋掃描」的差別：
  - 錄的時候只在手機上抓快照（page_source＋截圖），不做 LLM、不做自動捲動收集，
    每一步只要等點擊＋一次快照
  - 元件 unique_id 開頭是畫面名稱，所以要等名稱確定（按「確認建立」）才產生，
    錄製中途改名稱沒有任何副作用
  - 使用者在錄製視窗按上滑／下滑時多抓一輪快照，建立時跟掃描的「邊捲邊抓」一樣合併

換頁判斷用爬蟲的骨架簽章（platform.skeleton）：
  - 跟目前畫面一樣 → 沒換頁（輸入、勾選），不建節點
  - 跟這次錄過的某個畫面一樣 → 回到那個畫面（按返回走另一條分支），下一個節點接在它底下
  - 都不一樣 → 新節點，入口是這次點的元件
判斷錯的時候網頁有「這一步不是新畫面」（併回上一頁）跟「點擊前勾『一定是新畫面』」可以修正。

快照存在 <專案>/recordings/<錄製 id>/，建立完成（或取消）就刪掉；不會跟著專案匯出。
"""

import shutil
import time
from pathlib import Path
from urllib.parse import quote

from aitest.core.capture_screen_map import (
    assign_unique_ids,
    build_screen_data,
    describe_candidates,
    downgrade_duplicate_native_ids,
    generated_fingerprint,
    is_screenshot_blocked,
)
from aitest.core.crawl_app_tree import _hits_risky_keyword
from aitest.core.run_test_case import run_input_text, run_tap, wait_after_action
from aitest.platforms.scan_core import candidate_signature

RECORDINGS_DIR = "recordings"


class RecordingError(Exception):
    pass


def candidate_key(el: dict) -> tuple:
    """
    同一個畫面不同輪快照之間認元件用的 key（不看座標，捲動後座標會變）。
    用還沒降級的 native_id：降級要等所有輪合併完才做，點擊當下那一輪還不知道結果。
    """
    return (el["class"], el.get("_raw_native_id", el["native_id"]), el["label"], tuple(el["nearby_text"]))


def merge_rounds(platform, rounds: list[tuple[str, int, int]]) -> list[dict]:
    """
    把同一個畫面的幾輪快照（page_source, 寬, 高）合併成一份候選元件清單：第一輪的元件
    全部保留（跟只掃一次的結果一樣），之後每一輪只加「前面幾輪都沒出現過」的元件，
    全部合併完才做 native id 降級（同掃描的「邊捲邊抓」）。
    每個元件記 "_round_index"（哪一輪抓到的）、"_pos"（(輪, 那一輪裡第幾個)）、"_key"（candidate_key）。
    """
    seen: set[tuple] = set()
    merged: list[dict] = []
    for round_index, (page_source, width, height) in enumerate(rounds):
        round_keys = set()
        for pos, el in enumerate(platform.parse_candidates_raw(page_source, width, height)):
            el["_raw_native_id"] = el["native_id"]
            key = candidate_key(el)
            if key in seen:
                continue
            el.update({"_round_index": round_index, "_pos": (round_index, pos), "_key": key})
            merged.append(el)
            round_keys.add(key)
        seen |= round_keys
    return downgrade_duplicate_native_ids(platform, merged)


def find_entry_uid(candidates: list[dict], entry: dict) -> str | None:
    """
    入口元件在上一頁（已 assign_unique_ids）對應的 unique_id：先找點擊當下那一輪的同一個位置，
    那一輪的元件在前面幾輪已經出現過（被合併掉）的話，改用 candidate_key 找第一個
    """
    for el in candidates:
        if el["_pos"] == entry["pos"]:
            return el["_unique_id"]
    for el in candidates:
        if el["_key"] == entry["key"]:
            return el["_unique_id"]
    return None


def _entry_text(el: dict) -> str:
    """預設節點名稱用的元件文字：label → value → native id 最後一段"""
    for text in (el.get("label"), el.get("value")):
        if text and text.strip():
            return text.strip()
    native_id = el.get("native_id") or ""
    return native_id.split(":id/")[-1].split("/")[-1]


def _tap_target(cand: dict) -> dict:
    """把快照裡的候選元件轉成 run_tap／run_input_text 吃的元件格式"""
    if cand["native_id"]:
        return {"id_source": "native", "native_id": cand["native_id"], "class": cand["class"],
                "label": cand["label"], "value": cand["value"], "bounds": cand["bounds"]}
    return {"id_source": "generated", "native_id": None, "class": cand["class"], "label": cand["label"],
            "value": cand["value"], "bounds": cand["bounds"], "fingerprint": generated_fingerprint(cand)}


class Recording:
    """
    一次錄製。screens[0] 是起點節點（地圖上已經有的畫面），之後每個都是新畫面：
      {"n", "parent"(screens 的索引), "entry"(點的元件), "signatures", "rounds"}
    rounds 每一輪是 {"xml", "png", "w", "h"}（檔名，相對錄製資料夾）。
    current 是手機目前在哪個畫面（None＝不在這次錄過的畫面上），live 是畫面上
    目前顯示給使用者點的那一輪 (screen 索引, round 索引)。
    """

    def __init__(self, platform, project_dir: Path, project_id: str, map_name: str,
                 start_uid: str, start_name: str):
        self.platform = platform
        self.project_id = project_id
        self.map_name = map_name
        self.id = time.strftime("rec_%Y%m%d_%H%M%S")
        self.dir = project_dir / RECORDINGS_DIR / self.id
        self.dir.mkdir(parents=True, exist_ok=False)
        self.start_uid = start_uid
        self.start_name = start_name
        self.screens: list[dict] = [{"n": 0, "parent": None, "entry": None, "signatures": [], "rounds": []}]
        self.current: int | None = 0
        self.live: tuple[int, int] | None = None
        self.live_candidates: list[dict] = []
        self.message = ""
        # 平台在錄製期間要記的狀態（Android：改過的 enableMultiWindows 原始值、App 的 package），結束時還原
        self.platform_state = None
        self._seq = 0

    # ---- 快照 ----
    def _snapshot(self, driver) -> dict:
        """抓一次目前畫面：page_source、截圖、畫面大小、骨架簽章，寫進錄製資料夾"""
        size = driver.get_window_size()
        page_source = self.platform.recording_source(driver.page_source, self.platform_state)
        png = driver.get_screenshot_as_png()
        _, signature = self.platform.skeleton(driver, page_source)
        self._seq += 1
        stamp = f"{self._seq:03d}_{int(time.time() * 1000)}"  # 加流水號：同一毫秒抓兩次也不會互相蓋掉
        (self.dir / f"{stamp}.xml").write_text(page_source, encoding="utf-8")
        (self.dir / f"{stamp}.png").write_bytes(png)
        return {"xml": f"{stamp}.xml", "png": f"{stamp}.png", "w": size["width"], "h": size["height"],
                "signature": signature, "content": self._content(page_source, size["width"], size["height"]),
                "_source": page_source}

    def _content(self, page_source: str, width: int, height: int) -> tuple:
        """這一輪的候選元件內容（同掃描判斷「畫面穩定」用的簽章），用來認「捲回看過的位置」"""
        return candidate_signature(self.platform.parse_candidates_raw(page_source, width, height))

    def _add_round(self, screen_index: int, snap: dict) -> None:
        screen = self.screens[screen_index]
        if snap["signature"] not in screen["signatures"]:
            screen["signatures"].append(snap["signature"])
        screen["rounds"].append({k: snap[k] for k in ("xml", "png", "w", "h", "content")})
        self._set_live(screen_index, len(screen["rounds"]) - 1, snap["_source"])

    def _set_live(self, screen_index: int, round_index: int, page_source: str | None = None) -> None:
        r = self.screens[screen_index]["rounds"][round_index]
        if page_source is None:
            page_source = (self.dir / r["xml"]).read_text(encoding="utf-8")
        self.live = (screen_index, round_index)
        self.live_candidates = merge_rounds(self.platform, [(page_source, r["w"], r["h"])])

    def _find_screen(self, signature: str) -> int | None:
        for screen in self.screens:
            if signature in screen["signatures"]:
                return screen["n"]
        return None

    def _reuse_round(self, screen_index: int, snap: dict) -> bool:
        """
        這個畫面已經有內容一樣的一輪（例如下滑後又上滑回頂部）：不多存，畫面改顯示那一輪，
        回傳 True；都不一樣回傳 False（呼叫端再決定要不要多記一輪）
        """
        for round_index, r in enumerate(self.screens[screen_index]["rounds"]):
            if r["content"] == snap["content"]:
                self._discard(snap)
                self._set_live(screen_index, round_index)
                return True
        return False

    def _arrive(self, snap: dict, entry_cand: dict | None, force_new: bool = False) -> str:
        """一個動作之後抓到新快照，判斷換頁了沒，回傳給使用者看的訊息"""
        if force_new and entry_cand is not None and self.current is not None:
            return self._new_screen(snap, entry_cand)
        known = self._find_screen(snap["signature"])
        if known is not None:
            moved = known != self.current
            self.current = known
            if not self._reuse_round(known, snap):
                self._add_round(known, snap)
            if not moved:
                return "沒有換頁（輸入、勾選這類操作），不建節點"
            return f"回到「{self._label(known)}」，接下來點出的畫面會接在它底下"
        if entry_cand is None or self.current is None:
            self.current = None
            self._discard(snap)
            self.live_candidates = []
            return "手機現在不在這次錄過的畫面上：請在手機上回到錄過的畫面，再按「重新抓畫面」"
        return self._new_screen(snap, entry_cand)

    def _new_screen(self, snap: dict, entry_cand: dict) -> str:
        n = len(self.screens)
        # 點的是 live 那一輪的第幾個元件（live_candidates 只有一輪，_pos 的「輪」固定是 0）
        pos = (self.live[1], entry_cand["_pos"][1])
        self.screens.append({
            "n": n, "parent": self.current, "signatures": [], "rounds": [],
            "entry": {"key": entry_cand["_key"], "pos": pos, "class": entry_cand["class"],
                      "native_id": entry_cand["native_id"], "label": entry_cand["label"],
                      "value": entry_cand["value"], "text": _entry_text(entry_cand)},
        })
        self.current = n
        self._add_round(n, snap)
        return f"新畫面：第 {n} 步（點「{_entry_text(entry_cand)}」進來）"

    def _discard(self, snap: dict) -> None:
        for name in (snap["xml"], snap["png"]):
            (self.dir / name).unlink(missing_ok=True)

    def _label(self, screen_index: int) -> str:
        if screen_index == 0:
            return self.start_name
        return f"第 {screen_index} 步：{self.screens[screen_index]['entry']['text']}"

    # ---- 使用者操作（都要拿著裝置鎖呼叫）----
    def start(self, driver) -> None:
        self.platform_state = self.platform.begin_recording(driver)
        snap = self._snapshot(driver)
        self._add_round(0, snap)
        self.message = f"已記錄起點「{self.start_name}」，點畫面上的元件開始錄製"

    def _live_cand(self, index: int) -> dict:
        if self.current is None or not 0 <= index < len(self.live_candidates):
            raise RecordingError("畫面已經變了，請重新整理後再點")
        return self.live_candidates[index]

    def tap(self, ctx, driver, index: int, force_new: bool = False) -> str:
        cand = self._live_cand(index)
        result = run_tap(ctx, self.platform, driver, _tap_target(cand))
        note = ""
        if not result.startswith("PASS"):
            # 快照是剛抓的，畫面上的位置可信：定位不到（例如浮動層裡的元件）就照錄製畫面上的座標點
            ctx.raise_if_stopped()
            self.platform.tap_by_coordinates(driver, cand["bounds"])
            print(f"[錄製] 定位不到「{_entry_text(cand)}」（{result}），改用座標點擊")
            note = "（定位不到這個元件，改用座標點擊；之後跑測案可能也要靠座標）"
        wait_after_action(self.platform, driver)
        self.message = self._arrive(self._snapshot(driver), cand, force_new) + note
        return self.message

    def input(self, ctx, driver, index: int, value: str) -> str:
        cand = self._live_cand(index)
        result = run_input_text(ctx, self.platform, driver, _tap_target(cand), value)
        if not result.startswith("PASS"):
            self.message = f"輸入失敗：{result}"
            return self.message
        wait_after_action(self.platform, driver)
        self.message = "已輸入。" + self._arrive(self._snapshot(driver), None)
        return self.message

    def swipe(self, driver, direction: str) -> str:
        if self.current is None:
            raise RecordingError("手機現在不在這次錄過的畫面上，請先回到錄過的畫面再按「重新抓畫面」")
        size = driver.get_window_size()
        self.platform.scroll_screen(driver, size["width"], size["height"], direction)
        time.sleep(0.5)
        snap = self._snapshot(driver)
        # 使用者自己按的捲動不會換頁；iOS 的骨架只看看得到的元件，捲動後簽章可能會變，
        # 直接當成同一個畫面的另一輪
        before = self.live
        if self._reuse_round(self.current, snap):
            self.message = ("畫面沒有變化（可能已經捲到底或頂）" if self.live == before
                            else "捲回之前記錄過的位置，不重複記錄")
        else:
            self._add_round(self.current, snap)
            self.message = "已捲動，多記錄一輪畫面"
        return self.message

    def back(self, driver) -> str:
        self.platform.go_back(driver)
        wait_after_action(self.platform, driver)
        self.message = self._arrive(self._snapshot(driver), None)
        return self.message

    def refresh(self, driver) -> str:
        self.message = self._arrive(self._snapshot(driver), None)
        return self.message

    def merge_last(self) -> str:
        """最後一步其實不是新畫面：把它的快照併回上一頁，之後看到同樣的簽章也當成上一頁"""
        last = self.screens[-1]
        if last["n"] == 0:
            raise RecordingError("還沒有錄到任何新畫面")
        parent = self.screens[last["parent"]]
        self.screens.pop()
        parent["rounds"].extend(last["rounds"])
        parent["signatures"].extend(s for s in last["signatures"] if s not in parent["signatures"])
        if self.current == last["n"]:
            self.current = parent["n"]
            self._set_live(parent["n"], len(parent["rounds"]) - 1)
        self.message = f"已把「{last['entry']['text']}」併回「{self._label(parent['n'])}」，不會建成節點"
        return self.message

    def undo(self) -> str:
        """刪掉最後一個新畫面的紀錄（不會把手機帶回去）"""
        last = self.screens[-1]
        if last["n"] == 0:
            raise RecordingError("還沒有錄到任何新畫面")
        self.screens.pop()
        for r in last["rounds"]:
            for name in (r["xml"], r["png"]):
                (self.dir / name).unlink(missing_ok=True)
        if self.current == last["n"]:
            self.current = None
            self.live_candidates = []
        self.message = (f"已刪掉「{last['entry']['text']}」這一步。請在手機上回到要繼續的畫面，"
                        f"再按「重新抓畫面」")
        return self.message

    # ---- 給網頁的狀態 ----
    def file_url(self, name: str) -> str:
        return f"/projects/{quote(self.project_id)}/{RECORDINGS_DIR}/{self.id}/{name}"

    def state(self) -> dict:
        live = None
        if self.current is not None and self.live is not None:
            r = self.screens[self.live[0]]["rounds"][self.live[1]]
            live = {
                "screen": self.live[0], "screenshot": self.file_url(r["png"]), "w": r["w"], "h": r["h"],
                "candidates": [{
                    "i": i, "class": c["class"], "native_id": c["native_id"], "label": c["label"],
                    "value": c["value"], "bounds": c["bounds"],
                    "a": bool(self.platform.element_flags(c).get("accessible", c.get("clickable"))),
                    "risky": _hits_risky_keyword(c),
                } for i, c in enumerate(self.live_candidates)],
            }
        return {
            "active": True, "id": self.id, "project": self.project_id, "map": self.map_name,
            "start": {"uid": self.start_uid, "name": self.start_name},
            "current": self.current, "message": self.message, "live": live,
            "steps": [{"n": s["n"], "parent": s["parent"], "entry_text": s["entry"]["text"],
                       "entry_class": s["entry"]["class"], "rounds": len(s["rounds"]),
                       "screenshot": self.file_url(s["rounds"][0]["png"])}
                      for s in self.screens[1:]],
        }

    # ---- 建立 ----
    def _screen_inputs(self, screen: dict) -> tuple[list[dict], list[bytes], int, int]:
        rounds = screen["rounds"]
        sources = [((self.dir / r["xml"]).read_text(encoding="utf-8"), r["w"], r["h"]) for r in rounds]
        pngs = [(self.dir / r["png"]).read_bytes() for r in rounds]
        return merge_rounds(self.platform, sources), pngs, rounds[0]["w"], rounds[0]["h"]

    def build(self, ctx, out_dir: Path, names: dict[int, str], deleted: set[int]) -> dict:
        """
        names：{步驟編號: 節點名稱}，deleted：不要建的步驟。被刪的步驟底下的步驟改接到
        被刪那步的父節點（入口元件可能不在那個畫面上，回傳 warnings 提醒）。
        回傳 {"start_entry_uids": {n: uid}, "screens": [{n, name, parent, entry_uid, screen_data}], "warnings"}；
        parent 是 0（起點節點）或另一個步驟編號。
        """
        kept = [s for s in self.screens[1:] if s["n"] not in deleted]
        missing = [s["n"] for s in kept if not (names.get(s["n"]) or "").strip()]
        if missing:
            raise RecordingError(f"第 {missing} 步還沒有名稱")
        all_names = [names[s["n"]].strip() for s in kept]
        if len(set(all_names)) != len(all_names):
            raise RecordingError("有兩步的名稱一樣，請改成不同名稱")

        warnings = []
        # 起點畫面只需要算元件 id（找入口元件在地圖上對應哪個 uid），不存截圖
        candidates_by_n: dict[int, list[dict]] = {}
        start_candidates, _, _, _ = self._screen_inputs(self.screens[0])
        assign_unique_ids(self.start_name, start_candidates)
        candidates_by_n[0] = start_candidates

        built = []
        for screen in kept:
            name = names[screen["n"]].strip()
            candidates, pngs, w, h = self._screen_inputs(screen)
            screen_data = build_screen_data(ctx, self.platform, name, candidates, pngs, pngs[0], w, h,
                                            describe=False, out_dir=out_dir, scroll_collected=len(pngs) > 1)
            candidates_by_n[screen["n"]] = candidates
            built.append({"n": screen["n"], "name": name, "screen": screen, "screen_data": screen_data})

        out = []
        for item in built:
            screen = item["screen"]
            parent = screen["parent"]
            skipped = False
            while parent != 0 and parent in deleted:
                parent = self.screens_by_n(parent)["parent"]
                skipped = True
            entry_uid = find_entry_uid(candidates_by_n[parent], screen["entry"])
            if skipped:
                warnings.append(f"「{item['name']}」的上一步被刪掉了，改接到上一層，入口元件可能不在那個畫面上")
            if entry_uid is None:
                warnings.append(f"「{item['name']}」的入口元件「{screen['entry']['text']}」在上一頁的元件裡找不到")
            out.append({"n": item["n"], "name": item["name"], "parent": parent, "entry_uid": entry_uid,
                        "entry": screen["entry"], "screen_data": item["screen_data"]})
        return {"screens": out, "warnings": warnings}

    def screens_by_n(self, n: int) -> dict:
        return next(s for s in self.screens if s["n"] == n)

    def describe(self, ctx, n: int, name: str) -> dict:
        """建立後補描述：用錄到的每一輪截圖幫這一步的元件產生描述，回傳 {unique_id: 描述}"""
        candidates, pngs, _, _ = self._screen_inputs(self.screens_by_n(n))
        assign_unique_ids(name, candidates)
        return describe_candidates(ctx, self.platform, name, candidates, pngs, is_screenshot_blocked(pngs[0]))

    def finish(self, driver) -> None:
        """錄製結束（建立或放棄）：還原錄製期間改過的裝置設定"""
        self.platform.end_recording(driver, self.platform_state)
        self.platform_state = None

    def remove_files(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def recording_summary(recording: "Recording | None") -> dict:
    return recording.state() if recording is not None else {"active": False}

