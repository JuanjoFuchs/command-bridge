# voice-tunnel

[![CI](https://img.shields.io/github/actions/workflow/status/JuanjoFuchs/voice-tunnel/ci.yml?branch=main&label=CI)](https://github.com/JuanjoFuchs/voice-tunnel/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/actions/workflow/status/JuanjoFuchs/voice-tunnel/release.yml?label=Release)](https://github.com/JuanjoFuchs/voice-tunnel/actions/workflows/release.yml)
[![PyPI](https://img.shields.io/pypi/v/voice-tunnel)](https://pypi.org/project/voice-tunnel/)
[![npm](https://img.shields.io/npm/v/%40juanjofuchs%2Fvoice-tunnel)](https://www.npmjs.com/package/@juanjofuchs/voice-tunnel)
[![Python](https://img.shields.io/pypi/pyversions/voice-tunnel)](https://pypi.org/project/voice-tunnel/)
[![GitHub Release](https://img.shields.io/github/v/release/JuanjoFuchs/voice-tunnel)](https://github.com/JuanjoFuchs/voice-tunnel/releases)
[![License](https://img.shields.io/github/license/JuanjoFuchs/voice-tunnel)](LICENSE)

**Talk to your coding agent from your phone.** One command opens a page any phone browser can
load — no app, no App Store — and carries audio both ways.

```bash
pipx install voice-tunnel
voice-tunnel serve --wake claude
```

Open the printed URL on your phone, tap once, and say *"hey Claude, what's the status?"*

Everything runs on your machine. **No GPU. No account. No speech API.**

## What this is

The tool holds no model and makes no decisions. It turns your speech into lines in a log and text
into speech; the agent that started it does the thinking. That is why it works with **any** agent
— Claude Code, Codex and Grok have each driven it unchanged.

```
Phone browser (no install)
   |  HTTPS
   v
voice-tunnel serve  ── the dumb half ──────────────────────────────┐
   mic  -> wake gate -> speech recognition -> a line in a log      │
   spkr <- speech synthesis <- text handed to `voice-tunnel say`   │
                                                                   v
                                       The agent that started it (the smart half)
                                       watch --since <cursor> -> reason -> say
```

## Install

### pipx (recommended)

```bash
pipx install voice-tunnel
voice-tunnel doctor
```

### pip

```bash
pip install voice-tunnel
```

### npm

```bash
npm install -g @juanjofuchs/voice-tunnel
```

The npm package is a launcher, not a bundle — **it needs Python 3.10+ on PATH** and builds a
private environment inside itself on install. `pip install voice-tunnel` does the same thing with
one less layer.

### WinGet (Windows, no Python needed)

```powershell
winget install JuanjoFuchs.voice-tunnel
```

The only channel that needs nothing else installed. Windows may flag it on first run: the bundle
is unsigned, and Defender's heuristic dislikes unsigned Python bundles.

### Better voice and better recognition

The default install works immediately using your system voice and `whisper base.en`. Both are
upgradeable, and the upgrades are worth it:

```bash
pip install voice-tunnel[all]      # or [piper] / [parakeet] individually

voice-tunnel download asr          # Parakeet — 8x faster than whisper, more accurate
voice-tunnel download voice        # a neural voice instead of the robotic one
voice-tunnel download voiceprint   # learns your voice, so the wake phrase becomes optional
voice-tunnel download --list       # what is available, what you already have
```

Models are downloaded, never bundled — a Parakeet checkpoint is 631 MB and would make the package
unusable on a slow connection.

## Use it

Start the tunnel with **your agent's own name**, and put its page somewhere your phone can reach.

```bash
voice-tunnel serve --wake claude          # or codex, grok, whatever is driving
```

Your phone needs HTTPS to reach a microphone at all — that is a browser rule, and a plain LAN
address like `http://192.168.1.20:8765` gives **no microphone**, not a broken one. The simplest
fix:

```bash
tailscale serve --bg 8765
voice-tunnel config set VOICE_TUNNEL_ALLOW_CIDRS 100.64.0.0/10
```

Then, from the agent's side, this is the whole loop:

```bash
voice-tunnel watch --session dev --since -1   # BLOCKS until you speak; returns turns + a cursor
voice-tunnel say   --session dev "All green." # speaks back
voice-tunnel watch --session dev --since 7    # always resume from the cursor you were given
```

`voice-tunnel describe` prints the full contract as JSON. **Point your agent at that** — it is
written to be read by a machine, and it carries the rules that matter, including the one an agent
gets wrong first: `watch` blocks, so an agent not sitting in it has left you talking to nobody.

### Saying its name

The summons is a greeting plus a name: *"hey claude"*, *"hi codex"*, *"ok grok"*. **The greeting
is always required**, which is what makes any name safe — `grok` is an English verb and `cursor`
is a word you say constantly, but nobody says "hey grok" by accident.

```bash
voice-tunnel wake --name codex     # change it, live, no restart
```

Once the voiceprint knows you, you are addressed **without saying anything**.

## Requirements

**No GPU.** Every model here is CPU inference by construction — nothing in the codebase can use a
graphics card. Measured on a 20-core desktop CPU:

| | Memory | Disk | Speech recognition |
|---|---|---|---|
| **Minimum** — system voice + `whisper base.en` | 219 MB | ~150 MB | usable |
| **Recommended** — Parakeet + neural voice + voiceprint | ~1.0 GB | 788 MB | **RTF 0.11** — 7.4 s of speech in 0.85 s |

- **Python 3.10+** for pip and npm. WinGet needs nothing.
- **A phone browser.** Android Chrome is what this is tested on. The tab must stay in the
  foreground — background recording needs a native app, which this deliberately is not.
- **HTTPS to the phone**, via Tailscale or any tunnel. See [Privacy](#privacy).
- A slower CPU raises the real-time factor but does not break anything; the recognizer runs
  faster than real time with a lot of headroom.

## Privacy

Speech recognition, synthesis, the voiceprint, and every turn of the transcript stay on your
machine. There is no account and nothing is sent to a speech API.

**The transport is your choice and they are not equivalent.** Tailscale terminates TLS on your
own device, so nobody else can decrypt the audio. An ngrok or Cloudflare tunnel terminates it at
the vendor's edge — which puts the plaintext of everything you say on a machine you do not
control. Sometimes that is the only thing that works, and it is still a trade worth making
knowingly. If you expose it publicly, put real authentication in front of it: the built-in CIDR
allowlist cannot help you there, because a tunnel forwards from localhost and every request
therefore arrives from an allowed peer.

Your files are plain files. `voice-tunnel config path` says where. The voiceprint is a
192-dimension centroid — speech cannot be reconstructed from it.

## Settings

```bash
voice-tunnel config show           # every setting, its value, and where it came from
voice-tunnel rate --speed 1.4      # talk faster — applies now and every session after
voice-tunnel verbose on            # narrate every action before doing it
voice-tunnel timing                # where the time actually went, per exchange
```

Precedence is **process env > settings file > built-in default**. `.env.example` documents every
variable.

## For agents

```bash
voice-tunnel describe
```

That is the product wedge, borrowed from [agent-mail][am]: an agent runs it, reads the JSON, and
learns the whole contract without MCP setup, a daemon, or separate documentation. Every command
also returns a `next` field telling the agent what to do at the moment it applies.

[am]: https://github.com/JuanjoFuchs/agent-mail-cli

## Development

```bash
python -m venv venv
venv/Scripts/python -m pip install -e ".[dev,all]"

venv/Scripts/python -m pytest tests/ -q     # unit tests, no mic and no model
venv/Scripts/python scripts/e2e.py          # the pipeline, through a real browser
venv/Scripts/python scripts/layout.py       # page geometry, at five viewports
```

| Path | What |
|---|---|
| `voice_tunnel/` | store, asr, wake, tts, voiceprint, security, server, cli, config |
| `voice_tunnel/web/index.html` | the phone client, self-contained, no build step |
| `specs/` | 001 the tunnel · 002 packaging · 003 npm |
| `ai-docs/reference/` | security model, turn-log contract, browser constraints |

## License

MIT.
