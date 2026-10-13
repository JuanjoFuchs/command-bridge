"""Spec 039 — say acronyms, money, dates, times and temperatures the way a person would.

Measured 2026-10-09 on the engine he listens to (kokoro, bm_daniel), with Parakeet as the judge and
the tokenizer's own phonemes as the second witness:

    "AWS"      -> phonemes ˈɔːz            (a word, "awz"); "CLI" -> klˈaɪ; "ASR" -> ˈasə; "CI" -> sˈaɪ
    "$225"     -> heard "dollar 225" at 1.2x, and "still two hundred..." at 2.0x (the dollar gone)
    "10/24"    -> heard "ten slash twenty four"
    "72°F"     -> heard "72 degrees F"

STRING LOGIC ONLY (no model, no audio): the audible half is `scripts/speechcheck.py`. Every rule has
a counter-case where it must NOT fire, because an over-firing rule is silent (spec 008 TC2).
"""
import pytest

from command_bridge import speech

N = speech.normalize_for_speech


# ------------------------------------------------------------------------------------ acronyms

@pytest.mark.parametrize("text,want", [
    ("The AWS bill.", "The A-W-S bill."),
    ("Run the CLI now.", "Run the C-L-I now."),
    ("QA and UX are blocked.", "Q-A and U-X are blocked."),
    ("The ASR model.", "The A-S-R model."),
    ("Check CI first.", "Check C-I first."),
    ("Build the APK.", "Build the A-P-K."),
    ("An API call.", "An A-P-I call."),
    ("Four HTTP calls.", "Four H-T-T-P calls."),        # 4+ letters with no vowel: an acronym
    ("Two PRs merged.", "Two P-R's merged."),          # P-Rs reads the s as the letter "ess"
])
def test_runs_of_capitals_are_spelled_out(text, want):
    assert N(text) == want


@pytest.mark.parametrize("text", [
    "Do NOT push.",          # emphasis, not an acronym
    "It is DONE now.",       # a 4+ letter word in capitals is read as the word
    "OK, go ahead.",         # said as a word
    "I think so.",           # a single capital
    "Read the JSON file.",   # said as a word ("jason")
    "The LAN is up.",        # said as a word
    "NASA launched.",
    "Ask JJ.",               # a name said as letters is fine either way; JJ is two letters...
])
def test_capitalised_words_that_are_words_are_left_alone(text):
    if text == "Ask JJ.":
        assert N(text) == "Ask J-J."     # ...and spelling it is what a person does
        return
    assert N(text) == text


def test_mixed_case_and_identifiers_are_not_acronyms():
    assert N("Use iOS and macOS.") == "Use iOS and macOS."
    assert N("Set COMMAND_BRIDGE_TTS now.") == "Set COMMAND_BRIDGE_TTS now."   # an identifier


# ---------------------------------------------------------------------------------------- money

@pytest.mark.parametrize("text,want", [
    ("It costs $225.", "It costs two hundred twenty five dollars."),
    ("It costs $3.50 today.", "It costs three dollars and fifty cents today."),
    ("Only $1.", "Only one dollar."),
    ("Just $0.99.", "Just ninety nine cents."),
    ("About $1,200 a month.", "About one thousand two hundred dollars a month."),
    ("Spent $3k.", "Spent three thousand dollars."),
    ("Raised $2.5M.", "Raised two point five million dollars."),
])
def test_money_is_read_as_money(text, want):
    assert N(text) == want


# ---------------------------------------------------------------------------------------- dates

@pytest.mark.parametrize("text,want", [
    ("On 10/24 we ship.", "On October twenty fourth we ship."),
    ("Due 3/15.", "Due March fifteenth."),
    ("Since 10/9/2026.", "Since October ninth, twenty twenty six."),
    ("Since 1/2/26.", "Since January second, twenty twenty six."),
])
def test_dates_are_read_as_dates(text, want):
    assert N(text) == want


@pytest.mark.parametrize("text", [
    "Three of them, 3/4 done.",      # a fraction, not March fourth
    "Half is 1/2.",
    "Path src/app.",
])
def test_a_slash_that_is_not_clearly_a_date_is_left_alone(text):
    out = N(text)
    assert "March" not in out and "January" not in out


# ---------------------------------------------------------------------------------------- times

@pytest.mark.parametrize("text,want", [
    ("The call is at 6:20 PM.", "The call is at six twenty P-M."),
    ("At 9:05 am.", "At nine oh five A-M."),
    ("At 6:00 PM.", "At six P-M."),
    ("At 18:30.", "At eighteen thirty."),
    ("Meet at 9 AM.", "Meet at nine A-M."),            # no colon: "AM" alone would read "am"
    ("Before 5pm.", "Before five P-M."),
])
def test_times_are_read_as_times(text, want):
    assert N(text) == want


# ---------------------------------------------------------------------------------- temperatures

@pytest.mark.parametrize("text,want", [
    ("It is 72°F outside.", "It is seventy two degrees Fahrenheit outside."),
    ("About 22°C.", "About twenty two degrees Celsius."),
    ("A low of -5°C.", "A low of minus five degrees Celsius."),
    ("Bake at 350°F (177°C).", "Bake at three hundred fifty degrees Fahrenheit (one hundred seventy seven degrees Celsius)."),
])
def test_temperatures_are_read_with_their_scale(text, want):
    assert N(text) == want


# --------------------------------------------------------------------- spec 008 still holds

def test_versions_and_identifiers_still_read_as_before():
    assert N("The version is 0.2.6 and it is ready.") == "The version is zero point two point six and it is ready."
    assert N("Open config.py.") == "Open config dot py."
