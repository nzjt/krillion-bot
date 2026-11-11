import pytest

from krillion_bot.parser import parse_noillirk, parse_result

SHARE = "Krillion #58 🦐\n340\n\n🦑🦑🦑🦑🦑🐟🫧"


def test_parses_share_text():
    r = parse_result(SHARE)
    assert r is not None
    assert r.puzzle_number == 58
    assert r.score == 340
    assert r.tiers == "🦑🦑🦑🦑🦑🐟🫧"


def test_parses_with_share_link_and_surrounding_chatter():
    text = "gg everyone\n\n" + SHARE + "\nkrillion.io\n\nthat last one was brutal"
    r = parse_result(text)
    assert r is not None
    assert (r.puzzle_number, r.score) == (58, 340)


def test_parses_windows_line_endings_and_markdown():
    text = "**Krillion #7** 🦐\r\n85\r\n\r\n🫧🫧🐟🫧🫧🤡⬛"
    r = parse_result(text)
    assert r is not None
    assert (r.puzzle_number, r.score, r.tiers) == (7, 85, "🫧🫧🐟🫧🫧🤡⬛")


def test_rejects_without_emoji_row():
    r = parse_result("Krillion #12\n0")
    assert r is None


def test_parses_zero_with_blank_tiers():
    r = parse_result("Krillion #12\n0\n\n⬛⬛⬛⬛⬛⬛⬛")
    assert r is not None
    assert (r.puzzle_number, r.score, r.tiers) == (12, 0, "⬛⬛⬛⬛⬛⬛⬛")


def test_parses_max_score():
    r = parse_result("Krillion #100 🦐\n700\n\n🌟🌟🌟🌟🌟🌟🌟")
    assert r is not None
    assert r.score == 700


@pytest.mark.parametrize(
    "text",
    [
        "",
        "I love Krillion",
        "Krillion #58 was hard",  # no score line
        "Krillion #58\n\n🦑🦑🦑🦑🦑🐟🫧",  # emoji row where the score should be
        "Krillion #58\n9999",  # above the maximum possible score
        "Krillion #58\n350\n\n🦑🦑🦑🦑🦑🐟🫧",  # tier total does not match
        "Krillion #58\n340\n\n🦑🦑🦑🦑🦑🐟",  # too few tiers
        "Krillion #58\n340\n\n🦑🦑🦑🦑🦑🐟🫧⬛",  # too many tiers
        "Krillion #58\n700\n\n🏮🏮🏮🏮🏮🏮🏮",  # score/tier mismatch
        "Wordle 1,234 4/6\n\n⬛🟩⬛⬛⬛",
    ],
)
def test_rejects_non_results(text):
    assert parse_result(text) is None


@pytest.mark.parametrize(
    "header",
    [
        "Noillirk: The Daily Climb | No. 46 - 12,345m",
        "Noillirk: The Daily Climb | No. 46 - [height]",
        "noillirk: the daily climb | no. 46 - 99999 m",
        "Noillirk Daily #46",
    ],
)
def test_noillirk_share_headers(header):
    squares = "🟨🟥🟧🟩🟦⬜⬛"
    result = parse_noillirk(f"{header}\n\n{squares}")
    assert result is not None
    assert (result.puzzle_number, result.score, result.tiers) == (46, 224, squares)


def test_noillirk_new_header_with_markdown_and_windows_newlines():
    text = "**Noillirk: The Daily Climb | No. 46 - 12345m**\r\n\r\n🟨🟥🟧🟩🟦⬜⬛"
    result = parse_noillirk(text)
    assert result is not None
    assert (result.puzzle_number, result.score) == (46, 224)


@pytest.mark.parametrize(
    "text",
    [
        "Noillirk: The Daily Climb | No. 46 - 12345m",
        "Noillirk: The Daily Climb | No. 46 - 12345m\n\n🟨🟥🟧🟩🟦⬜",
        "Noillirk: The Daily Climb | No. 46 - 12345m\n\n🟨🟥🟧🟩🟦⬜⬛⬛",
        "Noillirk: The Daily Climb | No. - 46m\n\n🟨🟥🟧🟩🟦⬜⬛",
    ],
)
def test_noillirk_new_header_rejects_incomplete_share(text):
    assert parse_noillirk(text) is None
