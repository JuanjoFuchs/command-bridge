"""Spec 008 — say technical terms as terms, and breathe where the meaning breaks.

**The defect, measured on the engine he is actually listening to (kokoro/bm_daniel/1.2), not
assumed.** The dot in `0.2.6` is not turned into a pause. It is DROPPED: the recognizer hears
`026`, and `1.0.0` comes back as "one hundred". A real sentence boundary measured 990 ms of
silence; a dotted term measured none at all. That is worse than the original report described —
a pause is a term arriving awkwardly, an elision is a term arriving **wrong**, with nothing in
the sound to signal that anything was lost.

**Why this file is mostly counter-cases.** Over-normalising is the failure that costs more than
the bug, because it is SILENT (TC2): a rule that fires on an ellipsis, on `e.g.`, on a
sentence-final period or on `$3.50` changes what he hears with nothing to say it did. So every
rule below is paired with the case that proves it does NOT fire, and the pairing is the point —
a rule with no counter-case is a rule you cannot tell is over-firing.

Everything here is STRING LOGIC. No model, no audio, no server (TC4 — a live session is running).
The audible half — synthesize, transcribe, assert the recognizer hears `0.2.6` — is the
acceptance harness's job, deliberately, because the agent that produced the audio must not be the
judge of it (TC1).
"""
import pytest

from tests.test_cli_surface import run
from voice_tunnel import cli, speech, timing, tts

# ============================================================ AC1: the FR2 table, class by class


@pytest.mark.parametrize("text, expected", [
    # version / dotted number -> "point". Three or more parts is a version, so every part is read
    # as a whole number: 1.10.2 is "one point ten point two", never "one point one zero point two".
    ("0.2.6", "zero point two point six"),
    ("1.0.0", "one point zero point zero"),
    ("1.10.2", "one point ten point two"),
    # two parts is a decimal, and English reads the fraction digit by digit
    ("3.5", "three point five"),
    ("3.14", "three point one four"),
    ("0.85", "zero point eight five"),
    # a leading zero inside a part survives, because dropping it loses a digit — the same class of
    # failure as the elision this whole spec exists to fix
    ("1.02.3", "one point zero two point three"),
])
def test_a_dotted_number_is_voiced_with_point(text, expected):
    assert speech.normalize_for_speech(text) == expected


@pytest.mark.parametrize("text, expected", [
    # dotted identifier -> "dot". The underscore goes with it: it is not a sound, and "voice
    # underscore tunnel" is not what an engineer says.
    ("voice_tunnel.config.speech_speed", "voice tunnel dot config dot speech speed"),
    # file extension
    ("config.py", "config dot py"),
    ("README.md", "README dot md"),
    (".env", "dot env"),
    # domain
    ("example.com", "example dot com"),
])
def test_a_dotted_identifier_extension_or_domain_is_voiced_with_dot(text, expected):
    assert speech.normalize_for_speech(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("The version is 0.2.6", "The version is zero point two point six"),
    ("We are on 0.2.6.", "We are on zero point two point six."),
    ("The version is 0.2.6 and it is ready.",
     "The version is zero point two point six and it is ready."),
    ("Version 1.0.0 shipped.", "Version one point zero point zero shipped."),
    ("3.5 dollars", "three point five dollars"),
    ("Open config.py.", "Open config dot py."),
    ("Go to example.com.", "Go to example dot com."),
])
def test_the_case_he_named_reads_as_a_term_inside_a_real_sentence(text, expected):
    """The clips from the measurement table, which is what makes these the acceptance cases.

    Note the surrounding sentence survives untouched — the sentence-final period after
    `0.2.6.` is still a sentence-final period, not a fourth version segment.
    """
    assert speech.normalize_for_speech(text) == expected


# ================================================== AC2: the counter-cases (TC2, over-firing)
#
# Each of these is asserted UNCHANGED. They are the only instrument that can see the failure
# mode this spec is most likely to introduce, because over-normalising produces no error, no log
# line and no audible cue that a rule fired where it should not have.


