"""vm.server — the tunnel. Serves the client page and carries audio both ways.

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
import time
from typing import Any, Dict, Optional, Set

from aiohttp import WSMsgType, web

from . import asr as asr_mod
from . import config, security, store, tts
from .wake import WakeGate

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


class TunnelState:
    """Everything the server knows. Deliberately small — state that grows is state that lies."""

    def __init__(self, session: str, token: Optional[str], gate_enabled: bool = True) -> None:
        self.session = session
        self.token = token
        self.started_at = time.time()
        self.clients: Set[web.WebSocketResponse] = set()
        self.wake = WakeGate(enabled=gate_enabled)
        self.recognizer = asr_mod.Recognizer()
        self.buffer = asr_mod.UtteranceBuffer()
        self.turns_logged = 0
        self.frames_received = 0
        self.samples_received = 0
        self.last_played: Optional[str] = None
        self.last_error: Optional[str] = None
        self.client_sr: int = config.TARGET_SR
        self.consumed_cursor: int = -1
        self.agent_state: str = "idle"
        self.partial_busy: bool = False
        self.last_partial_at: float = 0.0
        self.partial_text: str = ""

    def snapshot(self) -> Dict[str, Any]:
        return {
            "session": self.session,
            "uptime_s": round(time.time() - self.started_at, 1),
            "clients": len(self.clients),
            "frames_received": self.frames_received,
            "audio_seconds": round(self.samples_received / float(config.TARGET_SR), 2),
            "turns_logged": self.turns_logged,
            "consumed_cursor": self.consumed_cursor,
            "pending_turns": max(0, self.turns_logged - 1 - self.consumed_cursor),
            "agent_state": self.agent_state,
            "speech_active": self.buffer.speech_active,
            "last_played": self.last_played,
            "last_error": self.last_error,
            "wake_enabled": self.wake.enabled,
            "wake_phrases": list(self.wake.phrases),
            "tts_backend": tts.available(),
            "asr_model": self.recognizer.model_name,
            "log": store.log_path(self.session),
        }


def _peer_ip(request: web.Request) -> str:
    return (request.remote or "").strip() or "0.0.0.0"


def _request_token(request: web.Request) -> Optional[str]:
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


async def _broadcast_json(state: TunnelState, payload: Dict[str, Any]) -> None:
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
        return web.Response(status=500, text="web/index.html missing")
    return web.FileResponse(path)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def handle_status(request: web.Request) -> web.Response:
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    return web.json_response(state.snapshot())


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
    if not state.clients:
        return web.json_response({"error": "no client connected"}, status=409)

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


async def _speak(state: TunnelState, text: str, voice: Optional[str]) -> Dict[str, Any]:
    """Synthesize, hold if the speaker is mid-sentence, then push the audio.

    Shared by the blocking and fire-and-forget paths so the interruption guard cannot be
    bypassed by choosing the async one.
    """
    await _set_agent_state(state, "synthesizing")
    try:
        pcm, rate = await asyncio.get_running_loop().run_in_executor(
            None, lambda: tts.synthesize(text, voice=voice)
        )
    except tts.TTSError as exc:
        state.last_error = str(exc)
        await _set_agent_state(state, "idle")
        raise

    clip_id = f"clip-{int(time.time() * 1000)}"

    # Do not talk over the speaker. Synthesis is done, but if they are mid-utterance, hold the
    # audio until they finish. JJ, live 2026-07-29: a batch of voice auditions cut across him
    # mid-sentence, which is the difference between a conversation and a machine that shouts.
    # Bounded so a stuck VAD can never silence the agent permanently.
    waited = 0.0
    announced = False
    while waited < 15.0:
        if state.buffer.speech_active:
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
            if state.buffer.speech_active:
                break
        if not state.buffer.speech_active:
            break
    await _set_agent_state(state, "speaking")

    # Header first so the client knows how to interpret the bytes that follow.
    await _broadcast_json(
        state,
        {
            "type": "audio_header",
            "id": clip_id,
            "sample_rate": rate,
            "bytes": len(pcm),
            "text": text,
            "held_for": round(waited, 1),
        },
    )
    for ws in list(state.clients):
        try:
            await ws.send_bytes(pcm)
        except Exception:
            state.clients.discard(ws)
    return {
        "queued": True,
        "id": clip_id,
        "seconds": round(len(pcm) / 2 / rate, 2),
        "held_for": round(waited, 1),
    }


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
    agent_state = str((body or {}).get("state") or "thinking")
    await _broadcast_json(
        state,
        {"type": "consumed", "cursor": cursor, "pending": max(0, state.turns_logged - 1 - cursor)},
    )
    await _set_agent_state(state, agent_state)
    return web.json_response({"consumed": cursor, "state": agent_state})


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
    await ws.send_json({"type": "ready", "session": state.session})

    loop = asyncio.get_running_loop()
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                await _on_audio(state, msg.data, loop)
            elif msg.type == WSMsgType.TEXT:
                await _on_control(state, msg.data, ws)
            elif msg.type == WSMsgType.ERROR:
                state.last_error = str(ws.exception())
                break
    finally:
        state.clients.discard(ws)
        # Flush whatever was mid-sentence when the socket dropped, so a disconnect never
        # silently eats the last thing that was said.
        try:
            await _flush(state, loop)
        except Exception as exc:
            state.last_error = f"flush failed: {exc}"
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
    elif kind == "client_error":
        state.last_error = f"client: {str(msg.get('message'))[:300]}"


async def _on_audio(state: TunnelState, raw: bytes, loop: asyncio.AbstractEventLoop) -> None:
    samples = asr_mod.pcm16_to_float32(raw)
    if state.client_sr != config.TARGET_SR:
        samples = asr_mod.resample_linear(samples, state.client_sr, config.TARGET_SR)
    state.frames_received += 1
    state.samples_received += samples.size

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
            text = await loop.run_in_executor(None, state.recognizer.transcribe, audio)
            if text and text != state.partial_text:
                state.partial_text = text
                await _broadcast_json(state, {"type": "partial", "text": text})
        except Exception as exc:  # a preview failing must never break the session
            state.last_error = f"partial: {exc}"
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
    # Announce each stage. The user cannot see any of this from outside, and an unexplained
    # pause is the difference between "it's working" and "it's broken" to someone waiting.
    await _set_agent_state(state, "transcribing")
    # ASR is CPU-bound; keep it off the event loop or audio ingest stalls.
    text = await loop.run_in_executor(None, state.recognizer.transcribe, samples)
    if not text:
        await _set_agent_state(state, "idle")
        return  # silence or a hallucination artifact — never a turn (AC-1)

    # Session-relative audio time, not wall clock: it is monotonic, it matches the timestamps
    # written to the log, and it lets the gate compare "silence between turns" rather than
    # "time since the last transcription finished".
    addressed, agent_text = state.wake.evaluate(text, now=t_start, ended=t_end)
    turn = store.append_turn(
        session=state.session,
        text=agent_text,
        t_start=t_start,
        t_end=t_end,
        addressed=addressed,
    )
    state.turns_logged += 1
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
    await _set_agent_state(state, "idle")


def build_app(session: str, token: Optional[str], gate_enabled: bool = True) -> web.Application:
    app = web.Application()
    app["state"] = TunnelState(session, token, gate_enabled)
    app.add_routes(
        [
            web.get("/", handle_index),
            web.get("/health", handle_health),
            web.get("/status", handle_status),
            web.post("/say", handle_say),
            web.post("/consumed", handle_consumed),
            web.get("/ws", handle_ws),
        ]
    )
    return app


def run(
    session: str = "dev",
    host: str = config.DEFAULT_HOST,
    port: int = config.DEFAULT_PORT,
    token: Optional[str] = None,
    gate_enabled: bool = True,
) -> None:
    store.validate_session(session)
    if token is None:
        token = os.environ.get("VM_TOKEN") or security.generate_token()

    is_loopback = security.ip_in_cidrs(host, config.LOOPBACK_CIDRS) or host in ("localhost",)
    if not is_loopback and not token:
        raise SystemExit(
            "refusing to bind a non-loopback interface without a token; set VM_TOKEN"
        )

    app = build_app(session, token, gate_enabled)
    # flush=True: without it the banner sits in the pipe buffer when serve is launched
    # detached (which is the normal way an agent runs it), so the operator never sees the URL.
    print(f"voice-mode serving   http://{host}:{port}/?token={token}", flush=True)
    print(f"  session            {session}", flush=True)
    print(f"  log                {store.log_path(session)}", flush=True)
    print(f"  tts                {tts.available()}", flush=True)
    print(f"  allowlist          {', '.join(security.allowed_cidrs())}", flush=True)
    print(
        "  (a phone needs HTTPS: `tailscale serve` this port — a LAN IP will NOT work)",
        flush=True,
    )
    web.run_app(app, host=host, port=port, print=None)
