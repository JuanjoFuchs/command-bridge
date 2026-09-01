"""Speaker identity — Phase 5b. Pure logic plus gallery I/O; no model needed."""
import numpy as np
import pytest

from command_bridge import voiceprint as vp


@pytest.fixture()
def gallery(tmp_path):
    return str(tmp_path / "voiceprints.json")


def _vec(seed, dim=192):
    rng = np.random.RandomState(seed)
    return rng.randn(dim).astype(np.float32)


def test_cosine_bounds():
    a = _vec(1)
    assert vp.cosine(a, a) == pytest.approx(1.0, abs=1e-5)
    assert vp.cosine(a, -a) == pytest.approx(-1.0, abs=1e-5)
    assert abs(vp.cosine(_vec(1), _vec(2))) < 0.3      # unrelated vectors are near-orthogonal


def test_cosine_handles_zero_vectors_without_dividing_by_zero():
    assert vp.cosine(np.zeros(8, np.float32), _vec(1)[:8]) == 0.0


def test_enrolling_grows_the_count_so_the_print_strengthens(gallery):
    for i in range(3):
        rec = vp.enroll("me", _vec(1) + _vec(100 + i) * 0.1, path=gallery)
    assert rec["count"] == 3


def test_centroid_moves_toward_repeated_samples(gallery):
    target = _vec(1)
    vp.enroll("me", _vec(9), path=gallery)             # one bad sample
    for _ in range(20):
        vp.enroll("me", target, path=gallery)
    name, sim = vp.match(target, path=gallery)
    assert name == "me"
    assert sim > 0.9, "the print should converge on the voice it keeps hearing"


def test_match_returns_nothing_on_an_empty_gallery(gallery):
    assert vp.match(_vec(1), path=gallery) == (None, 0.0)


def test_match_prefers_the_closer_speaker(gallery):
    vp.enroll("me", _vec(1), path=gallery)
    vp.enroll("someone_else", _vec(2), path=gallery)
    name, sim = vp.match(_vec(1), path=gallery)
    assert name == "me" and sim > 0.9


def test_forget_removes_a_speaker(gallery):
    vp.enroll("me", _vec(1), path=gallery)
    assert vp.forget("me", path=gallery) is True
    assert vp.known(path=gallery) == []
    assert vp.forget("nobody", path=gallery) is False


def test_a_corrupt_gallery_does_not_crash(gallery):
    with open(gallery, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert vp.match(_vec(1), path=gallery) == (None, 0.0)


# --- the gate ---------------------------------------------------------------


def test_wake_phrase_always_wins_regardless_of_voice():
    """THE safety property: a voice match can GRANT attention but never withhold it.

    A false positive costs one wasted read. A false negative means being ignored mid-sentence —
    the exact failure this project kept tripping over — so the wake phrase must keep working
    even when the voiceprint disagrees, is untrained, or is broken.
    """
    for speaker, sim in [(None, 0.0), ("someone_else", 0.99), ("me", 0.0)]:
        addressed, reason = vp.should_address(True, speaker, sim)
        assert addressed is True
        assert reason == "wake"


def test_a_confident_voice_match_addresses_without_the_wake_phrase():
    addressed, reason = vp.should_address(False, "me", vp.AUTO_THRESHOLD + 0.05)
    assert addressed is True
    assert reason.startswith("voice:")


def test_a_weak_match_does_not_address():
    addressed, _ = vp.should_address(False, "me", vp.AUTO_THRESHOLD - 0.05)
    assert addressed is False


def test_another_speaker_never_addresses_however_confident():
    """Someone else's voice, or the television, must not summon the agent."""
    addressed, _ = vp.should_address(False, "someone_else", 0.99)
    assert addressed is False


def test_a_confident_stranger_loses_the_conversation_window():
    """The one case where identity may withhold attention, and only this one.

    Found live 2026-08-07: another person spoke inside the owner's 30 s window and it logged as an
    addressed turn. The window is an INFERENCE — that whoever is speaking now is whoever spoke a
    moment ago — and a confident stranger is direct evidence that the inference is wrong.
    """
    addressed, reason = vp.should_address(
        True, "me", vp.STRANGER_MAX - 0.05, grant="window", scored=True
    )
    assert addressed is False
    assert reason.startswith("not-owner:")


def test_a_spoken_phrase_still_wins_even_from_a_stranger():
    """A phrase is an instruction, not an inference. Someone who walks up and says the words is
    asking for attention on purpose, and a voiceprint does not get to overrule that — otherwise
    nobody but the owner could ever hand the agent a sentence."""
    addressed, reason = vp.should_address(
        True, "me", 0.0, grant="phrase", scored=True
    )
    assert addressed is True
    assert reason == "wake"


def test_an_unsure_score_keeps_the_window():
    """Between STRANGER_MAX and AUTO_THRESHOLD the gate does not know, and not knowing must not
    cost him his turn. This is the band that protects against the failure the additive rule was
    written for."""
    for sim in [vp.STRANGER_MAX + 0.01, 0.30, vp.AUTO_THRESHOLD - 0.01]:
        addressed, reason = vp.should_address(True, "me", sim, grant="window", scored=True)
        assert addressed is True, f"{sim} is unsure, not a stranger"
        assert reason == "wake"


def test_an_unscored_turn_is_never_read_as_a_stranger():
    """A turn too short or too quiet to embed reports similarity 0.0, which is numerically
    indistinguishable from a perfect stranger. Without the `scored` flag every "uh huh" inside a
    conversation would be thrown away as somebody else — which is precisely the class of turn the
    window exists to keep."""
    addressed, reason = vp.should_address(True, None, 0.0, grant="window", scored=False)
    assert addressed is True
    assert reason == "wake"


def test_embedder_reports_unavailable_rather_than_raising(tmp_path):
    e = vp.Embedder(model_path=str(tmp_path / "missing.onnx"))
    assert e.available is False
    assert e.embed(np.zeros(16000, np.float32)) is None


def test_too_short_audio_is_not_enrolled():
    e = vp.Embedder()
    tiny = np.random.RandomState(0).randn(int(16000 * 0.3)).astype(np.float32)
    assert e.embed(tiny) is None, "a fragment carries too little voice to learn from"


def test_gallery_follows_the_session_dir_so_tests_cannot_poison_the_real_one(tmp_path, monkeypatch):
    """The UI harness speaks with a SYNTHETIC voice. If the gallery ever moved to a fixed
    location (say ~/.voice-tunnel), every test run would enrol Piper as the owner and quietly
    destroy the real print. Keeping it under VOICE_TUNNEL_DIR is what makes the harness safe to run.
    """
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path / "isolated"))
    assert str(tmp_path / "isolated") in vp.gallery_path()

    vp.enroll("me", _vec(1))
    assert [s["name"] for s in vp.known()] == ["me"]

    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path / "elsewhere"))
    assert vp.known() == [], "a different VOICE_TUNNEL_DIR must see a different gallery"