@pytest.mark.parametrize("text", [
    # ELLIPSIS — prosody, not a term. "point point point" is exactly what TC2 forbids.
    "Wait... really?",
    "Hmm ... let me think.",
    "...",
    # SENTENCE-FINAL PERIOD — it really is a sentence boundary.
    "It is ready.",
    "The version is ready. It is ready now.",
    "Is it ready? It is.",
    # ABBREVIATIONS — a single-character component never matches, so "e dot g" cannot happen.
    "e.g. this",
    "i.e. that",
    "The U.S.A. is big.",
    "He has a Ph.D. in it.",
    "Send it c.o.d.",
    # A NUMBER WITH NO DOT — there is no separator to voice.
    "There are 26 files.",
    "Version 2 shipped.",
    "100",
    # MONEY — the engine already reads this correctly. Rewriting a correct reading is a
    # regression that nothing else in the system would ever report.
    "It costs $3.50 today.",
    "£1.25 each",
    # AN IDENTIFIER WITH NO DOT — the underscore rule is scoped to a matched dotted token, so a
    # bare setting name in prose keeps its own spelling.
    "speech_speed is a setting",
    # ORDINARY PROSE.
    "Tell me when the build finishes.",
    "First, second, third.",
    # A MISSING SPACE BETWEEN SENTENCES is far likelier than an identifier with a capitalised
    # component, so an uppercase tail is refused.
    "Done.Next thing",
    # A DOTTED TERM ALREADY INSIDE A LONGER WORD is not a version.
    "python3.11.2 is installed",
])
def test_the_transform_does_not_fire_where_it_must_not(text):
    assert speech.normalize_for_speech(text) == text


def test_an_ellipsis_never_becomes_a_point_or_a_dot():
    """Stated as its own assertion rather than only as an equality, because this is the exact
    sentence TC2 uses to define over-normalisation."""
    out = speech.normalize_for_speech("So... about that config, then... maybe?")

    assert "point" not in out
    assert " dot " not in out


def test_a_sentence_final_period_survives_beside_a_term_that_is_voiced():
    """The hard case for both rules at once: one dot in this string must be voiced and the other
    must not, and telling them apart is the whole job."""
    assert speech.normalize_for_speech("Open config.py.") == "Open config dot py."
    assert speech.normalize_for_speech("We are on 0.2.6.") == "We are on zero point two point six."


def test_a_decimal_in_prose_is_voiced_and_that_is_deliberate():
    """**A DOCUMENTED DISAGREEMENT INSIDE THE SPEC, resolved in favour of FR2.**

    AC2 lists "a bare decimal already inside prose that reads correctly today" among the strings
    that must come back unchanged. FR2's own table, its rationale and the Test-cases table all say
    the opposite for the same string: `3.5` is listed as a dotted number, voiced with "point",
    with the reason spelled out — *"`3.5` and `0.2.6` are the same construction and must not
    diverge"* — and `3.5 dollars` -> "three point five dollars" is a named test case.

    Three statements to one, and the three carry the argument, so the requirement wins over the
    checklist. What is NOT lost is AC2's intent: the criterion exists to stop a correct reading
    being broken, and the reading here does not change — "three point five" is what "3.5" already
    sounds like. The genuinely at-risk case, money, IS protected and is asserted above.
    """
    assert speech.normalize_for_speech("3.5 dollars") == "three point five dollars"
    assert speech.normalize_for_speech("It costs $3.50 today.") == "It costs $3.50 today."


def test_a_letter_glued_to_a_number_gets_its_seam_back():
    """`1.5x` is currently heard as "fifteen ex". Voicing it must not produce "fivex", which is a
    word no engine can pronounce — a fix that trades one wrong reading for another."""
    assert speech.normalize_for_speech("1.5x faster") == "one point five x faster"


