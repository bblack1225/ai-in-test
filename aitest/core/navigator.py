"""
批次執行用的畫面導航：把手機從「目前不知道在哪」帶到某份測案的起始畫面。

測完 A 接著測 B 時，手機停在 A 的最後一頁，B 要從自己的第一步畫面開始。這裡用
地圖的樹狀結構（每個節點記了 parent_screen_uid＝上一層、entry_element_uid＝從
上一層點哪個元件進來）算路線，分層嘗試，前一層不行才用下一層：

  1. 辨識目前在哪一頁（identify_screen）：拿目前畫面跟地圖每個節點比對、挑最像的
  2. 目前這頁在「根 → 起始畫面」的路徑上：照路徑往下點入口元件
     不在路徑上：按返回往上退（Android 系統返回鍵、iOS 猜返回鈕），每退一步重新辨識
  3. 辨識不出來、或退到底還回不去：重開 App（重開後要登入的話，先跑使用者指定的
     登入測案），再從首頁照路徑點過去

每走一步都重新辨識，不會照著舊路線盲點一整串；走不到就老實回報失敗，
由呼叫端把那份測案標成 SKIP，接著跑下一份。

已知限制（也寫在 README）：
  - 入口元件是清單裡某一列（例如自選股第一支）時，照路徑點的是同一個位置，
    清單順序變了就會點到別的
  - 兩個節點長得幾乎一樣（同一個頁面模板）時可能辨識錯，靠每一步重新辨識把關
"""

import time
import xml.etree.ElementTree as ET

from selenium.common.exceptions import InvalidSessionIdException, WebDriverException

from aitest.core.run_test_case import raise_if_stopped, run_tap, try_dismiss_dialogs, wait_after_action

# 一個畫面要有過半數的指紋元件出現在目前畫面上，才算「可能是這一頁」（跟第0步檢查同標準）
MATCH_MIN_RECALL = 0.5


def _short_el_to_full(el: dict) -> dict:
    """viewer 內部的精簡欄位（u/n/c/l…）轉成執行端用的全名欄位"""
    out = {"unique_id": el.get("u"), "id_source": el.get("src"), "native_id": el.get("n"),
           "class": el.get("c"), "label": el.get("l"), "value": el.get("v"), "bounds": el.get("b")}
    if el.get("custom_xpath"):
        out["custom_xpath"] = el["custom_xpath"]
    return out


class MapIndex:
    """一張地圖（viewer 匯出的全名格式，或爬蟲的 app_map_tree.json）整理成方便查詢的索引"""

    def __init__(self, map_json: dict):
        self.screens = []
        for s in map_json.get("screens", []):
            if "screen_uid" in s:
                screen = {"uid": s["screen_uid"], "name": s["screen_name"], "parent": s.get("parent_screen_uid"),
                          "entry": s.get("entry_element_uid"), "elements": s.get("elements") or []}
            else:
                screen = {"uid": s.get("uid"), "name": s.get("name"), "parent": s.get("parent"),
                          "entry": s.get("entry"), "elements": [_short_el_to_full(e) for e in s.get("els") or []]}
            self.screens.append(screen)
        self.by_uid = {s["uid"]: s for s in self.screens}
        self.by_name = {s["name"]: s for s in self.screens}
        self.known_ids = {e["native_id"] for s in self.screens for e in s["elements"]
                          if e.get("id_source") == "native" and e.get("native_id")}

    def screens_for_execution(self) -> dict:
        """執行測案步驟用的 {畫面名稱: {"elements": [...]}}，跟 /run_test_script 收到的格式一樣"""
        return {s["name"]: {"elements": s["elements"]} for s in self.screens}

    def chain_to(self, uid: str) -> list[str]:
        """根 → uid 的節點 uid 清單（含兩端）"""
        chain, seen = [], set()
        while uid and uid in self.by_uid and uid not in seen:
            seen.add(uid)
            chain.append(uid)
            uid = self.by_uid[uid]["parent"]
        return list(reversed(chain))

    def entry_element(self, uid: str) -> dict | None:
        """從上一層進到 uid 這一頁要點的那個元件"""
        screen = self.by_uid.get(uid)
        parent = self.by_uid.get(screen["parent"]) if screen else None
        if not parent:
            return None
        return next((e for e in parent["elements"] if e.get("unique_id") == screen["entry"]), None)


