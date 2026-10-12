from ccslack.providers.codex import CodexProvider

_ANSWER = "Here is the final answer with a summary."


def _agent_message(text: str) -> dict:
    return {"type": "event_msg", "payload": {"type": "agent_message", "message": text}}


def _task_complete(text: str) -> dict:
    return {
        "type": "event_msg",
        "payload": {"type": "task_complete", "last_agent_message": text},
    }


def _user(text: str) -> dict:
    return {"type": "input_item", "payload": {"role": "user", "content": text}}


def _texts(messages) -> list[str]:
    return [m.text for m in messages if m.content_type == "text"]


def test_agent_message_and_task_complete_dedup_in_one_batch():
    provider = CodexProvider()
    msgs, _ = provider.parse_transcript_entries(
        [_agent_message(_ANSWER), _task_complete(_ANSWER)], {}
    )
    assert _texts(msgs) == [_ANSWER]
    # The surviving copy carries the final_answer phase.
    final = next(m for m in msgs if m.content_type == "text")
    assert final.phase == "final_answer"


def test_dedup_survives_split_across_incremental_reads():
    provider = CodexProvider()
    # Poll 1 sees only the agent_message; poll 2 sees the task_complete repeat.
    batch1, pending = provider.parse_transcript_entries([_agent_message(_ANSWER)], {})
    batch2, _ = provider.parse_transcript_entries([_task_complete(_ANSWER)], pending)

    assert _texts(batch1) == [_ANSWER]
    assert _texts(batch2) == []  # the repeat is dropped, not re-posted


def test_identical_answer_in_a_new_turn_is_not_dropped():
    provider = CodexProvider()
    batch1, pending = provider.parse_transcript_entries(
        [_agent_message(_ANSWER), _task_complete(_ANSWER)], {}
    )
    # A new human turn, then the agent happens to repeat the exact same text.
    batch2, _ = provider.parse_transcript_entries(
        [_user("do it again"), _agent_message(_ANSWER)], pending
    )

    assert _texts(batch1) == [_ANSWER]
    assert _ANSWER in _texts(batch2)  # legitimately repeated, kept


def test_empty_tool_result_suppressed():
    """Empty exec results must NOT emit a message (the old bare "Done" was the
    recurring channel noise — one per exec call)."""
    from ccslack.providers.codex import CodexProvider

    p = CodexProvider()
    entries = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c1",
                "arguments": '{"cmd":"ls"}',
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "",  # empty → suppressed
            },
        },
    ]
    msgs, _ = p.parse_transcript_entries(entries, pending_tools={})
    # tool_use forwards; the empty result emits nothing (no "Done").
    assert len(msgs) == 1
    assert msgs[0].content_type == "tool_use"
    assert "exec_command" in msgs[0].text


def test_nonempty_tool_result_still_forwards():
    from ccslack.providers.codex import CodexProvider

    p = CodexProvider()
    entries = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c2",
                "arguments": '{"cmd":"ls"}',
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c2",
                "output": "file_a\nfile_b\nfile_c",
            },
        },
    ]
    msgs, _ = p.parse_transcript_entries(entries, pending_tools={})
    results = [m for m in msgs if m.content_type == "tool_result"]
    assert len(results) == 1
    assert "file_a" in results[0].text


def test_literal_done_output_still_forwards():
    """A command whose REAL output is the word "Done" still forwards — only
    genuinely empty results are suppressed."""
    from ccslack.providers.codex import CodexProvider

    p = CodexProvider()
    entries = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c3",
                "arguments": '{"cmd":"echo Done"}',
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c3",
                "output": "Done",
            },
        },
    ]
    msgs, _ = p.parse_transcript_entries(entries, pending_tools={})
    results = [m for m in msgs if m.content_type == "tool_result"]
    assert len(results) == 1