def test_empty_and_whitespace_are_returned_untouched():
    """The transform is not where empty input is rejected; `tts.synthesize` is, and it says so
    with a message about the CALLER's own string."""
    assert speech.normalize_for_speech("") == ""
    assert speech.normalize_for_speech("   ") == "   "


# ================================================= AC3 / FR5: it is on the SHARED path
#
# Asserted by DRIVING the path with each backend selected, not by reading the source. Every
# backend here is a stub: no model is loaded and no audio is produced. The claim under test is a
# routing claim — that normalisation happens above the dispatch — and a stub is the only way to
# see what each backend was handed.


@pytest.fixture
def handed(monkeypatch):
    """Capture the text each backend actually receives, for every backend `synthesize` knows."""
    seen: dict[str, str] = {}

    def record(name):
        def _synth(text, *a, **k):
            seen[name] = text
            return b"\x00\x00" * 64, 22050
        return _synth

    monkeypatch.setattr(tts, "_synth_sapi", record("sapi"))
    monkeypatch.setattr(tts, "_synth_piper", record("piper"))
    monkeypatch.setattr(tts, "_synth_none", record("none"))
    monkeypatch.setattr(tts._KOKORO, "synthesize", record("kokoro"))
    return seen


@pytest.mark.parametrize("backend", ["sapi", "piper", "kokoro", "none"])
def test_every_backend_is_handed_the_normalised_string(handed, backend):
    """FR5. Both installed engines were measured dropping the dot, so this is not a kokoro
    workaround — and a fix that lived in one backend would silently un-fix itself the next time
    the backend changed, which has already happened once on this project."""
    tts.synthesize("The version is 0.2.6 and it is ready.", backend=backend)

    assert handed[backend] == "The version is zero point two point six and it is ready."


def test_an_unspeakable_string_is_still_rejected_before_anything_is_normalised():
    """The emptiness check has to keep describing the CALLER's input, not the transform's output."""
    with pytest.raises(tts.TTSError) as exc:
        tts.synthesize("   ", backend="none")
    assert "nothing to speak" in str(exc.value)


# ============================================================ FR3: phrase-level pacing
#
# Measured: a sentence boundary yields 990 ms and a comma yields 0 ms. These pin the ARITHMETIC —
# that a clause break exists, that it is shorter than a sentence break, and that a clip with
# neither gains nothing. The milliseconds themselves are the acceptance harness's measurement.


def test_a_comma_becomes_a_break_of_its_own():
    """Today it is 0 ms: the reply only breathes where a full stop happens to fall."""
    pieces = speech.segments("Alpha, beta. Gamma.")

    assert [p for p, _ in pieces] == ["Alpha,", "beta.", "Gamma."]
    assert pieces[0][1] == speech.CLAUSE_PAUSE_RATIO


def test_the_clause_break_is_shorter_than_the_sentence_break():
    """THE NEGATIVE ARM THAT SEPARATES PACING FROM SLOWER. A change that simply lengthened every
    gap would satisfy "the comma has a gap" and be a worse reply than the one it replaced."""
    pieces = speech.segments("Alpha, beta. Gamma.")
    clause, sentence = pieces[0][1], pieces[1][1]

    assert 0 < clause < sentence
    assert 0 < speech.CLAUSE_PAUSE_RATIO < 1.0, "a clause can never outlast the sentence it is in"


def test_the_gap_is_a_fraction_of_his_own_pause_rather_than_a_number_of_seconds():
    """`rate --pause` is tuned by ear and its own note tells him to raise it when a list runs
    together. A clause break stated in absolute seconds would stop tracking that, and could
    overtake the sentence pause at the bottom of the permitted range."""
    from voice_tunnel import config

    for pause in (0.3, config.SENTENCE_SILENCE_S, 0.85, config.PAUSE_MAX):
        clause = pause * speech.CLAUSE_PAUSE_RATIO
        assert 0 < clause < pause, f"a {pause}s sentence pause gave a {clause}s clause break"
    # And it collapses with the setting rather than surviving it: pause 0 means no gaps at all.
    assert 0.0 * speech.CLAUSE_PAUSE_RATIO == 0.0


