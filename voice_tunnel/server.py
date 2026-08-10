"""voice_tunnel.server — the tunnel. Serves the client page and carries audio both ways.

Endpoints
    GET  /              the phone client (single self-contained page)
    GET  /health        liveness, no auth, no information leak
    GET  /status        session state (token required)
    POST /say           synthesize text and push it to the connected client (token required)
    WS   /ws            the audio channel (token required, checked BEFORE the first frame)

The server holds **no LLM and makes no decisions**. It turns audio into turns in a log, and
turns text into audio. Everything else belongs to the agent driving it.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import threading
import time
import wave
from typing import Any

import numpy as np
from aiohttp import WSMsgType, web

from . import asr as asr_mod
from . import config, cues, security, store, timing, tts, turndetect, voiceprint
from .wake import WakeGate

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
"""The client page ships INSIDE the package, not beside it.

This used to resolve to the parent of the package — correct in a checkout, and in an installed
wheel it points at `site-packages/web`, which does not exist. The failure would have been the
entire user interface returning 500 while every test on a developer's machine passed, because a
checkout always has the directory in both places."""


class TunnelState:
    """Everything the server knows. Deliberately small — state that grows is state that lies."""

    def __init__(self, session: str, token: str | None, gate_enabled: bool = True) -> None:
        self.session = session
        self.token = token
        self.started_at = time.time()
        self.clients: set[web.WebSocketResponse] = set()
        self.wake = WakeGate(enabled=gate_enabled)
        self.recognizer = asr_mod.Recognizer()
        # The one place the model meets the pure segmenter. Injected rather than imported
        # so UtteranceBuffer stays testable without an 8 MB download — see its docstring.
        self.turn = turndetect.TurnDetector() if config.turn_detect_enabled() else None
        self.buffer = asr_mod.UtteranceBuffer(
            turn_detector=(self.turn.complete if self.turn else None)
        )
        self.turns_logged = 0
        self.frames_received = 0
        self.samples_received = 0
        self.last_played: str | None = None
        self.last_error: str | None = None
        """The most recent error from ANY subsystem. Kept for compatibility, and it is the field
        that misleads: see `errors` below."""

        self.errors: dict[str, str] = {}
        """The most recent error PER SUBSYSTEM — tts, asr, voiceprint, transport, client, turn.

        Twelve places write `last_error` and they share one slot, so the last one to fail wins.
        On 2026-08-10 that meant a `voiceprint: No module named 'sherpa_onnx'` — an optional
        subsystem nobody was asking about — sat in the field while the TTS failure being actively
        diagnosed had already been overwritten. The person debugging was reading a true statement
        about the wrong component.

        Keyed by subsystem so an unrelated failure can never hide the one you are chasing."""
        self.client_sr: int = config.TARGET_SR
        self.consumed_cursor: int = -1
        self.agent_state: str = "idle"
        self.cues_enabled: bool = config.cues_enabled()
        self.speech_speed: float = config.speech_speed()
        """Live speech speed, in the SPEED unit (higher is faster). Held in server state rather
        than read from env per call, so "talk faster" can be honoured mid-conversation instead of
        requiring a restart — and seeded from the settings file, so the value tuned by ear last
        session is the value this session starts with."""
        self.sentence_pause: float = config.sentence_pause()
        """Live too, and persisted for the same reason: this is a pacing preference tuned by ear,
        and re-setting it on every server start meant the right value lived only in a running
        process."""
        self.undelivered: list = []
        """Clips synthesized while nobody was connected, waiting for the phone to come back.

        Android suspends a backgrounded tab, so the socket drops every time the phone locks — and
        `/say` used to answer 409 and throw the reply away. Over one session that silently ate
        several answers, and from the owner's side he had simply asked a question and never been told
        anything. Live, 2026-08-01: "whenever the client is disconnected or you detect that my
        phone is locked or whatever, you should queue up your reply back to me. So whenever I
        reconnect, they all send down to me."

        Bounded by BOTH count and age, because the failure mode of an unbounded queue is worse
        than the one it fixes: reconnecting after a long break and being read a stack of stale
        answers to questions you have stopped caring about."""
        self._clip_lock = asyncio.Lock()
        """Held across a clip's header AND its bytes, so the pair cannot be split.

        The protocol is "JSON header, then the binary frame it describes", and the client pairs
        bytes with the most recent header. Concurrent `_speak` tasks broke that invariant: with
        two replies in flight the wire could carry header1, header2, bytes1, bytes2, and the
        second header overwrote the first before any audio arrived — so one reply played under
        the other's identity and the other was dropped.

        This is the SAME defect the owner heard on the move ("those two play together, overlapping one
        on top of the other") surviving one layer deeper than the first fix. Queuing playback on
        the client made the audio serial; it could not restore a pairing the server had already
        scrambled. An invariant the client depends on has to be guaranteed by the sender.
        """
        self.verbose: bool = config.verbose_default()
        """Does the owner want every action narrated? Toggled from the page or by voice, read by the agent.

        **The SERVER is the source of truth, not the browser.** Each client used to push its own
        localStorage value on connect, so opening the page on a phone silently reverted a
        preference set on the laptop — last connection wins, which is the opposite of a shared
        setting. Now the server sends its value on connect and the client adopts it.


        The TOOL only stores and publishes this — it does not act on it. Deciding what counts as
        "an action worth narrating" is exactly the unbounded judgment that belongs in the agent
        (AGENTS.md rule 1), and a flag is the smallest thing that can carry the preference across
        the boundary. Live, 2026-07-31: "a toggle that says a verbose mode for you so that I
        don't have to ask you to describe the actions and we don't have to write it into the
        guide. This button would toggle live how you respond."
        """
        self.capturing: bool = False
        """Has the client actually started its microphone, or is it just *connected*?

        These look identical from the agent's side and are not the same thing at all. A page that
        has loaded but never been tapped holds an open WebSocket, reports `clients: 1`, and sends
        no audio — so an agent sits in `watch` believing someone is listening to it. Live
        2026-08-03: "I just refreshed the UI and I didn't hit tap to start, you should have a way
        to be aware of that."

        Reported by the client rather than guessed, and backed up by `last_audio_at` so a wedged
        page that claims to be capturing is still detectable.
        """
        self.last_audio_at: float = 0.0
        """`time.monotonic()` of the most recent audio frame. 0 means none ever arrived."""
        self.muted: bool = False
        """Whether the client is holding its own microphone shut. Published for `status` only —
        the mute is enforced ON THE CLIENT by not sending frames, because a mute that still
        streams audio to a server that promises to ignore it is not a mute anyone should trust.

        **Mute is one direction only.** He cannot be heard; he can still hear. That is the whole
        difference from `channel_open` below, and separating them is what lets him silence his
        own microphone in a meeting without also losing the reply he is waiting for."""
        self.channel_open: bool = False
        """Is the conversation live AT ALL — the orb.

        Off means neither direction carries: no audio up, and **nothing plays down**. Replies
        synthesized while it is closed go to `undelivered` and arrive when he reopens it.

        Live, 2026-08-06: *"the orb should control whether or not this interaction is live...
        if I turn off the orb, that means that you cannot talk to me either. And your responses
        get queued up server side until I turn the orb back on."*

        **Why this is not the same flag as `capturing`.** `capturing` answers "is the microphone
        running", which is a fact about hardware and permissions. This answers "is he in the
        conversation", which is a decision he made. They coincided while the orb did both jobs,
        and that is exactly why the orb could not express "listen to me but do not talk yet"."""
        self.barge_buf: np.ndarray | None = None
        """Audio accumulated WHILE THE AGENT IS SPEAKING, for the barge-in identity check.

        Separate from the utterance buffer on purpose: that one is segmenting a turn, this one is
        answering a different question — "is the person talking over me actually him". Discarded
        the moment the agent stops speaking, so it never grows without bound."""
        self.watch_open: bool = False
        """Is an agent sitting in `watch` RIGHT NOW.

        Unambiguous on purpose. `agent_state == "idle"` almost means this, and cannot be used for
        it: idle is also what a freshly started server reports before any agent has ever called.
        A watchdog that reads idle as "someone is listening" would stay silent on exactly the
        session where nobody ever arrived.

        Needed because a watch run DETACHED leaves the harness idle, which is what triggers the
        watchdog — so without this the net fires every minute against a watch that is already
        running, and each firing starts another one."""
        self.barges: int = 0
        self.last_barge_score: float = 0.0
        self.user_speaking: bool = False
        """The CLIENT's own report that he is talking right now.

        The server already infers this from `buffer.speech_active`, and that inference is what let
        the agent cut across him: it is derived from audio that has already crossed the network
        and been segmented, so it lags the actual sound by a buffer plus a hop. The client knows
        from the microphone level, immediately.

        Used ADDITIVELY with the server-side signal — hold if either says he is talking. A false
        positive costs a moment of delay; a false negative costs interrupting him, which is the
        failure this exists to prevent. Live, 2026-08-06, having just been interrupted: *"the
        client needs to send a signal back to the server whenever it's detecting me speaking so
        that you don't interrupt me."*"""
        self.embedder = voiceprint.Embedder()
        self.voice_samples: int = 0
        self.partial_busy: bool = False
        self.last_partial_at: float = 0.0
        self.partial_text: str = ""
        # Failsafe capture: everything the server actually received, at 16 kHz. Borrowed from
        # a sibling project, where it repeatedly turned "the ASR is bad" into a question you can
        # answer by listening — is the audio quiet, clipped, or fine and the model just wrong?
        self._wav: wave.Wave_write | None = None

    def capture(self, samples) -> None:
        """Append to the failsafe WAV, opening it on first audio."""
        if self._wav is None:
            path = os.path.join(config.session_dir(), f"{self.session}.wav")
            os.makedirs(config.session_dir(), exist_ok=True)
            self._wav = wave.open(path, "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(config.TARGET_SR)
        clipped = np.clip(samples, -1.0, 1.0)
        self._wav.writeframes((clipped * 32767).astype("<i2").tobytes())

    def close_capture(self) -> None:
        if self._wav is not None:
            try:
                self._wav.close()
            finally:
                self._wav = None

    def fail(self, subsystem: str, message: str) -> None:
        """Record a failure against the subsystem it came from, and as the most recent overall.

        Both, deliberately. `last_error` stays for anything already reading it, and `errors[...]`
        is what survives a later failure elsewhere — which is the whole point. An optional
        subsystem failing is not a reason to lose the error you were chasing.
        """
        text = str(message)[:300]
        self.errors[subsystem] = text
        self.last_error = text

    def clear_error(self, subsystem: str) -> None:
        """Drop a subsystem's error once it succeeds, so a stale one cannot be re-diagnosed."""
        self.errors.pop(subsystem, None)

    def snapshot(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "uptime_s": round(time.time() - self.started_at, 1),
            # THE LIVE WAKE NAME. `voice-tunnel wake` reads this to show what the running gate
            # actually accepts, as distinct from what is persisted — the two differ for the whole
            # life of a server started before the name was changed. `wake_phrases` was here from
            # the start and the NAME was not, so that comparison reported `live: {"name": null}`
            # beside a correct list of phrases containing it: the server visibly knew the name and
            # would not say it.
            "wake": config.wake_name(),
            "clients": len(self.clients),
            "frames_received": self.frames_received,
            "audio_seconds": round(self.samples_received / float(config.TARGET_SR), 2),
            # THE CURSOR TO WATCH FROM. Read from the LOG, not from this process.
            #
            # `turns_logged` below counts turns THIS SERVER has written since it started, and a
            # watchdog took it for the cursor: 26 against a real last id of 367, which would have
            # replayed three hundred old turns as if they had just been spoken. The two are only
            # equal on a server that has never restarted, which is the case every author tests in
            # and no long-running session is ever in.
            #
            # Named for what it is and published beside its impostor, because the trap was never
            # that the number was hard to find — it was that a plausible wrong one was closer to
            # hand.
            "last_turn_id": store.last_turn_id(self.session),
            "turns_logged": self.turns_logged,
            "consumed_cursor": self.consumed_cursor,
            "pending_turns": max(0, self.turns_logged - 1 - self.consumed_cursor),
            "agent_state": self.agent_state,
            "speech_active": self.buffer.speech_active,
            "last_played": self.last_played,
            "last_error": self.last_error,
            # Read THIS when diagnosing: `last_error` is whichever subsystem failed most
            # recently, which is not the same question as "what is wrong with the thing I am
            # debugging".
            "errors": dict(self.errors),
            "verbose": self.verbose,
            "muted": self.muted,
            "capturing": self.capturing,
            "channel_open": self.channel_open,
            "watch_open": self.watch_open,
            "barges": self.barges,
            "last_barge_score": self.last_barge_score,
            "turn_detection": (self.turn.describe() if self.turn else {"enabled": False}),
            "last_end_reason": self.buffer.last_end_reason,
            "user_speaking": self.user_speaking,
            "seconds_since_audio": (
                None if not self.last_audio_at
                else round(time.monotonic() - self.last_audio_at, 1)
            ),
            "undelivered": len(self.undelivered),
            "wake_enabled": self.wake.enabled,
            "voice_enrolled_samples": self.voice_samples,
            "voiceprint_available": self.embedder.available,
            "wake_phrases": list(self.wake.phrases),
            "tts_backend": tts.available(),
            "speech_speed": round(self.speech_speed, 2),
            "sentence_pause": self.sentence_pause,
            "asr_model": self.recognizer.model_name,
            "log": store.log_path(self.session),
            "capture_wav": os.path.join(config.session_dir(), f"{self.session}.wav"),
        }


def _peer_ip(request: web.Request) -> str:
    return (request.remote or "").strip() or "0.0.0.0"


def _request_token(request: web.Request) -> str | None:
    """Accept the token from a query param (the phone opens a URL) or a Bearer header
    (scripts). Both are equivalent; neither is more trusted than the other."""
    tok = request.query.get("token")
    if tok:
        return tok
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


def _check(request: web.Request, state: TunnelState) -> tuple[bool, str]:
    gate = security.Gate(state.token)
    return gate.check(
        _peer_ip(request),
        _request_token(request),
        request.headers.get("X-Forwarded-For"),
    )


async def _set_agent_state(state: TunnelState, value: str) -> None:
    """Publish what the agent is doing: idle | thinking | waiting | speaking.

    The point is that silence is ambiguous. Without this the user cannot tell "it did not hear
    me" from "it heard me and is working" from "it is holding back so as not to interrupt" —
    and all three look like a broken tool.
    """
    state.agent_state = value
    await _broadcast_json(state, {"type": "agent_state", "state": value})


async def _broadcast_json(state: TunnelState, payload: dict[str, Any]) -> None:
    dead = []
    for ws in list(state.clients):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.clients.discard(ws)


async def handle_index(request: web.Request) -> web.StreamResponse:
    """The page itself is not secret — the token is. Serving it unauthenticated keeps the
    phone flow to a single URL, and it can do nothing without a valid token on the socket."""
    path = os.path.join(WEB_DIR, "index.html")
    if not os.path.exists(path):
        return web.Response(status=500, text="voice_tunnel/web/index.html missing")
    # Never cache the client. The page is edited constantly during development and a stale copy
    # is the worst kind of bug to chase: the server has the fix, the user reloads, and nothing
    # changes — so you conclude the fix did not work and go break something else.
    return web.FileResponse(
        path,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def handle_status(request: web.Request) -> web.Response:
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    return web.json_response(state.snapshot())


async def handle_shutdown(request: web.Request) -> web.Response:
    """Stop the server on request.

    Behind the same auth as everything else — this is the one route whose entire purpose is to
    end the process, so an unauthenticated caller must not reach it.

    Graceful rather than a signal because the turn log is the only durable artifact a conversation
    leaves behind. The response is sent BEFORE the loop is asked to stop, or the caller sees a
    dropped connection and cannot distinguish a clean shutdown from a crash.

    Exists because the CLI could start a detached server and never stop one. An audit that
    followed `describe` into a background `serve` had to save the OS handle itself to get back
    out, and in a new session that handle is simply gone.
    """
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)

    async def _later() -> None:
        await asyncio.sleep(0.15)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.ensure_future(_later())
    return web.json_response({"stopping": True, "session": state.session})


async def handle_say(request: web.Request) -> web.Response:
    """Synthesize and push to every connected client.

    Returns as soon as the audio is queued, not when playback finishes — the agent should not
    block on the speed of human hearing.
    """
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    text = (body or {}).get("text", "")
    voice = (body or {}).get("voice") or None
    fire_and_forget = bool((body or {}).get("async"))
    if not isinstance(text, str) or not text.strip():
        return web.json_response({"error": "text is required"}, status=400)
    # No 409 when nobody is connected. The reply is synthesized and held; see
    # TunnelState.undelivered. Refusing here is what made a locked phone eat answers silently.

    if fire_and_forget:
        # Return before synthesis so the agent can acknowledge and keep working in parallel,
        # instead of the user waiting out a TTS round trip before anything else starts.
        asyncio.get_running_loop().create_task(_speak(state, text, voice))
        return web.json_response({"queued": True, "async": True})

    try:
        result = await _speak(state, text, voice)
    except tts.TTSError as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response(result)


async def _speak(state: TunnelState, text: str, voice: str | None) -> dict[str, Any]:
    """Synthesize, hold if the speaker is mid-sentence, then push the audio.

    Shared by the blocking and fire-and-forget paths so the interruption guard cannot be
    bypassed by choosing the async one.
    """
    timing.stamp(state.session, "say_requested", chars=len(text))
    await _set_agent_state(state, "synthesizing")
    try:
        pcm, rate = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: tts.synthesize(
                text, voice=voice, speed=state.speech_speed, pause=state.sentence_pause
            ),
        )
    except tts.TTSError as exc:
        # `tts`, so that "SAPI produced no audio" survives a later voiceprint or capture error.
        # It did not, on 2026-08-10, and the person debugging TTS spent twenty-five minutes
        # reading an accurate message about an optional subsystem nobody had asked about.
        state.fail("tts", str(exc))
        await _set_agent_state(state, "idle")
        raise

    timing.stamp(state.session, "synthesized", audio_s=round(len(pcm) / 2 / rate, 2))
    clip_id = f"clip-{int(time.time() * 1000)}"

    # Do not talk over the speaker. Synthesis is done, but if they are mid-utterance, hold the
    # audio until they finish. Live, 2026-07-29: a batch of voice auditions cut across him
    # mid-sentence, which is the difference between a conversation and a machine that shouts.
    # Bounded so a stuck VAD can never silence the agent permanently.
    waited = 0.0
    announced = False
    # A muted microphone cannot be mid-sentence. Without this the hold ran anyway: muting stops
    # frames arriving, so nothing ever closes the utterance and `speech_active` stays stuck true
    # from the last frame before the mute — the agent then announces it is waiting for someone to
    # finish speaking who has physically switched their microphone off. Live, 2026-08-01:
    # "whenever I mute, you say that you're listening and you're waiting for me to finish, but
    # I'm muted."
    # EITHER signal holds the clip. The client's is faster — it reads the microphone level
    # directly, while the server's is derived from audio that has already crossed the network and
    # been segmented, so it lags by a buffer plus a hop. That lag is what let a reply land on top
    # of him. Additive on purpose: a false positive costs a moment of delay, a false negative
    # costs interrupting him.
    def talking() -> bool:
        return state.user_speaking or state.buffer.speech_active

    while waited < 15.0 and not state.muted:
        if talking():
            if not announced:
                await _set_agent_state(state, "waiting")
                announced = True
            await asyncio.sleep(0.1)
            waited += 0.1
            continue
        # Quiet — but a pause longer than END_OF_UTTERANCE_MS closes the turn, so "thinking"
        # and "finished" look identical from here. Wait a beat and re-check before committing.
        grace = 0.0
        while grace < config.SPEAK_GRACE_S:
            await asyncio.sleep(0.1)
            grace += 0.1
            waited += 0.1
            if talking():
                break
        if not talking():
            break
    await _set_agent_state(state, "speaking")
    await _push_cue(state, "speaking")

    # Header first so the client knows how to interpret the bytes that follow — and both under
    # the clip lock, because a second clip slipping between them is what made two replies play
    # as one. See TunnelState._clip_lock.
    header = {
        "type": "audio_header",
        "id": clip_id,
        "sample_rate": rate,
        "bytes": len(pcm),
        "text": text,
        "held_for": round(waited, 1),
    }
    # TWO ways to have nobody to talk to, and they queue identically. A dropped socket is an
    # accident; a closed orb is a decision. Either way the reply is held rather than played to an
    # empty room, and arrives when he is back — which is what the undelivered queue was built for
    # when a locked phone was silently eating answers.
    deliverable = bool(state.clients) and state.channel_open
    if deliverable:
        await _send_clip(state, header, pcm)
    else:
        state.undelivered.append({"header": header, "pcm": pcm, "at": time.time()})
        _prune_undelivered(state)
        timing.stamp(state.session, "undelivered_queued", clip=clip_id,
                     depth=len(state.undelivered),
                     why="channel_closed" if state.clients else "no_client")
    timing.stamp(state.session, "spoken", clip=clip_id, held_for=round(waited, 1))
    return {
        "queued": True,
        "id": clip_id,
        "seconds": round(len(pcm) / 2 / rate, 2),
        "held_for": round(waited, 1),
        "delivered": deliverable,
        # Say WHICH kind of not-delivered, so the agent can tell "his phone dropped" from "he
        # closed the channel deliberately" without a second call.
        "reason": None if deliverable else ("channel_closed" if state.clients else "no_client"),
    }


def _prune_undelivered(state: TunnelState) -> None:
    """Drop anything too old or too far back in the queue."""
    now = time.time()
    state.undelivered = [
        c for c in state.undelivered if now - c["at"] <= config.UNDELIVERED_MAX_AGE_S
    ][-config.UNDELIVERED_MAX:]


async def _flush_undelivered(state: TunnelState) -> int:
    """Deliver everything that was said while nobody was listening, oldest first."""
    _prune_undelivered(state)
    queued, state.undelivered = state.undelivered, []
    for clip in queued:
        header = dict(clip["header"])
        # Say how long it waited. A reply arriving 90 seconds after the question, with no
        # acknowledgement that time passed, reads as the agent being slow rather than the phone
        # having been asleep.
        header["delayed_s"] = round(time.time() - clip["at"], 1)
        await _send_clip(state, header, clip["pcm"])
    if queued:
        timing.stamp(state.session, "undelivered_flushed", count=len(queued))
    return len(queued)


async def _send_clip(state: TunnelState, header: dict[str, Any], pcm: bytes) -> None:
    """Send one clip's header and its bytes as an indivisible pair.

    The lock is the whole function. Everything that puts audio on the wire goes through here so
    there is exactly one place the pairing can be reasoned about — a second sender that forgot
    to take the lock would reintroduce the bug silently.
    """
    async with state._clip_lock:
        await _broadcast_json(state, header)
        for ws in list(state.clients):
            try:
                await ws.send_bytes(pcm)
            except Exception:
                state.clients.discard(ws)


async def _push_cue(state: TunnelState, name: str) -> None:
    """Send a cue to the client. Silently no-ops on an unknown name — a missing sound must never
    be able to break a conversation."""
    if not state.cues_enabled or not state.clients:
        return
    try:
        pcm, rate = cues.render(name)
    except KeyError:
        return
    # Cues go through the same lock as speech. A cue landing between a reply's header and its
    # bytes is the exact sequence that made a reply play tagged as a cue, with no transcript row
    # and no playback receipt — so the server believed it had spoken and the owner had heard nothing.
    await _send_clip(
        state,
        {"type": "audio_header", "id": f"cue-{name}", "sample_rate": rate,
         "bytes": len(pcm), "text": "", "cue": name},
        pcm,
    )


async def handle_cue(request: web.Request) -> web.Response:
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    name = str((body or {}).get("name") or "")
    if name not in cues.CUES:
        return web.json_response(
            {"error": f"unknown cue {name!r}", "available": cues.names()}, status=400
        )
    await _push_cue(state, name)
    return web.json_response({"played": name})


async def handle_rate(request: web.Request) -> web.Response:
    """Set speech speed and/or sentence pause at runtime. Either may be omitted.

    The owner asked whether "talk faster" could take effect mid-conversation or would need a code
    change. Holding these in server state rather than reading env per call is what makes the
    former possible — a preference the user expresses out loud should not require restarting the
    conversation to apply.

    **This endpoint does not write the settings file.** Persistence belongs to `voice-tunnel rate`, which
    writes `.env` and then calls this. A long-running server that edits config on disk is a
    process racing every other `voice-tunnel config set`, for no behaviour anyone needs.
    """
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    body = body or {}
    previous = {"speed": round(state.speech_speed, 2), "pause": state.sentence_pause}

    # SPEED, not duration. Piper's length_scale is inverted — lower means faster — and exposing
    # that leaked straight through: the owner asked for 2.0 expecting twice as fast and got half speed.
    # An API that contradicts the plain meaning of its own parameter name is a defect, so speed
    # is what crosses this boundary and the inversion lives in config.length_scale_for alone.
    raw_speed = body.get("speed", body.get("value"))     # `value` kept: the original field name
    if raw_speed is not None:
        try:
            speed = float(raw_speed)
        except (TypeError, ValueError):
            return web.json_response({"error": "speed must be a number"}, status=400)
        if not (config.SPEED_MIN <= speed <= config.SPEED_MAX):
            return web.json_response(
                {"error": f"speed must be between {config.SPEED_MIN} (half speed) and "
                          f"{config.SPEED_MAX} ({config.SPEED_MAX}x faster)"},
                status=400,
            )
        state.speech_speed = speed

    raw_pause = body.get("pause")
    if raw_pause is not None:
        try:
            pause = float(raw_pause)
        except (TypeError, ValueError):
            return web.json_response({"error": "pause must be a number"}, status=400)
        if not (0.0 <= pause <= config.PAUSE_MAX):
            return web.json_response(
                {"error": f"pause must be between 0 and {config.PAUSE_MAX} seconds"}, status=400
            )
        state.sentence_pause = pause

    return web.json_response(
        {"speed": round(state.speech_speed, 2), "pause": state.sentence_pause,
         "previous": previous}
    )


async def handle_verbose(request: web.Request) -> web.Response:
    """Set narration mode from the agent side, so the owner can flip it by ASKING rather than tapping.

    Live, 2026-08-03: "I also want you to be able to toggle the verbose. If I ask it via here,
    I would want you to toggle it and I would want the UI properly updated with that." The
    broadcast is what makes the second half true — every connected page repaints its switch.
    """
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    value = (body or {}).get("value")
    if not isinstance(value, bool):
        return web.json_response({"error": "value must be true or false"}, status=400)
    previous = state.verbose
    state.verbose = value
    await _broadcast_json(state, {"type": "verbose", "value": state.verbose})
    return web.json_response({"verbose": state.verbose, "previous": previous})


async def handle_wake(request: web.Request) -> web.Response:
    """Change what the agent answers to, live.

    The name is the one string that ties this tool to any particular agent, so the party that
    knows it is whatever started the tunnel — not the tool. Changing it without a restart matters
    because the alternative is telling someone mid-conversation to stop, restart the server, and
    reopen the page on their phone.

    The broadcast is not cosmetic. The page prints "say hey <name>" as its instruction, and a page
    still showing the old name would be telling the user to say something the gate no longer
    accepts — the tool actively teaching a wrong password.
    """
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    name = (body or {}).get("name")
    if not isinstance(name, str) or not name.strip():
        return web.json_response({"error": "name must be a non-empty string"}, status=400)

    previous = config.wake_name()
    # Set it in this process's environment so config.wake_name() — the single reader every other
    # layer goes through — returns the new value. Writing state.wake.phrases alone would leave the
    # ready frame and `describe` reporting the old name.
    os.environ["VOICE_TUNNEL_WAKE_NAME"] = name.strip().lower()
    state.wake.set_phrases(config.wake_phrases())
    await _broadcast_json(
        state, {"type": "wake", "name": config.wake_name(),
                "phrases": list(state.wake.phrases)}
    )
    return web.json_response(
        {"wake": config.wake_name(), "previous": previous, "phrases": list(state.wake.phrases)}
    )


async def handle_watching(request: web.Request) -> web.Response:
    """The agent has gone back into `watch`. Therefore it is listening — derive, do not ask.

    **The agent no longer declares what it is doing; the tool observes it.** Reported 2026-08-07:
    *"I have noticed that you are controlling the status changes using commands. But I would like
    to remove that control... when a watch resolves and you don't run another one, that means
    you're thinking. If it detects that you are running another watch, that means you're
    listening."*

    He is right, and this file already contains the argument: the comment on the read boundary
    says **state that can drift from reality should not be maintained by hand.** That was written
    about the cursor, and it applies unchanged here — an agent announcing "I am thinking" is a
    claim, and a claim is wrong exactly when it matters, which is when the agent has stopped
    doing what it said.

    The four states are now each derived from an event the server can see for itself:

        transcribing   audio arrived and Parakeet is running
        thinking       `watch` HANDED TURNS OVER and no new watch has started
        synthesizing   `/say` is in flight
        speaking       the client reported playback beginning
        idle           a `watch` is open  <- this endpoint

    Nothing is left for the agent to remember, which means nothing is left for it to forget.
    """
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    watching = bool((body or {}).get("open", True))

    # BOTH EDGES, and the second one is the half that was missing. A watch OPENING means the
    # agent is listening; a watch RETURNING means it is not — it is holding turns and deciding
    # what to do, which is precisely "thinking".
    #
    # Reporting only the opening made the state useless in the case that matters: the loop
    # drains by re-watching, so the flip to thinking survived for milliseconds and the real
    # 20-second think that followed the last drain was painted "Listening" the whole way. The owner,
    # 2026-08-07: "I haven't seen the thinking that often. Whenever the watch resolves and there's
    # no watch going on, I don't see thinking."
    #
    # Either direction only ever moves between idle and thinking. Neither may interrupt work
    # already in flight: a watch is routinely re-armed while a reply is still playing (that is the
    # normal loop), and stamping over `speaking` or `synthesizing` would blank the orb
    # mid-sentence.
    state.watch_open = watching
    if watching:
        if state.agent_state == "thinking":
            await _set_agent_state(state, "idle")
    elif state.agent_state == "idle":
        await _set_agent_state(state, "thinking")
    return web.json_response({"agent_state": state.agent_state, "verbose": state.verbose})


async def handle_consumed(request: web.Request) -> web.Response:
    """The agent reports how far it has read — mirrors `mc consumed`.

    This is what turns the page from a transcript into a status display: the user can see the
    gap between what they have said and what the agent has actually processed, instead of
    guessing whether they are talking into a void.
    """
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    try:
        cursor = int((body or {}).get("cursor", -1))
    except (TypeError, ValueError):
        return web.json_response({"error": "cursor must be an int"}, status=400)
    state.consumed_cursor = cursor
    # The agent has the turn in hand. Everything from here to `say_requested` is IT thinking —
    # the stage that dominated every measurement and was invisible until it had a name.
    timing.stamp(state.session, "consumed", cursor=cursor)
    agent_state = str((body or {}).get("state") or "thinking")
    await _broadcast_json(
        state,
        {"type": "consumed", "cursor": cursor, "pending": max(0, state.turns_logged - 1 - cursor)},
    )
    await _set_agent_state(state, agent_state)
    if agent_state == "thinking":
        await _push_cue(state, "thinking")
    # `verbose` rides back on the response every `watch` already makes, so the agent learns the
    # current preference without polling for it. A toggle the agent has to remember to ask about
    # is a toggle that silently stops working.
    return web.json_response(
        {"consumed": cursor, "state": agent_state, "verbose": state.verbose}
    )


async def handle_ws(request: web.Request) -> web.StreamResponse:
    """The audio channel.

    **Auth happens here, before `prepare()`.** This is the whole point of not relying on HTTP
    middleware: a middleware that short-circuits on non-HTTP scopes would leave this endpoint —
    the one that turns on a microphone — wide open.
    """
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)

    ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=8 * 1024 * 1024)
    await ws.prepare(request)
    state.clients.add(ws)
    # `verbose` rides on the ready frame so the client ADOPTS it rather than pushing its own.
    # `muted` deliberately does NOT — that is a fact about this device's microphone, and a phone
    # muting a desktop in another room would be wrong.
    # The wake name travels too, so the page's prompt cannot drift from what the gate accepts.
    # Hardcoding "hey claude" in the copy is how a renamed assistant keeps telling people to say
    # the old name.
    await ws.send_json(
        {"type": "ready", "session": state.session, "verbose": state.verbose,
         "wake": config.wake_name(), "wake_phrases": list(state.wake.phrases)}
    )
    # Deliver anything said while the phone was asleep, before any new audio arrives.
    try:
        delivered = await _flush_undelivered(state)
        if delivered:
            await _broadcast_json(
                state, {"type": "resumed", "delivered": delivered}
            )
    except Exception as exc:
        state.fail("transport", f"flush undelivered failed: {exc}")

    loop = asyncio.get_running_loop()
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                await _on_audio(state, msg.data, loop)
            elif msg.type == WSMsgType.TEXT:
                await _on_control(state, msg.data, ws)
            elif msg.type == WSMsgType.ERROR:
                state.fail("transport", str(ws.exception()))
                break
    finally:
        state.clients.discard(ws)
        if not state.clients:
            # The last page went away, so nothing is capturing regardless of what it last said.
            state.capturing = False
        # Flush whatever was mid-sentence when the socket dropped, so a disconnect never
        # silently eats the last thing that was said.
        try:
            await _flush(state, loop)
        except Exception as exc:
            state.fail("transport", f"flush failed: {exc}")
        state.close_capture()
    return ws


