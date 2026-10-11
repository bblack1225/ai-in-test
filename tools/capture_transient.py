"""
捕捉「只出現一下下」的元素（例如下單後上方的委託回報跑馬燈）

Appium Inspector 按 Refresh 抓 page source 太慢，跑馬燈常常在抓到之前就消失了。
這支腳本會開一個 session，用 iOS predicate 高頻輪詢含關鍵字的元素，
一出現就把屬性、截圖、當下的 page source 存下來，之後再用這些屬性寫定位。

用法：
    1. 先在 Appium Inspector 按 Quit Session（同一台手機只能有一個 session）
    2. uv run python tools/capture_transient.py
    3. 看到「開始監聽」後，在手機上手動完成下單
    4. 抓到後結果會存在 transient_captures/ 底下

    自訂關鍵字 / 監聽秒數：
    uv run python tools/capture_transient.py --keyword 委託 --keyword 成交 --seconds 120
"""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from appium import webdriver
from appium.options.ios import XCUITestOptions
from appium.webdriver.common.appiumby import AppiumBy

from aitest.config import APPIUM_SERVER_URL, ios_capabilities

OUTPUT_DIR = Path("transient_captures")


def build_predicate(keywords: list[str]) -> str:
    """任一關鍵字出現在 name / label / value 就算命中"""
    clauses = []
    for kw in keywords:
        for attr in ("name", "label", "value"):
            clauses.append(f"{attr} CONTAINS '{kw}'")
    return " OR ".join(clauses)


def element_info(el) -> dict:
    return {
        "type": el.get_attribute("type"),
        "name": el.get_attribute("name"),
        "label": el.get_attribute("label"),
        "value": el.get_attribute("value"),
        "visible": el.get_attribute("visible"),
        "accessible": el.get_attribute("accessible"),
        "rect": el.rect,
    }


def main():
    parser = argparse.ArgumentParser(description="捕捉短暫出現的元素")
    parser.add_argument("--keyword", action="append",
                        help="要找的文字，可重複指定（預設：委託、委托、回報）")
    parser.add_argument("--seconds", type=int, default=30, help="監聽秒數（預設 90）")
    args = parser.parse_args()
    keywords = args.keyword or ["委託", "委托", "回報"]

    options = XCUITestOptions().load_capabilities(ios_capabilities())
    driver = webdriver.Remote(APPIUM_SERVER_URL, options=options)

    # 跑馬燈是持續動畫，XCUITest 預設會等 App「靜止」才查詢，會拖慢甚至卡住輪詢
    driver.update_settings({"waitForIdleTimeout": 0, "animationCoolOffTimeout": 0})

    predicate = build_predicate(keywords)
    OUTPUT_DIR.mkdir(exist_ok=True)

    # 畫面上原本就有的（例如「委託下單」按鈕）先記下來，只回報新出現的
    baseline = {
        (info["type"], info["label"])
        for info in map(element_info,
                        driver.find_elements(AppiumBy.IOS_PREDICATE, predicate))
    }
    print(f"已忽略畫面上原有的 {len(baseline)} 個符合元素")
    print(f"開始監聽 {args.seconds} 秒，關鍵字：{keywords}，現在可以去手機上下單了...")

    seen = set(baseline)
    deadline = time.time() + args.seconds
    try:
        while time.time() < deadline:
            try:
                elements = driver.find_elements(AppiumBy.IOS_PREDICATE, predicate)
                infos = [element_info(el) for el in elements]
            except Exception:
                # 元素剛好在讀屬性時消失（stale），直接下一輪
                continue

            new_infos = [i for i in infos if (i["type"], i["label"]) not in seen]
            if not new_infos:
                continue

            stamp = datetime.now().strftime("%H%M%S_%f")[:-3]
            driver.save_screenshot(str(OUTPUT_DIR / f"{stamp}.png"))
            (OUTPUT_DIR / f"{stamp}_source.xml").write_text(driver.page_source, encoding="utf-8")
            (OUTPUT_DIR / f"{stamp}_elements.json").write_text(
                json.dumps(new_infos, ensure_ascii=False, indent=2), encoding="utf-8")

            for info in new_infos:
                seen.add((info["type"], info["label"]))
                print(f"\n[{stamp}] 抓到新元素：")
                print(json.dumps(info, ensure_ascii=False, indent=2))
    finally:
        driver.quit()

    print(f"\n結束，結果在 {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