def test_a_clip_with_no_comma_and_no_full_stop_gains_nothing():
    """The other negative arm: without it, "insert a pause everywhere" passes both tests above."""
    assert speech.segments("The version is ready") == [("The version is ready", 0.0)]


def test_a_reply_never_ends_on_a_gap():
    """A trailing gap is dead air on the end of every single reply, which reads from the phone as
    the tunnel having hung — the same reason piper's sentence silence never follows the last
    sentence."""
    for text in ("One.", "One. Two.", "One, two.", "One, two. Three, four."):
        assert speech.segments(text)[-1][1] == 0.0


def test_comma_free_text_segments_exactly_as_it_did_before():
    """The kokoro backend already split on `(?<=[.!?])\\s+` and joined with the sentence pause.
    Text with no clause break must come out of the new splitter identical, or every measurement
    taken before this change stops being comparable to the ones taken after it."""
    import re

    for text in ("It is ready.", "The version is ready. It is ready now.", "A. B. C."):
        before = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        assert [p for p, _ in speech.segments(text)] == before


def test_normalisation_does_not_manufacture_a_pause_where_there_was_none():
    """The two halves of this spec have to compose. If voicing a version left a stray period or
    comma behind, the acceptance clip would gain an internal silence and AC10 would fail for a
    reason nobody would look for in the transform."""
    spoken = speech.normalize_for_speech("The version is 0.2.6 and it is ready.")

    assert len(speech.segments(spoken)) == 1


def test_the_kokoro_path_inserts_the_shorter_gap_for_a_comma(monkeypatch):
    """The wiring, not just the arithmetic — with a stub standing in for the model, so no model
    is loaded and nothing is synthesized. What is measured is the number of ZERO bytes inserted
    between pieces, which is the same silence the acceptance harness measures in the audio."""
    from voice_tunnel import config

    class _Stub:
        def create(self, text, voice=None, speed=1.0, lang=None):
            return [0.0] * 240, config.KOKORO_SR

    monkeypatch.setattr(tts._KOKORO, "_load", lambda: _Stub())
    pause = 0.85                                   # his live setting

    one_clause, rate = tts._KOKORO.synthesize("Alpha beta.", "bm_daniel", 1.2, pause)
    comma, _ = tts._KOKORO.synthesize("Alpha, beta.", "bm_daniel", 1.2, pause)
    full_stop, _ = tts._KOKORO.synthesize("Alpha. Beta.", "bm_daniel", 1.2, pause)

    speech_bytes = 240 * 2 * 2                      # two pieces of stub audio, 16-bit
    comma_gap = len(comma) - speech_bytes
    sentence_gap = len(full_stop) - speech_bytes

    assert len(one_clause) == 240 * 2, "a clip with neither break gained silence"
    assert comma_gap > 0, "the comma is still worth 0 ms"
    assert comma_gap < sentence_gap, "a comma that costs as much as a full stop is not pacing"
    assert comma_gap == int(rate * pause * speech.CLAUSE_PAUSE_RATIO) * 2
    assert comma_gap % 2 == 0, "an odd byte count shifts every later 16-bit sample"


# ================================================== AC11 / AC12 / AC13: inspectability (FR4)


def test_pronounce_prints_the_normalised_form_through_the_cli(capsys):
    """FR4a, driven through the CLI entry point rather than the function, because the entry point
    is what an agent debugging a wrong reading actually runs."""
    code, payload, _ = run(["pronounce", "The version is 0.2.6"], capsys)

    assert code == 0
    assert payload["text"] == "The version is 0.2.6"
    assert payload["spoken"] == "The version is zero point two point six"


