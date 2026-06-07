from ccslack.handlers.new_modal import build_new_session_view


def _checkbox_values(view: dict) -> set[str]:
    values: set[str] = set()
    for block in view["blocks"]:
        element = block.get("element", {})
        if element.get("type") != "checkboxes":
            continue
        for option in element.get("options", []):
            values.add(option.get("value"))
    return values


def test_modal_offers_worktree_and_yolo():
    view = build_new_session_view(default_provider="claude", private_metadata="C0META")
    assert _checkbox_values(view) == {"worktree", "yolo"}


def test_modal_defaults_unknown_provider_to_claude():
    view = build_new_session_view(default_provider="bogus", private_metadata="C0META")
    provider_block = next(
        b for b in view["blocks"] if b.get("block_id") == "provider_block"
    )
    initial = provider_block["element"]["initial_option"]["value"]
    assert initial == "claude"