async def _on_control(state: TunnelState, raw: str, ws: web.WebSocketResponse) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return
    kind = msg.get("type")
    if kind == "hello":
        sr = int(msg.get("sample_rate") or config.TARGET_SR)
        state.client_sr = sr
        await ws.send_json({"type": "hello_ack", "server_sample_rate": config.TARGET_SR})
    elif kind == "played":
        state.last_played = str(msg.get("id") or "")
        timing.stamp(state.session, "played", clip=state.last_played)
        # Playback finished, so the agent is no longer speaking. The client is the only party
        # that knows when a clip actually ended — the server only knows when it finished
        # sending — so the receipt is what closes the state machine back to idle.
        await _set_agent_state(state, "idle")
    elif kind == "verbose":
        # Stored and republished, never acted on here — see TunnelState.verbose.
        state.verbose = bool(msg.get("value"))
        await _broadcast_json(state, {"type": "verbose", "value": state.verbose})
        timing.stamp(state.session, "verbose_toggled", value=state.verbose)
    elif kind == "capturing":
        state.capturing = bool(msg.get("value"))
        if not state.capturing:
            state.last_audio_at = 0.0
    elif kind == "muted":
        state.muted = bool(msg.get("value"))
        if state.muted:
            # Flush whatever was mid-sentence when the mute landed. Two reasons: the buffer would
            # otherwise sit forever holding a half-utterance that no further frame can close, and
            # muting mid-thought should not silently eat the words already spoken — the same
            # guarantee a dropped socket gets.
            try:
                await _flush(state, asyncio.get_running_loop())
            except Exception as exc:
                state.fail("transport", f"flush on mute failed: {exc}")
        await _broadcast_json(state, {"type": "muted", "value": state.muted})
        # DELIBERATELY does not touch agent_state. It used to force "idle" while muted, which
        # made muting mid-thought overwrite what the agent was actually doing — the orb dropped
        # from Thinking to Listening and the elapsed-seconds counter vanished with it, while the
        # agent kept working. Found live on 2026-08-07: "the thinking status had the timer,
        # and then the counter disappeared while it was in thinking status."
        #
        # The rule: agent_state says what the AGENT is doing; `muted` says whether he can be
        # heard. They are different facts about different parties, and expressing one through
        # the other's channel means the receiver cannot recover either.
    elif kind == "channel":
        was_open = state.channel_open
        state.channel_open = bool(msg.get("value"))
        timing.stamp(state.session, "channel", open=state.channel_open)
        if state.channel_open and not was_open:
            # Reopening is what releases anything said while he was away. Same path a reconnect
            # takes, because from the queue's point of view they are the same event: somebody is
            # listening again.
            delivered = await _flush_undelivered(state)
            if delivered:
                # Clear only the TRANSPORT error this delivery disproves. Wiping `last_error`
                # wholesale, as this used to, threw away an unrelated TTS or voiceprint failure
                # that was still true — the mirror image of the bug where one subsystem's error
                # buried another's.
                state.clear_error("transport")
                state.last_error = state.errors.get("tts") or None
        await _broadcast_json(state, {"type": "channel", "value": state.channel_open})
        # Same reason as mute above: closing the channel is HIS state, not the agent's, and
        # publishing it as agent_state destroys the agent's.
    elif kind == "speaking":
        # Deliberately NOT broadcast and NOT stamped per transition. It flips several times a
        # sentence, so publishing it would be a firehose for a flag whose only consumer is the
        # hold loop in `_speak`.
        state.user_speaking = bool(msg.get("value"))
    elif kind == "client_error":
        state.fail("client", str(msg.get("message")))


