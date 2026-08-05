# voice-tunnel

**Talk to your coding agent from your phone.** One command opens a page any phone browser can
load — no app, no App Store — and carries audio both ways to the agent that started it.

It works with **any** agent. The tool holds no LLM: it turns speech into lines in a log and text
into speech, and every ounce of intelligence lives in whatever is driving it. Claude Code, Codex
and Grok have each driven this one unchanged; the only thing that knows which is the name you
give it (`serve --wake codex`).

```
Phone browser (no install)
   |  HTTPS
   v
voice-tunnel serve  ── the dumb half ──────────────────────────────┐
   mic  -> wake gate -> ASR -> append turn to a JSONL log          │
   spkr <- TTS  <- text handed to `voice-tunnel say`               │
                                                                   v
                                       The agent that started it (the smart half)
                                       watch --since <cursor> -> reason -> say
```

## What it does

- **It runs on your machine.** Speech recognition, speech synthesis, the voiceprint, and every
  turn of the transcript. Nothing is sent to a speech API and there is no account. Reaching your
  phone needs *some* tunnel, and `tailscale serve` is the one with no third party able to decrypt
  the audio — see `ai-docs/reference/security.md` for what each option costs.
- **It learns to recognise your voice, and the wake phrase becomes optional.** Every turn
  confirmed by the phrase enrols another sample, so the speaker model sharpens with use. Measured
  here: 0.711 / 0.782 / 0.801 for the owner against a highest impostor of 0.132 — a 5.4× margin
  around the threshold — and it generalised to a phone microphone having learned only from
  desktop recordings. Once it knows you, you are addressed without saying anything.
- **It answers to whatever your agent is called.** `serve --wake codex`, live-changeable, with a
  greeting always required — which is what keeps ordinary words like `grok` and `cursor` safe as
  names.
- **Your files are yours.** Turn logs, audio, and the voiceprint are plain files in a directory
  `voice-tunnel config path` will tell you. The voiceprint is a 192-dimension centroid; speech
  cannot be reconstructed from it.
- **Models are downloaded, not bundled**, and `voice-tunnel download` fetches them. See below.

## Quick start

```bash
python -m venv venv
venv/Scripts/python -m pip install -r requirements.txt

# Put the shims on PATH once, and `voice-tunnel` works from any directory with no venv activation.
#   PowerShell:  [Environment]::SetEnvironmentVariable('Path', "$env:Path;$PWD\bin", 'User')
#   bash:        export PATH="$PATH:$PWD/bin"
voice-tunnel doctor            # anything missing? every failing check names the command that fixes it

# Models are NOT bundled — a Parakeet checkpoint is ~600 MB and a voice 60–120 MB. It works
# without them (whisper + the system voice); these are the upgrades, and all three are optional.
voice-tunnel download --list       # what is available, what you already have
voice-tunnel download asr          # Parakeet: 8x faster than whisper, more accurate
voice-tunnel download voice        # a neural voice instead of the robotic system one
voice-tunnel download voiceprint   # learns your voice, so the wake phrase becomes optional

voice-tunnel serve --session dev --wake claude   # `--wake` = YOUR name: claude, codex, grok
# -> http://127.0.0.1:8765/?token=<generated>
```

Open that URL, tap once to start, and say *"hey claude, what's the status?"*

Then, from anywhere:

```bash
voice-tunnel watch  --session dev --since -1      # blocks until you speak; returns turns + a cursor
voice-tunnel say    --session dev "All green."    # speaks back
voice-tunnel rate   --session dev --speed 1.3     # talk faster, now and every session after
voice-tunnel status --session dev
voice-tunnel describe                             # the live contract — read this first
```

`bin/voice-tunnel` and `bin/voice-tunnel.cmd` resolve the repo root and the venv themselves, from any cwd, under Git
Bash and PowerShell alike. If `voice-tunnel` is not on PATH, call the shim by absolute path — never reach
for `python -c "import sys; sys.path.insert(...)"`.

## Settings

Anything you would otherwise re-export on every call lives in a gitignored `.env` at the repo
root, loaded automatically by every command:

```bash
voice-tunnel config set VOICE_TUNNEL_TTS piper       # persist it once
voice-tunnel config show                   # every setting, its live value, and where it came from
```

Precedence is **process env > `.env` > built-in default**, so a one-off override is still just a
prefix: `VOICE_TUNNEL_TTS=none voice-tunnel say --session dev "hi"`. Piper's binary and voice are discovered from the
checkout (`venv/Scripts/piper.exe`, `models/en_GB-alan-medium.onnx`), so `VOICE_TUNNEL_TTS=piper` is
usually the only setting a piper session needs. `.env.example` documents every variable.

### How it sounds

Speed and sentence pause are tuned by ear during a live conversation, so `voice-tunnel rate` changes them
**immediately and permanently** — no restart, and the value is still there next session:

```bash
voice-tunnel rate --session dev --speed 1.4      # a MULTIPLE of native pace; higher is faster
voice-tunnel rate --session dev --pause 0.6      # silence between sentences
voice-tunnel rate --session dev                  # what is it now
```

Speed is deliberately not piper's `length_scale`, which is inverted — asking for 2.0 and getting
half speed is a defect, not a quirk, so the inversion lives in exactly one function and nothing
above it ever sees it. Raise `--pause` before reading a list aloud: speech has no scrollback, so
the pause is the punctuation.

### Why replies are fast

The piper voice is **loaded once into the server process**, not spawned per reply. Measured on
this machine through the live server, same text, same voice:

| | spawning `piper.exe` | resident |
|---|---|---|
| "All green." | 4.39 s | 1.20 s |
| one sentence | 4.43 s | 1.29 s |
| three sentences | 5.45 s | 1.59 s |

Nearly all of the old cost was startup — interpreter, onnxruntime, and the ONNX model — re-paid
on every sentence spoken, which had made TTS more than 10x slower than transcription without
anyone measuring it. The remaining ~0.8 s is `SPEAK_GRACE_S`, the deliberate pause that keeps the
agent from talking over you. The model is warmed on a background thread at `serve` time so the
first reply is not the slow one. `VOICE_TUNNEL_PIPER_INPROCESS=0` goes back to spawning; `voice-tunnel status` says
which path is live, because a silent fall back is a 20x regression that only presents as "it
feels slow again".

## Putting it on your phone

The page needs a **secure context** to reach a microphone. `localhost` qualifies; a LAN IP does
**not** — on `http://192.168.x.x` the browser leaves `navigator.mediaDevices` undefined and there
is no microphone at all. So:

```bash
tailscale serve --bg 8765
export VOICE_TUNNEL_ALLOW_CIDRS=100.64.0.0/10
```

That gives a real Let's Encrypt certificate on `<host>.<tailnet>.ts.net` with nothing exposed to
the public internet. Open that URL on the phone.

**Tailscale is the recommended transport, and the reason is not convenience.** It is the only
common option where TLS terminates on your own device, so nobody else can decrypt what you say.
An ngrok or Cloudflare tunnel works and is sometimes the only thing that works — on a network
where Tailscale conflicts with a corporate VPN, for instance — but it terminates TLS at the
vendor's edge, which puts the plaintext of every word on a machine you do not control. If you go
that way, put real authentication in front of it (`ngrok --oauth google --oauth-allow-email …`):
the CIDR allowlist cannot help, because a tunnel forwards from localhost and every request
therefore arrives from an allowed peer. Full comparison in `ai-docs/reference/security.md`.

**Android Chrome, session-based.** The mic only records while the tab is foreground — that is a
platform rule, not a bug, and true background capture would need a native app. The page holds a
Screen Wake Lock so the screen stays on without installing anything.

## Verifying it

```bash
venv/Scripts/python -m pytest tests/ -q     # unit tests, no mic and no model
venv/Scripts/python scripts/e2e.py          # the pipeline, through a real browser
venv/Scripts/python scripts/layout.py       # the page geometry, at five viewports
```

The end-to-end run drives a real Chrome, feeds it a synthesized WAV as its microphone, and
asserts the whole path: capture → WebSocket → Whisper → wake gate → turn log → cursor → TTS →
playback → acknowledgement, plus the CLI an agent actually uses.

It substitutes the **device layer only** — Chrome's own fake-capture flags deliver silence on
this machine (see `ai-docs/reference/browser.md`), so `getUserMedia` is replaced with a real
`MediaStream` built from the WAV. Everything above the OS driver is genuine. A real phone with a
real microphone is the one thing still needing a human.

`scripts/layout.py` checks something neither of the others looks at: **geometry**. It asserts the
page never outgrows the viewport and that the newest transcript row is actually on screen, across
five viewports — including a Pixel 7 both with and without the URL bar showing, a 183px
difference that is larger than most desktop breakpoints and is where a real cropping bug lived
while every other test stayed green. `--shots DIR` writes a PNG per viewport.

## Layout

| Path | What |
|---|---|
| `voice_tunnel/` | store, asr, wake, tts, security, server, cli, config |
| `voice_tunnel/web/index.html` | the phone client, self-contained, no build step |
| `specs/` | numbered metaspecs — WHAT and acceptance criteria |
| `ai-docs/reference/` | security model, turn-log contract, browser constraints |
| `scripts/e2e.py` | the acceptance gate |
| `scripts/probe_capture.py` | diagnostic for "is the browser sending silence?" |
| `bin/` | PATH shims — `voice-tunnel` (bash), `voice-tunnel.cmd` (PowerShell/cmd), both exec `voice-tunnel-run.py` |
| `.env.example` | every setting, with its default and the reason for it |

Start at `AGENTS.md` if you are an agent, `PROJECT_UNDERSTANDING.md` if you are a person.
