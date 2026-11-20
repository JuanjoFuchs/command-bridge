# Install and start Command Bridge

You are a coding agent, and the person you work for pasted a line asking you to read this page and
follow it. By the end they should have a link open in their browser, be talking to you, and hear you
answer. Do every step yourself. Ask them only for the things marked **ask**.

Command Bridge is a local command-line tool. It turns their speech into text you read, and your text
into speech they hear, and it gives you a canvas to draw on while you explain. Everything runs on
their machine, with no account and no API key. Source: https://github.com/JuanjoFuchs/command-bridge

## 1. Check for Python

It needs **Python 3.10 or newer on PATH**. Check with `python3 --version`, or `py -3 --version` on
Windows.

If it is missing on Windows, use the zip instead of steps 2 and 3. Each
[release](https://github.com/JuanjoFuchs/command-bridge/releases) has
`command-bridge-<version>-windows-x64.zip`, the whole tool with no Python needed. Unzip it, put the
folder on PATH, and go to step 4. Anywhere else, **ask** them to install Python 3.10+ first.

## 2. Install it

macOS or Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/JuanjoFuchs/command-bridge/main/install.sh | bash
```

Windows, in PowerShell:

```powershell
irm https://raw.githubusercontent.com/JuanjoFuchs/command-bridge/main/install.ps1 | iex
```

This clones the repository into `~/command-bridge` and builds a private virtual environment inside
it with every engine. It does not touch their system Python, and deleting that folder removes all
of it. If they already use pipx or uv, `pipx install "command-bridge[all]"` (or
`uv tool install "command-bridge[all]"`) does the same from PyPI.

## 3. Put it on PATH

After the script, the command is `~/command-bridge/bin/command-bridge`. Add `~/command-bridge/bin`
to PATH for your shell, or call that path directly. Check it with `command-bridge --version`.

## 4. Read its contract

```bash
command-bridge describe
```

This returns JSON with every command, its arguments, and what it returns, generated from the
installed version. **Read all of it. Where it disagrees with this page, it is right.** It also
explains the loop you will run in step 8.

## 5. Download the models

```bash
command-bridge setup
```

This downloads the speech recognition, voice, voiceprint and turn-detection models. It is several
hundred megabytes, so tell them it is downloading. Then:

```bash
command-bridge doctor
```

Fix anything it reports. It gives the command for each fix. Read its `degraded` list even when `ok`
is true: a model can be on disk and still not be used.

## 6. Start the server

Start it with **your own name** as the wake word, as a long-running process in the background:

```bash
command-bridge serve --wake claude      # or codex, or whatever your name is
```

Then get the link:

```bash
command-bridge status
```

`url` is the page with its token. Treat it like a password: anyone with it can use the microphone.

## 7. Get the link to them

On **this computer**, the `url` works as it is: open it in a browser here.

On **a phone**, check `phone.ready` in `status` first. A phone browser only allows the microphone
over https, so a plain network address gives a page that looks connected and hears nothing. If
`ready` is false, **ask** whether they want to use their phone. If they do, front the port with a
tunnel. `ngrok http 8765` is the smallest, and `status` detects it on its own. `command-bridge
status --phone` explains the options and what each one exposes.

Give them the link and tell them: open it, tap once, and say *"hey claude, can you hear me?"*
(with your name).

## 8. Listen

Now run the listening loop from `describe`: `command-bridge watch` waits until they speak and
returns what they said; `command-bridge say` answers out loud. **Stay in `watch`.** If you are not
watching, they are talking to nobody. Answer their first question through `say`, not in your chat.