def _present_ids_and_labels(page_source: str, platform) -> tuple[set, set]:
    root = ET.fromstring(page_source)
    ids, labels = set(), set()
    for node in root.iter():
        for attr in platform.screen_id_attrs:
            if node.attrib.get(attr):
                ids.add(node.attrib[attr])
        for attr in platform.screen_label_attrs:
            text = (node.attrib.get(attr) or "").strip()
            if text:
                labels.add(text)
    return ids, labels


def identify_screen(platform, page_source: str, index: MapIndex) -> tuple[dict | None, str]:
    """
    目前畫面最像地圖裡的哪一頁，回傳 (畫面 or None, 說明)。

    比對方式比照第0步檢查（run_test_case.match_screen_in_source）：有 native id 的畫面
    看 id，沒有的看不含數字的固定文字。多個畫面都過門檻時，要分出誰「更像」：
    子畫面常常也帶著上一層的導覽列、分頁列，光看「這頁的元件有幾成出現」分不出來，
    所以同時看反方向——目前畫面上「地圖認得的 id」有幾成屬於這一頁，兩個比例相乘當分數。
    """
    try:
        present_ids, present_labels = _present_ids_and_labels(page_source, platform)
    except ET.ParseError:
        return None, "畫面結構解析失敗"
    present_known = present_ids & index.known_ids

    best, best_score = None, 0.0
    for screen in index.screens:
        ids = {e["native_id"] for e in screen["elements"] if e.get("id_source") == "native" and e.get("native_id")}
        if ids:
            found = len(ids & present_ids)
            recall = found / len(ids)
            precision = found / len(present_known) if present_known else 0.0
        else:
            labels = {(e.get("label") or "").strip() for e in screen["elements"]}
            labels = {t for t in labels if t and not any(ch.isdigit() for ch in t)}
            if not labels:
                continue
            found = len(labels & present_labels)
            recall = found / len(labels)
            precision = recall * 0.5  # 只靠文字比對比較不可靠，同分時讓有 id 的畫面優先
        if recall < MATCH_MIN_RECALL:
            continue
        score = recall * precision
        if score > best_score:
            best, best_score = screen, score
    if best is None:
        return None, "目前畫面跟地圖裡的畫面都對不上"
    return best, f"「{best['name']}」（相似度 {best_score:.0%}）"


