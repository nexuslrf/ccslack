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
