from ccslack.slack_formatting import to_blocks, to_mrkdwn


def test_to_mrkdwn_bold_and_links():
    out = to_mrkdwn("**hello** and [github](https://github.com)")
    assert "*hello*" in out
    assert "<https://github.com|github>" in out


def test_to_blocks_plain_text_emits_section():
    blocks, fallback = to_blocks("just a single line")
    assert len(blocks) == 1
    assert blocks[0]["type"] == "section"
    assert "just a single line" in fallback


def test_to_blocks_code_fence_splits_into_rich_text():
    text = "before\n```py\ncode here\n```\nafter"
    blocks, fallback = to_blocks(text)
    types = [b["type"] for b in blocks]
    assert "rich_text" in types
    assert "section" in types
    assert "code here" in fallback


def test_to_blocks_empty_returns_no_blocks():
    blocks, fallback = to_blocks("   \n  ")
    assert blocks == []
    assert fallback == ""
