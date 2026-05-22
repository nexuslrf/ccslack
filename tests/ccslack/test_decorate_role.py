from ccslack.handlers.messaging_pipeline.message_routing import _decorate
from ccslack.session_monitor import NewMessage


def _msg(text: str, *, role: str = "assistant", content_type: str = "text", **kw):
    return NewMessage(
        session_id="sess-1",
        text=text,
        is_complete=True,
        content_type=content_type,
        role=role,
        **kw,
    )


def test_user_role_gets_silhouette_prefix():
    out = _decorate(_msg("ls", role="user"), "ls")
    assert out.startswith(":bust_in_silhouette: ")
    assert out.endswith("ls")


def test_assistant_role_keeps_plain_text():
    out = _decorate(_msg("here is the answer"), "here is the answer")
    assert out == "here is the answer"
    assert ":bust_in_silhouette:" not in out


def test_user_role_truncates_long_text():
    long = "x" * 5000
    out = _decorate(_msg(long, role="user"), long)
    assert out.startswith(":bust_in_silhouette: ")
    assert out.endswith("…")
    assert len(out) < 5050  # prefix + 3000 chars + ellipsis budget


def test_user_role_wins_over_content_type():
    # A user-role 'thinking' would be unusual but role should still take precedence.
    out = _decorate(
        _msg("musing", role="user", content_type="thinking"), "musing"
    )
    assert out.startswith(":bust_in_silhouette: ")
    assert ":thought_balloon:" not in out


def test_assistant_thinking_keeps_thought_balloon():
    out = _decorate(_msg("hmm", content_type="thinking"), "hmm")
    assert out.startswith(":thought_balloon: ")


def test_assistant_tool_use_keeps_wrench():
    msg = _msg("(args)", content_type="tool_use", tool_name="Bash")
    out = _decorate(msg, "(args)")
    assert out.startswith(":wrench: *Bash*")
