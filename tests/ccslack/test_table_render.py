import pytest

from ccslack.config import config
from ccslack.handlers import table_render
from ccslack.handlers.table_render import (
    find_table_blocks,
    maybe_offer_table_render,
    render_tables_png,
)
from ccslack.slack_client import FakeSlackClient

_TABLE = """Here are the results:

| Name | Score | Notes |
|------|------:|:-----:|
| foo  | 12    | ok    |
| bar  | 7     | retry |

That's the summary.
"""


def _action_ids(blocks: list[dict]) -> list[str]:
    ids: list[str] = []
    for block in blocks:
        if block.get("type") == "actions":
            ids.extend(e["action_id"] for e in block["elements"])
    return ids


def test_detects_a_basic_table():
    blocks = find_table_blocks(_TABLE)
    assert len(blocks) == 1
    assert "| Name | Score | Notes |" in blocks[0]
    assert "| bar  | 7     | retry |" in blocks[0]


def test_horizontal_rule_is_not_a_table():
    assert find_table_blocks("Some text\n\n---\n\nMore text") == []


def test_header_without_data_rows_ignored():
    assert find_table_blocks("| a | b |\n| - | - |\n") == []


def test_table_inside_code_fence_is_skipped():
    text = "```\n| a | b |\n| - | - |\n| 1 | 2 |\n```"
    assert find_table_blocks(text) == []


def test_detects_multiple_tables():
    text = (
        "| a | b |\n| - | - |\n| 1 | 2 |\n\n"
        "intro\n\n"
        "| x | y |\n| - | - |\n| 9 | 8 |\n"
    )
    assert len(find_table_blocks(text)) == 2


def test_table_without_outer_pipes():
    text = "col1 | col2\n---- | ----\nv1 | v2"
    blocks = find_table_blocks(text)
    assert len(blocks) == 1


@pytest.mark.asyncio
async def test_render_tables_png_produces_png(monkeypatch):
    captured = {}

    async def _fake_text_to_image(text, with_ansi=True, **kw):  # noqa: ANN001, ARG001
        captured["text"] = text
        return b"\x89PNG\r\n\x1a\nFAKE"

    monkeypatch.setattr(
        "ccslack.screenshot.text_to_image", _fake_text_to_image
    )

    png = await render_tables_png(find_table_blocks(_TABLE))
    assert png == b"\x89PNG\r\n\x1a\nFAKE"
    # The monospace layout uses box-drawing borders and keeps the cells.
    assert "┌" in captured["text"]
    assert "Name" in captured["text"]
    assert "retry" in captured["text"]


@pytest.mark.asyncio
async def test_offer_posts_button_when_table_present(monkeypatch):
    monkeypatch.setattr(config, "table_render_offer", True)
    client = FakeSlackClient()
    client.returns["chat_postMessage"] = {"ok": True, "ts": "1.1"}

    await maybe_offer_table_render(client, "C1", _TABLE)

    msg = client.last_call("chat_postMessage")
    assert msg is not None
    ids = _action_ids(msg.kwargs["blocks"])
    assert "ccslack_render_table" in ids
    assert "ccslack_render_table_dismiss" in ids


@pytest.mark.asyncio
async def test_offer_silent_without_table(monkeypatch):
    monkeypatch.setattr(config, "table_render_offer", True)
    client = FakeSlackClient()
    await maybe_offer_table_render(client, "C1", "no tables here, just prose")
    assert client.call_count("chat_postMessage") == 0


@pytest.mark.asyncio
async def test_offer_suppressed_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "table_render_offer", False)
    client = FakeSlackClient()
    await maybe_offer_table_render(client, "C1", _TABLE)
    assert client.call_count("chat_postMessage") == 0


def test_offer_remembers_blocks_under_token(monkeypatch):
    # The posted token must resolve back to the detected blocks for the click.
    table_render._PENDING.clear()
    blocks = find_table_blocks(_TABLE)
    table_render._remember("tok123", "C1", blocks)
    assert table_render._PENDING["tok123"] == ("C1", blocks)


def test_markdown_to_table_block_basic():
    from ccslack.handlers.table_render import markdown_to_table_block

    block = (
        "| Name | Score |\n"
        "|:-----|------:|\n"
        "| alice | 42 |\n"
        "| bob | 7 |"
    )
    table = markdown_to_table_block(block)
    assert table is not None
    assert table["type"] == "table"
    assert table["rows"][0][0] == {"type": "raw_text", "text": "Name"}
    assert table["rows"][1][1] == {"type": "raw_number", "value": 42.0, "text": "42"}
    assert table["column_settings"][1] == {"align": "right"}


def test_markdown_to_table_block_aligns():
    from ccslack.handlers.table_render import markdown_to_table_block

    block = (
        "| a | b | c |\n"
        "|:--|:-:|--:|\n"
        "| 1 | 2 | 3 |"
    )
    table = markdown_to_table_block(block)
    assert table["column_settings"] == [
        {"align": "left"},
        {"align": "center"},
        {"align": "right"},
    ]


