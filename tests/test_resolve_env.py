"""run_test_case.resolve_env_placeholders：測案裡的 ${KEY} 執行時換成變數檔（ctx.vars）或環境變數的值"""

from aitest.core.run_test_case import missing_vars_outcome, resolve_env_placeholders
from aitest.run_context import RunContext


def test_no_placeholder():
    ctx = RunContext()
    assert resolve_env_placeholders(ctx, "2330") == ("2330", [])
    assert resolve_env_placeholders(ctx, None) == ("", [])


def test_run_vars_take_priority_over_env(monkeypatch):
    monkeypatch.setenv("LOGIN_ID", "from-env")
    ctx = RunContext()
    ctx.set_vars({"LOGIN_ID": "from-vars"}, ["帳號A"])
    assert resolve_env_placeholders(ctx, "${LOGIN_ID}") == ("from-vars", [])


def test_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("STOCK_CODE", "2882")
    assert resolve_env_placeholders(RunContext(), "代號 ${STOCK_CODE}") == ("代號 2882", [])


def test_missing_vars_left_as_is_and_reported(monkeypatch):
    monkeypatch.delenv("NOT_SET_1", raising=False)
    monkeypatch.delenv("NOT_SET_2", raising=False)
    ctx = RunContext(vars={"LOGIN_ID": "A123"})
    value, missing = resolve_env_placeholders(ctx, "${LOGIN_ID}/${NOT_SET_1}/${NOT_SET_2}")
    assert value == "A123/${NOT_SET_1}/${NOT_SET_2}"
    assert missing == ["NOT_SET_1", "NOT_SET_2"]


def test_invalid_names_not_replaced():
    assert resolve_env_placeholders(RunContext(), "${1ABC} $LOGIN_ID") == ("${1ABC} $LOGIN_ID", [])


def test_each_run_has_its_own_vars(monkeypatch):
    """原本變數放在全域，兩次執行會互相蓋掉；現在各自一份"""
    monkeypatch.delenv("LOGIN_ID", raising=False)
    a = RunContext(vars={"LOGIN_ID": "帳號A"}, var_sources=["A"])
    b = RunContext(vars={"LOGIN_ID": "帳號B"}, var_sources=["B"])
    assert resolve_env_placeholders(a, "${LOGIN_ID}")[0] == "帳號A"
    assert resolve_env_placeholders(b, "${LOGIN_ID}")[0] == "帳號B"
    assert "這次選的變數檔：B" in missing_vars_outcome(b, ["X"])
