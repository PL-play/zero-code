"""Test _tool_call_args, _validate_tool_args, _sanitize_tool_calls."""
import json
from core.agent import _tool_call_args, _validate_tool_args, _sanitize_tool_calls


def test_normal_args():
    tc = {"function": {"name": "write_file", "arguments": json.dumps({"path": "a.txt", "content": "hello"})}}
    assert _tool_call_args(tc) == {"path": "a.txt", "content": "hello"}


def test_dict_args():
    tc = {"function": {"name": "write_file", "arguments": {"path": "a.txt", "content": "hello"}}}
    assert _tool_call_args(tc) == {"path": "a.txt", "content": "hello"}


def test_malformed_args_returns_empty():
    tc = {"function": {"name": "write_file", "arguments": "not json at all"}}
    assert _tool_call_args(tc) == {}


def test_unescaped_newlines_repaired():
    raw = '{"path": "a.txt", "content": "line1\nline2"}'
    tc = {"function": {"name": "write_file", "arguments": raw}}
    result = _tool_call_args(tc)
    assert result.get("path") == "a.txt", f"got {result}"


def test_validate_missing_params():
    err = _validate_tool_args("write_file", {})
    assert "path" in err and "content" in err

    assert _validate_tool_args("write_file", {"path": "a", "content": "b"}) is None
    assert _validate_tool_args("bash", {"command": "ls"}) is None
    assert _validate_tool_args("unknown_tool", {}) is None


def test_sanitize_malformed():
    tcs = [
        {"id": "1", "function": {"name": "write_file", "arguments": "{invalid json"}},
        {"id": "2", "function": {"name": "read_file", "arguments": json.dumps({"path": "x"})}},
        {"id": "3", "function": {"name": "bash", "arguments": {"command": "ls"}}},
        {"id": "4", "function": {"name": "bash", "arguments": None}},
    ]
    sanitized = _sanitize_tool_calls(tcs)
    # malformed -> '{}'
    assert sanitized[0]["function"]["arguments"] == "{}"
    # valid string stays valid
    json.loads(sanitized[1]["function"]["arguments"])
    # dict -> serialized string
    assert isinstance(sanitized[2]["function"]["arguments"], str)
    json.loads(sanitized[2]["function"]["arguments"])
    # None -> '{}'
    assert sanitized[3]["function"]["arguments"] == "{}"


def test_sanitize_does_not_mutate_original():
    original = [{"id": "1", "function": {"name": "bash", "arguments": "{bad"}}]
    _sanitize_tool_calls(original)
    # original should be unchanged
    assert original[0]["function"]["arguments"] == "{bad"
