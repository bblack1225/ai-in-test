"""setup_logging：log 每行帶日期時間到毫秒，同時寫到 logs/<名稱>-<時間>.log（報告 #13）"""

import logging
import re

import pytest

from aitest import log

LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} (\w+)\s+(.*)$")


@pytest.fixture
def clean_logging(monkeypatch, tmp_path):
    # setup_logging 改的是全域的 root logger，測完還原，不影響其他測試
    root = logging.getLogger()
    saved = (list(root.handlers), root.level,
             {n: logging.getLogger(n).level for n in ("aitest", "__main__")})
    monkeypatch.setattr(log, "LOG_DIR", tmp_path / "logs")
    yield tmp_path / "logs"
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in saved[0]:
        root.addHandler(handler)
    root.setLevel(saved[1])
    for name, level in saved[2].items():
        logging.getLogger(name).setLevel(level)


def _lines(path):
    return [LINE.match(line).groups() for line in path.read_text(encoding="utf-8").splitlines()]


def test_file_has_date_time_with_milliseconds(clean_logging):
    path = log.setup_logging("server")
    assert path.parent == clean_logging and path.name.startswith("server-")
    logging.getLogger("aitest.core.x").info("開始掃描畫面")
    logging.getLogger("__main__").warning("Appium session 已失效")
    lines = _lines(path)
    assert ("INFO", "開始掃描畫面") in lines
    assert ("WARNING", "Appium session 已失效") in lines


def test_console_has_timestamp_and_no_file_without_name(clean_logging, capsys):
    assert log.setup_logging() is None
    logging.getLogger("aitest.core.x").info("哈囉")
    out = capsys.readouterr().out.splitlines()
    assert [LINE.match(line).groups() for line in out] == [("INFO", "哈囉")]
    assert not clean_logging.exists()


def test_third_party_only_warnings(clean_logging):
    path = log.setup_logging("crawl")
    logging.getLogger("urllib3").info("Starting new HTTP connection")
    logging.getLogger("urllib3").warning("Retrying")
    messages = [m for _, m in _lines(path)]
    assert "Retrying" in messages
    assert "Starting new HTTP connection" not in messages


def test_setup_twice_does_not_duplicate_lines(clean_logging, capsys):
    log.setup_logging()
    log.setup_logging()
    logging.getLogger("aitest").info("只印一次")
    assert capsys.readouterr().out.count("只印一次") == 1
