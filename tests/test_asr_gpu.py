"""Spec 041 — Parakeet on the GPU, with a clean fall back to the CPU. No model, no GPU needed here.

Measured 2026-10-09: the int8 model on CUDA was barely faster (p50 487 vs 469 ms) because the CUDA
provider runs no int8 integer matmuls; the fp16 export is what the GPU runs. And the sherpa CUDA
provider fails to LOAD ("Failed to load shared library") unless the nvidia-* wheels' DLL folders are
on the search path — which must never turn into a dead ASR: it falls back to the CPU and says why.
"""
import os

from command_bridge import asr, config


def _touch(d, *names):
    os.makedirs(d, exist_ok=True)
    for n in names:
        open(os.path.join(d, n), "w").close()


def test_the_int8_model_is_found_as_before(tmp_path):
    d = str(tmp_path)
    _touch(d, "encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
    f = asr.parakeet_files(d)
    assert f["precision"] == "int8"
    assert f["encoder"].endswith("encoder.int8.onnx") and f["tokens"].endswith("tokens.txt")


def test_an_fp16_model_is_found(tmp_path):
    d = str(tmp_path)
    _touch(d, "encoder.fp16.onnx", "decoder.fp16.onnx", "joiner.fp16.onnx", "tokens.txt")
    f = asr.parakeet_files(d)
    assert f["precision"] == "fp16" and f["joiner"].endswith("joiner.fp16.onnx")


def test_an_fp32_model_is_found(tmp_path):
    d = str(tmp_path)
    _touch(d, "encoder.fp32.onnx", "decoder.fp32.onnx", "joiner.fp32.onnx", "tokens.txt")
    assert asr.parakeet_files(d)["precision"] == "fp32"


def test_cuda_uses_the_fp32_dir_and_never_fp16(tmp_path, monkeypatch):
    """fp16 on CUDA returned 20 of 21 of his turns empty or `<unk>` — it must never be chosen."""
    models = tmp_path
    _touch(str(models / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"), "encoder.int8.onnx")
    _touch(str(models / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-fp16"), "encoder.fp16.onnx")
    monkeypatch.setattr(config, "models_dir", lambda: str(models))
    monkeypatch.delenv("COMMAND_BRIDGE_PARAKEET_DIR", raising=False)
    monkeypatch.setenv("COMMAND_BRIDGE_ASR_PROVIDER", "cuda")
    assert config.parakeet_dir().endswith("-int8")        # no fp32 yet: int8, never fp16
    _touch(str(models / config.PARAKEET_GPU_DIR), "encoder.fp32.onnx")
    assert config.parakeet_dir().endswith("-fp32")
    monkeypatch.setenv("COMMAND_BRIDGE_ASR_PROVIDER", "cpu")
    assert config.parakeet_dir().endswith("-int8")       # the CPU keeps the int8 it is fast on


class _FakeSherpa:
    """Records the providers asked for; the CUDA build "fails to load its shared library"."""

    def __init__(self, cuda_ok):
        self.cuda_ok, self.asked = cuda_ok, []

    class OfflineRecognizer:
        pass

    def make(self):
        fake = self

        class R:
            @staticmethod
            def from_transducer(**kw):
                fake.asked.append(kw["provider"])
                if kw["provider"] == "cuda" and not fake.cuda_ok:
                    raise RuntimeError("OrtSessionOptionsAppendExecutionProvider_Cuda: "
                                       "Failed to load shared library")
                return object()
        return R


def _recognizer(monkeypatch, tmp_path, cuda_ok):
    d = str(tmp_path / "m")
    _touch(d, "encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
    fake = _FakeSherpa(cuda_ok)
    import types
    mod = types.SimpleNamespace(OfflineRecognizer=fake.make())
    monkeypatch.setitem(__import__("sys").modules, "sherpa_onnx", mod)
    monkeypatch.setenv("COMMAND_BRIDGE_PARAKEET_DIR", d)
    monkeypatch.setenv("COMMAND_BRIDGE_ASR_PROVIDER", "cuda")
    monkeypatch.setattr(config, "asr_force_greedy", lambda: True)
    monkeypatch.setattr(asr, "_add_cuda_dll_dirs", lambda: 0)
    monkeypatch.setattr(asr, "_sherpa_has_cuda", lambda: True)
    r = asr.Recognizer.__new__(asr.Recognizer)
    r._model, r._hotwords = None, ""
    r.provider_effective, r.provider_note = None, None
    return r, fake


def test_a_cuda_load_failure_falls_back_to_the_cpu_and_says_why(monkeypatch, tmp_path):
    r, fake = _recognizer(monkeypatch, tmp_path, cuda_ok=False)
    assert r._ensure_parakeet() is not None
    assert fake.asked == ["cuda", "cpu"]
    assert r.provider_effective == "cpu"
    assert "Failed to load shared library" in (r.provider_note or "")


def test_a_working_cuda_load_stays_on_cuda(monkeypatch, tmp_path):
    r, fake = _recognizer(monkeypatch, tmp_path, cuda_ok=True)
    r._ensure_parakeet()
    assert fake.asked == ["cuda"] and r.provider_effective == "cuda"


def test_a_cpu_only_sherpa_build_never_claims_cuda(monkeypatch, tmp_path):
    """The CPU wheel takes provider="cuda" and silently runs on the CPU — and in the server nvidia-smi
    cannot tell (Kokoro already makes the process a GPU app). The build itself can: a CUDA wheel
    ships onnxruntime_providers_cuda.dll. Without it, the int8 CPU model and a note, never fp32."""
    r, fake = _recognizer(monkeypatch, tmp_path, cuda_ok=True)
    monkeypatch.setattr(asr, "_sherpa_has_cuda", lambda: False)
    r._ensure_parakeet()
    assert fake.asked == ["cpu"] and r.provider_effective == "cpu"
    assert "CPU build" in (r.provider_note or "")


def test_the_cpu_default_is_untouched(monkeypatch, tmp_path):
    r, fake = _recognizer(monkeypatch, tmp_path, cuda_ok=True)
    monkeypatch.setenv("COMMAND_BRIDGE_ASR_PROVIDER", "cpu")
    r._ensure_parakeet()
    assert fake.asked == ["cpu"] and r.provider_effective == "cpu"
