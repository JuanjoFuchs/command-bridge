"""Diagnostic: does Chrome's fake audio capture actually deliver signal here?

Kept in the repo because "the browser is silently sending silence" is the single most
expensive failure mode in this project — it is indistinguishable from a broken server, and
this script tells the two apart in 30 seconds.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

sys.argv = ["probe"]
from playwright.sync_api import sync_playwright  # noqa: E402

from scripts.e2e import build_fake_mic  # noqa: E402

MEASURE = """async () => {
  const stream = await navigator.mediaDevices.getUserMedia({audio:{
    channelCount:1, echoCancellation:false, noiseSuppression:false, autoGainControl:false}});
  const ctx = new AudioContext();
  const src = ctx.createMediaStreamSource(stream);
  const node = ctx.createScriptProcessor(4096,1,1);
  let peak = 0, frames = 0;
  node.onaudioprocess = (e) => {
    const ch = e.inputBuffer.getChannelData(0);
    let s = 0; for (let i=0;i<ch.length;i++) s += ch[i]*ch[i];
    const r = Math.sqrt(s/ch.length); if (r > peak) peak = r; frames++;
  };
  const g = ctx.createGain(); g.gain.value = 0;
  src.connect(node); node.connect(g); g.connect(ctx.destination);
  await new Promise(r => setTimeout(r, 5000));
  stream.getTracks().forEach(t => t.stop());
  return {peak: Math.round(peak*100000)/100000, frames};
}"""


def main() -> int:
    artifacts = os.path.join(ROOT, "tests", "artifacts")
    os.makedirs(artifacts, exist_ok=True)
    wav = os.path.join(artifacts, "mic.wav")
    build_fake_mic(wav)
    wav_fwd = wav.replace("\\", "/")
    print(f"wav: {wav_fwd}  ({os.path.getsize(wav)} bytes)")

    token = "probe"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    proc = subprocess.Popen(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, r'{ROOT}'); from voice_tunnel.cli import main; "
         f"raise SystemExit(main(['serve','--session','probe','--port','{port}']))"],
        cwd=ROOT,
        env=dict(os.environ, VOICE_TUNNEL_DIR=tempfile.mkdtemp(), VOICE_TUNNEL_TOKEN=token),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(4)

    base = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-capture"]
    cases = [
        ("beep only (no file)", {"channel": "chrome"}, base),
        ("file, chrome", {"channel": "chrome"}, base + [f"--use-file-for-fake-audio-capture={wav_fwd}"]),
        ("file, backslash path", {"channel": "chrome"}, base + [f"--use-file-for-fake-audio-capture={wav}"]),
        ("file, bundled chromium", {}, base + [f"--use-file-for-fake-audio-capture={wav_fwd}"]),
    ]
    try:
        with sync_playwright() as pw:
            for label, kw, args in cases:
                try:
                    b = pw.chromium.launch(headless=True, args=args, **kw)
                    pg = b.new_page()
                    pg.goto(f"http://127.0.0.1:{port}/?token={token}")
                    print(f"{label:24s} -> {pg.evaluate(MEASURE)}")
                    b.close()
                except Exception as exc:  # noqa: BLE001
                    print(f"{label:24s} -> ERROR {str(exc)[:160]}")
    finally:
        proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
