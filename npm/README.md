# @juanjofuchs/voice-tunnel

Talk to your coding agent from a phone browser. No app, no App Store, nothing to install on the
phone — the agent that starts the tunnel is the one you talk to.

```bash
npm install -g @juanjofuchs/voice-tunnel
voice-tunnel doctor
voice-tunnel serve --wake claude      # use YOUR agent's name
```

Full documentation: https://github.com/JuanjoFuchs/voice-tunnel

## This package needs Python

It is a launcher, not a bundle. On install it creates a private virtualenv inside the package
directory and `pip install voice-tunnel` into it; the `voice-tunnel` command then hands every
argument straight through. Nothing outside the package directory is touched, and
`npm uninstall -g @juanjofuchs/voice-tunnel` removes all of it.

**Requires Python 3.10+ on PATH.** If it is missing, install it and run
`npm rebuild @juanjofuchs/voice-tunnel`.

Why not a self-contained binary, when the sibling `@juanjofuchs/agent-mail` ships one: agent-mail
is pure stdlib and compiles to about 10 MB. This depends on aiohttp, numpy and faster-whisper, and
optionally onnxruntime and sherpa-onnx — native, per-platform wheels totalling hundreds of
megabytes. Bundling those produces something enormous, slow to start, and reliably flagged by
Windows Defender's ML heuristic. Requiring a real Python is the honest trade.

**`pip install voice-tunnel` does the same thing with one less layer.** Use this package if npm is
how you install your tools; use pip if you would rather skip the wrapper.

## Optional extras

Models are not bundled — a Parakeet checkpoint is ~600 MB and a voice 60–120 MB. The tunnel works
without them, using whisper and your system voice.

```bash
voice-tunnel download --list       # what is available, what you already have
voice-tunnel download asr          # Parakeet: 8x faster than whisper, more accurate
voice-tunnel download voice        # a neural voice instead of the robotic one
voice-tunnel download voiceprint   # learns your voice, so the wake phrase becomes optional
```

Each also needs its runtime, which is a pip extra rather than a default so a first install is not
several hundred megabytes of things you have not asked for yet:

```bash
pip install voice-tunnel[all]      # or [piper] / [parakeet] individually
```

MIT.