def test_what_pronounce_prints_is_what_the_synthesis_path_uses(handed, capsys):
    """AC11, and the reason it is asserted against the PATH instead of against a literal.

    An inspector that computes its answer a second way can disagree with the thing it inspects,
    and it would be believed — which is a way to be confidently wrong about a bug you are already
    confused by. So the claim under test is equality with what the backend was handed, and it
    would fail if `pronounce` ever grew its own copy of the rules.
    """
    text = "Open config.py, then check 0.2.6."

    _, payload, _ = run(["pronounce", text], capsys)
    tts.synthesize(text, backend="none")

    assert payload["spoken"] == handed["none"]


def test_describe_documents_the_new_command_and_the_new_field():
    """AC12 — convention 3, asserted rather than remembered. `test_cli_surface` already fails if a
    command is undocumented; this pins the CONTENT, so an empty stub cannot satisfy it."""
    pronounce = cli.DESCRIBE["commands"]["pronounce"]

    assert set(pronounce["returns"]) == {"text", "spoken"}
    assert "pronounce" in cli.DESCRIBE["commands"]
    # The field FR4b adds to the timing log, named where someone reading `timing` will find it.
    normalized = cli.DESCRIBE["commands"]["timing"]["returns"]["normalized"]
    assert "clip" in normalized
    assert "voice-tunnel pronounce" in normalized, "point at the half that needs no server"


def test_the_cli_command_is_wired_to_a_handler():
    """A parser entry with no handler is a KeyError at the moment someone reaches for the tool."""
    import argparse as _ap

    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, _ap._SubParsersAction))

    assert "pronounce" in sub.choices
    args = parser.parse_args(["pronounce", "config.py"])
    assert cli.cmd_pronounce(args)["spoken"] == "config dot py"


def test_the_say_path_records_the_normalised_string_against_the_clip_id(tmp_sessions):
    """AC13, FR4b — by calling the recording helper directly.

    **End-to-end retrieval through a running `say` is NOT verified here and cannot be**: a live
    voice session is on the machine and TC4 forbids starting a server. Stated rather than papered
    over. What IS verified is the part that carries the requirement — that the helper the say path
    calls writes the normalised form beside the clip id, in the log `voice-tunnel timing` reads.
    """
    from voice_tunnel import server

    returned = server.record_spoken("dev", "clip-42", "We are on 0.2.6.", 0.9)
    events = [e for e in timing.read("dev") if e.get("stage") == "spoken"]

    assert returned == "We are on zero point two point six."
    assert len(events) == 1
    assert events[0]["clip"] == "clip-42"
    assert events[0]["normalized"] == returned
    assert events[0]["held_for"] == 0.9


def test_the_recorded_string_is_the_one_synthesis_would_have_used(handed, tmp_sessions):
    """Same anti-drift rule as `pronounce`: recorded, not re-derived by a parallel rendering."""
    from voice_tunnel import server

    text = "Open .env and read voice_tunnel.config.speech_speed."

    recorded = server.record_spoken("dev", "clip-1", text, 0.0)
    tts.synthesize(text, backend="none")

    assert recorded == handed["none"]


def test_pronounce_needs_no_server(monkeypatch, capsys):
    """TC4. The whole reason FR4a exists as a separate half: the per-clip record can only be read
    back through a running server, and there is one running that must not be disturbed."""
    def explode(*a, **k):
        raise AssertionError("pronounce talked to a server")

    monkeypatch.setattr(cli, "_request", explode)
    code, payload, _ = run(["pronounce", "example.com"], capsys)

    assert code == 0 and payload["spoken"] == "example dot com"


def test_pronounce_takes_a_positional_and_not_a_json_blob():
    """Convention 7, measured rather than aesthetic: a constrained argument surface scored 5/5
    across every model tested while JSON degraded on the smaller ones."""
    import argparse as _ap

    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, _ap._SubParsersAction))
    flags = [o for a in sub.choices["pronounce"]._actions for o in a.option_strings]

    assert "--json" not in flags
    assert parser.parse_args(["pronounce", "0.2.6"]).text == "0.2.6"
