"""
log 設定：各模組用 logging.getLogger(__name__) 寫 log，程式進入點（main）呼叫一次 setup_logging()。

- 每一行都帶日期時間到毫秒：2026-10-16 14:03:22.123 INFO    訊息
- 終端機和 logs/<名稱>-<時間>.log 同時輸出（有給名稱才寫檔），跟 Appium 的 log 一樣每次啟動一個檔
- handler 掛在 root：aitest 的 logger 記 INFO 以上；selenium、urllib3、google-genai 這些套件只記警告以上，
  不然它們的 debug 訊息會洗版。用 python -m 執行時模組名稱是 __main__，所以 __main__ 也記 INFO

給人看的指令輸出（aitest doctor、workspace、compare_versions 的報告）維持 print，不走這裡。
"""

import logging
import sys
import time
from pathlib import Path

# repo 根目錄的 logs/（跟 Appium 的 log 放一起）；不 import config，讓 platforms/ 也能放心用
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(name: str | None = None) -> Path | None:
    """name：log 檔名開頭（server、crawl…），有給就同時寫到 logs/<name>-<時間>.log 並回傳路徑。
    重複呼叫會先拿掉上次加的 handler，不會每行印兩次。"""
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    for logger_name in ("aitest", "__main__"):
        logging.getLogger(logger_name).setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter(FORMAT, DATE_FORMAT)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)
    if name is None:
        return None
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"{name}-{time.strftime('%Y%m%d_%H%M%S')}.log"
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    logging.getLogger(__name__).info(f"log 檔：{path.relative_to(LOG_DIR.parent)}")
    return path
