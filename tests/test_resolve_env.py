"""run_test_case.resolve_env_placeholders：測案裡的 ${KEY} 執行時換成變數檔或環境變數的值"""

import pytest

from run_test_case import resolve_env_placeholders, set_run_vars


@pytest.fixture(autouse=True)
def _clear_run_vars():
    set_run_vars(None)
    yield
    set_run_vars(None)


def test_no_placeholder():
    assert resolve_env_placeholders("2330") == ("2330", [])
    assert resolve_env_placeholders(None) == ("", [])


def test_run_vars_take_priority_over_env(monkeypatch):
    monkeypatch.setenv("LOGIN_ID", "from-env")
    set_run_vars({"LOGIN_ID": "from-vars"}, ["帳號A"])
    assert resolve_env_placeholders("${LOGIN_ID}") == ("from-vars", [])


def test_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("STOCK_CODE", "2882")
    assert resolve_env_placeholders("代號 ${STOCK_CODE}") == ("代號 2882", [])


def test_missing_vars_left_as_is_and_reported(monkeypatch):
    monkeypatch.delenv("NOT_SET_1", raising=False)
    monkeypatch.delenv("NOT_SET_2", raising=False)
    set_run_vars({"LOGIN_ID": "A123"})
    value, missing = resolve_env_placeholders("${LOGIN_ID}/${NOT_SET_1}/${NOT_SET_2}")
    assert value == "A123/${NOT_SET_1}/${NOT_SET_2}"
    assert missing == ["NOT_SET_1", "NOT_SET_2"]


def test_invalid_names_not_replaced():
    assert resolve_env_placeholders("${1ABC} $LOGIN_ID") == ("${1ABC} $LOGIN_ID", [])
