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


def test_to_blocks_long_text_chunks_without_truncation():
    # A single 10k-char run with no newlines must spread across section blocks,
    # not get clipped with an ellipsis.
    body = "x" * 10000
    blocks, _ = to_blocks(body)
    assert len(blocks) > 1
    joined = "".join(
        b["text"]["text"] for b in blocks if b["type"] == "section"
    )
    assert joined == body  # content preserved exactly
    assert all(len(b["text"]["text"]) <= 3000 for b in blocks if b["type"] == "section")


def test_to_blocks_long_code_chunks_without_truncation():
    code = "C" * 7000
    blocks, _ = to_blocks(f"```\n{code}\n```")
    rich = [b for b in blocks if b["type"] == "rich_text"]
    assert len(rich) > 1
    joined = "".join(
        el["elements"][0]["text"]
        for b in rich
        for el in b["elements"]
    )
    assert joined.count("C") == 7000  # every code char preserved, none truncated
    assert "…" not in joined


def test_header_h1_becomes_bold():
    from ccslack.slack_formatting import to_mrkdwn

    assert to_mrkdwn("# Title") == "*Title*"


def test_header_h2_h6_become_bold():
    from ccslack.slack_formatting import to_mrkdwn

    assert to_mrkdwn("## Subtitle") == "*Subtitle*"
    assert to_mrkdwn("### Deep") == "*Deep*"
    assert to_mrkdwn("###### Deepest") == "*Deepest*"


def test_header_inline_in_paragraph():
    from ccslack.slack_formatting import to_mrkdwn

    text = "intro\n\n## Results\n\ndone"
    assert "*Results*" in to_mrkdwn(text)
    assert "##" not in to_mrkdwn(text)


def test_hashtag_without_space_stays_literal():
    from ccslack.slack_formatting import to_mrkdwn

    assert to_mrkdwn("#hashtag") == "#hashtag"
    assert to_mrkdwn("a # b") == "a # b"


def test_header_bold_combined():
    from ccslack.slack_formatting import to_mrkdwn

    # Header containing bold: "# **Big** Title" → "*Big Title*"
    assert to_mrkdwn("# **Big** Title") == "*Big Title*"


def test_header_trailing_whitespace_trimmed():
    from ccslack.slack_formatting import to_mrkdwn

    assert to_mrkdwn("# Title  ") == "*Title*"