class Navigator:
    """
    一次批次執行共用一個 Navigator：記住 App 的 id（重開 App 用）、登入測案、
    使用者的選項，navigate_to() 每次導航的過程都寫進傳進來的 log。
    """

    def __init__(self, platform, driver, app_id: str | None, run_setup=None, setup_start: str | None = None,
                 allow_restart: bool = True, max_moves: int = 15, max_backs: int = 6):
        self.platform = platform
        self.driver = driver
        self.app_id = app_id
        self.run_setup = run_setup  # 重開 App 後要先跑的登入測案；None 代表不需要
        self.setup_start = setup_start  # 登入測案的第一個畫面（登入頁）：導航途中停在這頁也先跑登入
        self.allow_restart = allow_restart
        self.max_moves = max_moves
        self.max_backs = max_backs

    def _ensure_app_foreground(self, log: list) -> None:
        """Android 在首頁按返回會把 App 退到背景，拉回來"""
        if not self.app_id:
            return
        try:
            if self.driver.query_app_state(self.app_id) != 4:  # 4 = 在前景執行中
                log.append("App 不在前景（可能是返回鍵退出了），重新拉回前景")
                self.driver.activate_app(self.app_id)
                wait_after_action(self.platform, self.driver, max_wait=5)
        except InvalidSessionIdException:
            raise
        except WebDriverException as e:
            log.append(f"確認 App 狀態失敗：{type(e).__name__}")

    def restart_app(self, log: list) -> bool:
        if not self.app_id:
            log.append("不知道 App 的 id，沒辦法重開")
            return False
        log.append(f"重開 App（{self.app_id}）")
        try:
            self.driver.terminate_app(self.app_id)
            self.driver.activate_app(self.app_id)
        except InvalidSessionIdException:
            raise
        except WebDriverException as e:
            log.append(f"重開 App 失敗：{type(e).__name__}：{e}")
            return False
        time.sleep(2)  # 啟動畫面、splash 動畫
        wait_after_action(self.platform, self.driver, max_wait=8)
        dismissed = try_dismiss_dialogs(self.platform, self.driver)
        if dismissed:
            log.append(f"App 啟動後跳出彈窗，先關掉（按了「{'」「'.join(dismissed)}」）")
        if self.run_setup:
            ok, summary = self.run_setup()
            log.append(f"重開後執行登入測案：{summary}")
            if not ok:
                return False
        return True

    def navigate_to(self, index: MapIndex, target_name: str, log: list) -> bool:
        target = index.by_name.get(target_name)
        if target is None:
            log.append(f"地圖裡沒有起始畫面「{target_name}」")
            return False
        chain = index.chain_to(target["uid"])
        attempts = ["目前位置"] + (["重開 App"] if self.allow_restart else [])
        for attempt in attempts:
            if attempt == "重開 App":
                log.append("返回／往下點都到不了，改用最後手段")
                if not self.restart_app(log):
                    return False
            if self._walk(index, target, chain, log):
                return True
        return False

    def _walk(self, index: MapIndex, target: dict, chain: list[str], log: list) -> bool:
        backs = 0
        setup_done = False
        for _ in range(self.max_moves):
            raise_if_stopped()
            self._ensure_app_foreground(log)
            current, detail = identify_screen(self.platform, self.driver.page_source, index)
            if current is not None and current["uid"] == target["uid"]:
                log.append(f"已在起始畫面 {detail}")
                return True

            if (current is not None and self.run_setup and not setup_done
                    and current["name"] == self.setup_start and current["uid"] not in chain):
                # 停在登入頁（例如登入逾時被登出）：先登入，登入完再重新辨識
                setup_done = True
                ok, summary = self.run_setup()
                log.append(f"目前在登入頁 {detail}，先執行登入測案：{summary}")
                if not ok:
                    return False
                continue

            if current is not None and current["uid"] in chain:
                next_uid = chain[chain.index(current["uid"]) + 1]
                entry = index.entry_element(next_uid)
                next_name = index.by_uid[next_uid]["name"]
                if entry is None:
                    log.append(f"目前在 {detail}，但地圖沒記錄怎麼進到「{next_name}」，沒辦法往下點")
                    return False
                outcome = run_tap(self.platform, self.driver, entry)
                log.append(f"目前在 {detail}，往下點「{entry.get('label') or entry.get('native_id') or entry.get('unique_id')}」"
                           f"進「{next_name}」：{outcome}")
                wait_after_action(self.platform, self.driver)
                continue

            if current is None:
                dismissed = try_dismiss_dialogs(self.platform, self.driver)
                if dismissed:
                    log.append(f"辨識不出目前畫面，先關掉彈窗（按了「{'」「'.join(dismissed)}」）")
                    continue
            if backs >= self.max_backs:
                log.append(f"已經按了 {backs} 次返回，還是回不到「{target['name']}」的路徑上")
                return False
            where = f"目前在 {detail}，不在通往「{target['name']}」的路徑上" if current else f"{detail}"
            log.append(f"{where}，按返回")
            self.platform.go_back(self.driver)
            backs += 1
            wait_after_action(self.platform, self.driver)
        log.append(f"走了 {self.max_moves} 步還沒到「{target['name']}」，放棄")
        return False
