"""
「依變數定位」：幫清單裡的某一列組出含 ${KEY} 的自訂 XPath（網頁元件視窗的「🔁 依變數定位」）。

使用者在地圖上選清單第一列（例如搜尋結果），工具讀手機當下的畫面結構，列出這一列裡的文字
讓使用者挑「哪段文字每次會換」，再照規則組出 XPath，例如：
    //android.widget.ListView/android.widget.LinearLayout[./android.widget.TextView[@text='${STOCK_CODE}']]
這一列本身通常沒有 id、也沒有文字，現成的定位方式找不到，所以用地圖記的位置在當下畫面結構裡
找同類別、位置最接近的節點。這裡只負責解析 page source、組 XPath；讀畫面、實測在 viewer_server。

平台差異只用平台物件提供的：node_class／node_bounds、screen_label_attrs（文字放在哪些屬性）、
list_classes（清單容器的類別）。
"""

import xml.etree.ElementTree as ET

# 組出來的 XPath 裡變數的位置先用這個佔著，網頁套用時換成 ${使用者選的 KEY}
VAR_PLACEHOLDER = "{{VAR}}"


def _distance(a: dict, b: dict) -> float:
    return abs(a["x"] - b["x"]) + abs(a["y"] - b["y"]) + abs(a["width"] - b["width"]) + abs(a["height"] - b["height"])


def find_row_node(platform, root, el: dict):
    """當下畫面結構裡跟地圖這個元件同類別、位置最接近的節點；差太多（畫面不對）回傳 None"""
    target = el.get("bounds") or {}
    if not all(k in target for k in ("x", "y", "width", "height")):
        return None
    best, best_d = None, None
    for node in root.iter():
        if platform.node_class(node) != el.get("class"):
            continue
        b = platform.node_bounds(node)
        if b is None:
            continue
        d = _distance(b, target)
        if best_d is None or d < best_d:
            best, best_d = node, d
    tolerance = max(40.0, (target["width"] + target["height"]) * 0.25)
    return best if best is not None and best_d <= tolerance else None


def _text_attr(platform, node) -> tuple[str, str] | None:
    """這個節點的文字放在哪個屬性、內容是什麼（照平台的 screen_label_attrs 順序找第一個有值的）"""
    for attr in platform.screen_label_attrs:
        value = (node.attrib.get(attr) or "").strip()
        if value:
            return attr, value
    return None


def _condition(attr: str, token_mode: bool) -> str:
    if token_mode:
        # 值跟其他文字擠在同一個元件裡（iOS 常見「元大台灣50\n0050」）：前後補空白、比對完整的字，
        # 060050、0050N 這種只有一部分相同的不會中
        return f"contains(concat(' ', normalize-space(@{attr}), ' '), ' {VAR_PLACEHOLDER} ')"
    return f"@{attr}='{VAR_PLACEHOLDER}'"


def build_options(platform, page_source: str, el: dict) -> dict:
    """
    回傳 {"found": bool, "error": 說明, "row_bounds": {...}, "options": [...]}。每個 option：
      text      這段文字目前的值（網頁顯示、實測時代入）
      template  XPath，變數的位置是 VAR_PLACEHOLDER
      whole     這段文字是元件的完整內容（False 代表只是其中一個字）
    """
    try:
        root = ET.fromstring(page_source)
    except ET.ParseError as e:
        return {"found": False, "error": f"畫面結構解析失敗：{e}", "options": []}
    row = find_row_node(platform, root, el)
    if row is None:
        return {"found": False, "error": "目前畫面上找不到這一列，請確認手機停在清單有出來的畫面", "options": []}

    parents = {child: parent for parent in root.iter() for child in parent}
    # 往上找最近的清單容器，記下清單到這一列中間經過的每一層
    chain, node, list_node = [row], row, None
    while node in parents:
        node = parents[node]
        if node.tag in platform.list_classes:
            list_node = node
            break
        chain.append(node)
    if list_node is None:
        return {"found": False, "error": "這一列不在認得的清單容器裡（"
                + "、".join(platform.list_classes) + "），請改用 AI定位", "options": []}
    row_path = "//" + list_node.tag + "/" + "/".join(n.tag for n in reversed(chain))

    options, seen = [], set()
    for text_node in row.iter():
        found = _text_attr(platform, text_node)
        if found is None:
            continue
        attr, value = found
        rel = []
        n = text_node
        while n is not row:
            rel.append(n.tag)
            n = parents[n]
        rel_path = "./" + "/".join(reversed(rel)) if rel else None
        # 一段文字裡有好幾個字（以空白、換行分開）時，每個字都可以單獨當變數；整段沒有換行的話
        # 整段也列出來（例如名稱本身有空白）
        tokens = value.split()
        candidates = [(value, False)] if len(tokens) == 1 or "\n" not in value else []
        if len(tokens) > 1:
            candidates += [(t, True) for t in tokens]
        for text, token_mode in candidates:
            if not text or "'" in text or (text, rel_path) in seen:
                continue
            seen.add((text, rel_path))
            cond = _condition(attr, token_mode)
            template = f"{row_path}[{rel_path}[{cond}]]" if rel_path else f"{row_path}[{cond}]"
            options.append({"text": text, "template": template, "whole": not token_mode})
    if not options:
        return {"found": False, "error": "這一列裡沒有可以用的文字", "options": []}
    return {"found": True, "row_bounds": platform.node_bounds(row), "options": options}


def fill(template: str, value: str) -> str:
    """實測用：把 VAR_PLACEHOLDER 換成這段文字目前的值"""
    return template.replace(VAR_PLACEHOLDER, value)