async def _maybe_barge(state: TunnelState, samples: np.ndarray) -> None:
    """Stop the reply if HE is talking over it — and only if it is him.

    Reported 2026-08-06: "barge in but only for my voice." The constraint is the feature: a tunnel that
    stops talking whenever the room makes a noise is worse than one you cannot interrupt at all.

    **The gate is deliberately "not the agent" rather than "definitely him", because those need
    very different amounts of audio.** Confirming him needs about two seconds (see
    config.BARGE_IN_THRESHOLD for the scores), and two seconds of talking over a reply before it
    stops is an apology, not an interruption. But the one thing that must NEVER trigger this — the
    agent's own voice leaking back through the speakers — scores 0.000 at every window length, so
    a low threshold separates it cleanly at one second.

    Both signals must agree: the client says someone is speaking (it has its own 120 ms minimum
    run against coughs) AND the voiceprint says that someone is not the agent.
    """
    if not config.barge_in_enabled() or state.agent_state != "speaking":
        state.barge_buf = None
        return
    if not state.user_speaking or not state.embedder.available:
        return

    state.barge_buf = (samples if state.barge_buf is None
                       else np.concatenate([state.barge_buf, samples]))
    need = int(config.TARGET_SR * config.BARGE_IN_MIN_MS / 1000)
    if state.barge_buf.size < need:
        return

    try:
        emb = await asyncio.get_running_loop().run_in_executor(
            None, state.embedder.embed, state.barge_buf[-need:]
        )
    except Exception as exc:                       # identity is a bonus; never break ingest
        state.fail("voiceprint", f"barge-in: {exc}")
        state.barge_buf = None
        return
    if emb is None:
        return

    _, score = voiceprint.match(emb)
    state.last_barge_score = round(float(score), 3)
    state.barge_buf = None                          # ask again on the next second, not per chunk
    if score < config.barge_in_threshold():
        return

    state.barges += 1
    timing.stamp(state.session, "barge_in", score=state.last_barge_score)
    await _broadcast_json(state, {"type": "stop_playback", "reason": "barge_in",
                                  "score": state.last_barge_score})
    # Drop anything still queued as well. Stopping the clip he interrupted and then playing the
    # next one at him would be the same interruption wearing a different hat.
    state.undelivered.clear()
    await _set_agent_state(state, "idle")


