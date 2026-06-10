from ccslack.handlers.status import _build_blocks
from ccslack.session import session_manager
from ccslack.window_state_store import window_store


def _action_ids(blocks: list[dict]) -> list[str]:
    ids: list[str] = []
    for block in blocks:
        if block.get("type") == "actions":
            ids.extend(e["action_id"] for e in block["elements"])
    return ids


def test_status_message_has_file_button_with_screenshot_and_toolbar():
    window_store.window_states.clear()
    session_manager.set_window_provider("@1", "claude", cwd="/tmp")
    blocks, _ = _build_blocks("@1", "idle")
    ids = _action_ids(blocks)
    assert "ccslack_screenshot" in ids
    assert "ccslack_toolbar_open" in ids
    assert "ccslack_send_open" in ids
    assert "ccslack_archive" in ids
    window_store.window_states.clear()
