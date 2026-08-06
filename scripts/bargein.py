"""Barge-in acceptance — does HIS voice stop a reply, and does nothing else?

JJ, 2026-08-06: "barge in but only for my voice." Both halves matter and they fail differently:
a tunnel that cannot be interrupted is annoying, and one that stops whenever the room makes a
noise is unusable.

This drives the REAL socket with REAL audio — his recorded speech from `sessions/live.wav`, and
the agent's own synthesized voice as the impostor. The impostor case is the important one: the
agent's reply leaks out of the speakers and back into the microphone on every device without
perfect echo cancellation, so "the agent interrupts itself" is the default failure, not an
exotic one.

    <repo>/venv/Scripts/python scripts/bargein.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave

import aiohttp
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PORT = 8806
TOKEN = "barge-check"
SESSION = "barge"

fails = []


def check(ok, label, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail else ""))
    if not ok:
        fails.append(label)


def his_voice(seconds: float) -> np.ndarray:
    """Real recorded speech, taken from the middle of a turn so it is voice and not a pause."""
    with wave.open(os.path.join(ROOT, "sessions", "live.wav")) as f:
        n, sr = f.getnframes(), f.getframerate()
        pcm = np.frombuffer(f.readframes(n), dtype=np.int16)
    turns = [json.loads(x) for x in
             open(os.path.join(ROOT, "sessions", "live.jsonl"), encoding="utf-8") if x.strip()]
    good = [t for t in turns if t.get("t_end", 1e9) * sr <= n and t["t_end"] - t["t_start"] > 3]
    if not good:
        raise SystemExit("no usable recorded speech in sessions/live.wav")
    t = good[-1]
    start = int((t["t_start"] + 0.3) * sr)
    return pcm[start:start + int(sr * seconds)]


def agent_voice(seconds: float) -> np.ndarray:
    """The agent's own TTS — what leaks back through the speakers."""
    from voice_tunnel import config, tts
    from voice_tunnel.asr import resample_linear

    pcm, rate = tts.synthesize("This is the agent speaking a reply that should not interrupt "
                               "itself, however loudly it comes back through the microphone.")
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if rate != config.TARGET_SR:
        a = resample_linear(a, rate, config.TARGET_SR)
    out = (a * 32768.0).astype(np.int16)
    return out[:int(config.TARGET_SR * seconds)]


async def _say(s, url: str) -> None:
    """`now: true` returns as soon as it is queued, so the caller stays free to listen while the
    clip starts playing."""
    async with s.post(f"{url}/say?token={TOKEN}",
                      json={"text": "A long reply with plenty of words in it, so there is still "
                                    "something playing when he starts to talk over it.",
                            "now": True}) as r:
        await r.json()


async def drive(label: str, audio: np.ndarray, expect_barge: bool) -> None:
    from voice_tunnel import config

    url = f"http://127.0.0.1:{PORT}"
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"{url}/ws?token={TOKEN}") as ws:
            await ws.send_json({"type": "hello", "sample_rate": config.TARGET_SR})
            await ws.send_json({"type": "channel", "value": True})
            await ws.send_json({"type": "capturing", "value": True})
            await asyncio.sleep(0.4)

            # Fire-and-forget, because /say BLOCKS until the clip is queued and we need to be
            # listening while it starts.
            asyncio.create_task(_say(s, url))

            # WAIT FOR IT TO ACTUALLY BE SPEAKING before claiming to talk over it. Saying
            # "speaking" first makes `_speak` HOLD the clip — the interruption guard doing its
            # job — so the agent never reaches the speaking state and barge-in never arms. That
            # is the correct behaviour and the wrong test: real barge-in happens AFTER the reply
            # has started, not before.
            for _ in range(120):
                async with s.get(f"{url}/status?token={TOKEN}") as r:
                    if (await r.json())["agent_state"] == "speaking":
                        break
                await asyncio.sleep(0.25)

            async with s.get(f"{url}/status?token={TOKEN}") as r:
                baseline = (await r.json())["barges"]
            await ws.send_json({"type": "speaking", "value": True})
            # Stream in 100 ms frames, as the browser does.
            step = config.TARGET_SR // 10
            stopped = False
            for i in range(0, audio.size, step):
                await ws.send_bytes(audio[i:i + step].tobytes())
                await asyncio.sleep(0.02)
                while not ws._response.content.at_eof() and not ws.closed:
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=0.001)
                    except (TimeoutError, asyncio.TimeoutError):
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        if json.loads(msg.data).get("type") == "stop_playback":
                            stopped = True
                if stopped:
                    break
            await asyncio.sleep(1.5)

            async with s.get(f"{url}/status?token={TOKEN}") as r:
                st = await r.json()
            # DELTA, not the absolute count — `barges` is cumulative for the session, so the
            # impostor run would inherit the previous run's success and always look like a pass.
            got = (st["barges"] - baseline) > 0 or stopped
            check(got is expect_barge, label,
                  f"barges +{st['barges'] - baseline} score={st['last_barge_score']}")


async def main() -> int:
    # A scratch turn log, but the REAL voiceprint gallery — it lives in the session dir, so
    # isolating that isolates his identity too and every score comes back 0.0 against an empty
    # gallery. Found the hard way: the check failed for three rounds while the code was correct.
    scratch = tempfile.mkdtemp(prefix="voice-tunnel-barge-")
    gallery = os.path.join(ROOT, "sessions", "voiceprints.json")
    if os.path.exists(gallery):
        shutil.copy(gallery, os.path.join(scratch, "voiceprints.json"))
    else:
        print("NOTE: no voiceprint gallery — barge-in cannot identify anyone. "
              "Run a live session first, or `voice-tunnel voiceprint --learn-from <dir>`.")
    env = dict(os.environ, VOICE_TUNNEL_DIR=scratch, VOICE_TUNNEL_TTS="piper")
    server = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "bin", "voice-tunnel-run.py"), "serve",
         "--session", SESSION, "--port", str(PORT), "--token", TOKEN],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        await asyncio.sleep(12)     # let the voice and the embedder warm
        await drive("HIS voice stops the reply", his_voice(3.0), expect_barge=True)
        await asyncio.sleep(1.0)
        await drive("the agent's OWN voice does not", agent_voice(3.0), expect_barge=False)
    finally:
        server.terminate()
    print()
    print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
