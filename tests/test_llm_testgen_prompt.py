"""測案編輯（/llm_chat）的系統提示詞：送出前補齊輸入欄位、xpath_vars 的規則有帶到"""

from aitest import viewer_server
from aitest.platforms import get_platform


def test_prompt_has_input_and_xpath_vars_rules(monkeypatch):
    monkeypatch.setattr(viewer_server, "PLATFORM", get_platform("android"))
    screens = '[{"screen_name": "s", "elements": [{"unique_id": "s::gen::1", "xpath_vars": ["STOCK_CODE"]}]}]'
    prompt = viewer_server._build_llm_testgen_system_prompt(screens, {})
    assert "送出前補齊輸入欄位" in prompt
    assert "xpath_vars 的元件" in prompt and "見規則9" in prompt
    assert '"xpath_vars": ["STOCK_CODE"]' in prompt
    assert "${STOCK_CODE}" in prompt  # f-string 的大括號有正確跳脫