def test_markdown_to_table_block_negative_and_float_numbers():
    from ccslack.handlers.table_render import markdown_to_table_block

    block = "| v |\n|---|\n| -5 |\n| 3.14 |"
    table = markdown_to_table_block(block)
    assert table["rows"][1][0]["type"] == "raw_number"
    assert table["rows"][1][0]["value"] == -5.0
    assert table["rows"][2][0]["value"] == 3.14


def test_markdown_to_table_block_text_cells():
    from ccslack.handlers.table_render import markdown_to_table_block

    block = "| v |\n|---|\n| hello world |"
    table = markdown_to_table_block(block)
    assert table["rows"][1][0] == {"type": "raw_text", "text": "hello world"}


def test_markdown_to_table_block_too_many_rows():
    from ccslack.handlers.table_render import markdown_to_table_block

    rows = "\n".join(f"| {i} |" for i in range(150))
    block = f"| v |\n|---|\n{rows}"
    assert markdown_to_table_block(block) is None


def test_markdown_to_table_block_char_limit():
    from ccslack.handlers.table_render import markdown_to_table_block

    cell = "x" * 100
    rows = "\n".join(f"| {cell} | {cell} |" for _ in range(60))
    block = f"| a | b |\n|---|---|\n{rows}"
    assert markdown_to_table_block(block) is None


def test_split_single_table_in_place():
    from ccslack.handlers.table_render import split_into_table_messages

    text = (
        "Here is the summary:\n\n"
        "| a | b |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
        "\nThat's all."
    )
    msgs = split_into_table_messages(text)
    assert msgs is not None
    # One message: pre-text section + table block. Trailing text is its own msg.
    assert len(msgs) == 2
    first_blocks = msgs[0][0]
    assert any(b["type"] == "table" for b in first_blocks)
    assert any(b["type"] == "section" for b in first_blocks)
    assert msgs[1][0][0]["type"] == "section"  # trailing text


def test_split_multiple_tables_one_per_message():
    from ccslack.handlers.table_render import split_into_table_messages

    text = (
        "Intro text.\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "Middle text.\n\n"
        "| c | d |\n|---|---|\n| 3 | 4 |\n\n"
        "Outro text."
    )
    msgs = split_into_table_messages(text)
    assert msgs is not None
    assert len(msgs) == 3  # table1+intro, table2+middle, outro
    # Each of the first two messages has exactly one table block.
    for blocks, _ in msgs[:2]:
        tables = [b for b in blocks if b["type"] == "table"]
        assert len(tables) == 1
        assert any(b["type"] == "section" for b in blocks)
    # Final message is text-only.
    assert all(b["type"] != "table" for b in msgs[2][0])


def test_split_no_table_returns_none():
    from ccslack.handlers.table_render import split_into_table_messages

    assert split_into_table_messages("just plain text") is None


def test_split_oversized_table_returns_none():
    from ccslack.handlers.table_render import split_into_table_messages

    cell = "x" * 100
    rows = "\n".join(f"| {cell} | {cell} |" for _ in range(60))
    text = f"| a | b |\n|---|---|\n{rows}"
    assert split_into_table_messages(text) is None


def test_split_table_only_no_trailing():
    from ccslack.handlers.table_render import split_into_table_messages

    text = "| a | b |\n|---|---|\n| 1 | 2 |"
    msgs = split_into_table_messages(text)
    assert msgs is not None
    assert len(msgs) == 1  # no trailing text message
    assert msgs[0][0][0]["type"] == "table"


def test_table_cell_empty_becomes_space():
    from ccslack.handlers.table_render import _table_cell

    # Slack raw_text requires minLength 1 — empty cells must not be "".
    assert _table_cell("") == {"type": "raw_text", "text": " "}
    assert _table_cell("   ") == {"type": "raw_text", "text": " "}


def test_table_cell_strips_markdown_artifacts():
    from ccslack.handlers.table_render import _table_cell

    assert _table_cell("**bold**") == {"type": "raw_text", "text": "bold"}
    assert _table_cell("`code`") == {"type": "raw_text", "text": "code"}
    assert _table_cell("*ital*") == {"type": "raw_text", "text": "ital"}
    assert _table_cell("_ital_") == {"type": "raw_text", "text": "ital"}


def test_table_with_empty_header_cell_renders():
    from ccslack.handlers.table_render import markdown_to_table_block

    block = (
        "| | sweep | datamix |\n"
        "|---|---:|---:|\n"
        "| quality `n` | 1044 | 1500 |"
    )
    table = markdown_to_table_block(block)
    assert table is not None
    # The empty header cell became a single space, not "".
    assert table["rows"][0][0] == {"type": "raw_text", "text": " "}


def test_short_row_padded_with_space_cells():
    from ccslack.handlers.table_render import markdown_to_table_block

    block = "| a | b |\n|---|---|\n| only-one |"
    table = markdown_to_table_block(block)
    assert table is not None
    # The short row is padded to 2 cols with a space cell (not "").
    assert table["rows"][1][1] == {"type": "raw_text", "text": " "}
