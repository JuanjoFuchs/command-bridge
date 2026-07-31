"""Speaker identity — Phase 5b. Pure logic plus gallery I/O; no model needed."""
import numpy as np
import pytest

from vm import voiceprint as vp


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


def test_embedder_reports_unavailable_rather_than_raising(tmp_path):
    e = vp.Embedder(model_path=str(tmp_path / "missing.onnx"))
    assert e.available is False
    assert e.embed(np.zeros(16000, np.float32)) is None


def test_too_short_audio_is_not_enrolled():
    e = vp.Embedder()
    tiny = np.random.RandomState(0).randn(int(16000 * 0.3)).astype(np.float32)
    assert e.embed(tiny) is None, "a fragment carries too little voice to learn from"


def test_gallery_follows_vm_dir_so_tests_cannot_poison_the_real_one(tmp_path, monkeypatch):
    """The UI harness speaks with a SYNTHETIC voice. If the gallery ever moved to a fixed
    location (say ~/.voice-mode), every test run would enrol Piper as the owner and quietly
    destroy the real print. Keeping it under VM_DIR is what makes the harness safe to run.
    """
    monkeypatch.setenv("VM_DIR", str(tmp_path / "isolated"))
    assert str(tmp_path / "isolated") in vp.gallery_path()

    vp.enroll("me", _vec(1))
    assert [s["name"] for s in vp.known()] == ["me"]

    monkeypatch.setenv("VM_DIR", str(tmp_path / "elsewhere"))
    assert vp.known() == [], "a different VM_DIR must see a different gallery"
