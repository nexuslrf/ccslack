from ccslack.providers.claude import ClaudeProvider
from ccslack.transcript_parser import TranscriptParser


def _asst_text(text: str, stop_reason=None) -> dict:
    message: dict = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    if stop_reason is not None:
        message["stop_reason"] = stop_reason
    return {"type": "assistant", "message": message}


def _asst_thinking(stop_reason: str = "tool_use") -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "weighing options"}],
            "stop_reason": stop_reason,
        },
    }


def _asst_tool(stop_reason: str = "tool_use") -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}],
            "stop_reason": stop_reason,
        },
    }


def _user_text(text: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _text_entries(entries):
    parsed, _ = TranscriptParser.parse_entries(entries)
    return [e for e in parsed if e.content_type == "text"]


def test_text_before_tool_use_is_commentary():
    (e,) = _text_entries([_asst_text("Let me check the repo", stop_reason="tool_use")])
    assert e.phase == "commentary"


def test_terminal_text_is_final_answer():
    (e,) = _text_entries([_asst_text("All done.", stop_reason="end_turn")])
    assert e.phase == "final_answer"


def test_stop_sequence_text_is_final_answer():
    (e,) = _text_entries([_asst_text("Done.", stop_reason="stop_sequence")])
    assert e.phase == "final_answer"


def test_missing_stop_reason_defaults_to_final_answer():
    (e,) = _text_entries([_asst_text("no stop reason here")])
    assert e.phase == "final_answer"


def test_user_text_has_no_phase():
    (e,) = _text_entries([_user_text("hi there")])
    assert e.role == "user"
    assert e.phase is None


def test_thinking_and_tool_use_have_no_phase():
    parsed, _ = TranscriptParser.parse_entries([_asst_thinking(), _asst_tool()])
    by_type = {e.content_type: e for e in parsed}
    assert by_type["thinking"].phase is None
    assert by_type["tool_use"].phase is None


def test_mixed_turn_classifies_each_text_entry():
    entries = [
        _asst_text("First I'll look around", stop_reason="tool_use"),
        _asst_tool(),
        _asst_text("Here's the answer.", stop_reason="end_turn"),
    ]
    phases = [e.phase for e in _text_entries(entries)]
    assert phases == ["commentary", "final_answer"]


def test_claude_provider_carries_phase_into_agent_message():
    entries = [
        _asst_text("narration", stop_reason="tool_use"),
        _asst_text("the answer", stop_reason="end_turn"),
    ]
    messages, _ = ClaudeProvider().parse_transcript_entries(entries, {})
    text_msgs = [m for m in messages if m.content_type == "text"]
    assert [m.phase for m in text_msgs] == ["commentary", "final_answer"]