async def _on_audio(state: TunnelState, raw: bytes, loop: asyncio.AbstractEventLoop) -> None:
    samples = asr_mod.pcm16_to_float32(raw)
    if state.client_sr != config.TARGET_SR:
        samples = asr_mod.resample_linear(samples, state.client_sr, config.TARGET_SR)
    state.frames_received += 1
    state.samples_received += samples.size
    state.last_audio_at = time.monotonic()
    try:
        state.capture(samples)
    except Exception as exc:  # never let diagnostics break the session
        state.fail("capture", str(exc))

    await _maybe_barge(state, samples)

    completed = state.buffer.feed(samples)
    if completed is None:
        _maybe_partial(state, loop)
        return
    await _emit(state, completed, loop)


def _maybe_partial(state: TunnelState, loop: asyncio.AbstractEventLoop) -> None:
    """Fire a live preview transcription of the in-flight utterance, if one isn't already running.

    Scheduled rather than awaited: audio ingest must never block on ASR, or the buffer falls
    behind the microphone and turns arrive late. If a previous partial is still running this
    one is simply skipped — the preview gets sparser under load instead of queueing up work
    that will be stale by the time it finishes.
    """
    if config.PARTIAL_INTERVAL_S <= 0 or state.partial_busy:
        return
    now = time.monotonic()
    if now - state.last_partial_at < config.PARTIAL_INTERVAL_S:
        return
    if not state.buffer.speech_active:
        return
    audio = state.buffer.snapshot()
    if audio is None or audio.size == 0:
        return
    state.partial_busy = True
    state.last_partial_at = now

    async def run() -> None:
        try:
            text = await loop.run_in_executor(None, state.recognizer.try_transcribe, audio)
            if text and text != state.partial_text:
                state.partial_text = text
                await _broadcast_json(state, {"type": "partial", "text": text})
        except Exception as exc:  # a preview failing must never break the session
            state.fail("asr", f"partial: {exc}")
        finally:
            state.partial_busy = False

    loop.create_task(run())


