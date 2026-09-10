from echelon_engine.codex_bridge import _translated_decision


TOOLS = [{"name": "Bash", "input_schema": {"type": "object"}}, {"name": "Read", "input_schema": {"type": "object"}}]


def test_translates_valid_claude_tool_use():
    text, calls = _translated_decision('{"type":"tool_use","tool_uses":[{"name":"Bash","input":{"command":"git status"}}]}', TOOLS)
    assert text == ""
    assert calls[0]["name"] == "Bash"
    assert calls[0]["input"] == {"command": "git status"}
    assert calls[0]["id"].startswith("toolu_")


def test_rejects_unknown_or_invalid_tool_calls_as_plain_text():
    raw = '{"type":"tool_use","name":"Destroy","input":{}}'
    assert _translated_decision(raw, TOOLS) == (raw, [])


def test_translates_final_response():
    assert _translated_decision('{"type":"final","text":"done"}', TOOLS) == ("done", [])
