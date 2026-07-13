from ccslack.slack_inbound import decode_slack_text


def test_autolinked_url_is_unwrapped():
    assert (
        decode_slack_text("git clone <https://github.com/Lightricks/LTX-2>")
        == "git clone https://github.com/Lightricks/LTX-2"
    )


def test_url_with_label_keeps_url():
    assert decode_slack_text("<https://repo|My Repo>") == "https://repo"


def test_entities_inside_url_are_restored():
    assert (
        decode_slack_text("see <https://x.com/a?b=1&amp;c=2>")
        == "see https://x.com/a?b=1&c=2"
    )


def test_user_mention_prefers_display_name():
    assert decode_slack_text("hi <@U123|alice>") == "hi @alice"


def test_user_mention_without_name_keeps_id():
    assert decode_slack_text("hi <@U999>") == "hi @U999"


def test_channel_mention():
    assert decode_slack_text("in <#C1|general>") == "in #general"


def test_special_mentions():
    assert decode_slack_text("<!here> hey") == "@here hey"
    assert decode_slack_text("<!subteam^S1|@team>") == "@team"


def test_mailto_link():
    assert decode_slack_text("<mailto:a@b.com|a@b.com>") == "a@b.com"


def test_literal_escaped_chars_are_restored_not_treated_as_links():
    # User literally typed `a < b && c > d`; Slack delivers entities, not spans.
    assert decode_slack_text("a &lt; b &amp;&amp; c &gt; d") == "a < b && c > d"


def test_plain_text_untouched():
    assert decode_slack_text("echo hello world") == "echo hello world"


def test_empty_string():
    assert decode_slack_text("") == ""