async def _flush(state: TunnelState, loop: asyncio.AbstractEventLoop) -> None:
    completed = state.buffer.flush()
    if completed is not None:
        await _emit(state, completed, loop)


async def _emit(state: TunnelState, completed, loop: asyncio.AbstractEventLoop) -> None:
    """Transcribe a completed utterance, gate it, log it, and tell the client."""
    samples, t_start, t_end = completed
    # The clock the USER experiences starts the moment they stop talking, not when we finish
    # some internal step, so this is where the timing log begins an exchange.
    timing.stamp(state.session, "utterance_end", audio_s=round(t_end - t_start, 2))
    # Announce each stage. The user cannot see any of this from outside, and an unexplained
    # pause is the difference between "it's working" and "it's broken" to someone waiting.
    await _set_agent_state(state, "transcribing")
    # ASR is CPU-bound; keep it off the event loop or audio ingest stalls.
    text = await loop.run_in_executor(None, state.recognizer.transcribe, samples)
    timing.stamp(state.session, "transcribed", chars=len(text or ""))
    if not text:
        # No cue here: nothing was heard, and a sound would announce a turn that does not exist.
        await _set_agent_state(state, "idle")
        return  # silence or a hallucination artifact — never a turn (AC-1)

    # Session-relative audio time, not wall clock: it is monotonic, it matches the timestamps
    # written to the log, and it lets the gate compare "silence between turns" rather than
    # "time since the last transcription finished".
    # Captured BEFORE evaluate, which extends the window as it grants: if the voiceprint then
    # rejects this turn, the window it opened has to be wound back or a stranger's speech
    # would hold the conversation open on the owner's behalf.
    window_anchor = state.wake.window_anchor
    wake_said, agent_text = state.wake.evaluate(text, now=t_start, ended=t_end)
    grant = state.wake.last_grant

    # Voice identity. Learn from turns the wake phrase already confirmed, and let a confident
    # match grant attention on turns where it did not fire. Additive only — see
    # voiceprint.should_address for why a match can never withhold attention.
    speaker, similarity, scored = None, 0.0, False
    if state.embedder.available:
        try:
            emb = await loop.run_in_executor(None, state.embedder.embed, samples)
            if emb is not None:
                speaker, similarity = voiceprint.match(emb)
                scored = True
                if wake_said:
                    rec = voiceprint.enroll(config.owner_name(), emb)
                    state.voice_samples = int(rec.get("count", 0))
        except Exception as exc:      # identity is a bonus; never break a turn over it
            state.fail("voiceprint", str(exc))

    addressed, reason = voiceprint.should_address(
        wake_said, speaker, similarity, owner=config.owner_name(),
        grant=grant, scored=scored,
    )
    if not addressed:
        state.wake.mark_addressed(window_anchor)
    if addressed and not wake_said:
        # Identity granted this one, so identity has to extend the window too — otherwise the
        # conversation only ever continues from phrases, and short follow-ups (too brief for the
        # embedder to score) drop out mid-exchange. See WakeGate.mark_addressed.
        state.wake.mark_addressed(t_end)
    turn = store.append_turn(
        session=state.session,
        text=agent_text,
        t_start=t_start,
        t_end=t_end,
        addressed=addressed,
        reason=reason,
    )
    state.turns_logged += 1
    timing.stamp(state.session, "turn_logged", turn=turn.get("id"),
                 text=(agent_text or "")[:80], addressed=addressed,
                 # WHICH mechanism closed this turn, and what the model cost. Without it,
                 # "is turn detection helping" is a matter of opinion — and it goes in the
                 # timing log rather than the turn schema, because that schema is a
                 # published contract agents parse and this is an internal diagnostic.
                 end_reason=state.buffer.last_end_reason,
                 turn_model_ms=(round(state.turn.last_ms, 1) if state.turn else None))
    state.partial_text = ""  # the final turn supersedes any live preview
    await _broadcast_json(state, {"type": "turn", **turn})
    # Republish the read-lag so the gap between "said" and "read by the agent" stays visible
    # without the agent having to report anything.
    await _broadcast_json(
        state,
        {
            "type": "consumed",
            "cursor": state.consumed_cursor,
            "pending": max(0, state.turns_logged - 1 - state.consumed_cursor),
        },
    )
    await _push_cue(state, "heard")
    await _set_agent_state(state, "idle")


