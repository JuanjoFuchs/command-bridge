# voice-mode

A **voice tunnel**. One command opens a page your phone can load in a browser — no app, no
install — and carries audio both ways to the agent that started it.

The tool holds no LLM. It turns speech into lines in a log, and text into speech. Every ounce of
intelligence lives in the agent driving it.

```
Phone browser (no install)
   |  HTTPS
   v
vm serve  ── the dumb half ───────────────────────────────────┐
   mic  -> wake gate -> ASR -> append turn to a JSONL log      │
   spkr <- TTS <- text handed to `vm say`                      │
                                                               v
                                          The agent that started it (the smart half)
                                          vm watch --since <cursor> -> reason -> vm say
```

## Quick start

```bash
python -m venv venv
venv/Scripts/python -m pip install -r requirements.txt

# Put the shims on PATH once, and `vm` works from any directory with no venv activation.
#   PowerShell:  [Environment]::SetEnvironmentVariable('Path', "$env:Path;$PWD\bin", 'User')
#   bash:        export PATH="$PATH:$PWD/bin"
vm doctor            # anything missing? every failing check names the command that fixes it
vm serve --session dev
# -> http://127.0.0.1:8765/?token=<generated>
```

Open that URL, tap once to start, and say *"hey claude, what's the status?"*

Then, from anywhere:

```bash
vm watch  --session dev --since -1      # blocks until you speak; returns turns + a cursor
vm say    --session dev "All green."    # speaks back
vm status --session dev
vm describe                             # the live contract — read this first
```

`bin/vm` and `bin/vm.cmd` resolve the repo root and the venv themselves, from any cwd, under Git
Bash and PowerShell alike. If `vm` is not on PATH, call the shim by absolute path — never reach
for `python -c "import sys; sys.path.insert(...)"`.

## Settings

Anything you would otherwise re-export on every call lives in a gitignored `.env` at the repo
root, loaded automatically by every command:

```bash
vm config set VM_TTS piper       # persist it once
vm config show                   # every setting, its live value, and where it came from
```

Precedence is **process env > `.env` > built-in default**, so a one-off override is still just a
prefix: `VM_TTS=none vm say --session dev "hi"`. Piper's binary and voice are discovered from the
checkout (`venv/Scripts/piper.exe`, `models/en_GB-alan-medium.onnx`), so `VM_TTS=piper` is
usually the only setting a piper session needs. `.env.example` documents every variable.

## Putting it on your phone

The page needs a **secure context** to reach a microphone. `localhost` qualifies; a LAN IP does
**not** — on `http://192.168.x.x` the browser leaves `navigator.mediaDevices` undefined and there
is no microphone at all. So:

```bash
tailscale serve --bg 8765
export VM_ALLOW_CIDRS=100.64.0.0/10
```

That gives a real Let's Encrypt certificate on `<host>.<tailnet>.ts.net` with nothing exposed to
the public internet. Open that URL on the phone.

**Android Chrome, session-based.** The mic only records while the tab is foreground — that is a
platform rule, not a bug, and true background capture would need a native app. The page holds a
Screen Wake Lock so the screen stays on without installing anything.

## Verifying it

```bash
venv/Scripts/python -m pytest tests/ -q     # unit tests, no mic and no model
venv/Scripts/python scripts/e2e.py          # 29 checks through a real browser
```

The end-to-end run drives a real Chrome, feeds it a synthesized WAV as its microphone, and
asserts the whole path: capture → WebSocket → Whisper → wake gate → turn log → cursor → TTS →
playback → acknowledgement, plus the CLI an agent actually uses.

It substitutes the **device layer only** — Chrome's own fake-capture flags deliver silence on
this machine (see `ai-docs/reference/browser.md`), so `getUserMedia` is replaced with a real
`MediaStream` built from the WAV. Everything above the OS driver is genuine. A real phone with a
real microphone is the one thing still needing a human.

## Layout

| Path | What |
|---|---|
| `vm/` | store, asr, wake, tts, security, server, cli, config |
| `web/index.html` | the phone client, self-contained, no build step |
| `specs/` | numbered metaspecs — WHAT and acceptance criteria |
| `ai-docs/reference/` | security model, turn-log contract, browser constraints |
| `scripts/e2e.py` | the acceptance gate |
| `scripts/probe_capture.py` | diagnostic for "is the browser sending silence?" |
| `bin/` | PATH shims — `vm` (bash), `vm.cmd` (PowerShell/cmd), both exec `vm-run.py` |
| `.env.example` | every setting, with its default and the reason for it |

Start at `AGENTS.md` if you are an agent, `PROJECT_UNDERSTANDING.md` if you are a person.