def build_app(session: str, token: str | None, gate_enabled: bool = True) -> web.Application:
    app = web.Application()
    app["state"] = TunnelState(session, token, gate_enabled)
    app.add_routes(
        [
            web.get("/", handle_index),
            web.get("/health", handle_health),
            web.get("/status", handle_status),
            web.post("/say", handle_say),
            web.post("/shutdown", handle_shutdown),
            web.post("/consumed", handle_consumed),
            web.post("/watching", handle_watching),
            web.post("/cue", handle_cue),
            web.post("/rate", handle_rate),
            web.post("/verbose", handle_verbose),
            web.post("/wake", handle_wake),
            web.get("/ws", handle_ws),
        ]
    )
    return app


def run(
    session: str = "dev",
    host: str = config.DEFAULT_HOST,
    port: int = config.DEFAULT_PORT,
    token: str | None = None,
    gate_enabled: bool = True,
) -> None:
    store.validate_session(session)
    if token is None:
        token = os.environ.get("VOICE_TUNNEL_TOKEN") or security.generate_token()

    is_loopback = security.ip_in_cidrs(host, config.LOOPBACK_CIDRS) or host in ("localhost",)
    if not is_loopback and not token:
        raise SystemExit(
            "refusing to bind a non-loopback interface without a token; set VOICE_TUNNEL_TOKEN"
        )

    app = build_app(session, token, gate_enabled)

    # Load the voice model NOW, on a background thread, so the ~4 s load does not land on the
    # first thing the agent says. That is the worst possible place for it: the user has just
    # spoken and is waiting to learn whether any of this works. Backgrounded rather than awaited
    # so the URL is printable and the socket is accepting connections while it loads.
    threading.Thread(target=tts.warm, name="tts-warm", daemon=True).start()
    # Same reason, same shape: 4.7 s cold against 133 ms warm, and unwarmed that cost lands on the
    # very first turn. Reached through the app because the state lives there, not in this scope.
    _turn = app["state"].turn
    if _turn is not None:
        threading.Thread(target=_turn.warm, name="turn-warm", daemon=True).start()

    # flush=True: without it the banner sits in the pipe buffer when serve is launched
    # detached (which is the normal way an agent runs it), so the operator never sees the URL.
    print(f"voice-tunnel serving   http://{host}:{port}/?token={token}", flush=True)
    print(f"  session            {session}", flush=True)
    # The FIRST thing an agent reads after starting the tunnel, because it is the one instruction
    # this CLI cannot enforce on its own. Everything else here makes staying in `watch` the easy
    # path; nothing makes an agent RE-ENTER once it has fallen out, and that is the failure this
    # project keeps hitting — five times in one session, always the same shape: answer in prose,
    # end the turn, and the person on the phone is talking to nobody.
    print("  watchdog           register a ~1/min job in YOUR harness that re-arms `watch` "
          "(see `voice-tunnel describe` -> watchdog)", flush=True)
    print(f"  log                {store.log_path(session)}", flush=True)
    # The banner is what the operator reads before speaking, so it has to say the phrase. Without
    # this line the one thing you must know to use the tool at all was the one thing it never
    # told you — and a name set on a previous run persists, so it is not safely guessable either.
    if gate_enabled:
        print(
            f'  say                "hey {config.wake_name()}"  '
            f"(`voice-tunnel serve --wake <your name>` to change it)",
            flush=True,
        )
    else:
        print("  wake gate          OFF — every turn is treated as addressed", flush=True)
    # Which mechanism ends a turn, on the banner, because a silent fall back to the fixed
    # timer and a deliberate one look identical from the outside — the same reason the TTS
    # line says whether the resident path is live.
    _t = app["state"].turn
    if _t is None:
        print("  turns end on       a fixed silence timer (turn detection off)", flush=True)
    elif not turndetect.installed():
        print(f"  turns end on       a fixed {config.END_OF_UTTERANCE_MS} ms silence "
              f"(`voice-tunnel download turn` to use prosody instead)", flush=True)
    else:
        print("  turns end on       smart-turn — when you sound finished, not on a timer",
              flush=True)
    print(f"  tts                {tts.available()}", flush=True)
    print(
        f"  voice              speed {config.speech_speed()}x, "
        f"{config.sentence_pause()}s between sentences  (`voice-tunnel rate` to change and persist)",
        flush=True,
    )
    print(f"  allowlist          {', '.join(security.allowed_cidrs())}", flush=True)
    # THE WAY OUT, on the banner that starts it. This told you how to rename the assistant and how
    # to change the speech rate, and not how to shut the thing down — an audit had to find `stop`
    # by reading the whole `describe` payload. The command that starts a background process should
    # say how it ends, in the same breath.
    print(f"  stop it with       voice-tunnel stop --session {session}", flush=True)
    print(
        "  (a phone needs HTTPS: `tailscale serve` this port — a LAN IP will NOT work)",
        flush=True,
    )
    web.run_app(app, host=host, port=port, print=None)
