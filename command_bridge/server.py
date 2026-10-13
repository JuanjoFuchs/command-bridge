"""command_bridge.server — the tunnel. Serves the client page and carries audio both ways.

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
import datetime as _dt
import heapq
import json
import os
import pickle
import signal
import threading
import time
import wave
import weakref
from typing import Any

import numpy as np
from aiohttp import WSMsgType, web

from . import asr as asr_mod
from . import colors as colors_mod
from . import config, cues, pairing, security, speech, store, timing, tts, turndetect, voiceprint
from . import lanes as lanes_mod
from . import guest
from . import liaison as liaison_mod
from . import native as native_mod
from . import notices
from .wake import WakeGate

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
"""The client page ships INSIDE the package, not beside it.

This used to resolve to the parent of the package — correct in a checkout, and in an installed
wheel it points at `site-packages/web`, which does not exist. The failure would have been the
entire user interface returning 500 while every test on a developer's machine passed, because a
checkout always has the directory in both places."""


class _Pieces:
    """One turn's pieces, transcribed at his pauses while he is still talking.

    Parakeet is offline: a whole-turn decode costs ~0.1 s per second of speech and all of it lands
    AFTER he stops (2026-10-06: 11-20 s on minute-long turns). Committing a piece at each pause
    leaves only the tail for after he stops. Indices are in the segmenter's buffer samples, valid
    only while its `utterance_id` equals `base`."""

    def __init__(self, base: int | None = None) -> None:
        self.base = base
        self.upto: int | None = None
        """Buffer index the committed pieces cover up to; None until the first piece."""
        self.texts: list[str] = []
        self.task: asyncio.Task | None = None
        self.failed = False
        """A piece raised: the turn falls back to one whole decode rather than lose words."""
        self.cut = 0
        """The segmenter's front trim for the finished utterance, set at the close."""

    def usable(self, utterance_id: int) -> bool:
        return self.base == utterance_id and self.upto is not None and not self.failed


class TunnelState:
    """Everything the server knows. Deliberately small — state that grows is state that lies."""

    def __init__(self, session: str, token: str | None, gate_enabled: bool = True) -> None:
        self.session = session
        self.token = token
        self.started_at = time.time()

        # WHERE THE LOG STOOD WHEN THIS PROCESS STARTED — the debt line for `unanswered_s`.
        #
        # A restart meets a log it did not live through. Measured on the first one after FR8
        # shipped: with no baseline the scan reached the top of a 2,442-turn history and reported
        # `unanswered_s: 1035657` — twelve days, which is the age of the LOG rather than the length
        # of his wait, and it would have read as an alarm on every restart forever.
        # **An agent cannot owe a reply to a turn it was never given.**
        try:
            self.baseline_turn_id = store.last_turn_id(session)
        except OSError:
            self.baseline_turn_id = -1
        self.clients: set[web.WebSocketResponse] = set()
        # The pages that said in their `hello` they can play a STREAMED clip (2026-10-07). Weak, so
        # a closed socket drops out on its own without a second cleanup path to keep in step.
        self.stream_clients: weakref.WeakSet = weakref.WeakSet()
        # SPEC 044: the apps that declared the primitive protocol in their `hello`, each with what it
        # last reported. Only these get `native` directives; a browser or an older app never does.
        self.native_clients: dict[Any, native_mod.NativeClient] = {}
        # His output pick on the app's caret, kept in the session dir so a restart keeps it.
        self.native_pick: int | None = native_mod.load_pick(config.session_dir())
        self.wake = WakeGate(enabled=gate_enabled)
        self.lanes = lanes_mod.LaneRegistry(lanes_mod.lane_name_for_wake(config.wake_name()))
        """Who is in the meeting, and who is being talked to (spec 012 FR1).

        The server's own `--wake` name is the DEFAULT lane, and it is the lane every turn already
        on disk belongs to — those turns carry no `lane` key, and a live session has thousands of
        them. Until a second lane is registered this is a one-element registry that can neither
        switch nor refuse, which is what makes NFR3 true by construction rather than by care."""
        # 🔴 THE ROOM SURVIVES A RESTART (spec 019 FR1). The registry itself is pure state with no
        # I/O — deliberately, so the routing rules stay unit-testable without a session directory —
        # so the RESTORE lives here, at the one place that has both a session name and a registry.
        #
        # ⚠ **The DEFAULT is not restored, only the guests.** `--wake` is what he typed on THIS
        # start; a persisted default silently overriding it would make the flag a suggestion. A
        # name that is already the default is skipped rather than re-added.
        _persisted = store.read_lanes(session)
        for _name in _persisted:
            if _name != self.lanes.default:
                try:
                    self.lanes.add(_name)
                except lanes_mod.LaneError:
                    pass       # a stale or now-invalid name must not stop the server starting
        # AND THEIR ORDER (spec 029 FR3). The default is always re-created first, so without this a
        # `lane order` that moved it was undone by every restart.
        try:
            self.lanes.reorder([n for n in _persisted if n in self.lanes.names])
        except lanes_mod.LaneError:
            pass
        # Hues assigned to lanes the roster does not colour, by name (spec 029 FR5).
        self.lane_colors_assigned: dict[str, str] = store.read_lane_colors(session)
        # AND THE LANE HE WAS ON (JJ, 2026-09-30: *"I want the lane that I am on to be remembered across
        # restarts. So that this way you can just restart."*). Every restart used to drop him on the
        # default lane, so his next sentence to dexter was delivered to magnus — twice that afternoon.
        # Restored only if that lane is still in the room.
        _live = store.read_live_lane(session)
        if _live and _live in self.lanes.names:
            self.lanes.current = _live
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
        """The rate from the most recent `hello`, kept for `status`. NOT used to resample: see
        `client_srs`."""
        self.client_srs: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        """Each socket's own announced rate (spec 030 review). One session-wide rate let whichever
        client said `hello` last decide how EVERY client's frames were resampled — the Android app's
        16 kHz audio reached ASR at a third of its speed the moment a 48 kHz canvas page opened."""
        self.pairing = pairing.Pairing(session)
        """Live one-time pairing codes and the bad-code lockout (spec 031); devices live on disk."""
        self.client_mic: dict = {}
        """The page mic's actual processing settings, from its `hello` (see handle of "hello")."""
        self.consumed_cursor: int = store.read_consumed_cursor(session)
        """Seeded from disk, not from -1: the turn log survives a restart, so the read position
        must too, or a bounced server reports the whole log as pending (the 307-of-306 bug,
        2026-08-14). -1 still means "never consumed" — a fresh session has no file."""
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
        self.last_speech_at: float = 0.0
        """`time.monotonic()` of the last frame the segmenter called speech (or an open utterance).
        0 means never. The pre-speech grace measures his silence from here instead of starting a
        fresh 0.8 s wait every time."""
        self.muted: bool = False
        """Whether the client is holding its own microphone shut. Published for `status` only —
        the mute is enforced ON THE CLIENT by not sending frames, because a mute that still
        streams audio to a server that promises to ignore it is not a mute anyone should trust.

        **Mute is one direction only.** He cannot be heard; he can still hear. That is the whole
        difference from `channel_open` below, and separating them is what lets him silence his
        own microphone in a meeting without also losing the reply he is waiting for."""
        self.guest_lanes: set[str] = guest.load(session)
        """Spec 042: lanes where a voice that is not his is still addressed (tagged `guest`)."""
        self.liaisons: dict[str, list[str]] = liaison_mod.load(session)
        """Specs 037/038: each liaison lane → the workers it manages explicitly; empty = mode off.
        Persisted. See `liaison.py` for the rules."""
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
        it: idle is also what a freshly started server reports before any agent has ever called, so
        a caller that read idle as "someone is listening" would stay silent on exactly the session
        where nobody ever arrived.

        It is what the concurrent-watch refusal reads: a watch run DETACHED leaves the harness
        idle, so without a positive "an agent is in `watch`" signal the tool cannot tell a detached
        listener from an absent one, and would let a second watch start and race the first.

        **Session-wide, and it stays that way**: it means literally "some agent is waiting here".
        With lanes that is no longer the question a second agent needs answered, so it is joined
        by `watching_lanes` rather than redefined. Redefining it would have silently changed the
        answer given to every existing caller (spec 012 TC7)."""

        self.lane_states: dict[str, str] = {}
        """What EACH agent is doing (FR8), keyed by lane.

        `agent_state` above stays the LIVE lane's state and keeps its original meaning, because it
        is what the orb paints and the orb must not flicker with a background agent's work. This
        is the fan-out that lets the lane strip show all of them at once — which is what he asked
        for: *"I would like to be able to keep track of, still, see if one agent is thinking,
        transcribing, or synthesizing."*"""

        self.speaking_lane: str | None = None
        """WHOSE CLIP IS CURRENTLY IN THE AIR (spec 016 FR3).

        The `played` receipt arrives from the client carrying a clip id and nothing else, and it is
        the only party that knows when audio actually STOPPED — the server knows only when it
        finished sending. So the receipt is what returns a lane to rest, and by the time it lands
        the conversation may have moved to somebody else. Recording the owner when the clip goes
        out is what stops that receipt releasing the wrong agent."""

        self.lane_spoke_at: dict[str, float] = {}
        """When each lane last SPOKE, as a wall clock, for `unanswered_s` (spec 013 FR8).

        **The half of the bound the log cannot supply.** The turn log says when he spoke; only the
        server knows when an agent answered. Together they say how long he has been waiting, which
        is what turns *"fold the turns in and wait again"* from an instruction with no exit into a
        number an agent can compare — the livelock he named on 2026-08-24: *"you get blocked in
        watch instead of responding."*

        ⚠ **Keyed by lane and not session-wide.** With three agents, Codex answering says nothing
        about how long Claude has kept him waiting, and a session-wide stamp would report the most
        responsive lane's clock to every other one."""

        self.lane_consumed: dict[str, int] = store.read_lane_consumed(session)
        """How far EACH lane has actually read (spec 015 FR1). Seeded from disk like
        `consumed_cursor`, so a restart does not bring every lane back having "read nothing" and
        light up every old turn as unread on the cards (2026-09-30).

        🔴 **`consumed_cursor` is one integer for the whole session, and with N lanes that is a
        lie.** Whichever lane's `watch` reported last overwrites it, so a turn addressed to Claude
        renders as read the moment Atlas reads something later — a turn Claude's own
        `watch --lane claude` never delivered and never will.

        Spotted by JJ on the page, 2026-08-24: *"whenever one lane reads, I think we're marking
        everything as read."* 🎯 **It is the same seam as the unread-refusal bug caught that
        morning, and the half that was left:** `012` made delivery per-lane, `013` built the
        receipt on the session-wide cursor, and nothing connected them.

        ⚠ **Monotonic per lane.** A cursor only ever moves forward here, because a `watch` resuming
        from an older `--since` is re-reading, not un-reading, and a receipt that flickered back to
        grey would be worse than one that was never drawn."""

        self.lane_read_through: dict[str, int] = {}
        """Per lane, the highest turn id that lane had in hand when it last SPOKE (013 FR7).

        **The blue-tick half of the receipt, and the only one of the three states that is real
        evidence.** He asked for WhatsApp's semantics: *"once it's sent, it's one check. Once it
        arrives to the destination, it's two checks. And once the recipient has read the message,
        the two checks turn blue."*

        ⚠ **In this tool "delivered" and "read" are the SAME event** — a `watch` both hands a turn
        over and advances the cursor — so a naive mapping would have two of the three states rise
        together and the third never appear. What actually separates them is ANSWERING: the cursor
        moving proves an agent was given the turn, and a clip spoken afterwards proves something
        was done with it. That is the distinction he cares about, and it is the one a machine can
        honestly make."""

        self.lane_held: dict[str, list] = {}
        """Clips an off-lane agent produced while he was talking to somebody else (FR7).

        ⚠ **DELIBERATELY NOT `undelivered`.** That queue is cleared wholesale by barge-in, which
        is correct for the lane he is ON — playing the next clip at a man who just interrupted is
        the same interruption wearing a different hat — and wrong for a lane he is NOT on, which
        he has not interrupted and cannot even hear. TC3 records nine clips lost from that queue
        on 2026-08-20; sharing it would have inherited the bug on purpose.

        ⚠ **THIS DOCSTRING SPENT FIVE SPECS ATTACHED TO THE WRONG FIELD.** Specs 021 through 025
        each inserted a new attribute between `lane_held` and the string documenting it, so by
        2026-08-26 it was thirty lines below and reading as `playing_clip`'s. Keep new fields
        BELOW this closing quote."""

        # Replies that aged out before he came back to that lane, waiting to be handed to the
        # agent that wrote them the next time it opens a watch. Cleared on delivery, so each
        # expiry is reported exactly once and a re-armed watch does not keep re-announcing it.
        self.lane_expired: dict[str, list] = {}
        # 🔴 THE OTHER HALF OF `lane_expired`: clips the PAGE CONFIRMED it played, per lane, handed to
        # the agent that wrote them on its next watch-open (JJ, 2026-09-29: *"the client should have
        # a way of telling you that, right?"*). The page has always sent a `played` receipt; the
        # server used it only to free its copy, so an agent could never tell a clip he HEARD from one
        # still held — which is what sent agents re-saying clips he had already heard. Bounded by the
        # same count cap, cleared on delivery.
        self.lane_played: dict[str, list] = {}
        # THE IDLE ANCHOR (spec 024). Each lane's newest played receipt — {clip, played_at} — kept
        # beside `lane_played` because that list is cleared when a watch takes it, while the anchor
        # must survive every re-arm: `watch --idle` measures his silence from it. A cut clip counts
        # (KD6): its receipt marks the moment the sound stopped. In memory only, so a restart has no
        # anchor until a clip plays again, and therefore cannot produce a spurious idle (NFR3).
        self.lane_last_played: dict[str, dict] = {}
        # The anchor clip whose silence was already reported to that lane (spec 024 FR4): one idle
        # wake per silence, shared by every watch on the lane so a re-arm cannot report it twice.
        self.lane_idle_fired: dict[str, str] = {}
        # clip id -> the text spoken, so a `played` receipt (which carries only the id) can report
        # WHAT was heard. Popped on the receipt.
        self.clip_text: dict[str, str] = {}
        # The echo guard's memory (echo.py): recent clips as {id, text, sent_at, played_at}. Kept
        # apart from `clip_text`, which is popped on the receipt — the echo can still be arriving
        # after it.
        self.recent_spoken: list[dict] = []
        # lane -> the id of the ONE watch that owns it (the newest). See handle_watching.
        self.lane_watch_ids: dict[str, str] = {}
        # Clips between `say` and being stored (synthesizing, or holding for his speech), for the
        # graceful-shutdown drain; and the moment the page confirmed the shutdown notice played.
        self.synth_inflight: int = 0
        self.shutdown_notice_played: asyncio.Event | None = None
        # 🔴 SENT TO THE CLIENT, NOT YET CONFIRMED PLAYED (spec 022). The flush used to POP from
        # `lane_held` and push straight to the browser — which walked the clips out of the one
        # store barge-in is documented never to touch, and into the playback queue it empties
        # wholesale. JJ lost three of Kepler's replies that way on 2026-08-26. Holding a copy here
        # until the `played` receipt arrives is what makes the client queue disposable again.
        self.lane_inflight: dict[str, list] = {}
        # The lane that was live when he opened his mouth, or None between utterances. Routing
        # reads THIS rather than `lanes.current`, so a switch made while he is still being
        # transcribed does not redirect what he already said (spec 023).
        self.utterance_lane: str | None = None
        # 🔴 WHICH LANE EACH IN-FLIGHT CLIP BELONGS TO (spec 024). `speaking_lane` is ONE slot for
        # N agents, so a second clip going out overwrites the first, and the first `played`
        # receipt then releases the wrong lane and nulls the slot — leaving the real speaker on
        # "speaking" with nothing left that can clear it. JJ, 2026-08-26: *"I see an agent lane
        # that has its hand raised. And when I switch to it, it doesn't start playing. And another
        # lane says speaking."* The receipt has always carried a clip id; this is what lets it
        # name a lane.
        self.clip_owner: dict[str, str] = {}
        # A monotonic ticket per clip handed to the browser, in send order. The browser plays them
        # in the order it received them, so the OLDEST ticket still in flight is the one on the
        # speaker — which is what `playing_clip` derives from. A counter rather than a timestamp
        # because a flush sends clips that were composed minutes ago, and when they were WRITTEN
        # says nothing about when they were SENT.
        self._sent_seq: int = 0

        self.ambiguous: int = 0
        """How many summons this session could not route (TC2). Monotonic, never reset.

        A COUNTER rather than a flag, because the wait compares snapshots and a flag that goes up
        and down can be missed entirely between two polls — the agent would see the same `false`
        twice and conclude nothing happened. A number that only ever increases cannot hide an
        event that occurred between two reads of it."""

        self.last_ambiguous: list[str] = []
        """Which lanes the last unroutable summons was torn between, so the agent asking "did you
        mean Claude or Codex?" can name them instead of asking him to start over."""

        self.watching_lanes: set[str] = set()
        """WHICH lanes have a wait open. The single-waiter guard is per lane, not per session.

        The guard itself is right and stays: two waits on one log race for the same turns, so one
        cursor silently falls behind. But N agents watching N lanes is the NORMAL case here, and a
        session-wide flag would refuse every agent after the first — the feature would not work at
        all for its second user."""
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
        self.agent_holds_turns: bool = False
        """Has the agent been handed turns it has not yet answered?

        **THE ONE FACT THAT SEPARATES "CHECKING BEFORE I SPEAK" FROM "LISTENING", and it is
        DERIVED, never declared.** Both calls are the same command with the same arguments against
        the same tunnel state, so nothing else can tell them apart — and they want opposite
        things. Before speaking, the agent needs an answer in milliseconds. While listening, it
        needs to block, because an immediate empty return would make the loop it is required to
        sit in (`RULE_1`) a hot spin.

        Reported live 2026-08-17, on the first version that got this wrong: *"I don't like that
        this wait. If I say nothing, this waits for 30 seconds. That's slow."* Removing the rungs
        from the speaking path while leaving them on the silent path made the pre-reply pause
        WORSE than the drain it replaced — 30 s against 10.5 s.

        Set when turns are handed over (`/consumed`), cleared when the agent speaks (`/say`) and
        when a wait comes back with nothing — the check happened and the answer was "nothing new".
        That is exactly the drain loop: while turns keep arriving the next check stays instant,
        and the first empty one ends the batch.

        Same principle as `agent_state` itself: *"a status the agent announces is a claim, and a
        claim is wrong exactly when it matters."* Nothing here is a flag the caller passes, which
        is the point — an option is a decision an agent makes wrong under time pressure."""
        self.lane_holds_turns: dict[str, bool] = {}
        """The same question as `agent_holds_turns`, asked PER LANE (spec 018 FR1).

        🔴 **The single boolean above is set by ANY lane taking a turn and cleared by ANY lane's
        empty watch**, and those are different agents. So an agent about to answer him could have
        its pre-reply check downgraded to a listen by a completely unrelated lane going quiet —
        and then wait a full backoff rung before speaking.

        Reported 2026-08-25, timed on his phone: *"that watch resolves immediately because I am
        silent. But on the other lanes, it doesn't... and it goes the full sixty seconds before
        resolving. What that costs me is time. Time I'm waiting for an answer should have arrived
        sixty seconds earlier."*

        ⚠ **`agent_holds_turns` KEEPS ITS MEANING** — "some agent here is holding turns" — because
        it is published and read by callers that predate lanes (TC1, and the same ruling spec 012
        made for `watch_open`). This is the fan-out beside it, not a redefinition of it."""

        self.last_refusal: dict[str, tuple[int, int]] = {}
        """The identity `(since, last_turn_id)` of each lane's most recently built refusal.

        🔴 **KEYED BY LANE (spec 018 FR4).** It was one pair for the whole session, so one agent's
        refusal made a DIFFERENT agent's first refusal render as a repeat — ids instead of the turn
        text, on the one occasion the text is the thing that agent needs to recover. The saving
        below is per agent, so the memo has to be too.

        **THE MEMO THAT MAKES A REFUSED BATCH COST ONE COPY OF THE TURN INSTEAD OF N** (spec 011,
        FR1). Measured 2026-08-19: four `say --now` clips fired back to back, all four refused, and
        each refusal carried the full text of the same ~700-character turn — 6,268 characters to
        report one event. **The longer the thought the agent was trying to deliver, the more clips
        it takes, and the more it is charged for being interrupted.** That is exactly backwards.

        The pair is the whole identity, and both halves are load-bearing: `since` is where the
        agent has read to, `last_turn_id` is the head of the log. **A new turn moves the head; a
        successful read moves the cursor.** So either of the two things that could make the omitted
        text newly relevant changes the identity by itself, and the next refusal is full again —
        without this needing a hook on `/consumed` or on the append path, where it would rot.

        Kept HERE rather than in a module global because a global is shared by every session in a
        process and cannot be driven by a test, and because the refusal builder must be reproducible
        in-process: two calls with the same state and the same unread set produce first-then-repeat
        with no server and no HTTP."""
        self.refusal_repeat: dict[str, int] = {}
        """How many refusals each lane has already been answered on its current identity.

        Keyed by lane for the same reason as `last_refusal` above (spec 018 FR4): the count is a
        statement about one agent's retries, and sharing it charged the wrong agent for them.

        0 on the first (which carries the turn text in full), then 1, 2, 3 … on the repeats (which
        carry the ids alone). Published as `refusal_repeat` so the agent can see how many times it
        has now tried to speak over the same unread turn — a number that is itself a signal, since
        a rising count means the recovery `watch` is not being run."""
        self.speech_pending: int = 0
        """Utterances that have CLOSED and are not yet in the log — transcription in flight.

        THE INVARIANT MADE OBSERVABLE. He stated it himself, 2026-08-17: *"the gist is making sure
        that there's any speech drained before you speak."* A count of pending speech is that
        sentence as a field.

        It exists because there is a window in which both speech signals read false and he has
        nevertheless just spoken: the segmenter has closed the utterance and the recognizer has not
        finished. Measured 2026-08-05 at ~1-2 s, and ~13 s on long dictation. A wait that returned
        in that window would hand back nothing while he sat waiting for an answer — the same
        failure as interrupting him, wearing different clothes.

        Incremented synchronously at the close, before any await, so no `/status` can be served
        between the buffer emptying and this rising."""
        self.embedder = voiceprint.Embedder()
        self.voice_samples: int = 0
        self.partial_busy: bool = False
        self.last_partial_at: float = 0.0
        self.partial_text: str = ""
        self.pieces = _Pieces()
        self.lane_briefs: dict[str, list[dict[str, Any]]] = {}
        """Context posted for a lane's VOICE agent by the agent thinking behind it (2026-10-06).

        JJ's split: a fast agent holds the conversation and a slow one does the thinking, feeding
        it context. Claude Code delivers a message to a busy agent only between tool calls, and a
        voice agent is always inside a blocking `watch` — so the context sat queued until he spoke.
        `listen` is `watch` that ALSO returns when context lands here; `brief` posts it. Taken
        (and cleared) on delivery, like `expired` and `played`. In memory: a restart drops it."""
        """The in-flight turn's pieces, transcribed at his pauses while he is still talking. See
        `_maybe_commit`."""
        # Failsafe capture: everything the server actually received, at 16 kHz. Borrowed from
        # a sibling project, where it repeatedly turned "the ASR is bad" into a question you can
        # answer by listening — is the audio quiet, clipped, or fine and the model just wrong?
        self._wav: wave.Wave_write | None = None

    def hand_to_client(self, clip: dict) -> dict:
        """Stamp a clip with its place in the send order and return it (spec 026 F6).

        Every clip that goes on the wire gets one, from both senders — `_speak` for a live lane and
        `_flush_lane_held` for a lane he has come back to."""
        self._sent_seq += 1
        clip["seq"] = self._sent_seq
        return clip

    @property
    def playing_clip(self) -> str | None:
        """THE CLIP ON THE SPEAKER RIGHT NOW, or None — derived from the send order (spec 025).

        Barge-in must not return this one: he heard it, at least in part, and stopping it is
        exactly what makes the browser fire `onended` and send its receipt, so the receipt always
        lands a few milliseconds BEHIND the barge. Measured 2026-08-26: returned at 16:09:56.121,
        receipt at 16:09:56.129, and the clip played again five minutes later. JJ: *"you read back
        a turn that you have said a while ago... So something's up with the fix you did."*

        🔴 **THIS WAS A STORED FIELD FOR ONE DAY AND IT NAMED THE WRONG CLIP.** `_speak` assigned it
        on every send, so with two replies out it pointed at the SECOND — the one still queued —
        while the browser was playing the first. A barge would then have handed back the reply he
        had just heard and dropped the one he had not: the exact inversion of the fix. Derived from
        the send order it cannot be wrong, and it needs no clearing: a `played` receipt removes the
        clip from `lane_inflight`, which advances this to the next one on its own.
        """
        oldest = None
        for clips in self.lane_inflight.values():
            for c in clips:
                if oldest is None or c.get("seq", 0) < oldest.get("seq", 0):
                    oldest = c
        return (oldest.get("header") or {}).get("id") if oldest else None

    @property
    def agent_state(self) -> str:
        """What the agent he is TALKING TO is doing — derived, never stored (spec 026 FR1).

        🔴 **THIS WAS A CACHE WITH ONE WRITER AND NO INVALIDATION, AND IT COST HIM A REPLY.**
        `_set_agent_state` wrote it only when the lane it was told about was the live one, and
        `_set_lane` never recomputed it. So from the instant he switched lanes it described **the
        lane he had left**, and went on describing it until that new lane happened to change state
        on its own.

        The barge gate read it. He switched to Atlas while Magnus was mid-reply, said something to
        Atlas, and the gate — still holding Magnus's `speaking` — fired and killed Magnus's clip.
        *"But I didn't barge in. I just switched the lane and was listening. I didn't interrupt
        you."* (2026-08-26)

        🎯 The meaning never changed; only the mechanism was wrong. `lane_states` is the truth and
        has been since spec 016, and this is the live lane's entry in it. A derived value cannot go
        stale, which is the only durable answer to a class of bug that has now been fixed nine
        times one instance at a time.
        """
        return self.lane_states.get(self.lanes.current, "idle")

    @agent_state.setter
    def agent_state(self, value: str) -> None:
        """Assigning it means "the live lane is doing this" — the meaning it always had (TC1)."""
        self.lane_states[self.lanes.current] = value

    def pending_turns(self, last_turn_id: int | None = None) -> int:
        """How many turns he has said that nobody has read — from the LOG, never from a counter.

        This was `turns_logged - 1 - consumed_cursor`, and `turns_logged` is the field this file's
        own snapshot calls its impostor and no cursor should ever resume from: it counts what
        THIS process has written since it started, so on any server that has been restarted it is
        far below the real last id. Live on 2026-08-14, mid-conversation: `last_turn_id` 209,
        `turns_logged` 133, so the arithmetic clamped to zero and `pending_turns` read 0 no matter
        how far behind the reader actually was.

        THE FIELD IS WORSE THAN MISSING WHEN IT LIES. The obvious way to mechanise "drain until
        there is nothing left" is to loop until pending is zero, and a field that is already zero
        turns that loop into a single pass — the exact "he must have finished" mistake the drain
        discipline exists to stop. It also drives the page's own "read to here — N more below"
        divider, so the person on the phone was being told he was caught up while he was not.

        `last_turn_id` is passed in by callers that have already paid for the disk read, so
        publishing both in one snapshot costs one read rather than two.
        """
        if last_turn_id is None:
            last_turn_id = store.last_turn_id(self.session)
        return max(0, last_turn_id - self.consumed_cursor)

    def lane_cursor(self, lane: str | None) -> int:
        """WHERE HAS **THIS LANE** READ TO — the one place that answers it (spec 017 FR1).

        🔴 **`consumed_cursor` is a single number for the whole session, and every lane's `watch`
        overwrites it.** Spec 015 gave each lane its own cursor for the transcript's tick marks and
        stopped there, so everything ELSE that asks "where have you read to" still asks the session
        — and gets whichever lane read most recently.

        Measured live 2026-08-25 with three lanes on his phone: `consumed_cursor` 2604 while
        `lane_consumed` held `{magnus: 2604, atlas: 2597, kepler: 2589}`. **A kepler agent asking
        where to resume was told 2604 and would have skipped fifteen turns**, with the unread count
        reading zero the whole way, because the count was measured against the session number too.

        ⚠ **The session cursor is the FALLBACK, not the rival.** A lane that has never reported has
        no cursor of its own, and thousands of turns predate lanes entirely — for both, the session
        number is the honest answer, and in a solo session it is the same number anyway (NFR1).
        """
        if lane is None:
            return self.consumed_cursor
        return self.lane_consumed.get(lane, self.consumed_cursor)

    def frames_stale(self) -> bool:
        """Has audio stopped arriving for longer than it would have taken to end an utterance?

        **THE HALF OF THE MUTED-FLAG FIX THAT COVERS THE CASES NOBODY REPORTS.** Every event that
        stops frames also flushes the buffer, so the words are never lost — but events only cover
        what the client tells us about. This covers what it does not: frames still in flight when a
        mute lands and re-opening the buffer after the flush, an Android tab suspended in the
        background with its socket alive, a wedged page holding a live connection and sending
        nothing. Each of those leaves an utterance open forever.

        Under the old drain that was a wrong narration — the agent announcing it was waiting for
        someone whose microphone is off. Live, 2026-08-01: *"whenever I mute, you say that you're
        listening and you're waiting for me to finish, but I'm muted."* Under a speaking-gated wait
        (spec 005) the same lie HANGS THE TUNNEL, because the wait never observes him stopping.

        **The window is not a new constant, and that is deliberate.** It is END_OF_UTTERANCE_MS —
        exactly the silence that would have closed the utterance had frames kept coming. So the
        rule is not a guess about staleness, it is the segmenter's own rule applied to the case
        where the audio simply stopped: *frames stopping means speech stopped.*
        """
        if not self.clients or not self.capturing or self.muted:
            return True
        if not self.last_audio_at:
            return True
        return (time.monotonic() - self.last_audio_at) > (config.END_OF_UTTERANCE_MS / 1000.0)

    def speaking_now(self) -> dict[str, Any]:
        """The three facts a wait gates on, decided in ONE place.

        Published by `snapshot` and read by `_speak`'s hold loop, so the agent and the server can
        never disagree about whether he is talking. They used to compute it separately — `_speak`
        with a muted exception, `status` without one — which is how the CLI ended up carrying a
        "a MUTED microphone leaves speech_active stuck true, check muted first" caveat that every
        reader had to remember to apply.

        `speech_active` is the SERVER's segmenter and lags by a buffer plus a hop; `user_speaking`
        is the CLIENT reading its own microphone level and leads by ~120 ms. Spec 005 states the
        asymmetry that matters: the lagging one may END a wait, the leading one may only EXTEND it.
        """
        stale = self.frames_stale()
        return {
            "speech_active": False if stale else bool(self.buffer.speech_active),
            "user_speaking": False if stale else bool(self.user_speaking),
            "speech_pending": max(0, int(self.speech_pending)),
            # An utterance not yet closed, even through a pause the turn model is waiting out.
            "utterance_open": False if stale else bool(getattr(self.buffer, "utterance_open", False)),
        }

    def talking(self, *, amplitude: bool = True) -> bool:
        """Is he mid-sentence right now — EITHER signal, additively.

        A false positive costs a moment of delay; a false negative costs interrupting him, and the
        two are not worth the same. `speech_pending` counts too: an utterance already closed and
        still being transcribed is speech that has not reached anyone yet.

        `amplitude=False` DROPS `user_speaking` and trusts the segmenter alone (`speech_active` +
        `speech_pending`). `user_speaking` is the client reading its own microphone LEVEL, which
        rises on any room sound — a fan, a door, another voice — while `speech_active` is energy
        above an ADAPTIVE noise floor and absorbs steady noise instead of firing on it. The SAY-HOLD
        passes this so a synthesized clip stops being held back by ambient noise the segmenter never
        called speech: nothing here knows in real time that a sound is HIM (the voiceprint scores an
        utterance only after it closes), so for a clip already protected by barge-in the honest hold
        is the noise-robust one. Live 2026-09-04: *"are we just checking amplitude?"* — the hold was.
        Left ON (the default) for the lane-latch and everywhere else, where the amplitude lead is the
        earliest warning and a moment's over-hold costs nothing.
        """
        s = self.speaking_now()
        speech = s["speech_active"] or s["speech_pending"] or s.get("utterance_open")
        return bool(speech or s["user_speaking"]) if amplitude else bool(speech)

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

    @staticmethod
    def _turn_wall_seconds(turn: dict[str, Any]) -> float | None:
        """A turn's `wall` stamp as epoch seconds, or None if it cannot be read.

        The log's own timestamp is used rather than an arrival time the server would have to keep,
        so this survives a restart: an agent that comes back to a session mid-conversation still
        learns how long he has been waiting.
        """
        raw = turn.get("wall")
        if not isinstance(raw, str):
            return None
        try:
            return _dt.datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return None

    def lane_split_for(self, lane: str | None) -> dict[str, int]:
        """One lane's held-clip count split into `intent` vs `regular` (2026-09-22 idea #2).

        A single number ("3 waiting") can't tell him whether switching lanes is worth it: an
        intent clip is a supersedable *"on it…"* promise (spec 012) that the real reply will
        replace, so a lane whose whole backlog is intent is one he does NOT need to interrupt his
        current lane for. Splitting it lets the orb badge de-emphasise intent and make the regular
        count the thing that pulls his attention.

        `intent` is carried on each held clip's `header` (set where the clip is enqueued, and the
        same flag the supersede sweep reads).
        """
        clips = self.lane_held.get(str(lane), [])
        intent = sum(1 for c in clips if (c.get("header") or {}).get("intent"))
        return {"intent": intent, "regular": len(clips) - intent}

    def lane_waiting_split(self) -> dict[str, dict[str, int]]:
        """Every held lane's `intent`/`regular` split — sparse, absent means nothing held."""
        return {name: self.lane_split_for(name)
                for name, clips in self.lane_held.items() if clips}

    def lane_unanswered(self) -> dict[str, float]:
        """Per lane, how long he has been waiting for that lane to say something (spec 013 FR8).

        **The bound for the fold loop.** `watch` tells an agent to fold returned turns in and wait
        again; while he keeps talking that instruction never terminates, and he ends up saying
        *"respond"* to an agent that is following the rule exactly. This is the number that makes
        the exit observable: it RISES while he talks and RESETS when the lane answers.

        ⚠ **A lane that has never spoken is measured from THIS SERVER'S START**, not from the top
        of the log — a fresh agent that has been listening for two minutes without replying is
        worth reporting, and a twelve-day-old turn it was never given is not. See the comment on
        the baseline below for what that read before it was bounded.

        Absent from the result means the lane owes nothing, which is the common case and is why
        this is a sparse dict rather than one entry per registered lane.
        """
        now = time.time()
        out: dict[str, float] = {}
        try:
            turns = store.read_turns(self.session)
        except OSError:
            return out
        for lane in self.lanes.names:
            # WHICH turns are outstanding is decided by ID, and only HOW LONG is decided by a
            # clock. The first version compared the turn's `wall` against a float baseline and
            # could not do it reliably: `wall` is written to the SECOND, so a turn logged in the
            # same second as the baseline lands up to a second on the wrong side of it. Measured
            # both ways within one test run — a turn at 16:56:40 against a baseline of
            # 16:56:40.45 read as older than the server, and a one-second tolerance then stopped
            # an answer from ever clearing the debt inside the same second.
            #
            # 🎯 **Ids are exact and monotonic; timestamps at this resolution are neither.** Use
            # the precise instrument for the question that needs precision, and keep the clock for
            # the one place only a clock can answer — how many seconds have passed.
            since_id = self.lane_read_through.get(lane)
            if since_id is None:
                since_id = self.baseline_turn_id
            # 🔴 **A LANE THAT HAS NEVER SPOKEN IS MEASURED FROM THIS SERVER'S START, not from
            # zero.** Measured on the first restart after this shipped: with no baseline the scan
            # reached the oldest turn in a 2,442-turn log and reported `unanswered_s: 1035657` —
            # twelve days. It was the age of the LOG, not of his wait, and it would have read as
            # an alarm on every restart forever.
            #
            # 🎯 **An agent cannot owe a reply to a turn it was never given.** The log outlives the
            # process; a lane's debt cannot. The docstring above said "measured from its oldest
            # unanswered turn" and was right about a lane joining a live session and wrong about a
            # process meeting a history — the same sentence, two situations, one of them absurd.
            oldest: float | None = None
            for turn in turns:
                if not turn.get("addressed"):
                    continue
                if int(turn.get("id", -1)) <= since_id:
                    continue
                if not store.turn_is_for(turn, lane, self.lanes.default):
                    continue
                at = self._turn_wall_seconds(turn)
                if at is None:
                    continue
                oldest = at if oldest is None else min(oldest, at)
            if oldest is not None:
                out[lane] = round(max(0.0, now - oldest), 1)
        return out

    def snapshot(self) -> dict[str, Any]:
        # ONE disk read, two fields. `last_turn_id` and `pending_turns` are the same question
        # asked twice, and reading the log twice per status call is how a hot path acquires a
        # cost nobody chose.
        last_id = store.last_turn_id(self.session)
        return {
            "session": self.session,
            "uptime_s": round(time.time() - self.started_at, 1),
            # THE LIVE WAKE NAME. `command-bridge wake` reads this to show what the running gate
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
            # re-arming caller once took it for the cursor: 26 against a real last id of 367, which
            # would have replayed three hundred old turns as if they had just been spoken. The two are only
            # equal on a server that has never restarted, which is the case every author tests in
            # and no long-running session is ever in.
            #
            # Named for what it is and published beside its impostor, because the trap was never
            # that the number was hard to find — it was that a plausible wrong one was closer to
            # hand.
            "last_turn_id": last_id,
            "turns_logged": self.turns_logged,
            "consumed_cursor": self.consumed_cursor,
            # COMPUTED FROM THE LOG, like `last_turn_id` and for the same reason. It used to be
            # `turns_logged - 1 - consumed_cursor`, which read 0 on every restarted server no
            # matter how far behind the reader was — a field whose obvious use ("drain until it
            # is zero") was a one-pass no-op. See `pending_turns`.
            "pending_turns": self.pending_turns(last_id),
            "agent_state": self.agent_state,
            # WHO is being talked to (spec 012 FR1). Published even when there is only one lane:
            # a field that appears once a feature is in use is a field nothing can rely on, and
            # `watch` has to be able to tell "no lanes here" from "an old server that never had
            # them" — the same absent-versus-false distinction `watch_open` was bitten by.
            "lane": self.lanes.current,
            "lanes": list(self.lanes.names),
            "default_lane": self.lanes.default,
            # THE THREE SPEECH FACTS COME FROM ONE PLACE. `speech_active` used to be published
            # straight off the buffer, which is true and misleading the moment frames stop: a
            # muted or backgrounded page leaves an utterance open forever and the flag sticks
            # true. See `frames_stale`. `user_speaking` had the same hole from the other side —
            # nothing server-side ever reset it, so a socket that died mid-word left it true.
            **self.speaking_now(),
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
            # Spec 044: each connected app driven by the primitive protocol — its version, the ops it
            # supports and what it last reported (mic, call, route, in front or not).
            "native_clients": [c.as_status() for c in self.native_clients.values()],
            "native_route_pick": self.native_pick,
            # Specs 037/038: who speaks for the fleet. `liaison` is the first (back-compat); `liaisons`
            # the whole map, liaison → its explicit workers ({} when every lane speaks for itself).
            "liaison": next(iter(self.liaisons), None),
            "liaisons": {k: list(v) for k, v in self.liaisons.items()},
            # Spec 042: lanes where someone else's voice is addressed too (tagged speaker: guest).
            "guest_lanes": sorted(self.guest_lanes),
            "watch_open": self.watch_open,
            "watching_lanes": sorted(self.watching_lanes),
            "lane_watch_ids": dict(self.lane_watch_ids),
            # Context posted for a lane's voice agent and not yet collected (`brief` / `listen`).
            "lane_briefs_pending": {name: len(b) for name, b in self.lane_briefs.items() if b},
            "lane_waiting": {name: len(clips) for name, clips in self.lane_held.items() if clips},
            # The same backlog split intent vs regular (2026-09-22 idea #2), alongside the total so
            # every existing count-consumer is untouched; the orb badge reads this to de-emphasise
            # intent and make the regular count the thing that pulls his attention.
            "lane_waiting_split": self.lane_waiting_split(),
            "lane_states": dict(self.lane_states),
            "lane_unanswered": self.lane_unanswered(),
            "lane_read_through": dict(self.lane_read_through),
            "lane_consumed": dict(self.lane_consumed),
            "client_mic": dict(self.client_mic),
            "ambiguous": self.ambiguous,
            "last_ambiguous": list(self.last_ambiguous),
            "agent_holds_turns": self.agent_holds_turns,
            # THE SAME QUESTION PER LANE (spec 018 FR1). `agent_holds_turns` above still answers
            # "is any agent here holding turns", unchanged for every caller that predates lanes;
            # this is the fan-out a lane reads to decide whether ITS next wait is a pre-reply
            # check. Published rather than inferred — the CLI cannot see the other lanes.
            "lane_holds_turns": dict(self.lane_holds_turns),
            # spec 024: the idle anchor per lane, and which anchor's silence was already reported.
            "lane_last_played": {k: dict(v) for k, v in self.lane_last_played.items()},
            "lane_idle_fired": dict(self.lane_idle_fired),
            "barges": self.barges,
            "last_barge_score": self.last_barge_score,
            "turn_detection": (self.turn.describe() if self.turn else {"enabled": False}),
            "last_end_reason": self.buffer.last_end_reason,
            # `user_speaking` is published above, from `speaking_now`, alongside the two facts it
            # is only meaningful next to. It used to sit here on its own reporting the client's
            # last message verbatim — which is why a dead socket could leave it true forever.
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
            # `log` and `capture_wav` (the session jsonl + failsafe wav paths) were dropped here
            # 2026-09-15: informational-only — the agent never opens them, and `command-bridge
            # doctor` carries the paths when they are actually needed. (Server-side; landed on the
            # restart that followed.)
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
    # THE LAN LISTENER'S COOKIE (spec 034 FR2). Only the LAN listener sets it, after a `?token=`
    # request, so a browser page opened over the home Wi-Fi still authenticates its own sub-requests.
    if _on_lan(request):
        return (getattr(request, "cookies", None) or {}).get(LAN_TOKEN_COOKIE) or None
    return None


LAN_TOKEN_COOKIE = "cb_token"


def _on_lan(request: web.Request) -> bool:
    """Did this request arrive on the home-Wi-Fi listener (spec 034)? Decided by the LOCAL address
    the socket was accepted on, which a client cannot forge, not by anything in the request."""
    lan = request.app.get("lan") if hasattr(request, "app") else None
    if not lan:
        return False
    transport = getattr(request, "transport", None)
    sock = transport.get_extra_info("sockname") if transport is not None else None
    return bool(sock) and str(sock[0]) == lan["ip"]


def _check(request: web.Request, state: TunnelState) -> tuple[bool, str]:
    # On the LAN listener the allowlist is the Wi-Fi subnet and nothing else (spec 034 FR2); the
    # loopback listener keeps the configured allowlist exactly as before.
    gate = (security.Gate(state.token, cidrs=(request.app["lan"]["subnet"],)) if _on_lan(request)
            else security.Gate(state.token))
    token = _request_token(request)
    # A PAIRED DEVICE KEY STANDS IN FOR THE TOKEN (spec 031 FR4). The phone app cannot do the
    # tunnel's browser sign-in, so it carries its own credential; the peer allowlist still applies.
    # The app's WebView carries the same key as a COOKIE (spec 032 FR3): a WebView cannot add a header
    # to its page's sub-requests or its WebSocket, but it does send a cookie with every one of them.
    device = (request.headers.get(pairing.DEVICE_HEADER)
              or (getattr(request, "cookies", None) or {}).get(pairing.DEVICE_COOKIE))
    if device and pairing.device_ok(state.session, device):
        token = state.token
    return gate.check(
        _peer_ip(request),
        token,
        request.headers.get("X-Forwarded-For"),
    )


async def _set_agent_state(state: TunnelState, value: str, lane: str) -> None:
    """Publish what the agent is doing: idle | thinking | waiting | speaking.

    The point is that silence is ambiguous. Without this the user cannot tell "it did not hear
    me" from "it heard me and is working" from "it is holding back so as not to interrupt" —
    and all three look like a broken tool.

    With several agents in the room the same question is asked N times, so the state is ALSO
    recorded per lane (FR8). `agent_state` keeps meaning what it always meant — what the agent he
    is TALKING TO is doing — because that is what the orb shows and the orb must not start
    flickering with a background agent's work. `lane_states` is the fan-out.

    🔴 **`lane` IS REQUIRED, AND THAT IS THE WHOLE OF SPEC 016 FR4.** It used to default to the
    live lane, which made every stage a pair of independent lookups — one when the stage opened,
    one when it closed — with real work in between. The live lane moves during that work: he says
    "hey magnus" while atlas is live, `transcribing` is attributed to atlas because the words have
    not been recognised yet, the gate then moves the conversation, and the `idle` that would end
    the stage resolves to magnus. **Atlas is left reading `transcribing` until the page reloads.**
    Reported 2026-08-25: *"if I am talking to you, I do see that the Atlas lane says
    transcribing."*

    So the owner is now an input rather than a lookup: whoever opens a stage names the lane and
    passes the same name to the close. A caller that genuinely means "the live lane" says
    `state.lanes.current` at the top of its flow and holds it — which is the fix, spelled at the
    only place that knows how long the flow is.
    """
    who = lane
    # ONE WRITE, and `agent_state` reads through to it (spec 026 FR1). This used to be followed by
    # a second, conditional assignment into a session-wide cache — which is the assignment that
    # never happened again when he switched lanes, leaving the cache describing the lane he left.
    state.lane_states[who] = value
    await _broadcast_json(state, {"type": "agent_state", "state": value, "lane": who,
                                  "live": who == state.lanes.current,
                                  # WHO IS ACTUALLY SITTING IN A WAIT, sent with every state change
                                  # so the page can tell "listening" from "working" (015 FR2).
                                  # The server only learns a REPORTED state, and an agent
                                  # heads-down in a long task reports nothing — which is why every
                                  # busy background lane read "idle". This is the fact that
                                  # separates them, and the server already had it.
                                  "watching_lanes": sorted(state.watching_lanes),
                                  "lane_consumed": dict(state.lane_consumed)})


async def _set_lane(state: TunnelState, lane: str, why: str = "wake") -> None:
    """Make `lane` the one being talked to, and tell everybody (spec 012 FR1).

    Idempotent, so the two callers that reach it — the wake gate and an explicit switch — do not
    have to agree about who moved the registry first.

    **`why` is carried because the two reasons are not interchangeable to a reader.** A switch he
    spoke and a switch he tapped look identical in the state and mean different things when a
    transcript is read back later, or when he asks why the conversation moved.
    """
    # (The wait-mode tap-refusal toggle that briefly lived here was removed with specs 023/027 —
    # routing now follows the current lane, so there is no mid-transcription window to guard, and the
    # toggle's friction — a tap to hear an agent's held clip was refused too — is gone with it.)
    # 🔴 A DELIBERATE SWITCH OUTRANKS A LATCH MADE BEFORE IT (spec 027).
    #
    # `utterance_lane` is latched on the silence→speech edge and cleared in exactly one place —
    # when a turn is routed. So a speech edge that never produces a turn (a cough, a false start,
    # a blip under the turn model's threshold) leaves it set, and the `is None` guard that holds it
    # across an utterance then stops it ever being re-latched. It survives every switch after that
    # and delivers his next real sentence to the lane he was standing on when the noise happened.
    #
    # Measured 2026-08-27: he tapped to atlas at 12:24:46 and began speaking SIXTEEN SECONDS later,
    # and the turn was still delivered to magnus. He had to re-say it with the name on the front.
    # JJ: *"I had switched lanes before I started speaking, yet my previous turn was attributed to
    # you."* · *"That is critical, is currently blocking me interacting with the other agents."*
    #
    # ⚠ **ONLY WHEN NO UTTERANCE IS IN FLIGHT, AND `talking()` IS THE ONLY HONEST TEST OF THAT.**
    #
    # 🔴 The first draft of this asked `buffer.speech_active`, and that would have REINTRODUCED
    # spec 023. The window that spec exists for is not while he is talking — measured above, ASR
    # takes 3.3 s AFTER the buffer closes, and `speech_active` reads false for every millisecond
    # of it. Switching lanes inside that window is not a corner case, it is the exact gesture he
    # asked for: *"I would like to finish speaking and be able to switch to a different lane while
    # that thing that I just said just finishes transcribing."*
    #
    # `talking()` already composes the three signals — speech active, his client's own report, and
    # `speech_pending` for an utterance closed and still in transcription — and exists precisely
    # because "both speech signals read false while he has in fact just spoken."
    # 🔬 EVIDENCE: did this switch CLEAR the latch (he was between utterances — the next one re-latches
    # to `lane`) or KEEP it (he is mid-transcription, so the in-flight turn stays with the lane it was
    # spoken to)? A kept latch that then blocks the NEXT utterance's re-latch is the reported bug.
    if not state.talking():
        state.utterance_lane = None
        timing.stamp(state.session, "latch_cleared_on_switch", to=lane)
    else:
        timing.stamp(state.session, "latch_kept_talking", held=state.utterance_lane, to=lane)
    state.lanes.current = lane
    store.write_live_lane(state.session, lane)   # remembered across restarts (2026-09-30)
    # Spec 004 — the voice lane IS the canvas lane. This is the ONE place the live lane moves (the
    # wake gate, the tap, and an explicit switch all reach it), so driving the canvas here, in
    # process, is what lets it follow with no `/status` poll. `set_live` is a quick lock + fan-out
    # and no-ops when the lane is unchanged, so it is safe on this hot path and harmless before the
    # canvas is initialised (a bare set on an inert store).
    from .canvas import server as _canvas
    _canvas.set_live(lane)
    timing.stamp(state.session, "lane", lane=lane, why=why)
    await _broadcast_json(
        state,
        {"type": "lane", "lane": lane, "lanes": list(state.lanes.names), "why": why,
         # The page needs it to render a turn that carries NO lane key (013 FR3): absent means the
         # default lane, and thousands of turns predate lanes. Sent on every lane message rather
         # than only at handshake, so a page that connected late is never the one that gets it wrong.
         "default_lane": state.lanes.default,
         # EVERY lane's colour by NAME (2026-10-01; spec 029 FR5: unrostered lanes too, assigned once).
         "lane_colors": _lane_colors(state),
         "lane_roles": config.lane_roles(),     # each agent's role, shown on its card (2026-10-05)
         "waiting": {name: len(clips) for name, clips in state.lane_held.items() if clips},
         "waiting_split": state.lane_waiting_split()},
    )
    # HIS ATTENTION IS BACK HERE, so what this lane said while he was away stops being an
    # interruption and becomes an answer. Best-effort: a failure to play a held clip must never
    # be able to prevent the switch itself, or a broken clip would trap the conversation.
    try:
        await _flush_lane_held(state, lane)
    except Exception as exc:
        state.fail("transport", f"flush lane hold failed: {exc}")


def _native_routes(state: TunnelState) -> dict[str, Any]:
    """The output caret's picture (spec 044 FR5), from the first driven app that reported outputs."""
    client = next((c for c in state.native_clients.values() if c.devices), None)
    return native_mod.routes_payload(client, state.native_pick)


async def _broadcast_routes(state: TunnelState) -> None:
    await _broadcast_json(state, _native_routes(state))


async def _broadcast_json(state: TunnelState, payload: dict[str, Any]) -> None:
    dead = []
    for ws in list(state.clients):
        try:
            # SPEC 044: a driven app gets its directives for this event first (a notification for
            # a turn, the call released on a channel close). The policy is native_mod's; this is
            # the one place every broadcast passes, so no event can be missed by a new sender.
            client = state.native_clients.get(ws)
            if client is not None:
                for d in native_mod.on_broadcast(payload, client, channel_open=state.channel_open,
                                                 picked=state.native_pick):
                    await ws.send_json(d)
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.clients.discard(ws)


# --- objective fleet activity (spec 021) -------------------------------------
# The page's live state rides THIS socket, so the objective per-lane activity (files read/edited/
# written, git status, last commit — derived from git + the harness session logs, never self-reported)
# rides it too: on the `ready` frame for an immediate render, and on a paced `lane_activity` broadcast
# to keep the fleet home view live. Derivation is git + file IO, so it always runs in a thread.

FLEET_REFRESH_S = 2.0
"""How often the server recomputes and broadcasts objective per-lane activity. Paced, not instant:
the derivation tails the session logs and shells git, and the fleet view reads as live at this
cadence. `fleet.cached_board`'s 1s TTL collapses a burst of readers (the ready frame + this loop)."""


def _canvas_frame_lanes() -> set:
    """Which lanes currently hold at least one canvas frame — the 'has something to point at' gate for
    the on-demand canvas (spec 021 FR6). Read from the canvas store's per-lane frame map; best-effort
    (never fail the activity payload over it)."""
    try:
        from .canvas import server as canvas_server
        return {ln for ln, frames in getattr(canvas_server, "_lanes", {}).items() if frames}
    except Exception:
        return set()


def _fleet_activity() -> dict:
    """Objective per-lane activity shaped for the page: {server_epoch, lanes:{name: activity}}.
    Each lane also carries `has_frame` — whether it has a pointable canvas frame — so a card can mark
    an openable canvas (FR6). SYNCHRONOUS (git + file IO) — always call it through an executor."""
    from . import fleet
    board = fleet.cached_board()
    framed = _canvas_frame_lanes()
    lanes = {}
    for row in board.get("lanes", []):
        name = row.get("lane")
        if not name:
            continue
        act = row.get("activity") or {}
        # THE FRESHEST ROW WINS, not the last one (JJ, 2026-09-28: "other agents are also editing
        # files, but I don't see them drawn … I only see your edits"). The registry keeps one file per
        # PANE, and a lane relaunched into a new pane leaves its old pane files behind — 26 rows for 6
        # lanes. Plain last-write-wins let a dead duplicate (an old session, zero files in the window)
        # overwrite the live row: kepler's 29 edited files came through as none.
        prev = lanes.get(name)
        if prev is not None and (prev.get("last_activity_epoch") or 0) >= (act.get("last_activity_epoch") or 0):
            continue
        lanes[name] = {**act, "has_frame": name in framed}
    # Repos he asked to hide (spec 045) never reach the page: read from disk on every call, so a
    # `fleet hide` lands on the next broadcast (FLEET_REFRESH_S) with no restart and no refresh.
    lanes = fleet.filter_hidden(lanes, fleet.load_hidden())
    return {"server_epoch": board.get("server_epoch"), "lanes": lanes}


async def _broadcast_fleet_activity(app: web.Application) -> None:
    """Push the objective activity map to every client on a fixed cadence, so the fleet home view
    updates without the page polling. A failure logs and the loop continues rather than taking the
    server down; it stays quiet while nobody is connected."""
    state = app["state"]
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(FLEET_REFRESH_S)
        if not state.clients:
            continue
        try:
            payload = await loop.run_in_executor(None, _fleet_activity)
            await _broadcast_json(state, {"type": "lane_activity", **payload})
        except Exception as exc:
            state.fail("fleet", f"activity broadcast failed: {exc}")


async def _start_fleet_broadcaster(app: web.Application) -> None:
    app["fleet_task"] = asyncio.create_task(_broadcast_fleet_activity(app))


async def _stop_fleet_broadcaster(app: web.Application) -> None:
    task = app.get("fleet_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _serve_page(name: str) -> web.Response:
    """One page document, stamped with its own signature (spec 023) so a pushed `reload` can tell
    the open page whether ITS file changed.

    The page itself is not secret — the token is. Serving it unauthenticated keeps the phone flow
    to a single URL, and it can do nothing without a valid token on the socket."""
    from .canvas import aio as canvas_aio
    stamped = canvas_aio.stamp_page(name)
    if stamped is None:
        return web.Response(status=500, text=f"command_bridge/web/{name} missing")
    # Never cache the client. The page is edited constantly during development and a stale copy
    # is the worst kind of bug to chase: the server has the fix, the user reloads, and nothing
    # changes — so you conclude the fix did not work and go break something else.
    return web.Response(
        text=stamped[0],
        content_type="text/html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


async def handle_index(request: web.Request) -> web.Response:
    """`/` is the AUDIO SHELL (spec 023): the microphone, playback and the voice socket, with the
    whole visible UI loaded inside it as a frame — so a UI change reloads the frame and never the
    audio."""
    return _serve_page("shell.html")


async def handle_ui(request: web.Request) -> web.Response:
    """`/ui` is everything he sees (spec 023), loaded by the shell as a same-origin frame. Opened on
    its own it renders but holds no audio and no socket — which is what the mock and the
    single-page harnesses want."""
    return _serve_page("index.html")


async def handle_meeting(request: web.Request) -> web.Response:
    """The meeting page (spec 006) — the adaptive combined view: participant orbs, the shared canvas,
    and the transcript on ONE page, arranged by (agent count) × (canvas shared?) × (wants to see?).
    Unauthenticated like the index — the token gates the socket, not the page — and never cached, so
    an edit during development is never chased through a stale copy."""
    from . import meeting
    state: TunnelState = request.app["state"]
    return web.Response(
        text=meeting.render(state),
        content_type="text/html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


def _public_base(request: web.Request, offered: Any) -> str:
    """The https origin the page was opened on: the one the page offers if it is https, else what the
    tunnel forwarded (ngrok sets X-Forwarded-Proto/Host), else this request's own origin."""
    if isinstance(offered, str) and offered.startswith("https://"):
        return offered.rstrip("/")
    proto = request.headers.get("X-Forwarded-Proto") or request.scheme
    host = request.headers.get("X-Forwarded-Host") or request.host
    return f"{proto}://{host}"


async def handle_guest(request: web.Request) -> web.Response:
    """`POST /guest {lane, on}` — guest mode on or off for one lane (spec 042). Token-gated."""
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    lane = (body or {}).get("lane")
    if not isinstance(lane, str) or not state.lanes.knows(lane):
        return web.json_response({"error": f"no lane named {lane!r}", "code": "unknown_lane",
                                  "remedy": "command-bridge guest on --lane <lane>",
                                  "lanes": list(state.lanes.names)}, status=400)
    if (body or {}).get("on"):
        state.guest_lanes.add(lane)
    else:
        state.guest_lanes.discard(lane)
    guest.save(state.session, state.guest_lanes)
    timing.stamp(state.session, "guest", lanes=sorted(state.guest_lanes))
    return web.json_response({"guest_lanes": sorted(state.guest_lanes)})


async def handle_liaison(request: web.Request) -> web.Response:
    """`POST /liaison {on, liaisons: {lead: [workers]}}` (or `{on, lane}` for one) — turn liaison mode
    on with that map, or off (specs 037/038). Token-gated."""
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    if body.get("on"):
        raw = body.get("liaisons")
        if raw is None and isinstance(body.get("lane"), str):
            raw = {body["lane"]: []}
        if not isinstance(raw, dict) or not raw:
            return web.json_response({"error": "liaison on needs at least one liaison lane",
                                      "code": "bad_request",
                                      "remedy": "command-bridge liaison on magnus phoenix=atlas,vega"},
                                     status=400)
        try:
            new = liaison_mod.parse([k + ("=" + ",".join(v) if v else "") for k, v in raw.items()])
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc), "code": "bad_request",
                                      "remedy": "command-bridge liaison on magnus phoenix=atlas,vega"},
                                     status=400)
        unknown = [n for lead, ws in new.items() for n in [lead, *ws] if not state.lanes.knows(n)]
        if unknown:
            return web.json_response({"error": f"no lane named {unknown[0]!r}", "code": "unknown_lane",
                                      "remedy": "command-bridge lane list",
                                      "lanes": list(state.lanes.names)}, status=400)
        state.liaisons = new
    else:
        state.liaisons = {}
    liaison_mod.save(state.session, state.liaisons)
    from .canvas import server as _canvas
    _canvas.set_liaisons(state.liaisons)
    await _broadcast_json(state, {"type": "liaison", "liaisons": state.liaisons})
    timing.stamp(state.session, "liaison", liaisons=list(state.liaisons))
    return web.json_response({
        "liaison": next(iter(state.liaisons), None),
        "liaisons": state.liaisons,
        "groups": liaison_mod.groups(state.liaisons, list(state.lanes.names)),
        "camera": ("only a liaison's explicit point --look moves it; edits never do" if state.liaisons
                   else "edits on the live lane auto-focus again (spec 022)")})


_NOTICE_WAKE: dict[str, asyncio.Event] = {}
"""Per session, set when a notice is added or resolved, so a waiting `/notify/wait` returns at once."""

NOTICE_WAIT_MAX_S = 50
"""How long one long poll may wait. Under typical proxy and carrier idle timeouts (60 s), so the phone's
request ends cleanly with an empty answer instead of being cut."""


def _notices_changed(state: TunnelState) -> None:
    ev = _NOTICE_WAKE.pop(state.session, None)
    if ev is not None:
        ev.set()


async def handle_notify_wait(request: web.Request) -> web.Response:
    """`GET /notify/wait?since=<seq>&wait=<s>` — the phone's light connection while the orb is off
    (spec 036). Returns every notice added or resolved after `since`, oldest change first, as soon as
    there is one, or an empty list after `wait` seconds. Token or device key, like everything."""
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    try:
        since = max(0, int(request.query.get("since", "0")))
        wait = max(0.0, min(float(request.query.get("wait", "0")), NOTICE_WAIT_MAX_S))
    except ValueError:
        return web.json_response({"error": "since and wait must be numbers", "code": "bad_query",
                                  "remedy": "GET /notify/wait?since=0&wait=50"}, status=400)
    # The event is taken BEFORE reading, so a notice landing between the read and the wait still
    # wakes this request instead of waiting out the full poll.
    ev = _NOTICE_WAKE.setdefault(state.session, asyncio.Event())
    items, latest = notices.since(state.session, since)
    if not items and wait > 0:
        try:
            await asyncio.wait_for(ev.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
        items, latest = notices.since(state.session, since)
    colours = _lane_colors(state)
    return web.json_response({"items": [{**i, "color": colours.get(i.get("lane", ""))} for i in items],
                              "seq": latest})


async def handle_pair_page(request: web.Request) -> web.Response:
    """`GET /pair` — the pairing page (spec 035): its own link, not a button on the bridge. It mints
    with its own `?token=`, so it can do nothing without one."""
    return _serve_page("pair.html")


async def handle_pair_new(request: web.Request) -> web.Response:
    """`POST /pair/new` — mint a one-time pairing code for the phone app (spec 031 FR1). Token-gated:
    only a page that already holds the token (and is behind the tunnel's sign-in) can mint one."""
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    code, ttl = state.pairing.mint()
    base = _public_base(request, (body or {}).get("base"))
    # THE HOME ADDRESS TOO (spec 034 FR4), so the app learns it without his typing anything. A page
    # opened over the LAN has no https origin, so then the link carries the LAN address alone.
    lan = (request.app.get("lan") or {}).get("url")
    tunnel = base if base.startswith("https://") else None
    return web.json_response({"code": code, "expires_in": ttl, "base": tunnel, "lan": lan,
                              **pairing.pair_links(tunnel, code, lan)})


async def handle_pair_exchange(request: web.Request) -> web.Response:
    """`POST /pair/exchange` — the app trades a one-time code for its device key (spec 031 FR2). No
    token: the code IS the credential, consumed on first use; bad codes count toward a lockout."""
    state: TunnelState = request.app["state"]
    cidrs = (request.app["lan"]["subnet"],) if _on_lan(request) else None
    if not security.peer_allowed(_peer_ip(request), request.headers.get("X-Forwarded-For"), cidrs=cidrs):
        return web.json_response({"error": "peer not in allowlist"}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    status, out = state.pairing.exchange(str((body or {}).get("code") or ""),
                                         str((body or {}).get("name") or ""))
    return web.json_response(out, status=status)


async def handle_android_apk(request: web.Request) -> web.StreamResponse:
    """`GET /android/command-bridge.apk` — the companion app (spec 030 FR9), token-gated like
    `/status`, so he installs it on his phone from the same link he already opens. A missing build is
    a 404 that names the command which produces it, not a bare not-found."""
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    path = config.ANDROID_APK_PATH
    if not os.path.isfile(path):
        return web.json_response({
            "error": "the Android app has not been built",
            "code": "apk_not_built",
            "remedy": "cd android && gradlew assembleDebug (JDK 17 + Android SDK; see specs/030)",
        }, status=404)
    return web.FileResponse(path, headers={
        "Content-Type": "application/vnd.android.package-archive",
        "Content-Disposition": 'attachment; filename="command-bridge.apk"',
        "Cache-Control": "no-store",
    })


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def handle_status(request: web.Request) -> web.Response:
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    return web.json_response(state.snapshot())


# --- held clips survive a graceful restart (JJ, 2026-09-24; backlog 6277) ------------------------
# Held per-lane clips (an agent's off-lane replies, raised as hands) live in `state.lane_held`, in
# MEMORY — so a restart used to wipe them and JJ lost his agents' pending reports ("there are no
# pending turns on any of the lanes"). On a graceful RESTART we pickle them to a sibling of the
# runtime file and reload them on the next startup, so the hands stay raised across the restart. A
# plain stop does not persist (the session is ending). The window is short — a stale file older than
# this is ignored and cleaned up, so clips from a crash days ago never resurrect.
_HELD_STALE_S = 120.0


def _held_path(session: str) -> str:
    return os.path.join(config.session_dir(), f"{session}.held.pkl")


def _persist_held(state: "TunnelState") -> None:
    """Write the held clips to disk so the next server can raise the same hands (best-effort).

    The UNDELIVERED queue goes with them (JJ, 2026-09-29: held clips should *"survive restarts or
    toggles"*): a clip said while nobody was connected is as much his as one held for another lane,
    and a restart used to drop it silently."""
    if not state.lane_held and not state.undelivered:
        return
    try:
        os.makedirs(config.session_dir(), exist_ok=True)
        with open(_held_path(state.session), "wb") as fh:
            pickle.dump({"saved_at": time.time(), "lane_held": state.lane_held,
                         "undelivered": state.undelivered}, fh)
    except Exception as exc:
        state.fail("transport", f"persist held clips failed: {exc}")


async def _restore_held_on_startup(app: web.Application) -> None:
    """Reload held clips a graceful restart left behind, then delete the file (best-effort, inert in
    tests — no file, no-op). Only clips newer than the restart window are restored; anything older is
    a stale leftover and is discarded."""
    state: "TunnelState" = app["state"]
    path = _held_path(state.session)
    try:
        if not os.path.exists(path):
            return
        with open(path, "rb") as fh:
            data = pickle.load(fh)
        os.remove(path)
        if not isinstance(data, dict) or (time.time() - float(data.get("saved_at") or 0)) > _HELD_STALE_S:
            return
        held = data.get("lane_held")
        if isinstance(held, dict) and held:
            state.lane_held = held   # the ready/snapshot frame carries lane_waiting from this, so the
            #                          hands come back the instant a page (re)connects — no extra push.
        undelivered = data.get("undelivered")
        if isinstance(undelivered, list) and undelivered:
            state.undelivered = undelivered   # flushed on the next (re)connect, like any other
    except Exception as exc:
        state.fail("transport", f"restore held clips failed: {exc}")


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

    # A RESTART announces itself; a plain stop does not (JJ, 2026-09-22). `command-bridge stop
    # --restart` sends `restart: true` so this shutdown leaves a "restarting" hint for the other
    # agents instead of looking like a crash.
    body: dict = {}
    try:
        if request.can_read_body:
            body = await request.json()
    except Exception:
        body = {}
    restart = bool((body or {}).get("restart"))

    async def _say_going_down() -> None:
        """A SHUTDOWN IS HEARD, on whatever lane he is on (JJ, 2026-09-30: *"I would also like that
        the graceful shutdown to play something regardless of the lane I am on. So that I know that
        there is a shutdown coming."*). Sent straight to the page as a cue-tagged clip — not through
        `_speak`, which could hold it behind his speech or park it on a lane — and only when he can
        hear it: a notice queued for after the restart would announce something already over."""
        if not state.clients or not state.channel_open:
            return
        try:
            text = "Restarting the server." if restart else "Stopping the server."
            loop = asyncio.get_running_loop()
            pcm, rate = await loop.run_in_executor(None, lambda: tts.synthesize(
                text, voice=None, speed=state.speech_speed, pause=state.sentence_pause))
            state.shutdown_notice_played = asyncio.Event()
            await _send_clip(state, {"type": "audio_header", "id": "cue-shutdown",
                                     "sample_rate": rate, "bytes": len(pcm), "text": text,
                                     "cue": "shutdown"}, pcm)
            # WAIT FOR THE PAGE TO SAY IT PLAYED (JJ, 2026-09-30: *"the restarting the server thing
            # has properly finished playing before restarting"*). A fixed sleep undercounts whenever
            # another clip is still playing ahead of it. Capped, so a page that never answers can
            # never hold a restart hostage.
            clip_s = len(pcm) / 2.0 / max(1, rate)
            try:
                await asyncio.wait_for(state.shutdown_notice_played.wait(), timeout=clip_s + 20.0)
            except asyncio.TimeoutError:
                pass
        except Exception:
            pass

    async def _drain() -> None:
        """FINISH WHAT IS IN FLIGHT BEFORE GOING DOWN (JJ, 2026-09-30: *"the graceful shutdown should
        make sure that any synthesization or transcription is finished and properly stored in the
        server"*). A clip mid-synthesis and an utterance mid-transcription both lived only in this
        process; a restart killed them after the agent had been told "queued" (dexter's "shipped and
        live") or after he had finished speaking. Wait for both to land — as a held clip, a sent
        clip, or a logged turn — then persistence carries them over. Capped at 60 s so a wedged
        synthesis cannot block a restart forever."""
        await _wait_for_him_to_finish(state)
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and (state.synth_inflight > 0 or state.speech_pending > 0):
            await asyncio.sleep(0.2)

    async def _later() -> None:
        await _drain()
        await _say_going_down()
        if restart:
            # TWO AUDIENCES, both best-effort. The other AGENTS poll HTTP and read turns off disk, so
            # their durable signal is the runtime file — stamp it (`mark_restarting`) and `_request`'s
            # unreachable path reads it back as "restarting, re-arm". Any open PAGE holds a websocket,
            # so tell it directly so it shows "reconnecting" rather than "disconnected". A restart must
            # never be blocked by a failure to leave a hint.
            grace = 20.0
            try:
                from . import cli as _cli
                _cli.mark_restarting(state.session)
                grace = _cli.RESTART_GRACE_S
            except Exception:
                pass
            try:
                await _broadcast_json(state, {"type": "server_restarting", "resume_after_s": grace})
            except Exception:
                pass
            # Persist held clips so the restart does not wipe the agents' pending reports (JJ,
            # 2026-09-24). Only on restart — a plain stop is ending the session, not resuming it.
            _persist_held(state)
            await asyncio.sleep(0.35)   # let the mark reach disk and the frame flush before we die
        else:
            await asyncio.sleep(0.15)
        os.kill(os.getpid(), signal.SIGTERM)

    # Said up front, so the caller knows the stop may take until he finishes his sentence.
    waits = state.talking(amplitude=False)
    asyncio.ensure_future(_later())
    return web.json_response({"stopping": True, "restart": restart, "session": state.session,
                              **({"waiting_for_him": True} if waits else {})})


async def _wait_for_him_to_finish(state: TunnelState) -> None:
    """Hold a shutdown until he has stopped speaking — see `config.SHUTDOWN_SPEECH_WAIT_S`.

    Quiet means the SEGMENTER's view (`talking(amplitude=False)`: speech active or an utterance still
    open, on any lane), held for `SHUTDOWN_QUIET_S` so a breath between sentences is not mistaken for
    the end. The utterance it was waiting on then closes into transcription, which the drain that
    follows already waits for — so his last sentence is logged before the process goes."""
    deadline = time.monotonic() + config.SHUTDOWN_SPEECH_WAIT_S
    quiet_since: float | None = None
    while time.monotonic() < deadline:
        if state.talking(amplitude=False):
            quiet_since = None
        elif quiet_since is None:
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= config.SHUTDOWN_QUIET_S:
            return
        await asyncio.sleep(0.1)
    timing.stamp(state.session, "shutdown_speech_wait_capped", waited_s=config.SHUTDOWN_SPEECH_WAIT_S)


def _say_key(text: str) -> str:
    """What counts as THE SAME SENTENCE for the duplicate-hold refusal (spec 028 FR3).

    Case, surrounding whitespace and trailing punctuation are stripped, because an agent told not
    to *"say it another way"* varies exactly those first and most cheaply. Anything beyond that —
    a real paraphrase — is deliberately NOT matched: swallowing an agent's second, DIFFERENT
    sentence would be a worse failure than the repetition being fixed.
    """
    return " ".join(str(text or "").split()).strip(" .!?,;:—-").casefold()


def _held_duplicate(state: TunnelState, lane: str | None, text: str) -> dict | None:
    """The clip already waiting in THIS lane's hold with this same sentence, if there is one.

    Scoped to one lane on purpose (spec 028 TC2): two agents reaching the same words is honest,
    and they are different voices to him.
    """
    if not lane:
        return None
    key = _say_key(text)
    if not key:
        return None
    for clip in state.lane_held.get(str(lane), []):
        if _say_key((clip.get("header") or {}).get("text", "")) == key:
            return clip
    return None


def _unread_turns(state: TunnelState, lane: str | None = None) -> dict[str, Any]:
    """Everything he said that the agent has not read, for `say` to hand back as it speaks.

    **SCOPED TO THE CALLER'S LANE (spec 013 FR2), and the bug that forced it is worth stating.**
    Spec 007 refuses a `say` on anything unread in the SESSION; spec 012 then made delivery
    per-lane and scoped the waiter guard to `(session, lane)` -- and left this session-wide.
    Measured 2026-08-24 on the first three-agent run: a turn stamped `lane: atlas` refused
    Claude's `say`, naming a turn Claude's own `watch --lane claude` is structurally forbidden to
    deliver. **An agent cannot read its way out of a refusal built on turns it can never be given**,
    so the refusal has to count what that agent could actually have read.

    `lane=None` keeps the old session-wide behaviour, which is what a single-lane session wants and
    what every pre-lanes caller gets.

    **DELIBERATELY DOES NOT ADVANCE THE READ CURSOR**, which is the one design decision here worth
    defending. `watch` consumes what it delivers, because delivering IS the acknowledgement and
    the read boundary drives the divider on his page. `say` is not the reading loop: it is a
    WARNING that the reading loop was skipped. Two reasons to leave the cursor alone:

    * the next `watch` must still return these turns, so an agent that ignores this field loses
      nothing — the failure mode of double-delivery is a re-read, and the failure mode of
      consuming here would be words silently dropped;
    * the page's "read to here" divider would otherwise jump on a REPLY. He has not been read; he
      has been answered, and those are different facts to show someone.

    Bounded because this rides on a hot path and a caller that has been away for an hour must not
    be handed the whole log inside a `say` response.
    """
    last_id = store.last_turn_id(state.session)
    # MEASURED AGAINST **THIS LANE'S** CURSOR (spec 017 FR2). Spec 015 scoped WHICH turns count to
    # the lane; the threshold they were counted against stayed the session number, so a lane whose
    # own turns were unread still reported zero as soon as any other lane read past them. Half a
    # fix reads exactly like a whole one from the outside, which is why this was invisible.
    since = state.lane_cursor(lane)
    if last_id <= since:
        return {"unread": [], "unread_count": 0, "cursor": last_id, "since": since}
    turns = [t for t in store.read_turns(state.session)
             if int(t.get("id", -1)) > since and t.get("addressed")]
    if lane is not None:
        default = state.lanes.default
        turns = [t for t in turns if store.turn_is_for(t, lane, default)]
    # Whether the list below is EVERY unread turn (spec 029 FR9): only then may a refusal that hands
    # it over count as having delivered it.
    complete = len(turns) <= config.UNREAD_ON_SAY_MAX
    turns = turns[-config.UNREAD_ON_SAY_MAX:]
    return {"unread": turns, "unread_count": len(turns), "cursor": last_id, "since": since,
            "complete": complete}


def _partial_so_far(state: TunnelState) -> str:
    """The in-progress turn's text so far: committed pieces first (they are final for their span),
    else the live partial preview, else "" (spec 029 FR10)."""
    pieces = getattr(state, "pieces", None)
    texts = [t for t in (getattr(pieces, "texts", None) or []) if isinstance(t, str) and t.strip()]
    if texts:
        return " ".join(t.strip() for t in texts)
    return str(getattr(state, "partial_text", "") or "")


async def _mark_read(state: TunnelState, lane: str | None, cursor: int) -> None:
    """Advance a lane's read boundary as a `watch` delivering up to `cursor` would (spec 029 FR9):
    the session and per-lane cursors (monotonic per lane), persistence, the lane now holding turns
    it has not answered, and the `consumed` broadcast that moves his page's divider.

    No cue: the acknowledgement belongs to an agent announcing it will respond (`handle_consumed`),
    and a refused `say` is about to do exactly that by saying again."""
    who = lane or state.lanes.default
    state.consumed_cursor = cursor
    state.lane_consumed[who] = max(state.lane_consumed.get(who, -1), cursor)
    store.write_consumed_cursor(state.session, cursor, lanes=state.lane_consumed)
    timing.stamp(state.session, "consumed", cursor=cursor, lane=who, via="say_refusal")
    state.lane_holds_turns[who] = True
    state.agent_holds_turns = True
    await _broadcast_json(
        state,
        {"type": "consumed", "cursor": cursor, "pending": state.pending_turns(),
         "read_through": dict(state.lane_read_through),
         "lane_consumed": dict(state.lane_consumed)},
    )


def _unread_refusal(state: TunnelState, unread: dict[str, Any],
                    lane: str | None = None) -> dict[str, Any]:
    """The payload `say` returns INSTEAD of speaking, when he said something nobody read.

    **THE CURSOR IN THE REMEDY IS THE ONE FACT THIS FUNCTION EXISTS TO GET RIGHT.** It is
    `consumed_cursor` — where the agent has read to — and NOT `last_turn_id`, which is what
    `_unread_turns` reports and what the success path hands back for resuming. Those are different
    numbers here by definition, and handing over the wrong one makes the refusal unescapable:
    `watch --since <last_turn_id>` returns nothing, so nothing is marked read, so the cursor never
    moves, so the retried `say` refuses again on the same turns, forever. `--since
    <consumed_cursor>` delivers exactly the turns being complained about and advances the cursor
    past them as it does, which is what makes the next `say` succeed.

    So the two cursors are published under names that cannot be confused for one another —
    `since` (run this) and `last_turn_id` (the head of the log) — rather than under one `cursor`
    key whose meaning would depend on which branch produced the payload.

    JJ, 2026-08-18: *"It should say the operator did not hear you because there was this turn —
    process it, and if you want to restate your message, do so."* Hence `unread` riding along: the
    recovery is a read, and making the agent spend another round trip to find out WHAT it missed
    would be the tool asking for the discipline it just enforced.

    **AND IT RIDES ALONG EXACTLY ONCE PER REFUSAL EVENT, NOT ONCE PER CLIP** (spec 011, FR1). An
    answer is often several `say --now` clips; when the first is refused every one after it is
    refused too, on the same unread turn, and each used to carry the full text again. Measured
    2026-08-19: four clips, one ~700-character turn, **6,268 characters** — of which 2,625 were
    three redundant copies of something already delivered.

    So this function is a builder WITH A MEMORY, and the memory is the pair
    `(since, last_turn_id)`. Matching the previous refusal's pair means nothing the agent could act
    on has changed — it has not read anything and he has not said anything — so the repeat sends
    the ids alone. Anything that WOULD change what the agent needs moves one half of the pair, and
    the next refusal is full again.

    **Omitting the text is safe only because it is never unrecoverable**, and both routes back are
    in the same payload: the ids are listed, and `remedy` is still the literal `watch` that
    delivers every one of those turns with their text. Nothing here decides whether to refuse —
    that verdict is `unread_count` and it is untouched (spec 007 TC4).

    ⚠ **This mutates `state`, which a name ending in `_refusal` does not advertise.** It is
    deliberate and it is the seam FR1's measurement harness drives: two calls with the same state
    and the same unread set must produce first-then-repeat in process, with no server and no HTTP.
    A caller that wants the full payload twice resets `state.last_refusal` to None between them.
    """
    n = unread["unread_count"]
    # THE MEMO IS THIS LANE'S (spec 018 FR4). Two agents refused on the same turn are two separate
    # first refusals, and each needs the text. The identity's first half is this lane's own read
    # cursor for the same reason — it is what `since` was measured from (017 FR3).
    who = lane or state.lanes.default
    # ⚠ THE RESET CONTRACT SURVIVES THE FAN-OUT. `state.last_refusal = None` is documented as the
    # way to make the next call a first again, and both the FR1 harness and `context_cost.py` use
    # it. Normalising here keeps that sentence true now that the memo is a dict: assigning None
    # still clears every lane's memo, because an empty map has no entry for anybody.
    if not isinstance(state.last_refusal, dict):
        state.last_refusal = {}
    if not isinstance(state.refusal_repeat, dict):
        state.refusal_repeat = {}
    identity = (unread.get("since", state.consumed_cursor), unread["cursor"])
    if state.last_refusal.get(who) == identity:
        state.refusal_repeat[who] = state.refusal_repeat.get(who, 0) + 1
    else:
        state.last_refusal[who] = identity
        state.refusal_repeat[who] = 0
    repeat = state.refusal_repeat[who]

    error = (
        f"refusing to speak: he said {n} thing(s) you have not read. Nothing was synthesized "
        f"and nothing was queued — he did not hear this."
    )
    payload: dict[str, Any] = {
        "spoke": False,
        "error": error,
        "code": config.UNREAD_REFUSAL_CODE,
        # RESUMES FROM THE CURSOR THE COUNT WAS MEASURED AGAINST (spec 017 FR2). `unread["since"]`
        # is this lane's own read cursor, published by `_unread_turns` for exactly this reason: a
        # remedy computed from a different number than the complaint delivers the wrong turns and
        # leaves the refusal unescapable, which is the trap documented below.
        "remedy": (
            f"command-bridge watch --session {state.session} --since {unread['since']}"
        ),
        # Full turn objects the first time, ids alone on every repeat of the same identity. The
        # KEY does not change and neither does its type, so an agent that reads `unread[i]["id"]`
        # keeps working across both; only `text` is conditional, and `unread_text_omitted` says so.
        "unread": (unread["unread"] if repeat == 0
                   else [{"id": t.get("id")} for t in unread["unread"]]),
        "unread_count": n,
        # The cursor the remedy uses, published so a caller building its own command cannot
        # arrive at a different number than the one it was just handed.
        "since": unread["since"],
        "last_turn_id": unread["cursor"],
        # Always present, 0 on the first — a field that only appears on repeats would make its
        # absence ambiguous between "this is the first" and "this build of the tool predates the
        # idea", and an agent cannot branch on that difference.
        "refusal_repeat": repeat,
        # No `next` here. Every other `next` on a `say` response is written by the CLI, which is
        # the layer that knows how the caller invokes this tool; a second author for one field is
        # how two descriptions of the same situation start disagreeing. `remedy` is the part that
        # has to survive without a CLI, and it does.
    }
    if repeat:
        payload["unread_text_omitted"] = True
        # Said in prose as well as in the flag. An agent meeting `unread: [{"id": 42}]` with no
        # explanation has to decide whether the tool lost the text or withheld it, and those call
        # for opposite responses. Both facts it needs are here: the text was already handed over,
        # and the command that hands it over again is unchanged.
        payload["error"] = (
            f"{error} Text omitted: it went out on the first refusal for these ids, "
            f"and `remedy` still delivers it."
        )
    return payload


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
    timings = bool((body or {}).get("timings"))
    intent = bool((body or {}).get("intent"))   # spec 012: a supersedable announcement
    notify = bool((body or {}).get("notify"))   # spec 036: also a heads-up on his phone
    if notify and intent:
        return web.json_response({"error": "notify cannot be combined with intent", "code": "bad_request",
                                  "remedy": "notify on the result, not on the announcement"}, status=400)
    if not isinstance(text, str) or not text.strip():
        return web.json_response({"error": "text is required"}, status=400)

    # AN AGENT MAY NOT SPEAK WHILE HE IS TALKING TO SOMEBODY ELSE (spec 012 FR9).
    #
    # ⚠ **How FR9 is enforceable at all, ruled here because the spec could not settle it.** It
    # says an agent "cannot speak into a lane that is not its own", but there is no agent identity
    # in this tool and adding one would be a design change — `--lane` IS the caller's claim about
    # itself, so a check of the claim against itself enforces nothing. What IS enforceable, and is
    # what he actually asked for, is the LIVE lane: if he is talking to Codex, Claude does not get
    # to play audio at him.
    #
    # Refused BEFORE synthesis, so nothing is generated and nothing is queued — the same shape as
    # the unread refusal below, and for the same reason: a refusal that has already produced audio
    # is not a refusal.
    #
    # **In Slice B this refusal becomes a HOLD** (FR7): the clip waits and plays when its lane goes
    # live. Refusing is the correct behaviour until the hold is built and proven to deliver (TC3) —
    # the one thing that must not happen in the meantime is speaking over the conversation he is
    # actually having.
    lane = (body or {}).get("lane") or None
    if lane is None and len(state.lanes.names) > 1:
        # 🔴 **THE BUG THAT BROKE THE FIRST THREE-AGENT SESSION (spec 013 FR1).**
        #
        # The off-lane hold below is guarded by `bool(lane) and ...`, so a caller that omits
        # `--lane` is NEVER off-lane and always plays. Measured live 2026-08-24: Codex spoke over
        # a conversation JJ was having with Claude, which is the exact thing FR7 exists to
        # prevent. Every 012 test passed `--lane` explicitly, so nine acceptance criteria could
        # pass while the feature failed in the first minute of real use.
        #
        # 🎯 **A guard whose predicate is satisfied by the ABSENCE of the thing it guards.** The
        # check is present, correct and tested; it simply never fires for the caller who did the
        # least, and that is the caller who most needs it.
        #
        # JJ ruled it himself, minutes after it happened to him: *"I think we should force a lane
        # now."* / *"A say without a lane should reject."*
        #
        # **Refusing is the only thing that closes it.** TC1: the server cannot know who is
        # calling — identity is per-invocation — so there is nothing to infer from and any guess
        # would put audio in the wrong conversation, which is the failure being fixed.
        #
        # ⚠ Only when a SECOND lane exists. A one-agent session has exactly one place the audio
        # could go, so the flag carries no information and demanding it would be ceremony (NFR1).
        return web.json_response(
            {"error": "refusing to speak without a lane: several agents share this session, "
                      "and this tool cannot tell which one you are",
             "code": "no_lane",
             "remedy": f"command-bridge say --session {state.session} --lane <yours> \"…\"",
             "lanes": list(state.lanes.names),
             "live_lane": state.lanes.current},
            status=400,
        )
    if lane is not None:
        if not isinstance(lane, str) or not state.lanes.knows(lane):
            return web.json_response(
                {"error": f"no lane named '{lane}'", "code": "unknown_lane",
                 "remedy": "command-bridge lane list",
                 "lanes": list(state.lanes.names)},
                status=400,
            )
        # ⚠ **AN OFF-LANE `say` USED TO BE REFUSED HERE. IT IS HELD NOW (FR7).**
        #
        # Refusing was the correct interim while there was nowhere safe to put the audio -- the
        # one thing that must never happen is an agent talking over the conversation he is
        # actually having. But he asked for the other behaviour in as many words: *"while I am on
        # the lane with the other one, the other agent cannot barge in -- what it is saying would
        # be queued up."* A refusal makes the waiting agent's answer HIS problem to ask for again,
        # which is the opposite of the point. `_speak` holds it and releases it the moment his
        # attention comes back to that lane.

    # LIAISON MODE (spec 037): only the liaison speaks to him. Refused before anything is
    # synthesized, with the way to reach him through the liaison instead.
    _speaker = lane or state.lanes.default
    if liaison_mod.is_worker(state.liaisons, _speaker):
        return web.json_response(liaison_mod.refusal(state.session, state.liaisons, _speaker), status=409)

    # No 409 when nobody is connected. The reply is synthesized and held; see
    # TunnelState.undelivered. Refusing here is what made a locked phone eat answers silently.

    # WHAT HE SAID THAT NOBODY READ, HANDED BACK BY THE ACT OF SPEAKING ITSELF.
    #
    # *"the say command resolves, it should include in its response whatever I said that wasn't
    # drained before."* (2026-08-17)
    #
    # **This turns the project's core invariant from a discipline into a structural fact.** Spec
    # 005 states it as "no speech may be pending when you speak", and until now it was enforced by
    # an agent CHOOSING to run `watch` first — a rule, and one violated repeatedly in live
    # sessions. Handing the unread turns back from `say` means an agent physically cannot speak
    # without being given what it missed. The rule stops depending on memory.
    #
    # Sampled BEFORE synthesis so both paths carry it: `--now` is exactly the path an agent takes
    # when it is in a hurry, which is when it skips the check.
    unread = _unread_turns(state, lane)

    # 🔴 THE SAME SENTENCE IS NOT QUEUED TWICE INTO ONE LANE'S HOLD (spec 028).
    #
    # The off-lane hold response already says, in as many words, *"Do not repeat it and do not say
    # it another way: keep waiting on the watch above."* **That is advice to a model, not a
    # constraint on a system**, and on 2026-08-27 an agent ignored it: two clip ids, identical
    # text, eighty-five seconds apart, both held for kepler and both played when he came back.
    # JJ: *"The Kepler agent just spoke twice to me."* — and, tellingly, *"I don't know if it's an
    # issue of the agent or the command bridge."*
    #
    # ⚠ Against the HOLD only, never against what has already played (TC1): saying something again
    # an hour later is ordinary speech and not the tool's business. And a different lane holding
    # the same words is not a duplicate (TC2) — two agents may honestly reach the same sentence,
    # and they are different voices to him.
    _dup = _held_duplicate(state, lane, text)
    if _dup is not None:
        return web.json_response(
            {"error": "refusing to speak: this lane is already holding that exact sentence, "
                      "waiting for him to come back to it. Nothing was synthesized and nothing "
                      "was queued.",
             "code": "duplicate_held",
             "clip": (_dup.get("header") or {}).get("id"),
             "waiting_s": round(time.time() - _dup.get("at", time.time()), 1),
             "lane": lane,
             "remedy": f"command-bridge watch --session {state.session} --lane {lane}",
             "next": "IT IS ALREADY WAITING AND HE CAN SEE THE RAISED HAND. Say something NEW or "
                     "say nothing — keep waiting on the watch, which returns the moment his "
                     "attention is back on this lane."},
            status=428,
        )

    # AND NOW IT REFUSES, WHERE IT USED TO SPEAK AND THEN WARN (spec 007, FR1).
    #
    # JJ, 2026-08-18: *"The `say` command should exit with an error and not stream what you're
    # saying if there was a turn from me that you didn't see."*
    #
    # **THIS IS A CHANGE OF VERDICT, NOT OF MEASUREMENT.** The state was already here, already
    # sampled at exactly this point, already filtered to turns actually addressed to the agent.
    # What it did with it was hand it back attached to a reply that had already gone out — a
    # warning, and therefore optional, on a discipline that had failed in live session after live
    # session. Three rewrites of the operating guide did not fix it, which is the signal that the
    # rule belongs in the tool.
    #
    # FOUR PROPERTIES, each load-bearing:
    #
    # * **Before `_speak`, so nothing is synthesized and nothing is queued.** Not synthesise-then-
    #   discard: an early return further down would put a clip into `state.undelivered` and
    #   reintroduce the 409-and-drop this project already fixed (spec 007 TC3).
    # * **Before the `fire_and_forget` branch, so `--now` is refused too.** The hurried path is the
    #   one that skips checks; exempting it would put the hole exactly where the comment above says
    #   it is most likely to be used, and `--now` is one argument away.
    # * **The read cursor is not touched.** `_unread_turns` never advanced it and still does not,
    #   so refusing costs nothing: the next `watch` returns those turns exactly as it would have.
    # * **There is no flag that turns this off**, here or in the CLI. A bypass would be reached for
    #   under precisely the conditions the check exists for. This repo has deleted such a flag
    #   before, for the same reason.
    #
    # Muting does not enter into it and does not need to: a muted microphone sends no frames, so
    # no utterance closes and no turn is appended — muting cannot manufacture an unread turn. What
    # it also cannot do is forgive one said before the mute, and that is correct. Those words were
    # still said, and are still unread.
    #
    # 428 PRECONDITION REQUIRED, and deliberately not 409. The status means "do the thing that has
    # to happen first, then retry", which is exactly the instruction. 409 is the status this same
    # endpoint used to answer when nobody was connected, right before throwing the reply away —
    # and a reader meeting 409 in `handle_say` again would reasonably conclude that bug had come
    # back. It has not: nothing is discarded here because nothing was ever made.
    if unread["unread_count"]:
        refusal = _unread_refusal(state, unread, lane)
        # 🔴 A REFUSAL THAT HANDS OVER THE TEXT HAS DELIVERED IT (spec 029 FR9, KD4).
        #
        # Spec 007 left the cursor alone here so an agent ignoring `unread` would lose nothing. Since
        # 028 the refusal is the NORMAL path — the agent says, is refused, reads what came back — and
        # leaving the cursor put cost a second refusal and a `watch` to re-fetch text it already
        # held. JJ, 2026-10-08: *"Why force you to another watch?"* So when this refusal carries the
        # full text of EVERY unread turn, it counts as reading them. A truncated list, or a repeat
        # with the text omitted, still leaves the cursor and `remedy` as they were.
        if unread.get("complete") and not refusal.get("unread_text_omitted"):
            await _mark_read(state, lane, int(unread["cursor"]))
            refusal["marked_read"] = True
            refusal["cursor"] = int(unread["cursor"])
        else:
            refusal["marked_read"] = False
        return web.json_response(refusal, status=428)

    # AND IT REFUSES WHILE HE IS STILL TALKING TO THIS LANE (spec 028 FR1). The unread refusal above
    # covers a turn he FINISHED; this covers the one he has not finished yet, and together they are
    # the whole of what the pre-say `watch --reply` was checking — so a reply is one call, not two.
    # JJ, 2026-10-08: *"we are wasting turns on that extra watch … the say command can reject
    # immediately if I'm still speaking."*
    #
    # The SEGMENTER predicate, never raw amplitude (TC1): room noise must not refuse a reply, and
    # barge-in covers the segmenter's lag. Only a clip that would PLAY now is refused (FR2) — an
    # off-lane clip is held and cannot talk over him — and a muted mic cannot be mid-sentence.
    _plays_now = not (bool(lane) and lane != state.lanes.current and lane != lanes_mod.BROADCAST)
    if _plays_now and not state.muted and state.talking(amplitude=False):
        _mine = lane or state.lanes.default
        return web.json_response(
            {"spoke": False,
             "error": "refusing to speak: he is speaking to you right now. Nothing was synthesized "
                      "or queued — he did not hear this.",
             "code": config.SPEAKING_REFUSAL_CODE,
             "remedy": (f"command-bridge watch --session {state.session} --lane {_mine} "
                        f"--since {unread['since']} --reply"),
             "since": unread["since"],
             # WHAT HE HAS SAID SO FAR (spec 029 FR10): the pieces already transcribed at his
             # pauses, else the live preview. A preview, not a turn — the finished turn still arrives
             # through `watch`, so nothing is marked read.
             "partial": _partial_so_far(state),
             "live_lane": state.lanes.current},
            status=428,
        )
    # THE LOG HEAD AT ACCEPTANCE (spec 028 FR4/TC2). A turn above it landed after the agent asked to
    # speak, so the clip no longer answers everything he said — `_speak_now` drops it rather than
    # play it into the conversation he has moved on in.
    accepted_head = int(unread["cursor"])

    # SPEAKING IS WHAT ENDS THE BATCH. Whatever turns the agent was holding, it has now answered
    # them — so its next wait is a LISTEN and should block, rather than the instant pre-reply
    # check. Derived from the act, never declared: see TunnelState.agent_holds_turns.
    #
    # Below the refusal on purpose: a refused `say` answered nothing, so the agent is still
    # holding its turns and its next wait is still the pre-reply check.
    #
    # THIS LANE ANSWERED, so only this lane's batch ends (spec 018 FR1). The session-wide flag is
    # then re-derived from the fan-out rather than set, so one agent speaking cannot end another
    # agent's batch — which is half of the sixty-second pause he timed.
    #
    # ...UNLESS THIS IS AN --intent ANNOUNCEMENT, which has NOT answered anything (spec 012). It is
    # the supersedable "on it, running the tests" clip — a promise the real reply replaces — so the
    # agent is still holding its turns. Ending the batch here would clear the very flag that makes
    # the agent's NEXT pre-reply check return instantly off-lane, and JJ then hits the block this
    # path exists to prevent: root-caused live 2026-09-11 — he ack'd with --intent while on another
    # lane, the holds flag dropped, and the pre-say watch that followed was misread as a listen and
    # blocked off-lane until he came back. So an intent clip leaves the batch OPEN; only a real
    # reply ends it. (`unanswered_s`/blue-tick below still move — an ack is a spoken sign of life;
    # only the pre-vs-listen signal must not treat it as the answer.)
    if not intent:
        state.lane_holds_turns[lane or state.lanes.default] = False
        state.agent_holds_turns = any(state.lane_holds_turns.values())

    # THE RESET HALF OF `unanswered_s` (spec 013 FR8), and its position is the whole point: it sits
    # AFTER every refusal, so a `say` that was declined does not clear a debt it never paid. A
    # refused agent still owes him an answer, and reporting otherwise would hide exactly the case
    # the number exists to surface.
    _who = lane or state.lanes.default
    state.lane_spoke_at[_who] = time.time()
    # The blue tick (013 FR7): everything this lane had in hand at the moment it answered. Taken
    # from the READ cursor and not from the head of the log — a turn that arrived while the reply
    # was being synthesized has not been answered by it, and claiming otherwise would mark
    # something read that nobody has seen.
    #
    # ⚠ **THE LANE'S OWN CURSOR, NOT THE SESSION'S (spec 026 FR6).** `consumed_cursor` is whatever
    # cursor ANY lane last reported — assigned unconditionally, not even monotonically — so with
    # two agents reading, this tick was claiming that THIS lane had answered up to a point a
    # DIFFERENT lane reached. `lane_consumed` has been per-lane and monotonic since spec 015 and
    # `lane_cursor` was already the reader for it; only this write was still session-wide.
    state.lane_read_through[_who] = state.lane_cursor(_who)

    # `since` is the REFUSAL path's business — its remedy watches from it (see `_unread_refusal`).
    # On a delivered or held clip it is redundant with `cursor`, and the agent acts on `next`, not on
    # it, so it is not carried on the non-refusal responses (JJ, 2026-09-15, informational-field prune).
    public_unread = {k: v for k, v in unread.items() if k != "since"}
    if fire_and_forget:
        # Return before synthesis so the agent can acknowledge and keep working in parallel,
        # instead of the user waiting out a TTS round trip before anything else starts.
        # The lane travels on the fire-and-forget path too. `--now` is exactly the path an
        # agent takes when it is in a hurry, which is when it would otherwise talk over him.
        asyncio.get_running_loop().create_task(_speak(state, text, voice, lane=lane, intent=intent,
                                                      accepted_head=accepted_head, notify=notify))
        return web.json_response({"queued": True, "async": True, **({"notify": True} if notify else {}),
                                  **public_unread})

    try:
        result = await _speak(state, text, voice, lane=lane, timings=timings, intent=intent,
                              accepted_head=accepted_head, notify=notify)
    except tts.TTSError as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({**result, **public_unread})


def record_spoken(session: str, clip_id: str, text: str, held_for: float) -> str:
    """Stamp the clip AND the string the engine was actually handed (spec 008, FR4b).

    **A transform nobody can inspect is a transform nobody can debug**, and a clip that has
    already gone out cannot be re-derived from the reply text alone once the rules change. So the
    normalised form is written down beside the clip id at the moment it is spoken, which is the
    only moment both facts exist together.

    This is the AFTER-THE-FACT half. It can only be read back through a session that ran, so it
    is deliberately not the whole answer: `command-bridge pronounce` is the ahead-of-time half and
    needs no server at all. Both call `speech.normalize_for_speech`, so neither can drift from
    what synthesis actually used.

    A separate function rather than an inline `timing.stamp` because it is the one part of FR4b
    that can be verified without a running server — call it, then read the timing log.
    """
    normalized = speech.normalize_for_speech(text)
    timing.stamp(session, "spoken", clip=clip_id, held_for=round(held_for, 1),
                 normalized=normalized)
    return normalized


class _SynthGate:
    """Serialize TTS synthesis to ONE at a time, in PRIORITY order (JJ, 2026-09-22, dev.jsonl turn
    6259: *"I think the TTS queue should be next … only one TTS is running at a time. And the agent
    that has [the] live [lane] gets priority … Regular turns get priority over intent turns."*).

    Concurrent kokoro synthesis across lanes was the measured CPU spike (six agents synthesizing at
    once on the CPU); serializing it makes the agent he is TALKING TO synthesize first and lets
    background agents wait their turn instead of all fighting for the cores. The priority is a tuple,
    lower first: `(0 if live else 1, 1 if intent else 0)` — so live+regular beats live+intent beats
    off+regular beats off+intent, and arrival order (a monotonic seq) breaks ties FIFO.

    **Why a heap of futures and not an `asyncio.Lock`:** a Lock hands the baton FIFO, which cannot
    prefer the live lane over a background agent that happened to ask first. Each waiter parks on its
    own future in a min-heap keyed by (priority, seq); whoever finishes a synth wakes the current
    highest-priority waiter. The seq keeps the heap total-ordered so two tied priorities never
    compare their futures (futures aren't orderable).
    """

    def __init__(self) -> None:
        self._busy = False
        self._waiters: list = []      # min-heap of (priority, seq, future)
        self._seq = 0
        self._lock = asyncio.Lock()   # guards _busy/_waiters only — held for microseconds, never over a synth

    async def run(self, priority, fn):
        loop = asyncio.get_running_loop()
        fut = None
        async with self._lock:
            if not self._busy:
                self._busy = True          # take the slot directly — nothing is synthesizing
            else:
                fut = loop.create_future()
                self._seq += 1
                heapq.heappush(self._waiters, (priority, self._seq, fut))
        if fut is not None:
            await fut                        # our turn, handed to us by the finishing synth
        try:
            return await loop.run_in_executor(None, fn)
        finally:
            async with self._lock:
                if self._waiters:
                    _, _, nxt = heapq.heappop(self._waiters)
                    if not nxt.done():
                        nxt.set_result(None)  # hand the baton to the highest-priority waiter
                else:
                    self._busy = False        # queue drained — the next caller takes the slot directly


# ONE gate for the whole server, so every lane's synthesis shares the single ordering (module-level,
# like the resident TTS engine it fronts).
_SYNTH_GATE = _SynthGate()


async def _speak(state: TunnelState, text: str, voice: str | None,
                 lane: str | None = None, timings: bool = False,
                 intent: bool = False, accepted_head: int | None = None,
                 notify: bool = False) -> dict[str, Any]:
    """`_speak_now`, counted in `state.synth_inflight` so a graceful shutdown can wait for it.

    🔴 A restart used to kill a clip mid-synthesis (2026-09-30): dexter's "shipped and live" was
    acknowledged to it as queued (`say` returns before synthesis) and then died with the old process,
    so he never heard that the site was deployed. The count is what `handle_shutdown` drains on."""
    state.synth_inflight += 1
    try:
        return await _speak_now(state, text, voice, lane=lane, timings=timings, intent=intent,
                                accepted_head=accepted_head, notify=notify)
    finally:
        state.synth_inflight -= 1


async def _speak_now(state: TunnelState, text: str, voice: str | None,
                     lane: str | None = None, timings: bool = False,
                     intent: bool = False, accepted_head: int | None = None,
                     notify: bool = False) -> dict[str, Any]:
    """Synthesize, hold if the speaker is mid-sentence, then push the audio.

    Shared by the blocking and fire-and-forget paths so the interruption guard cannot be
    bypassed by choosing the async one.
    """
    # THE LANE RIDES EVERY STAMP THAT HAS ONE (spec 018 FR5). Without it the timing log cannot
    # tell two agents apart, which is why the sixty-second pause had to be found by reading source
    # rather than by reading the log JJ already had.
    timing.stamp(state.session, "say_requested", chars=len(text),
                 lane=lane or state.lanes.current)
    # 🔴 THE CLIP'S OWN LANE OWNS EVERY STAGE OF IT (spec 016 FR3), resolved ONCE here and never
    # re-read. Synthesis, the hold, playback and the closing idle are one flow that spans seconds
    # of real time, and he can move the conversation at any point inside it. Re-deriving the owner
    # at each step would say a different agent is talking than the one whose words are playing.
    owner = lane or state.lanes.current
    await _set_agent_state(state, "synthesizing", owner)
    try:
        # THE UNTIMED CALL IS THE ORIGINAL ONE, deliberately. `synthesize` is what nine test
        # stubs and every caller before spec 020 name, and the overwhelmingly common path should
        # not change shape to serve an opt-in flag. `synthesize_timed` is reached only when the
        # schedule was actually asked for.
        def _synth():
            if timings:
                return tts.synthesize_timed(
                    text, voice=voice, speed=state.speech_speed,
                    pause=state.sentence_pause, timings=True)
            pcm_, rate_ = tts.synthesize(
                text, voice=voice, speed=state.speech_speed, pause=state.sentence_pause)
            return pcm_, rate_, None

        # SERIALIZED + PRIORITISED (JJ, 2026-09-22): one synth at a time, live lane first, regular
        # before intent — see `_SynthGate`. `owner` is this clip's lane, resolved above; the live
        # lane is whoever he is talking to right now.
        synth_priority = (0 if owner == state.lanes.current else 1, 1 if intent else 0)
        # 🎯 STREAMED WHEN HE WILL HEAR IT NOW (JJ, 2026-10-07: "better improvements over the whole
        # chain. End to end."). The whole clip used to wait on its LAST piece — 1-6 s — before he
        # heard its first, which alone takes ~0.2 s. A clip for the live lane synthesizes its first
        # piece, takes the hold, and then plays while the rest renders (`_send_streamed`). A clip
        # that will be HELD (off-lane) gains nothing from streaming, and `--timings` needs the
        # whole schedule, so both keep the one-shot path below unchanged.
        stream = None
        if config.tts_stream() and not timings and state.clients and all(
                c in state.stream_clients for c in state.clients) and not (
                bool(lane) and lane != state.lanes.current and lane != lanes_mod.BROADCAST):
            gen = tts.synthesize_stream(text, voice=voice, speed=state.speech_speed,
                                        pause=state.sentence_pause)
            stream = gen
            first = await _SYNTH_GATE.run(synth_priority, lambda: next(gen, None))
            if first is None:
                raise tts.TTSError("nothing to speak")
            pcm, rate = first
            schedule = None
        else:
            pcm, rate, schedule = await _SYNTH_GATE.run(synth_priority, _synth)
    except tts.TTSError as exc:
        # `tts`, so that "SAPI produced no audio" survives a later voiceprint or capture error.
        # It did not, on 2026-08-10, and the person debugging TTS spent twenty-five minutes
        # reading an accurate message about an optional subsystem nobody had asked about.
        state.fail("tts", str(exc))
        # THE OWNER IS RELEASED ON THE FAILURE PATH TOO (016 FR2). A stage that opens and never
        # closes strands its lane exactly as a mis-attributed close does, and this is the one exit
        # from `_speak` that skips playback entirely.
        await _set_agent_state(state, "idle", owner)
        raise

    # On a streamed clip this is its FIRST piece — the moment playback can begin — and `audio_s` is
    # that piece's length; `synthesized_all` marks the last one.
    timing.stamp(state.session, "synthesized", audio_s=round(len(pcm) / 2 / rate, 2),
                 lane=owner, **({"streamed": True} if stream is not None else {}))
    # A sound when THIS reply has finished synthesizing (JJ, 2026-09-14). Fired on EVERY lane, NOT
    # gated to the live one: that is the point he clarified — from another lane he wants to hear
    # that an agent has a reply ready. On his live lane the reply plays right after (that is the
    # done signal); OFF his lane the reply is HELD, so this cue is the only audible "a reply is
    # waiting on that lane."
    await _push_cue(state, "synthesized")
    clip_id = f"clip-{int(time.time() * 1000)}"
    # A HEADS-UP, NOT THE MESSAGE (spec 036). The clip below is held or played exactly as any other;
    # the notice only tells his phone it exists, and its `played` receipt resolves the notice.
    if notify:
        try:
            notices.add(state.session, clip_id, owner, text)
            _notices_changed(state)
        except OSError as exc:   # a heads-up must never cost him the clip
            state.fail("notify", str(exc))

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
    # THE SEGMENTER HOLDS THE CLIP, NOT THE MICROPHONE LEVEL. `state.talking(amplitude=False)`
    # drops `user_speaking` in this loop: it is the client's raw microphone level and rises on any
    # room sound, and holding a FINISHED clip on it made him wait out noise for whatever he wanted
    # to hear — live 2026-09-04, *"are we just checking amplitude?"*, and *"I'm sitting here waiting
    # for the agent to say whatever it needs to say."* `speech_active` (energy above an ADAPTIVE
    # noise floor) and `speech_pending` still hold it, so real speech is never talked over; the
    # sub-second the segmenter lags his first syllable is covered by BARGE-IN, which cuts the clip
    # on his voiceprint. The CLI's pre-say check took the same narrowing first; this is its other
    # half, because `--now` runs THIS loop in a background task, so noise held the audible clip even
    # when the agent did not block. Everywhere else `talking()` keeps amplitude: the lane-latch and
    # `status` want the earliest possible warning, and a moment's over-hold there costs nothing.
    #
    # STILL ONE PLACE, ONE ANSWER: it goes through `state.talking(...)` rather than re-deriving the
    # test inline. Two copies once existed — this one, which knew about mute, and `status`, which
    # did not — so the CLI carried a caveat telling every reader to apply the exception by hand. It
    # picks up `speech_pending` too: an utterance closed and still in transcription is speech nobody
    # has heard.
    # ONLY THE LANE HE IS TALKING TO WAITS FOR HIM (JJ, 2026-10-05: "that doesn't make sense for all
    # agents. Of course, it makes sense for the agent I am speaking to … Dexter has shown waiting
    # multiple times because I have been speaking with you"). A clip for any other lane is HELD for his
    # switch-back below, never played now, so there is nothing to talk over: it skips the hold, is
    # stored ready, and its card does not sit on "waiting".
    off_lane_clip = owner != state.lanes.current
    # 🔴 THE CAP WAS 15 s AND IT RELEASED INTO HIM (spec 028 FR3). Five of seven held clips measured
    # 10-05 → 10-08 went out at exactly 15.1 s while his utterance was still running, and his voice cut
    # each one within 1.4 s. A long turn is not a stuck VAD; the bound is now `SPEAK_HOLD_MAX_S`, and
    # reaching it while he is still talking DROPS the clip below instead of playing it.
    while not off_lane_clip and waited < config.SPEAK_HOLD_MAX_S and not state.muted:
        if state.talking(amplitude=False):
            if not announced:
                await _set_agent_state(state, "waiting", owner)
                announced = True
                # WHICH SIGNAL HELD IT (JJ, 2026-10-07, from the car: "I distinctly remember seeing two
                # episodes of Waiting after I saw a synthesization happen … I feel like we're missing
                # logging"). The hold set "waiting" and left no trace, so road noise read as speech
                # could not be told from him talking.
                timing.stamp(state.session, "speech_hold", clip=clip_id, lane=owner,
                             speech_active=bool(state.buffer.speech_active),
                             utterance_open=bool(state.buffer.utterance_open),
                             speech_pending=state.speech_pending,
                             user_speaking=bool(state.user_speaking))
            await asyncio.sleep(0.1)
            waited += 0.1
            continue
        # Quiet — but a pause longer than END_OF_UTTERANCE_MS closes the turn, so "thinking"
        # and "finished" look identical from here. Wait a beat and re-check before committing.
        # 🔴 THE BEAT IS MEASURED FROM HIS LAST SPEECH, NOT FROM NOW (JJ, 2026-10-07: "I don't want
        # to have a deliberate pause"). A reply is ready seconds after his turn closed, so he has
        # almost always been quiet far longer than the grace already; a fresh 0.8 s wait on every
        # clip was pure added latency. Only the part of the grace he has NOT yet been silent for
        # is waited, which still protects a mid-sentence pause from being talked over.
        quiet_for = (time.monotonic() - state.last_speech_at) if state.last_speech_at else None
        # No speech ever seen (no audio yet, or a test driving the flags directly): the full wait.
        grace = 0.0 if quiet_for is None else min(config.SPEAK_GRACE_S, quiet_for)
        while grace < config.SPEAK_GRACE_S:
            await asyncio.sleep(0.1)
            grace += 0.1
            waited += 0.1
            if state.talking(amplitude=False):
                break
        if not state.talking(amplitude=False):
            break
    if announced:
        timing.stamp(state.session, "speech_hold_end", clip=clip_id, lane=owner,
                     held_s=round(waited, 1), muted=state.muted)
    # A CLIP THAT WOULD START INTO HIM, OR ANSWERS LESS THAN HE HAS NOW SAID, IS DROPPED (spec 028
    # FR3/FR4). Decided here — after the hold, immediately before the clip would go to the page — and
    # only for a clip that is about to PLAY: an off-lane clip is held and its fate is decided when he
    # comes back, never here.
    _plays_now = (not off_lane_clip and bool(state.clients) and state.channel_open
                  and not (bool(lane) and lane != state.lanes.current
                           and lane != lanes_mod.BROADCAST))
    if _plays_now:
        _why = _drop_reason(state, owner, accepted_head)
        if _why is not None:
            return await _drop_clip(state, clip_id, text, owner, _why, waited)
    # ⚠ **`speaking` IS NOT SET HERE ANY MORE.** It used to be, and it was a claim made before the
    # clip's fate was known: the deliverability test is thirty lines below, and a clip for a lane
    # he is not on gets HELD rather than played. So a background agent sat on "speaking" with
    # nothing audible, permanently — the `played` receipt that would clear it never arrives for a
    # clip that never went out.
    #
    # JJ, 2026-08-25: *"whenever an agent's lane is not focused and that agent is queuing turns,
    # their orb says speaking. But surely they're only speaking when that orb is selected and that
    # turn is being played."* The mark now lives with the send, in the branch that does it.

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
        # WHICH AGENT IS SPEAKING (spec 013 FR4), and its absence is why every reply on the page
        # rendered as "claude". JJ, 2026-08-24, with three agents running: *"In the transcript
        # everybody shows as Claude."* He was reading the page correctly -- the tag was a literal,
        # because before lanes there was only ever one speaker and the wake name was a safe
        # stand-in for it. With N agents the page cannot derive this from anything it holds, so
        # the clip has to carry it.
        "lane": lane or state.lanes.default,
        # SPEC 012: an intent announcement — supersedable while still held. Carried on the clip so the
        # held-queue append can recognise a stale one and the page could style it if it ever wanted.
        "intent": intent,
    }
    # TWO ways to have nobody to talk to, and they queue identically. A dropped socket is an
    # accident; a closed orb is a decision. Either way the reply is held rather than played to an
    # empty room, and arrives when he is back — which is what the undelivered queue was built for
    # when a locked phone was silently eating answers.
    # THREE ways to have nobody to talk to now, and the third is new. A dropped socket is an
    # accident, a closed orb is a decision, and an off-lane agent is HIM TALKING TO SOMEBODY ELSE.
    # The third is not a failure at all — it is the feature — so it is held in its own store and
    # released when his attention comes back to this lane (FR7).
    off_lane = bool(lane) and lane != state.lanes.current and lane != lanes_mod.BROADCAST
    deliverable = bool(state.clients) and state.channel_open and not off_lane
    if stream is not None and not deliverable:
        # Streamed for a live lane, but it will not go out now after all (he switched lanes during
        # the hold, the orb closed, the socket dropped): the held/undelivered stores keep WHOLE clips,
        # so finish the synthesis here and take the one-shot branches below unchanged.
        rest = stream
        while True:
            nxt = await _SYNTH_GATE.run(synth_priority, lambda: next(rest, None))
            if nxt is None:
                break
            pcm += nxt[0]
        stream = None
        header["bytes"] = len(pcm)
        timing.stamp(state.session, "synthesized_all", audio_s=round(len(pcm) / 2 / rate, 2),
                     lane=owner)
    if deliverable:
        # A LIVE CLIP SUPERSEDES THIS LANE'S HELD INTENTS TOO (2026-09-30). Supersede ran only on the
        # held path below, so an "about to" parked before a restart (held clips survive it) stayed
        # parked after the agent's next clip went out live — and the card showed an intent pending
        # on the very lane he was talking to: *"why does it say that there is an intent turn from you
        # pending?"* Same rule as below: only intents, never a held result.
        _held = state.lane_held.get(str(lane)) if lane else None
        if _held and any((c.get("header") or {}).get("intent") for c in _held):
            _before = len(_held)
            _held[:] = [c for c in _held if not (c.get("header") or {}).get("intent")]
            timing.stamp(state.session, "announcement_superseded", lane=lane,
                         count=_before - len(_held))
            await _broadcast_json(state, {"type": "lane_waiting", "lane": lane,
                                          "waiting": len(_held),
                                          "split": state.lane_split_for(lane)})
        # SPOKEN WHEN IT IS ACTUALLY GOING OUT, not when it was composed. `speaking_lane` is the
        # lane the `played` receipt will release: the receipt carries a clip id and no lane, and by
        # the time it lands he may have moved on.
        await _set_agent_state(state, "speaking", owner)
        state.speaking_lane = owner
        # KEYED BY CLIP, so a second lane going out cannot make this one unreleasable (spec 024).
        state.clip_owner[clip_id] = owner
        # 🔴 **SENT IS NOT HEARD ON THIS PATH EITHER (spec 026 F6).** `lane_inflight` was added by
        # spec 022 to the FLUSH path only, because that was the path that lost Kepler's three
        # replies. A clip sent straight out — his lane was live when the agent answered — was
        # tracked nowhere, so there was nothing to hand back if the browser's queue got dropped
        # before it played. The same guarantee, applied to one of two senders, is not a guarantee.
        # The seq is also what makes `playing_clip` derivable rather than guessed.
        inflight = state.hand_to_client({"header": dict(header), "pcm": pcm, "at": time.time()})
        state.lane_inflight.setdefault(owner, []).append(inflight)
        await _push_cue(state, "speaking")
        if stream is not None:
            pcm = await _send_streamed(state, header, pcm, stream, synth_priority)
            # A clip handed back later (reconnect, barge) is re-sent WHOLE from here, so the
            # in-flight record has to hold all of it, not the first piece it started with.
            inflight["pcm"] = pcm
            inflight["header"] = dict(header)
            timing.stamp(state.session, "synthesized_all",
                         audio_s=round(len(pcm) / 2 / rate, 2), lane=owner)
        else:
            await _send_clip(state, header, pcm)
    elif off_lane:
        # ⚠ A SEPARATE STORE FROM `undelivered`, and TC3 is the whole reason. Nine clips queued
        # there on 2026-08-20 and none of them played; the only mechanism found that can lose all
        # nine is barge-in clearing that queue wholesale. That clearing is RIGHT for the lane he
        # is on — playing the next clip at a man who just interrupted is the same interruption
        # wearing a different hat — and WRONG for a lane he is not on, which he has not
        # interrupted and is not listening to. Sharing the store would inherit the bug on purpose.
        held = state.lane_held.setdefault(str(lane), [])
        # SUPERSEDE (spec 012). A newer clip from this agent makes any still-held INTENT announcement
        # from it stale — by the time he comes back the thing is done, so *"I'm about to X"* is noise.
        # Drop those held intents (every clip in this lane's queue is from this lane's agent) so he
        # hears the current state, not a promise. Only intents — a result stands until he hears it.
        superseded = sum(1 for c in held if (c.get("header") or {}).get("intent"))
        if superseded:
            held[:] = [c for c in held if not (c.get("header") or {}).get("intent")]
            header["superseded"] = superseded
            timing.stamp(state.session, "announcement_superseded", lane=lane, count=superseded)
        held.append({"header": header, "pcm": pcm, "at": time.time()})
        _prune_lane_held(state, str(lane))
        timing.stamp(state.session, "lane_held", clip=clip_id, lane=lane,
                     depth=len(state.lane_held.get(str(lane), [])))
        await _broadcast_json(state, {"type": "lane_waiting", "lane": lane,
                                      "waiting": len(state.lane_held.get(str(lane), [])),
                                      "split": state.lane_split_for(lane)})
        # ITS STAGE ENDED HERE. The clip is composed and parked; the raised hand is what says so.
        # Reporting `idle` hands the lane back to the derivation, which — this agent being in no
        # watch at that instant — renders it as thinking rather than as an agent doing nothing.
        await _set_agent_state(state, "idle", owner)
    else:
        # THE LANE TRAVELS WITH THE CLIP (spec 026 TC3). `off_lane` above was evaluated against the
        # lane that is live NOW; by the time his phone reconnects it may be a different one, and
        # the flush has to be able to ask the question again rather than assume the answer held.
        state.undelivered.append({"header": header, "pcm": pcm, "at": time.time(),
                                  "lane": lane or state.lanes.default})
        _prune_undelivered(state)
        timing.stamp(state.session, "undelivered_queued", clip=clip_id,
                     depth=len(state.undelivered),
                     why="channel_closed" if state.clients else "no_client")
    record_spoken(state.session, clip_id, text, waited)
    state.clip_text[clip_id] = text
    state.recent_spoken.append({"id": clip_id, "text": text, "sent_at": time.time(),
                                "played_at": None})
    del state.recent_spoken[:-32]
    if len(state.clip_text) > 256:                       # clips that never get a receipt must not pile up
        for stale in list(state.clip_text)[:-256]:
            state.clip_text.pop(stale, None)
    return {
        "queued": True,
        "id": clip_id,
        # THE SCHEDULE, WHEN IT WAS ASKED FOR AND CAN BE ANSWERED (spec 020 FR1/FR4). Absent when
        # `--timings` was not passed; `{"words": [], "aligned": false}` never stands in for an
        # engine that has no durations — `timings_unavailable` says that in words instead, so a
        # caller can tell "nothing to report" from "cannot report".
        **({"words": schedule["words"], "words_aligned": schedule["aligned"]}
           if schedule else
           {"timings_unavailable":
            f"the {config.tts_backend()} backend has no per-token durations on this model"}
           if timings else {}),
        "held_for": round(waited, 1),
        # WAS IT HELD **BECAUSE HE WAS TALKING**, or is that just the grace pass?
        #
        # `held_for` is never zero on a blocking say: the loop always spends SPEAK_GRACE_S
        # re-checking before it commits, so every reply comes back at ~0.9 s whether or not
        # anyone said a word. Everything downstream branched on `held_for > 0`, so the warning
        # "he kept talking while you composed this — drain again" fired on EVERY SINGLE REPLY.
        #
        # A warning that always fires is one nobody reads, which is the same defect as a health
        # check that cannot be emptied. `announced` is already the exact fact — it is set only
        # when the loop actually observed him talking — so it is published rather than inferred
        # from a number that cannot express it.
        "held_for_speech": announced,
        "delivered": deliverable,
        # HELD BECAUSE HE IS TALKING TO SOMEBODY ELSE, which is not the same as undelivered. The
        # agent is not being told its reply failed; it is being told when he will hear it.
        "held_off_lane": off_lane,
        # Say WHICH kind of not-delivered, so the agent can tell "his phone dropped" from "he
        # closed the channel deliberately" without a second call.
        "reason": None if deliverable else (
            "off_lane" if off_lane else ("channel_closed" if state.clients else "no_client")
        ),
    }


def _drop_reason(state: TunnelState, owner: str, accepted_head: int | None) -> str | None:
    """Why a finished live-lane clip must not start now, or None to play it (spec 028 FR3/FR4).

    `still_speaking` — the hold reached `SPEAK_HOLD_MAX_S` with him still talking; the loop exits
    quiet in every other case, so this is the cap and only the cap.
    `newer_turn` — an addressed turn for this lane landed above the log head the `say` was accepted
    at (TC2: a turn the agent had already read is what it is answering). Read only when the head
    moved, so a clean reply costs one integer comparison (NFR2)."""
    if not state.muted and state.talking(amplitude=False):
        return "still_speaking"
    if accepted_head is None:
        return None
    if store.last_turn_id(state.session) <= accepted_head:
        return None
    default = state.lanes.default
    for t in store.read_turns(state.session):
        if (int(t.get("id", -1)) > accepted_head and t.get("addressed")
                and store.turn_is_for(t, owner, default)):
            return "newer_turn"
    return None


async def _drop_clip(state: TunnelState, clip_id: str, text: str, owner: str, why: str,
                     waited: float) -> dict[str, Any]:
    """Drop a clip instead of starting it into him, and TELL ITS AGENT (spec 028 FR5).

    Nothing goes to the page. The notice rides the channel the agent already reads — the lane's
    `played` list, which its next `watch` hands back as `played[]` — marked `cut: dropped` with
    `heard_s: 0`, so it reads exactly like the barge-in notice it replaces, minus the interruption.
    The idle anchor does NOT move (spec 024): he heard nothing, so no silence began after it."""
    timing.stamp(state.session, "clip_dropped", clip=clip_id, lane=owner, why=why,
                 held_s=round(waited, 1))
    heard = state.lane_played.setdefault(str(owner), [])
    heard.append({"clip": clip_id, "text": text, "played_at": round(time.time(), 1),
                  "cut": "dropped", "heard_s": 0, "why": why})
    del heard[:-config.UNDELIVERED_MAX]
    await _set_agent_state(state, "idle", owner)
    return {"queued": False, "id": clip_id, "held_for": round(waited, 1),
            "held_for_speech": why == "still_speaking", "delivered": False,
            "held_off_lane": False, "dropped": why, "reason": "dropped"}


def _prune_lane_held(state: TunnelState, lane: str) -> None:
    """Bound a lane's hold on both axes: coming back to an agent after half an hour and being read
    a stack of stale answers is a worse experience than being told to ask again.

    🔴 **AND THE AGENT IS TOLD WHAT IT LOST, which is the half that was missing.** An expiry used
    to be completely silent in both directions: he never learned a reply had existed, and the agent
    that wrote it went on believing it had been delivered. JJ, 2026-08-26, deciding where the
    notice belongs: *"when it expires, it shouldn't tell me. It should tell the agent. The agent
    should be aware that their replies had expired and did not reach me. Maybe it helps them
    restate."*

    **Telling him would be noise; telling the agent is actionable** — it is the only party that can
    do anything about it, and what it can do is say the thing again.

    ⚠ The age bound is `LANE_HELD_MAX_AGE_S`, not the `undelivered` one it used to share. See that
    constant for why the two situations do not take the same number.
    """
    now = time.time()
    held = state.lane_held.get(lane, [])
    kept = [c for c in held if now - c["at"] <= config.LANE_HELD_MAX_AGE_S][-config.UNDELIVERED_MAX:]
    dropped = [c for c in held if c not in kept]
    if dropped:
        # Text, not audio: the agent needs to know WHAT went unheard so it can decide whether to
        # restate it, and the synthesized bytes are worthless to it. Bounded by the same count cap
        # so a lane nobody visits cannot grow this without limit either.
        notice = state.lane_expired.setdefault(lane, [])
        # The text and id live on the HEADER, not on the hold record — the hold wraps
        # `{header, pcm, at}`. Reading them off the top level yields empty strings and a null id,
        # which is a notice that tells the agent something expired without saying what.
        notice.extend({"text": (c.get("header") or {}).get("text", ""),
                       "clip": (c.get("header") or {}).get("id"),
                       "waited_s": round(now - c["at"], 1)} for c in dropped)
        del notice[:-config.UNDELIVERED_MAX]
        timing.stamp(state.session, "lane_expired", lane=lane, count=len(dropped))
    if kept:
        state.lane_held[lane] = kept
    else:
        state.lane_held.pop(lane, None)


async def _flush_lane_held(state: TunnelState, lane: str) -> int:
    """Play what this lane said while he was talking to somebody else (FR7).

    Called when a lane BECOMES LIVE, which is the moment its speech stops being an interruption
    and becomes an answer. Oldest first, and each clip says how long it waited — a reply arriving
    four minutes after the question, with no acknowledgement that time passed, reads as the agent
    being slow rather than as him having been elsewhere.
    """
    # LIAISON MODE (spec 037): another lane's clips never play to him; they stay held (the hand stays
    # up, the agent's watch still reports them unheard) until liaison mode ends.
    if liaison_mod.is_worker(state.liaisons, lane):
        return 0
    # Read BEFORE the prune, so a lane whose held clips have ALL expired still counts as having had
    # a raised hand to lower. See the broadcast at the end of this function.
    had = bool(state.lane_held.get(lane))
    _prune_lane_held(state, lane)
    queued = state.lane_held.pop(lane, [])
    if queued:
        # NOW it is speaking — this is the moment the held audio actually goes out, and the mark
        # was deliberately not made when the clip was composed. See the comment in `_speak`.
        # The cue travels with it for the same reason: it announces that a reply is starting, and
        # this is where one starts.
        await _set_agent_state(state, "speaking", lane)
        state.speaking_lane = lane
        await _push_cue(state, "speaking")
    # KEEP A COPY UNTIL THE CLIENT SAYS IT PLAYED (spec 022 FR1). Sending is not hearing: the
    # browser can be told to drop its whole queue a second later, and until 2026-08-26 that took
    # these with it because nothing here still held them.
    if queued:
        # Stamped in send order, which is what makes `playing_clip` a derivation rather than a
        # guess: a flush sends SEVERAL and they play in order, so the first of them is on the
        # speaker and the rest are queued behind it.
        state.lane_inflight.setdefault(lane, []).extend(
            state.hand_to_client(c) for c in queued)
    for clip in queued:
        header = dict(clip["header"])
        header["delayed_s"] = round(time.time() - clip["at"], 1)
        header["held_off_lane"] = True
        # Each flushed clip records its own owner too (spec 024) — a flush sends SEVERAL at once,
        # which is precisely the case a single `speaking_lane` slot cannot represent.
        if header.get("id"):
            state.clip_owner[str(header["id"])] = lane
        await _send_clip(state, header, clip["pcm"])
    if queued:
        timing.stamp(state.session, "lane_held_flushed", lane=lane, count=len(queued))
    # THE HAND COMES DOWN WHENEVER THIS LANE'S HELD QUEUE EMPTIED — on a FLUSH (he switched here and
    # the audio is on its way, so lowering it is OPTIMISM; if the clips come back via barge-in, so
    # does the hand) OR on an EXPIRY (the clips are gone and nothing will play). Only the flush path
    # lowered it before, so a lane whose held clips had ALL expired kept a stale raised hand until
    # something else happened to move it — JJ, 2026-09-08: *"the hand was still up ... it showed at
    # number three ... I had to say something for stuff to play back."* Gated on `had` (read before
    # the prune) so this fires for the expiry case too, and does not broadcast for a lane that never
    # had a hand up.
    if had:
        await _broadcast_json(state, {"type": "lane_waiting", "lane": lane, "waiting": 0})
    return len(queued)


def _clip_played(state: TunnelState, clip_id: str) -> None:
    """A clip reached his ears, so the server can finally let go of its copy (spec 022 FR2).

    Matched by id rather than popped positionally: clips can be acknowledged out of order when a
    lane switch interleaves two flushes, and dropping the wrong one would keep a played clip
    forever while discarding an unplayed one.
    """
    def _not_it(c: dict) -> bool:
        return (c.get("header") or {}).get("id") != clip_id

    for lane, clips in list(state.lane_inflight.items()):
        kept = [c for c in clips if _not_it(c)]
        if len(kept) == len(clips):
            continue
        if kept:
            state.lane_inflight[lane] = kept
        else:
            state.lane_inflight.pop(lane, None)
        break
    # HEARD ANYWHERE IS HEARD (spec 032). With two devices connected a clip can be heard on one while
    # a race on the other (a drop, a barge) has already put it back on a hold or the undelivered queue;
    # a receipt from either device retires every copy, so no later page replays it.
    for lane, clips in list(state.lane_held.items()):
        kept = [c for c in clips if _not_it(c)]
        if len(kept) != len(clips):
            state.lane_held[lane] = kept
    state.undelivered = [c for c in state.undelivered if _not_it(c)]


async def _return_inflight(state: TunnelState, keep_playing: bool = True) -> int:
    """Put un-played clips back on their lane's hold, and raise the hand again (spec 022 FR3).

    `keep_playing` is whether the clip ON THE SPEAKER counts as heard (spec 026 FR3/FR4). True for
    a real interruption — he was listening to that agent and cut it off. False when he spoke to a
    DIFFERENT lane over the top of it, because then the audio had to stop but that agent was never
    answered, and its reply belongs back in its hold.

    🔴 **Called when the client is told to drop its queue.** Barge-in stops the clip he
    interrupted — that is right — but the same message empties the whole playback queue, and after
    a lane switch that queue can hold a DIFFERENT agent's replies he has not heard a word of.
    Measured 2026-08-26: three of Kepler's clips destroyed 16 seconds after he tapped to that lane.

    Returning them rather than discarding them keeps the guarantee `lane_held` was separated from
    `undelivered` to provide: **nothing an agent said is lost without somebody being told.** He
    interrupted the lane that was speaking; he did not interrupt one he cannot hear.
    """
    returned = 0
    # Read ONCE, before anything is handed back: `playing_clip` is derived from `lane_inflight`,
    # so consulting it while emptying that store would answer a different question each pass.
    playing = state.playing_clip
    for lane, clips in list(state.lane_inflight.items()):
        # 🔴 NEVER RETURN THE CLIP THAT WAS PLAYING (spec 025). He HEARD it — that is what he
        # interrupted — and stopping it is exactly what makes the browser fire `onended` and send
        # its receipt, so that receipt is milliseconds behind this code and finds nothing left to
        # release. Returning it replays a turn he has already heard AND strands the state machine
        # on "speaking". Everything QUEUED BEHIND it never reached his ears, and that is what has
        # to come back.
        #
        # ⚠ Unless he was talking to somebody ELSE over it (spec 026 FR4), in which case this clip
        # was interrupted by a conversation it is not part of and has to come back like the rest.
        if keep_playing:
            clips = [c for c in clips if (c.get("header") or {}).get("id") != playing]
        if not clips:
            continue
        # Oldest first, ahead of anything that arrived while they were in flight.
        state.lane_held[lane] = clips + state.lane_held.get(lane, [])
        _prune_lane_held(state, lane)
        returned += len(clips)
        await _broadcast_json(state, {"type": "lane_waiting", "lane": lane,
                                      "waiting": len(state.lane_held.get(lane, [])),
                                      "split": state.lane_split_for(lane)})
    state.lane_inflight.clear()
    if returned:
        timing.stamp(state.session, "inflight_returned", count=returned)
    return returned


def _prune_undelivered(state: TunnelState) -> None:
    """Drop anything too old or too far back in the queue."""
    now = time.time()
    state.undelivered = [
        c for c in state.undelivered if now - c["at"] <= config.UNDELIVERED_MAX_AGE_S
    ][-config.UNDELIVERED_MAX:]


async def _flush_undelivered(state: TunnelState) -> int:
    """Deliver everything that was said while nobody was listening, oldest first.

    🔴 **THE LANE IS RE-TESTED HERE, AND THAT IS SPEC 026 FR5.** `_speak` checks `off_lane` BEFORE
    queueing, so this store only ever holds clips for the lane that was live at the time. The flush
    then played all of them into whatever lane is live NOW — so a reply his phone missed while he
    was talking to Magnus arrived in the middle of a conversation with Atlas.

    ⚠ This is spec `012` TC3 in the sibling queue. The enqueue site says the two stores are
    *"DELIBERATELY NOT"* shared so this bug is not inherited — and the flush side never got the
    check the hold side has. **A guarantee enforced on one side of a queue is not a guarantee**,
    which is the same lesson spec `022` learned about `lane_held`.

    🔴 **AND THE ORB IS RE-TESTED HERE (spec 036).** A new SOCKET also flushes (`handle_ws`), and that
    flush used to ignore `channel_open`: on 2026-10-09 his app's socket reconnected at 13:10:38 with the
    orb still off, the flush sent the held "Enjoy lunch" clip, and the app played it. JJ: *"I had the orb
    turned off and you still spoke to me."* A closed channel holds; only opening it releases.
    """
    _prune_undelivered(state)
    if not state.channel_open:
        return 0
    queued, state.undelivered = state.undelivered, []
    lanes_parked: set[str] = set()
    for clip in queued:
        header = dict(clip["header"])
        # Say how long it waited. A reply arriving 90 seconds after the question, with no
        # acknowledgement that time passed, reads as the agent being slow rather than the phone
        # having been asleep.
        header["delayed_s"] = round(time.time() - clip["at"], 1)
        lane = str(clip.get("lane") or state.lanes.default)
        if lane != state.lanes.current and lane != lanes_mod.BROADCAST:
            # He has moved on. Park it where the hand can be raised, exactly as `_speak` would have
            # done had he been on this lane when it was composed — not played at him mid-sentence
            # with another agent.
            state.lane_held.setdefault(lane, []).append(
                {"header": header, "pcm": clip["pcm"], "at": clip["at"]})
            _prune_lane_held(state, lane)
            lanes_parked.add(lane)
            continue
        await _send_clip(state, header, clip["pcm"])
    for lane in lanes_parked:
        timing.stamp(state.session, "undelivered_parked", lane=lane,
                     depth=len(state.lane_held.get(lane, [])))
        await _broadcast_json(state, {"type": "lane_waiting", "lane": lane,
                                      "waiting": len(state.lane_held.get(lane, [])),
                                      "split": state.lane_split_for(lane)})
    # ⚠ **DELIVERED, NOT DEQUEUED.** The caller broadcasts this as `resumed.delivered` and the page
    # tells him that many replies just arrived; counting the parked ones would announce audio he is
    # not about to hear. The hand going up on their lanes is how those are reported.
    parked = sum(1 for c in queued
                 if str(c.get("lane") or state.lanes.default) not in
                 (state.lanes.current, lanes_mod.BROADCAST))
    if queued:
        timing.stamp(state.session, "undelivered_flushed",
                     count=len(queued) - parked, parked=parked)
    return len(queued) - parked


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
    # THE AGENT'S HALF OF THE TRANSCRIPT, ON DISK (JJ, 2026-09-25, phone after the laptop: "the turns
    # that I did while in the computer are not showing up here, why"). His turns were always in the
    # turn log, but a reply existed only in the browser that played it, so a second device showed a
    # one-sided — or empty — conversation. Logged HERE because every clip that reaches a page goes
    # through this function exactly once; a cue is punctuation and carries no text.
    if header.get("text") and not header.get("cue"):
        _log_said(state.session, header)


async def _send_streamed(state: TunnelState, header: dict[str, Any], first: bytes,
                         stream, priority) -> bytes:
    """Send a clip piece by piece as it synthesizes; return the whole PCM.

    The header goes out with `streamed: true` and no byte count, then the first piece, then each
    later piece the moment it renders, then `{"type": "audio_end", "id"}`. The page starts playing
    on the first piece (`playStream` in shell.html), so he hears a reply ~0.2 s after synthesis
    starts instead of after its last piece.

    🔴 **THE CLIP LOCK IS HELD FOR THE WHOLE STREAM**, for the reason `_send_clip` takes it at all:
    anything that slipped between this header and its bytes — a cue, another lane's clip — would be
    read by the page as part of this reply. Holding it across synthesis delays other audio only by
    what playback would have delayed it anyway, since the page plays one clip at a time.
    Each later piece takes the synth gate again, so the live lane's priority still applies per piece.
    """
    header["streamed"] = True
    header["bytes"] = None
    parts = [first]
    async with state._clip_lock:
        await _broadcast_json(state, header)
        await _broadcast_bytes(state, first)
        try:
            while True:
                nxt = await _SYNTH_GATE.run(priority, lambda: next(stream, None))
                if nxt is None:
                    break
                parts.append(nxt[0])
                await _broadcast_bytes(state, nxt[0])
        except tts.TTSError as exc:
            # A later piece failed: he has already heard the start, so end the clip where it got
            # to rather than leave the page waiting on bytes that will never come.
            state.fail("tts", str(exc))
        finally:
            await _broadcast_json(state, {"type": "audio_end", "id": header["id"]})
    pcm = b"".join(parts)
    header["bytes"] = len(pcm)
    if header.get("text"):
        _log_said(state.session, header)
    return pcm


async def _broadcast_bytes(state: TunnelState, data: bytes) -> None:
    for ws in list(state.clients):
        try:
            await ws.send_bytes(data)
        except Exception:
            state.clients.discard(ws)


def _said_path(session: str) -> str:
    return os.path.join(config.session_dir(), f"{session}.said.jsonl")


def _log_said(session: str, header: dict[str, Any]) -> None:
    """Append one spoken reply to `<session>.said.jsonl` — best-effort, never fails the clip."""
    try:
        os.makedirs(config.session_dir(), exist_ok=True)
        rec = {"id": header.get("id"), "lane": header.get("lane"), "text": header.get("text"),
               "wall": _dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        with open(_said_path(session), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def transcript_history(session: str, limit: int = 200) -> list[dict[str, Any]]:
    """His turns and the agents' spoken replies, merged in wall-clock order, newest `limit`.

    What a page rebuilds its transcript from on load, so every device shows the same conversation
    instead of only what it happened to hear live. Each row carries a stable `key` (`t:<turn id>` /
    `c:<clip id>`) so a row that also arrives live is not rendered twice."""
    rows: list[dict[str, Any]] = []
    try:
        for t in store.read_turns(session)[-limit * 2:]:
            if not t.get("final", True) or not t.get("text"):
                continue
            rows.append({"key": f"t:{t['id']}", "from": "you", "id": t["id"], "text": t["text"],
                         "lane": t.get("lane"), "addressed": bool(t.get("addressed")),
                         "wall": t.get("wall") or ""})
    except Exception:
        pass
    try:
        with open(_said_path(session), encoding="utf-8") as fh:
            said = fh.readlines()[-limit * 2:]
        seen: set[str] = set()
        for line in said:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            key = f"c:{r.get('id')}"
            if key in seen:
                continue
            seen.add(key)
            rows.append({"key": key, "from": "agent", "text": r.get("text") or "",
                         "lane": r.get("lane"), "wall": r.get("wall") or ""})
    except OSError:
        pass
    rows.sort(key=lambda r: r["wall"])
    return rows[-limit:]


async def handle_history(request: web.Request) -> web.Response:
    """`GET /history` — the recent transcript for a page to rebuild from (token-gated: it is his
    conversation)."""
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    try:
        limit = max(1, min(500, int(request.query.get("limit", "200"))))
    except ValueError:
        limit = 200
    return web.json_response({"rows": transcript_history(state.session, limit)})


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

    **This endpoint does not write the settings file.** Persistence belongs to `command-bridge rate`, which
    writes `.env` and then calls this. A long-running server that edits config on disk is a
    process racing every other `command-bridge config set`, for no behaviour anyone needs.
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


def _lane_colors(state: TunnelState) -> dict[str, str]:
    """Every registered lane's colour (spec 029 FR5): the roster's for a roster agent, else a hue
    assigned once by name and persisted. Never raises — colour is cosmetic (NFR2)."""
    try:
        roster = config.lane_colors()
        assigned = getattr(state, "lane_colors_assigned", None) or {}
        colours, grown = colors_mod.resolve(state.lanes.names, roster, assigned)
        if grown != assigned:
            state.lane_colors_assigned = grown
            store.write_lane_colors(state.session, grown)
        # Roster agents not in the room keep their colour on the page too (a transcript row from an
        # agent that has since left still paints in its own hue).
        return {**roster, **colours}
    except Exception:  # noqa: BLE001
        return config.lane_colors()


def _lane_reply(state: TunnelState) -> dict[str, Any]:
    """The whole registry, as every `/lane` reply returns it — plus each lane's colour and any pair
    too close to tell apart (spec 029 FR7)."""
    registered = {n: c for n, c in _lane_colors(state).items() if n in state.lanes.names}
    return {
        "lane": state.lanes.current,
        "lanes": list(state.lanes.names),
        "default_lane": state.lanes.default,
        "broadcast": lanes_mod.BROADCAST,
        "watching_lanes": sorted(state.watching_lanes),
        "colors": registered,
        "color_clashes": colors_mod.clashes(registered, config.LANE_COLOR_MIN_DE),
    }


async def handle_lane(request: web.Request) -> web.Response:
    """Who is in the meeting, and who is being talked to (spec 012 FR1, FR5, FR6).

    One endpoint for add / remove / switch because they are one piece of state, and every reply
    returns the WHOLE registry rather than just what changed — a caller that has to accumulate
    deltas to know the current lane set is a caller that will eventually disagree with the server.

    A refusal returns `{error, code, remedy}` and never a bare 400, so an agent can branch on
    `code` and run `remedy` instead of parsing prose (AGENTS.md convention 8).
    """
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)
    action = str((body or {}).get("action") or "list")
    name = (body or {}).get("name")

    try:
        if action == "list":
            pass
        elif action == "order":
            # MOVE LANES WITHOUT TEARING THEM DOWN (spec 029 FR1-FR3). Only the list's order changes:
            # the live lane, watches, holds, briefs and cursors are keyed by name and untouched.
            names = (body or {}).get("names")
            if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
                return web.json_response(
                    {"error": "names must be a non-empty list of lane names", "code": "bad_request",
                     "remedy": "command-bridge lane order <lane> <lane> ..."},
                    status=400,
                )
            state.lanes.reorder(names)
            store.write_lanes(state.session, state.lanes.names)
        elif action in ("add", "remove", "switch"):
            if not isinstance(name, str) or not name.strip():
                return web.json_response(
                    {"error": "name must be a non-empty string", "code": "bad_request",
                     "remedy": f"command-bridge lane {action} <name>"},
                    status=400,
                )
            if action == "add":
                state.lanes.add(name)
                # PERSISTED ON THE CHANGE, not on shutdown (spec 019 FR2). A server is stopped by
                # `stop`, by Ctrl-C, by a crash and by the machine sleeping, and only the first of
                # those runs anything — a save-on-exit would be absent in exactly the cases the
                # restart is most likely to follow.
                store.write_lanes(state.session, state.lanes.names)
            elif action == "remove":
                gone = state.lanes.remove(name)
                state.watching_lanes.discard(gone)
                state.watch_open = bool(state.watching_lanes)
                store.write_lanes(state.session, state.lanes.names)
            else:
                # Switching by tap is the hands-free path's twin, not a lesser one — he asked for
                # both. It goes through the same publish as the spoken switch so a page, a second
                # device and a waiting agent all learn about it the same way.
                # LIAISON MODE (spec 038): he speaks to liaisons only, so a worker is not a lane to
                # switch to — its liaison speaks for it.
                if liaison_mod.is_worker(state.liaisons, name):
                    lead = liaison_mod.liaison_for(state.liaisons, name)
                    return web.json_response(
                        {"error": f"liaison mode: {name} speaks through {lead}", "code": "liaison_mode",
                         "liaison": lead, "remedy": f"command-bridge lane switch {lead}"}, status=409)
                await _set_lane(state, state.lanes.require(name), why="tap")
        else:
            return web.json_response(
                {"error": f"unknown action '{action}'", "code": "bad_request",
                 "remedy": "command-bridge lane list"},
                status=400,
            )
    except lanes_mod.LaneError as exc:
        return web.json_response(
            {"error": str(exc), "code": exc.code, "remedy": exc.remedy}, status=400
        )

    if action in ("add", "remove", "order"):
        # The set changed even though the live lane may not have. Publish it, or the page keeps
        # offering a lane that is gone (or in the old order).
        await _set_lane(state, state.lanes.current, why=action)
    return web.json_response(_lane_reply(state))


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
    os.environ["COMMAND_BRIDGE_WAKE_NAME"] = name.strip().lower()
    state.wake.set_phrases(config.wake_phrases())
    await _broadcast_json(
        state, {"type": "wake", "name": config.wake_name(),
                "phrases": list(state.wake.phrases)}
    )
    return web.json_response(
        {"wake": config.wake_name(), "previous": previous, "phrases": list(state.wake.phrases)}
    )


async def handle_brief(request: web.Request) -> web.Response:
    """Post context for a lane's voice agent. Its open `listen` returns with it within a poll."""
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    lane = str((body or {}).get("lane") or state.lanes.default)
    text = str((body or {}).get("text") or "").strip()
    if not text:
        return web.json_response({"error": "empty brief", "code": "empty"}, status=400)
    pending = state.lane_briefs.setdefault(lane, [])
    pending.append({"text": text, "at": _dt.datetime.now().astimezone().isoformat(timespec="seconds")})
    timing.stamp(state.session, "brief", lane=lane, chars=len(text))
    return web.json_response({"queued": True, "lane": lane, "pending": len(pending)})


async def handle_briefs_take(request: web.Request) -> web.Response:
    """Hand a lane's pending context to its `listen` and clear it — delivered exactly once."""
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    lane = str((body or {}).get("lane") or state.lanes.default)
    briefs = state.lane_briefs.pop(lane, [])
    return web.json_response({"lane": lane, "context": briefs})


async def handle_idle_fired(request: web.Request) -> web.Response:
    """Record that a lane's watch reported the silence after this anchor clip (spec 024 FR4/TC2)."""
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    lane = str((body or {}).get("lane") or state.lanes.default)
    clip = str((body or {}).get("clip") or "")
    if clip:
        state.lane_idle_fired[lane] = clip
    return web.json_response({"lane": lane, "fired": clip or None})


async def handle_fleet_state(request: web.Request) -> web.Response:
    """Each lane's working / idle / unknown state, for `watch --fleet` (spec 025 FR4).

    Same activity the page's fleet view reads (`_fleet_activity`, cached), shaped by the pure
    `fleet.lane_states` with the page's own working threshold. Runs the derivation in an executor:
    it is git + file IO."""
    state: TunnelState = request.app["state"]
    ok, reason = _check(request, state)
    if not ok:
        return web.json_response({"error": reason}, status=403)
    from . import fleet
    loop = asyncio.get_running_loop()
    try:
        payload = await loop.run_in_executor(None, _fleet_activity)
    except Exception as exc:
        return web.json_response({"error": f"fleet activity unavailable: {exc}",
                                  "code": "fleet_unavailable"}, status=500)
    now = payload.get("server_epoch") or time.time()
    lanes = fleet.lane_states(payload.get("lanes") or {}, list(state.lanes.names), now,
                              config.FLEET_WORK_RECENT_S)
    return web.json_response({"server_epoch": now, "lanes": lanes})


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
    # A WAIT THAT CAME BACK WITH NOTHING ENDS THE BATCH. The agent asked "anything more before
    # I speak?" and the answer was no, so it is no longer holding unanswered turns and its next
    # call is a listen again. Without this the instant-return mode would latch on for an agent
    # that consumed turns and then never replied.
    # WHICH lane is waiting, so a second agent on a second lane is not refused (TC7). A caller
    # that names no lane is the single-agent case and is recorded against the default lane, which
    # is the lane it is in fact watching.
    lane = str((body or {}).get("lane") or state.lanes.default)
    # ONE WATCH PER LANE, THE NEWEST WINS (2026-10-01). An open records its `watch_id` as the lane's
    # owner (published on /status as `lane_watch_ids`, which the older watch polls and steps aside
    # on). A close from a watch that is no longer the owner is a superseded watch saying goodbye —
    # ignored entirely, or it would mark the lane unwatched while its replacement is listening.
    _wid = (body or {}).get("watch_id")
    if watching and _wid:
        state.lane_watch_ids[lane] = str(_wid)
    elif not watching and _wid and state.lane_watch_ids.get(lane) not in (None, str(_wid)):
        return web.json_response({"ok": True, "superseded": True})
    elif not watching and _wid:
        state.lane_watch_ids.pop(lane, None)
    # ⚠ ONLY THIS LANE'S BATCH ENDS (spec 018 FR1). This clear used to be session-wide, so a
    # BYSTANDER lane's empty watch ended the batch of an agent that was still composing an answer
    # — and that agent's next pre-reply check was downgraded to a listen and waited a full rung.
    # Timed by JJ on 2026-08-25 at sixty seconds. Moved below the lane resolution because it now
    # needs the name.
    if not watching and bool((body or {}).get("empty")):
        state.lane_holds_turns[lane] = False
        state.agent_holds_turns = any(state.lane_holds_turns.values())
    if watching:
        state.watching_lanes.add(lane)
    else:
        state.watching_lanes.discard(lane)
    state.watch_open = bool(state.watching_lanes)
    # A WATCH OPENING FROM `--since N` IS THIS LANE SAYING IT HAS READ THROUGH N (2026-09-30).
    # Only a watch that RETURNS turns ever posted `/consumed`, so a lane that booted and sat in quiet
    # watches never reported a read position, and the page counted every turn he ever spoke to it as
    # unread. Monotonic like the consume path, persisted and broadcast the same way.
    _raw_since = (body or {}).get("since")
    try:
        _since = int(_raw_since) if _raw_since is not None else None
    except (TypeError, ValueError):
        _since = None
    if watching and _since is not None and _since > state.lane_consumed.get(lane, -1):
        state.lane_consumed[lane] = _since
        store.write_consumed_cursor(state.session, state.consumed_cursor, lanes=state.lane_consumed)
        await _broadcast_json(state, {"type": "consumed", "cursor": state.consumed_cursor,
                                      "pending": state.pending_turns(),
                                      "read_through": dict(state.lane_read_through),
                                      "lane_consumed": dict(state.lane_consumed)})
    # 🔴 THE EVENT THE TIMING LOG NEVER HAD (spec 018 FR5). Every other stage of an exchange was
    # stamped and the WAIT was not — so the log could show how long an agent took to answer and
    # never why, and the sixty-second pause JJ timed was invisible in the one place built to make
    # latency visible. `holds` is the fact that decides fast-check versus long-listen, recorded at
    # the moment it is read rather than reconstructed afterwards.
    timing.stamp(state.session, "watch_open" if watching else "watch_closed",
                 lane=lane, holds=bool(state.lane_holds_turns.get(lane)),
                 watching=sorted(state.watching_lanes))
    # ATTRIBUTED TO THE LANE THAT SAID IT (FR8). Without this a background agent re-arming its
    # watch would repaint the orb of the agent he is actually talking to — publishing one agent's
    # state through another's channel, which is the defect `agent_state` and `muted` were already
    # separated to avoid.
    mine = state.lane_states.get(lane, "idle")
    if watching:
        if mine == "thinking":
            await _set_agent_state(state, "idle", lane=lane)
    elif mine == "idle":
        await _set_agent_state(state, "thinking", lane=lane)
    # 🔴 HANDED OVER HERE, ON THE OPENING EDGE, AND CLEARED IN THE SAME BREATH. This is the moment
    # the agent comes back to listen, which is exactly when "the thing you said never reached him"
    # is still worth acting on — it can restate before he asks again. Clearing on delivery is what
    # stops a re-armed watch announcing the same dead reply every 30 seconds forever.
    expired = state.lane_expired.pop(lane, []) if watching else []
    # …and what DID reach him, the same way (JJ, 2026-09-29): the agent can stop wondering whether a
    # held clip was heard, and never re-says one that was.
    played = state.lane_played.pop(lane, []) if watching else []
    return web.json_response({"agent_state": state.agent_state, "verbose": state.verbose,
                              "lane": lane, "watching_lanes": sorted(state.watching_lanes),
                              **({"expired": expired} if expired else {}),
                              **({"played": played} if played else {})})


def _will_respond(agent_state: str) -> bool:
    """Does this read signal carry an INTENT TO RESPOND? The acknowledgement cue follows this.

    **THE READ SIGNAL ALREADY CARRIED THE INTENT; nothing was listening to it.** `/consumed` has
    always taken a `state` — what the agent is doing now that it has the turn — and the two
    answers mean opposite things about whether an answer is coming:

        thinking (the default)   it has the turn and is working on a reply   -> acknowledge
        idle                     it has read the turn and is going straight
                                 back to listening without answering        -> say nothing

    `idle` is the case JJ reported. An agent that reads something and deliberately leaves it
    unanswered must not make a sound that says *I heard you and I am on it*, because then the
    absence of the sound means nothing and the presence of it means nothing either.

    Written as a function rather than an inline `!= "idle"` so there is one definition of "is an
    answer coming", and so a new agent state added later has to come here and decide which side of
    the line it is on rather than silently landing on the acknowledging side.
    """
    return agent_state != "idle"


async def handle_consumed(request: web.Request) -> web.Response:
    """The agent reports how far it has read — mirrors `mc consumed`.

    This is what turns the page from a transcript into a status display: the user can see the
    gap between what they have said and what the agent has actually processed, instead of
    guessing whether they are talking into a void.

    **It is also where the acknowledgement cue now lives** (spec 007, FR3), because this is the
    first moment at which the tunnel knows anything about the agent's intent. Reading is not
    acknowledgement; being answered is.
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
    # WHETHER THIS READ ACTUALLY MOVED THE BOUNDARY, sampled before it is overwritten. A repeated
    # `consumed` at the same cursor acknowledges nothing new, and a cue for it would be a sound
    # with no turn behind it — the same defect as firing on capture, one layer along.
    advanced = cursor > state.consumed_cursor
    state.consumed_cursor = cursor
    # PER-LANE, and the watch has been sending its lane here all along — the server simply threw
    # it away (spec 015 FR1). Monotonic, because a `watch` resuming from an older `--since` is
    # re-reading rather than un-reading, and a receipt flickering back to grey is worse than one
    # that was never drawn.
    _lane = (body or {}).get("lane")
    if isinstance(_lane, str) and _lane:
        state.lane_consumed[_lane] = max(state.lane_consumed.get(_lane, -1), cursor)
    # Persisted beside the log so a server restart resumes from the real read position instead
    # of reporting every turn ever logged as pending. Best-effort: see write_consumed_cursor.
    store.write_consumed_cursor(state.session, cursor, lanes=state.lane_consumed)
    # The agent has the turn in hand. Everything from here to `say_requested` is IT thinking —
    # the stage that dominated every measurement and was invisible until it had a name.
    # THE LANE THAT TOOK THE TURN (spec 018 FR5). `consumed` is the stamp that opens the agent's
    # thinking time, and with three lanes an unlabelled one cannot say whose thinking it is.
    timing.stamp(state.session, "consumed", cursor=cursor,
                 lane=(body or {}).get("lane") or state.lanes.default)
    # The agent now HOLDS turns it has not answered, which is what makes its next wait a
    # pre-reply check rather than a listen. See TunnelState.agent_holds_turns.
    agent_state = str((body or {}).get("state") or "thinking")
    consumed_lane = (body or {}).get("lane") or None
    # RECORDED AGAINST THE LANE THAT TOOK THEM (spec 018 FR1), with the session-wide flag kept in
    # step so every existing reader of `agent_holds_turns` sees what it always saw.
    state.lane_holds_turns[consumed_lane or state.lanes.default] = True
    state.agent_holds_turns = True
    await _broadcast_json(
        state,
        {"type": "consumed", "cursor": cursor, "pending": state.pending_turns(),
         # The blue-tick half rides the same message as the boundary it qualifies (013 FR7,
         # NFR2): one event moves both, so sending them separately would let the page paint a
         # turn delivered and read at two different instants for one cause.
         "read_through": dict(state.lane_read_through),
         # PER-LANE READ CURSORS ride the same message as the boundary they qualify (015 FR1):
         # one event moves both, so sending them apart would let the page paint a turn read at a
         # different instant from the cursor that made it so.
         "lane_consumed": dict(state.lane_consumed)},
    )
    # An agent reporting its OWN state names its own lane; `consumed_lane` is absent only on the
    # legacy call shape that predates lanes, where there was exactly one reader and it was live.
    await _set_agent_state(state, agent_state, consumed_lane or state.lanes.current)
    # THE ACKNOWLEDGEMENT, and it is the intent that earns it — not the arrival of the words, and
    # not the act of reading them. See `_will_respond`. An agent that reads a turn and signals it
    # is going back to listening (`state: "idle"`) makes no sound at all, which is the whole point:
    # the cue's absence is now information.
    if advanced and _will_respond(agent_state):
        await _push_cue(state, "heard")
    # UNAFFECTED, and deliberately so. `thinking` answers a different question — not "did you get
    # that" but "are you still working" — and spec 007 changes the acknowledgement cue only.
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
    await ws.send_json(await _ready_frame(state))
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
                await _on_audio(state, msg.data, loop, ws)
            elif msg.type == WSMsgType.TEXT:
                await _on_control(state, msg.data, ws)
            elif msg.type == WSMsgType.ERROR:
                state.fail("transport", str(ws.exception()))
                break
    finally:
        await _drop_client(state, ws)
        # Flush whatever was mid-sentence when the socket dropped, so a disconnect never
        # silently eats the last thing that was said.
        try:
            await _flush(state, loop)
        except Exception as exc:
            state.fail("transport", f"flush failed: {exc}")
        state.close_capture()
    return ws


async def _ready_frame(state: TunnelState) -> dict:
    """The whole picture a page needs to render, sent on connect and again on `resync`."""
    # `verbose` rides on the ready frame so the client ADOPTS it rather than pushing its own.
    # `muted` deliberately does NOT — that is a fact about this device's microphone, and a phone
    # muting a desktop in another room would be wrong.
    # The wake name travels too, so the page's prompt cannot drift from what the gate accepts.
    # Hardcoding "hey claude" in the copy is how a renamed assistant keeps telling people to say
    # the old name.
    # Objective per-lane activity on the ready frame, so the fleet home view renders immediately on
    # connect (a phone reconnects every lock) instead of waiting for the first broadcast tick.
    # Computed in a thread — the derivation shells git and tails the logs — and cached, so cheap.
    _activity = await asyncio.get_running_loop().run_in_executor(None, _fleet_activity)
    return (
        {"type": "ready", "session": state.session, "verbose": state.verbose,
         "wake": config.wake_name(), "wake_phrases": list(state.wake.phrases),
         # The echo guard's page half: silence the mic while a cue plays (shell.html CUE GATE).
         "echo_guard": config.echo_guard_enabled(),
         # THE WHOLE LANE PICTURE ON CONNECT. A page that learned about lanes only from the next
         # change event would show an empty room until somebody moved — and a phone reconnects
         # every time it locks, so that is the common case rather than the edge one.
         "lane": state.lanes.current, "lanes": list(state.lanes.names),
         # Each roster agent's own colour, so blue is magnus whoever else is up (2026-10-01).
         "lane_colors": _lane_colors(state),
         "lane_roles": config.lane_roles(),
         "broadcast": lanes_mod.BROADCAST,
         "lane_states": dict(state.lane_states),
         # A page that reconnects must be able to tell "listening" from "working" immediately
         # (015 FR2), not only after the next state change — a phone reconnects every time it
         # locks, so that is the common case rather than the edge one.
         "watching_lanes": sorted(state.watching_lanes),
         "lane_consumed": dict(state.lane_consumed),
         "default_lane": state.lanes.default,
         "lane_activity": _activity,
         "waiting": {n: len(c) for n, c in state.lane_held.items() if c},
         "waiting_split": state.lane_waiting_split(),
         "liaisons": {k: list(v) for k, v in state.liaisons.items()},
         # Spec 044: the app's outputs for the page's caret, so a page that (re)loads paints it now.
         "native_routes": _native_routes(state)}
    )


async def _drop_client(state: TunnelState, ws) -> None:
    """Forget a page, and everything that was only true because it existed.

    A named function rather than a block inside a `finally`, because what happens when the last
    page dies is now load-bearing enough to need a test — and a test that re-implemented this
    block would prove nothing about the server.
    """
    state.clients.discard(ws)
    state.native_clients.pop(ws, None)
    if state.clients:
        return
    # The last page went away, so nothing is capturing regardless of what it last said.
    state.capturing = False
    # NOR IS ANYONE SPEAKING. `user_speaking` is set only by a client message, and a socket that
    # dies mid-word never sends the matching `speaking: false` — so the flag stayed true for the
    # life of the server. Harmless while it only drove a hold loop that a reconnect would clear;
    # fatal once a wait gates on it, because the wait would hold forever on a page that no longer
    # exists.
    state.user_speaking = False
    # 🔴 NOR IS ANYTHING PLAYING, and both halves of that matter (spec 026 F5).
    #
    # * **The clips die with the page.** They were sent, never confirmed, and no receipt is coming
    #   from a socket that no longer exists — so without this they are simply gone, which is the
    #   one thing `lane_held` exists to prevent. `keep_playing` stays true: whatever was actually
    #   on the speaker he heard some of, and spec 025 is why that one does not come back.
    # * **`speaking_lane` would otherwise be a permanent open barge gate.** It now decides whether
    #   audio can be talked over, so a stale value means the first thing he says after reconnecting
    #   registers as an interruption of nobody — and a barge clears `undelivered`, which is where
    #   his replies were waiting while the phone was away.
    try:
        await _return_inflight(state)
    except Exception as exc:
        state.fail("transport", f"return in-flight on disconnect failed: {exc}")
    state.speaking_lane = None
    state.clip_owner.clear()


async def _on_control(state: TunnelState, raw: str, ws: web.WebSocketResponse) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return
    kind = msg.get("type")
    if kind == "hello":
        sr = int(msg.get("sample_rate") or config.TARGET_SR)
        state.client_sr = sr
        state.client_srs[ws] = sr
        # What the page's mic ACTUALLY got — which echo canceller, NS, AGC (2026-09-30). Reported
        # by the browser, published on `status` as `client_mic`, so an A/B of `?ec=system` can be
        # read rather than guessed.
        if isinstance(msg.get("mic"), dict):
            state.client_mic = msg["mic"]
        # A page that can play a streamed clip says so. An older shell still open in a tab would
        # play only the FIRST piece of one and drop the rest, so `_speak_now` streams only when
        # every connected page has declared it (a shell reload lands at the next orb-off).
        if msg.get("streams"):
            state.stream_clients.add(ws)
        else:
            state.stream_clients.discard(ws)
        # SPEC 044: the primitive handshake. An app that lists `native` is driven from here on, and
        # told so in the ack; anything else (a browser, an app before 0.8.0) never sees a directive.
        client = native_mod.from_hello(msg)
        ack: dict[str, Any] = {"type": "hello_ack", "server_sample_rate": config.TARGET_SR}
        if client is not None:
            state.native_clients[ws] = client
            ack["native"] = {"protocol": client.protocol}
        else:
            state.native_clients.pop(ws, None)
        await ws.send_json(ack)
        if client is not None and "awake" in client.ops:
            await ws.send_json({"type": "native", "op": "awake", "on": bool(state.channel_open)})
    elif kind == "native_state":
        # SPEC 044 FR2: what the app holds now. Correct it if it differs from the policy, and repaint
        # every page's output caret from it.
        client = state.native_clients.get(ws)
        if client is None:
            return
        native_mod.apply_report(client, msg)
        for d in native_mod.audio_if_needed(state.channel_open, client, state.native_pick):
            await ws.send_json(d)
        await _broadcast_routes(state)
    elif kind == "route_pick":
        # SPEC 044 FR5: his pick on the page's caret. Stored, so a restart keeps it, and applied to
        # every driven app at once.
        v = msg.get("value")
        state.native_pick = int(v) if isinstance(v, (int, float)) and int(v) >= 0 else None
        try:
            native_mod.save_pick(config.session_dir(), state.native_pick)
        except OSError as exc:
            state.fail("transport", f"saving the route pick failed: {exc}")
        for nws, client in list(state.native_clients.items()):
            try:
                for d in native_mod.audio_if_needed(state.channel_open, client, state.native_pick):
                    await nws.send_json(d)
            except Exception:
                pass
        await _broadcast_routes(state)
    elif kind == "resync":
        # The UI frame (re)loaded inside a shell whose socket never dropped (spec 023 FR4), so it
        # asks for the ready frame again rather than a replay the shell would have to keep current.
        # To this socket only: no other page is starting over.
        await ws.send_json(await _ready_frame(state))
    elif kind == "played" and str(msg.get("id") or "") == "cue-shutdown":
        # The shutdown notice finished on the page — release the waiting shutdown. It is not an
        # agent's clip, so none of the receipt bookkeeping below applies to it.
        if state.shutdown_notice_played is not None:
            state.shutdown_notice_played.set()
    elif kind == "played":
        state.last_played = str(msg.get("id") or "")
        # THE RECEIPT IS WHAT LETS THE SERVER FORGET IT (spec 022 FR2). Until this arrives the
        # clip is still the server's problem, because everything between here and his ear can
        # still drop it.
        _clip_played(state, state.last_played)
        # HE HEARD IT, SO ITS HEADS-UP IS DONE (spec 036): the phone clears the notification. Only
        # an UNCUT receipt does this: showing the notification never counts as hearing the clip, and
        # neither does a clip he switched off or talked over halfway.
        try:
            if not msg.get("cut") and notices.resolve(state.session, state.last_played):
                _notices_changed(state)
        except OSError:
            pass
        # TELL THE OTHER DEVICES (spec 032). Every clip goes to every socket, so a backgrounded browser
        # tab holds a copy in its queue and would play it on resume. JJ, 2026-10-09: *"when I switched
        # back to the browser, the two turns from you played back in the browser again, as if I hadn't
        # heard them in the app."* The receiving pages drop that id from their queues.
        if state.last_played:
            for other in list(state.clients):
                if other is ws:
                    continue
                try:
                    await other.send_json({"type": "clip_played", "id": state.last_played})
                except Exception:
                    state.clients.discard(other)
        for _r in state.recent_spoken:                   # the echo window runs from here (echo.py)
            if _r.get("id") == state.last_played:
                _r["played_at"] = time.time()
        # 🔴 THE LANE THIS CLIP ACTUALLY BELONGED TO (spec 024), looked up by its own id rather
        # than read from a slot the next clip may already have overwritten.
        owner = state.clip_owner.pop(state.last_played, None) \
            or state.speaking_lane or state.lanes.current
        # HOW IT ENDED (JJ, 2026-10-05: *"an agent should be aware of whether or not their turn
        # played or not or was interrupted by me speaking"*). The page sends `cut` when something
        # stopped the clip before its end — `barge_in` (he talked over it), `channel_off` (he turned
        # the orb off), `stopped` (capture was rebuilt) — with how far in he got. Without it a clip
        # he cut off after two seconds was reported as heard, and its agent built on an answer he
        # never got. Measured 2026-10-05: a barge-in on rigel at 17:38:46.990, `played` 13 ms later.
        cut = msg.get("cut") if isinstance(msg.get("cut"), str) else None
        cut_info = {}
        if cut:
            cut_info = {"cut": cut[:20]}
            for k in ("heard_s", "duration_s"):
                v = msg.get(k)
                if isinstance(v, (int, float)):
                    cut_info[k] = round(float(v), 1)
        timing.stamp(state.session, "played", clip=state.last_played, lane=owner, **cut_info)
        # TELL THE AGENT (JJ, 2026-09-29) — recorded for its next watch-open, see `lane_played`.
        if state.last_played and owner:
            heard = state.lane_played.setdefault(str(owner), [])
            heard.append({"clip": state.last_played,
                          "text": state.clip_text.pop(state.last_played, ""),
                          "played_at": round(time.time(), 1), **cut_info})
            del heard[:-config.UNDELIVERED_MAX]
            # The idle anchor moves with every receipt, cut or not (spec 024 FR2/KD6).
            state.lane_last_played[str(owner)] = {"clip": state.last_played,
                                                  "played_at": round(time.time(), 2)}
        # Playback finished, so the agent is no longer speaking. The client is the only party
        # that knows when a clip actually ended — the server only knows when it finished
        # sending — so the receipt is what closes the state machine back to idle.
        #
        # RELEASES THE LANE THAT WAS SPEAKING, not the one that is live now (016 FR3). A clip runs
        # for seconds and he can switch during it; releasing `lanes.current` would leave the real
        # speaker on `speaking` forever and reset an agent that never spoke.
        await _set_agent_state(state, "idle", owner)
        # ⚠ CLEARED ONLY IF THIS CLIP IS THE ONE IT NAMES. Nulling it unconditionally is what
        # stranded the other lane: with two clips in flight, the first receipt wiped the slot and
        # the second speaker had nothing left that could release it.
        if state.speaking_lane == owner:
            state.speaking_lane = None
        # `playing_clip` needs no clearing: `_clip_played` above removed this clip from
        # `lane_inflight`, which advances the derivation to whatever is next in the send order.
    elif kind == "verbose":
        # Stored and republished, never acted on here — see TunnelState.verbose.
        state.verbose = bool(msg.get("value"))
        await _broadcast_json(state, {"type": "verbose", "value": state.verbose})
        timing.stamp(state.session, "verbose_toggled", value=state.verbose)
    elif kind == "capturing":
        state.capturing = bool(msg.get("value"))
        if not state.capturing:
            state.last_audio_at = 0.0
            # SAME FLUSH MUTE AND DISCONNECT ALREADY DO, and this was the hole. Since 2026-08-16
            # switching the orb off RELEASES the microphone, so releasing capture became the most
            # common way to stop talking — and it was the one path that left a half-utterance open
            # with no further frame able to close it. Two costs: the words spoken immediately
            # before the tap were held rather than logged, and `speech_active` stuck true, which
            # under a speaking-gated wait hangs the wait outright.
            state.user_speaking = False
            try:
                await _flush(state, asyncio.get_running_loop())
            except Exception as exc:
                state.fail("transport", f"flush on capture stop failed: {exc}")
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
        if was_open and not state.channel_open:
            # Closing the conversation stops audio in both directions, so it ends an utterance for
            # the same reason a mute does. Ordered before the reopen branch below because the two
            # are mutually exclusive and this one is the one that can lose words.
            state.user_speaking = False
            try:
                await _flush(state, asyncio.get_running_loop())
            except Exception as exc:
                state.fail("transport", f"flush on channel close failed: {exc}")
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
            # NOTE (JJ, 2026-09-24): held per-lane clips are deliberately NOT flushed on reopen. He
            # wants them WAITING as raised hands until he goes to each lane — "I would want the turns
            # waiting for me ... so I hear my agent's reports." The real cause of "no sound after a
            # toggle" was a CLIENT bug (a stale cross-context audio analyser), fixed in index.html;
            # flushing here would auto-play and, worse, pop clips the dead context never played.
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

    🔴 **THE GATE ASKS A PHYSICAL QUESTION, NOT A CONVERSATIONAL ONE (spec 026 FR2).** It used to
    read `agent_state`, which is the LIVE lane's state — so it answered "is the agent I am talking
    to speaking", and got that answer from a cache that a lane switch left stale. `speaking_lane`
    is the honest source: it names whoever's clip is actually on the speaker, which is the only
    thing that can be talked over. WHOSE reply it is changes what happens next, not whether the
    gate opens — see the disposition after the threshold.
    """
    if not config.barge_in_enabled() or state.speaking_lane is None:
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
    # 🔴 WHOSE REPLY THIS ACTUALLY IS (spec 026 FR3/FR4). Two different events wear the same
    # signal, and only the lane tells them apart:
    #
    # * **He interrupted the agent he is talking to.** A real barge. He heard the clip, and spec
    #   025 is right to keep it from coming back.
    # * **He said something to somebody ELSE while a clip was in the air.** *"I didn't barge in. I
    #   just switched the lane and was listening. I didn't interrupt you."* (2026-08-26) The audio
    #   still has to stop — he cannot listen to one agent and talk to another at the same time —
    #   but that agent has NOT been answered, so its reply goes back to its hold with the hand up
    #   and he hears it when his attention returns.
    #
    # The lane he is addressing is `utterance_lane` when an utterance is already open, because a
    # switch made mid-sentence must not redirect what he has already started saying (spec 023).
    interrupted = state.speaking_lane
    addressed = state.utterance_lane or state.lanes.current
    cross_lane = bool(interrupted) and interrupted != addressed
    timing.stamp(state.session, "barge_in", score=state.last_barge_score,
                 lane=interrupted, addressed=addressed, cross_lane=cross_lane)
    await _broadcast_json(state, {"type": "stop_playback", "reason": "barge_in",
                                  "score": state.last_barge_score})
    # Drop anything still queued as well. Stopping the clip he interrupted and then playing the
    # next one at him would be the same interruption wearing a different hat.
    #
    # ⚠ `lane_held` IS DELIBERATELY NOT TOUCHED (spec 012 TC3). He interrupted the lane he is
    # talking to; he has not interrupted an agent he cannot hear, and discarding its held speech
    # would be the tool deciding on his behalf that an answer he never received is no longer
    # wanted. That conflation is the only mechanism found that can lose all nine of the clips
    # TC3 measured, and keeping the two stores apart is what stops it happening again.
    #
    # 🔴 **AND THE PROTECTION ABOVE HAD A HOLE IN IT UNTIL SPEC 022.** `lane_held` was safe here,
    # but `_flush_lane_held` popped clips out of it and pushed them to the browser — where the
    # `stop_playback` this very message sends empties the queue wholesale. So the store was
    # guarded and the clips were not, from the moment he switched lanes until they played.
    # Measured 2026-08-26: three of Kepler's replies gone, and the hand gone with them.
    #
    # `keep_playing` is what splits the two events above. On a cross-lane barge the clip that was
    # on the speaker comes back TOO, because he was not answering it — he was talking to somebody
    # else over the top of it.
    #
    # ⚠ Read BEFORE the return, which empties the store `playing_clip` is derived from.
    interrupted_clip = state.playing_clip
    await _return_inflight(state, keep_playing=not cross_lane)
    state.undelivered.clear()
    # THE INTERRUPTED LANE IS THE ONE RELEASED (016 FR3). Barge-in stops the clip that is playing,
    # so the agent returned to rest is the one whose clip it was — not whoever happens to be live,
    # which after a summons-shaped interruption is frequently somebody else.
    await _set_agent_state(state, "idle", state.speaking_lane or state.lanes.current)
    state.speaking_lane = None
    # The queue is dropped, so the owners would otherwise accumulate for the life of the session
    # (spec 024). The clips themselves went back to their lanes' holds a few lines above.
    #
    # ⚠ **EXCEPT THE ONE THAT WAS PLAYING, WHOSE RECEIPT IS ON ITS WAY.** This used to clear
    # wholesale, on the reasoning that nothing is in flight after a stop — and spec 025 measured
    # the opposite: `stop_playback` is precisely what makes the browser fire `onended`, so a
    # `played` for the interrupted clip lands about 8 ms later. With the map emptied it resolved
    # through the `or state.lanes.current` fallback and marked THE LANE HE IS NOW TALKING TO idle,
    # which is the wrong agent and often one that is mid-thought. Keeping this single entry is
    # what lets that receipt still name its own lane.
    _owner = state.clip_owner.get(interrupted_clip or "")
    state.clip_owner.clear()
    if interrupted_clip and _owner:
        state.clip_owner[interrupted_clip] = _owner


def _client_rate(state: TunnelState, ws: Any) -> int:
    """The rate THIS socket announced; a socket that never said `hello` is taken at the target rate."""
    if ws is None:
        return state.client_sr
    return state.client_srs.get(ws, config.TARGET_SR)


async def _on_audio(state: TunnelState, raw: bytes, loop: asyncio.AbstractEventLoop,
                    ws: Any = None) -> None:
    samples = asr_mod.pcm16_to_float32(raw)
    rate = _client_rate(state, ws)
    if rate != config.TARGET_SR:
        samples = asr_mod.resample_linear(samples, rate, config.TARGET_SR)
    state.frames_received += 1
    state.samples_received += samples.size
    state.last_audio_at = time.monotonic()
    try:
        state.capture(samples)
    except Exception as exc:  # never let diagnostics break the session
        state.fail("capture", str(exc))

    await _maybe_barge(state, samples)

    # 🔴 WHICH LANE HE STARTED SPEAKING TO (spec 023). Latched on the silence→speech edge and
    # held until the turn is routed, because everything between those two moments takes seconds —
    # ASR, the voiceprint, the turn model — and he can move the conversation inside that window.
    # Reading the live lane at the END of that pipeline delivers his words to whoever he switched
    # TO, which is an agent he was never talking to and cannot un-hear them.
    speaking_before = bool(state.buffer.speech_active)
    utterance_before = state.buffer.utterance_id
    completed = state.buffer.feed(samples)
    if state.buffer.speech_active or getattr(state.buffer, "utterance_open", False):
        state.last_speech_at = time.monotonic()
    if not speaking_before and state.buffer.speech_active:
        # 🔬 EVIDENCE (2026-09-02): a turn routed to a lane he had not tapped, and the logs could not
        # say why because the latch was never stamped. Now the silence→speech edge records which lane
        # it latched — or, when the slot is STILL OCCUPIED, that this fresh utterance could NOT re-latch
        # (the `is None` guard held a stale lane, which is the spec-027 failure) and will therefore route
        # to `held`, not to the lane that is live NOW. That single line is the whole diagnosis.
        if state.utterance_lane is None:
            state.utterance_lane = state.lanes.current
            timing.stamp(state.session, "utterance_latch", lane=state.utterance_lane)
        else:
            timing.stamp(state.session, "utterance_latch_stale",
                         held=state.utterance_lane, live=state.lanes.current)
    if completed is None:
        _maybe_commit(state, loop)
        _maybe_partial(state, loop)
        return
    # This turn's pieces leave with it, BEFORE any await: the next utterance can start committing
    # while `_emit` is still transcribing this one's tail.
    pieces = _take_pieces(state, utterance_before)
    # RAISED BEFORE THE FIRST AWAIT, and that ordering is the whole point. Between the buffer
    # emptying and the turn reaching the log, both speech signals read false while he has in fact
    # just spoken — and `_emit` awaits the recognizer, so `/status` IS served inside that window.
    # Counting from here closes it. See TunnelState.speech_pending.
    state.speech_pending += 1
    try:
        await _emit(state, completed, loop, pieces)
    finally:
        state.speech_pending = max(0, state.speech_pending - 1)


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
    # ONLY THE UNCOMMITTED TAIL (2026-10-06). The preview used to re-decode the WHOLE turn every
    # 0.7 s: on a minute-long turn that held the model, and four cores, for seconds at a time, and
    # the final decode queued behind it. The pieces already committed are text; prefix them.
    p = state.pieces
    prefix = ""
    if p.usable(state.buffer.utterance_id) and p.upto is not None:
        audio = state.buffer.span(p.upto)
        prefix = asr_mod.join_pieces(p.texts)
    else:
        audio = state.buffer.snapshot()
    if audio is None or audio.size == 0:
        return
    state.partial_busy = True
    state.last_partial_at = now

    async def run() -> None:
        try:
            tail = await loop.run_in_executor(None, state.recognizer.try_transcribe, audio)
            text = asr_mod.join_pieces([prefix, tail or ""]) if prefix else tail
            if text and text != state.partial_text:
                state.partial_text = text
                await _broadcast_json(state, {"type": "partial", "text": text})
        except Exception as exc:  # a preview failing must never break the session
            state.fail("asr", f"partial: {exc}")
        finally:
            state.partial_busy = False

    loop.create_task(run())


def _maybe_commit(state: TunnelState, loop: asyncio.AbstractEventLoop) -> None:
    """Transcribe the next piece of the in-flight turn if he is pausing right now.

    One piece in flight at a time, so pieces finish in order; a pause that arrives while one is
    still decoding is skipped and the next piece simply runs longer. Scheduled, never awaited:
    audio ingest must not block on ASR."""
    pause_ms = config.asr_commit_pause_ms()
    if pause_ms <= 0:
        return
    b = state.buffer
    p = state.pieces
    if p.base != b.utterance_id:
        # A new utterance — or the last one was discarded as noise, which `feed` reports the same
        # way as "not finished". Old indices would point into the wrong audio.
        p = state.pieces = _Pieces(b.utterance_id)
    if p.failed or (p.task is not None and not p.task.done()):
        return
    start = p.upto if p.upto is not None else b.onset_index()
    end = b.commit_point(start, int(b.sr * pause_ms / 1000), int(b.sr * config.asr_commit_min_s()))
    if end is None:
        return
    audio = b.span(start, end)
    p.upto = end  # claimed synchronously, so the same pause cannot cut the piece twice
    slot = len(p.texts)
    p.texts.append("")

    async def run() -> None:
        try:
            text = await loop.run_in_executor(
                None, lambda: state.recognizer.transcribe(audio, piece=True))
            p.texts[slot] = text
            timing.stamp(state.session, "asr_piece", audio_s=round(audio.size / b.sr, 2),
                         chars=len(text))
        except Exception as exc:  # a lost piece must not lose words: fall back to a whole decode
            p.failed = True
            state.fail("asr", f"piece: {exc}")

    p.task = loop.create_task(run())


def _take_pieces(state: TunnelState, utterance_id: int) -> _Pieces | None:
    """Hand the closed turn its pieces and start the next turn clean. None when the pieces belong
    to some other utterance, so the turn decodes whole."""
    p, state.pieces = state.pieces, _Pieces()
    if not p.usable(utterance_id):
        return None
    p.cut = state.buffer.last_cut
    return p


async def _transcribe_turn(state: TunnelState, samples, loop: asyncio.AbstractEventLoop,
                           pieces: _Pieces | None) -> str:
    """The turn's text: the pieces already transcribed at his pauses plus the tail, or one whole
    decode when there are no usable pieces."""
    if pieces is not None and pieces.task is not None:
        await pieces.task  # the last piece may still be decoding; it never raises
    if pieces is None or pieces.failed or pieces.upto is None or pieces.upto <= pieces.cut:
        return await loop.run_in_executor(None, state.recognizer.transcribe, samples)
    tail = samples[pieces.upto - pieces.cut:]
    tail_text = await loop.run_in_executor(
        None, lambda: state.recognizer.transcribe(tail, piece=True))
    return asr_mod.join_pieces([*pieces.texts, tail_text])


async def _flush(state: TunnelState, loop: asyncio.AbstractEventLoop) -> None:
    utterance_before = state.buffer.utterance_id
    completed = state.buffer.flush()
    pieces = _take_pieces(state, utterance_before)
    if completed is not None:
        # Same accounting as the ingest path: a flush is a close, and its turn is pending until
        # it is in the log. This is the path a mute, an orb tap or a dropped socket takes, which
        # is exactly when an agent is most likely to be asking whether he has finished.
        state.speech_pending += 1
        try:
            await _emit(state, completed, loop, pieces)
        finally:
            state.speech_pending = max(0, state.speech_pending - 1)


async def _emit(state: TunnelState, completed, loop: asyncio.AbstractEventLoop,
                pieces: _Pieces | None = None) -> None:
    """Transcribe a completed utterance, gate it, log it, and tell the client."""
    samples, t_start, t_end = completed
    # The clock the USER experiences starts the moment they stop talking, not when we finish
    # some internal step, so this is where the timing log begins an exchange.
    timing.stamp(state.session, "utterance_end", audio_s=round(t_end - t_start, 2))
    # Announce each stage. The user cannot see any of this from outside, and an unexplained
    # pause is the difference between "it's working" and "it's broken" to someone waiting.
    #
    # 🔴 THE OWNER OF THIS WHOLE FLOW, RESOLVED ONCE (spec 016 FR1, TC2). At this instant nobody
    # knows which lane the utterance is for — that is precisely what ASR is about to reveal — so
    # the stage opens against the lane that is live, which is the only honest answer available.
    # What was wrong was re-asking the same question at the close: ASR returns "hey magnus", the
    # gate moves the conversation, and the `idle` lands on magnus while atlas keeps `transcribing`
    # forever. Reported 2026-08-25: *"if I am talking to you, I do see that the Atlas lane says
    # transcribing."* Holding the name is the entire fix.
    opener = state.lanes.current
    await _set_agent_state(state, "transcribing", opener)
    # A sound the instant transcription starts, so the gap after he stops speaking is audible as
    # "working on your words" rather than silence (JJ, 2026-09-14). This is the live lane by
    # construction — `opener` is the lane that had the floor when he spoke.
    # ⚠ BEHIND A TOGGLE since 2026-09-30 (COMMAND_BRIDGE_TRANSCRIBING_CUE). It fires on ANY sound
    # the endpointer closes — before ASR and the voiceprint have said whether it was him — so on a desk
    # mic its own chime, picked back up, closed another "utterance" and fired it again. JJ: *"that cue
    # sound when you hear me plays regardless of … you recognize me or just any sound … can we put it
    # behind a toggle and turn off that toggle?"*
    if config.transcribing_cue_enabled():
        await _push_cue(state, "transcribing")
    # ASR is CPU-bound; keep it off the event loop or audio ingest stalls. With pieces committed at
    # his pauses, only the tail is decoded here.
    text = await _transcribe_turn(state, samples, loop, pieces)
    timing.stamp(state.session, "transcribed", chars=len(text or ""),
                 pieces=len(pieces.texts) if pieces is not None else 0)
    if not text:
        # No cue here: nothing was heard, and a sound would announce a turn that does not exist.
        await _set_agent_state(state, "idle", opener)
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
    # THE ECHO GUARD (echo.py, behind COMMAND_BRIDGE_ECHO_GUARD). An agent's own clip coming back
    # through his desk mic can pass both gates above — it was spoken inside the conversation window,
    # and the voiceprint is not a hard veto — so it is checked against what was just played, which is
    # the one thing the tool knows for certain. Logged, not discarded, so a false drop is findable.
    if config.echo_guard_enabled():
        from . import echo as echo_mod
        _echoed = echo_mod.match(text, echo_mod.candidates(state.recent_spoken, time.time()))
        # (The server-side CUE check — `echo.cue_blip` — ran here and was REMOVED the same day: at
        # 14:08 it dropped his real "ship it" to dexter, which overlapped a cue. The page now ships
        # silence while a cue plays (shell.html CUE GATE), which stops cue blips at the source, so a
        # text rule that can only guess from word count was all risk and no remaining benefit.)
        if _echoed is not None:
            addressed, reason = False, "echo"
            timing.stamp(state.session, "echo_dropped", text=(text or "")[:80],
                         clip=_echoed[:80])

    # WHICH agent was this for (spec 012). Resolved here, ONCE, on the same pass as the wake
    # verdict — before the window bookkeeping below, because a refusal has to wind the window back
    # exactly like any other unaddressed turn. A refused turn that left the window open would hold
    # the conversation on behalf of a summons nobody could route.
    # 🔴 RESOLVED AGAINST THE LANE HE IS ON WHEN TRANSCRIPTION FINISHES — the current live lane, NOT
    # the one he started speaking to. **This REVERTS specs 023 and 027** at JJ's explicit direction,
    # 2026-09-02: asked which he wanted, he said *"the latter, where I am at when transcription finishes.
    # That was the behaviour before we broke it."*
    #
    # 023 had latched the lane at the silence→speech edge (`utterance_lane`) so a tap made while ASR was
    # still running would not redirect words he had already finished saying. That protected the wrong
    # thing for how he actually works now — and its single-slot latch was the source of the recurring
    # mis-route (a fresh utterance inheriting a stale lane, turns 124/241). The simpler model has no
    # latch, no slot, no race: **your words follow you.** The cost, stated honestly so a future reader
    # is not surprised: if he speaks to A and switches to B before the transcript lands, the A-utterance
    # now goes to B — the exact thing 023 was built to stop (his 2026-08-26 complaint). He owns that
    # trade: he waits for transcription before switching. The `utterance_latch` timing stamps stay as
    # evidence but no longer drive routing.
    #
    # An explicit leading summons still switches — `resolve` reads the transcript for that and only uses
    # the lane to decide stay-versus-switch — because saying a name is him addressing someone ON PURPOSE.
    addressed_lane = state.lanes.current
    timing.stamp(state.session, "utterance_route",
                 addressed=addressed_lane, latched=state.utterance_lane, live=state.lanes.current)
    routing = lanes_mod.resolve(text, state.lanes.names, addressed_lane)
    state.utterance_lane = None
    if routing.action == "refuse":
        # He named somebody, and it was not exactly anybody. Deliver to NO ONE rather than to the
        # lane already live: that lane is precisely the wrong guess, since a summons is only
        # ambiguous when it looks like an attempt to leave it. Costs one repeat (TC2).
        addressed, reason = False, (routing.reason or "ambiguous")
        # AND HE IS TOLD, BUT NOT BY THIS PROCESS MAKING A NOISE.
        #
        # TC2 asks for the refusal to be "said audibly", and the obvious implementation — a fifth
        # cue — is one this repo has already refused in an executable guard: *"a fifth is his call,
        # not the implementer's"* (tests/test_acknowledgement_cue.py). That guard is right, and it
        # is right for a better reason than vocabulary size. **A cue could only say that something
        # went wrong. The agent can ask "did you mean Claude or Codex?"** — so the signal goes to
        # the layer that owns words, which is the whole dumb-tool/smart-agent split.
        #
        # It wakes the LIVE lane's wait, because that is the agent he was already talking to and
        # therefore the one who can sensibly ask. A lookup, not a guess.
        state.ambiguous += 1
        state.last_ambiguous = list(routing.candidates)
    lane = routing.lane
    # GUEST MODE (spec 042): on a guest lane a voice the voiceprint does not know (a guest in the
    # room) is addressed anyway and tagged, so the lane's agent hears him. Echo and refusals stand.
    addressed, reason, guest_speaker = guest.admit(addressed, reason, lane, state.guest_lanes)

    if not addressed:
        state.wake.mark_addressed(window_anchor)
    if addressed and not wake_said:
        # Identity granted this one, so identity has to extend the window too — otherwise the
        # conversation only ever continues from phrases, and short follow-ups (too brief for the
        # embedder to score) drop out mid-exchange. See WakeGate.mark_addressed.
        state.wake.mark_addressed(t_end)
    # A SWITCH ONLY LANDS ON AN ADDRESSED TURN. The wake gate can reject a greeting-led utterance
    # it heard from a confident stranger, and a stranger must not be able to move his conversation
    # to a different agent by saying a name out loud in the room.
    # A GUEST NEVER SWITCHES (spec 042): a guest saying an agent's name reaches the guest lane's
    # agent, it does not move the owner's conversation.
    moved = state.lanes.apply(routing) if (addressed and not guest_speaker) else False
    if moved:
        await _set_lane(state, state.lanes.current, why="wake")

    # A BARE SUMMONS — "hey rigel" and nothing else — moves him and asks nobody anything (JJ,
    # 2026-10-05: *"switching lanes with the wake word, but that not triggering the agent to
    # respond"*). So it is logged UNADDRESSED, which keeps it out of every agent's `watch` and out of
    # the unread count that would refuse their next `say`. Only the LOG entry: the window above was
    # already opened by it, so what he says next still reaches the lane he just moved to.
    summons_only = addressed and lanes_mod.is_bare_summons(text, state.lanes.names)

    turn = store.append_turn(
        session=state.session,
        text=agent_text,
        t_start=t_start,
        t_end=t_end,
        addressed=addressed and not summons_only,
        reason="summons" if summons_only else reason,
        # FR3: the turn carrying the wake word belongs to the NEW lane, instruction and all —
        # switching on turn N and routing on N+1 would lose the sentence he cared about.
        lane=lane,
        stamp_lane=True,
        speaker=guest_speaker,
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
            # The turn was just appended, so its id IS the log's last — no second read.
            "pending": state.pending_turns(int(turn.get("id", -1))),
        },
    )
    # NO ACKNOWLEDGEMENT CUE HERE ANY MORE (spec 007, FR3).
    #
    # `heard` was pushed on this line, at the end of the turn-logging path — the moment ASR
    # finished, **before any agent had seen the turn and whether or not one was even listening.**
    # So the sound asserted acknowledgement for EVERY utterance: room chatter the wake gate had
    # just rejected, turns nobody would read for twenty minutes, and turns an agent would
    # deliberately never answer.
    #
    # JJ, 2026-08-19: *"Whenever you hear something that you are not going to acknowledge, it
    # doesn't make sense to reproduce the sound as if you heard me, as if you are acknowledging
    # me."* The cue now follows the agent's intent to respond, on the read signal — see
    # `handle_consumed`. Its ABSENCE is information, which it could never be while it fired on
    # capture.
    #
    # WHAT THIS COSTS, stated rather than left to be discovered: he loses the immediate audible
    # confirmation that the tunnel heard him at all, and there is now silence between his last
    # word and the agent deciding. The page still paints the turn into the transcript the instant
    # it is logged, over the broadcast two lines above, so the confirmation survives VISUALLY —
    # but on a phone in a pocket it does not. A quieter capture tick, distinct from the
    # acknowledgement, would buy the liveness signal back at the cost of a fifth sound in a
    # four-sound vocabulary. That is his call to make, and the spec flags it rather than assuming.
    #
    # CLOSED ON THE LANE THAT OPENED IT (016 FR1). If the gate moved the conversation while ASR
    # ran, `opener` is the lane that was told `transcribing` and this is the only call that will
    # ever release it. The incoming lane needs nothing here — its own agent reports `thinking`
    # when it picks the turn up.
    await _set_agent_state(state, "idle", opener)


LAN_OPEN_PATHS = frozenset({"/pair/exchange"})
"""The only LAN route a request may reach without the key or the token: its one-time code is the
credential (spec 031), and `_check`-free handlers still enforce the Wi-Fi subnet."""


@web.middleware
async def _lan_gate(request: web.Request, handler):
    """NOTHING ANONYMOUS ON THE HOME WI-FI (spec 034 FR2). On the tunnel a browser sign-in stood in
    front of every route, so the pages and the canvas were left open on loopback. The LAN has nothing
    in front of it: every request that arrives there needs a subnet peer AND the device key or the
    token, before any handler runs. The loopback listener is untouched."""
    if not _on_lan(request) or request.path in LAN_OPEN_PATHS:
        return await handler(request)
    ok, reason = _check(request, request.app["state"])
    if not ok:
        return web.json_response({"error": reason, "code": "lan_auth",
                                  "remedy": "pair the phone app, or open the page with ?token="},
                                 status=403)
    resp = await handler(request)
    # A browser page opened with ?token= gets the token as an HttpOnly cookie, so the frames and
    # requests it makes afterwards (which carry no query) are authenticated too.
    tok = request.query.get("token")
    if tok and isinstance(resp, web.Response) and not resp.prepared:
        resp.set_cookie(LAN_TOKEN_COOKIE, tok, httponly=True, samesite="Strict", path="/")
    return resp


def build_app(session: str, token: str | None, gate_enabled: bool = True,
              lan: dict[str, Any] | None = None) -> web.Application:
    app = web.Application(middlewares=[_lan_gate])
    # The home-Wi-Fi listener (spec 034): {ip, subnet, url}, or None when `serve` ran without --lan.
    app["lan"] = lan
    app["state"] = TunnelState(session, token, gate_enabled)
    app.add_routes(
        [
            web.get("/", handle_index),
            web.get("/ui", handle_ui),
            # Vendored JS the self-contained parent page needs (d3-force for the fleet layout, spec
            # 021). Local, not a CDN — the parent holds the audio and must not depend on the network;
            # unauthenticated like the page (the library is not secret, the token gates the socket).
            web.static("/vendor/", os.path.join(WEB_DIR, "vendor")),
            web.get("/meeting", handle_meeting),
            web.get("/health", handle_health),
            web.get("/android/command-bridge.apk", handle_android_apk),
            web.get("/pair", handle_pair_page),
            web.get("/notify/wait", handle_notify_wait),
            web.post("/liaison", handle_liaison),
            web.post("/guest", handle_guest),
            web.post("/pair/new", handle_pair_new),
            web.post("/pair/exchange", handle_pair_exchange),
            web.get("/status", handle_status),
            web.get("/history", handle_history),
            web.post("/say", handle_say),
            web.post("/shutdown", handle_shutdown),
            web.post("/consumed", handle_consumed),
            web.post("/watching", handle_watching),
            web.post("/brief", handle_brief),
            web.post("/briefs/take", handle_briefs_take),
            web.post("/idle/fired", handle_idle_fired),
            web.get("/fleet/state", handle_fleet_state),
            web.post("/cue", handle_cue),
            web.post("/rate", handle_rate),
            web.post("/verbose", handle_verbose),
            web.post("/wake", handle_wake),
            web.post("/lane", handle_lane),
            web.get("/ws", handle_ws),
        ]
    )
    # The canvas rides the same server (spec 003): /events (SSE), the canvas page, and the frame-op
    # routes. Additive only — the voice routes above are untouched. Routes are safe at app-build
    # time; the store/follower threads start on serve (init_canvas), not here, so tests stay inert.
    from .canvas import aio as canvas_aio
    canvas_aio.setup(app)
    # Objective fleet activity is pushed on a cadence while the server runs (spec 021). Created on
    # startup and cancelled on cleanup, so a test that only BUILDS the app (never serves it) stays
    # inert — the task starts with the event loop, not at build time.
    app.on_startup.append(_start_fleet_broadcaster)
    # Reload held clips a graceful restart left behind, so the agents' raised hands survive it (JJ,
    # 2026-09-24). Inert in tests: no leftover file, no-op.
    app.on_startup.append(_restore_held_on_startup)
    app.on_cleanup.append(_stop_fleet_broadcaster)
    return app


def run(
    session: str = "dev",
    host: str = config.DEFAULT_HOST,
    port: int = config.DEFAULT_PORT,
    token: str | None = None,
    gate_enabled: bool = True,
    lan: dict[str, Any] | None = None,
) -> None:
    store.validate_session(session)
    if token is None:
        token = os.environ.get("COMMAND_BRIDGE_TOKEN") or security.generate_token()

    is_loopback = security.ip_in_cidrs(host, config.LOOPBACK_CIDRS) or host in ("localhost",)
    if not is_loopback and not token:
        raise SystemExit(
            "refusing to bind a non-loopback interface without a token; set COMMAND_BRIDGE_TOKEN"
        )

    if lan and not token:
        raise SystemExit("refusing to listen on the home Wi-Fi without a token")
    app = build_app(session, token, gate_enabled, lan)

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
    print(f"command-bridge serving   http://{host}:{port}/?token={token}", flush=True)
    # GIVE THIS TO THE HUMAN, WHOLE (JJ 2026-09-07). The line above is not just a status message —
    # it is the one thing the operator needs to open the page, and the `?token=` is load-bearing:
    # the page loads without it but cannot connect the microphone, so a URL handed over without it
    # looks like the tool is broken. An agent that restarts DETACHED will not see this banner and
    # must read the same URL back from `command-bridge doctor` / `status.url` instead.
    print("  ^ GIVE THIS URL to whoever will talk — including the ?token=, which the mic needs",
          flush=True)
    print(f"  session            {session}", flush=True)
    # The re-entry reality, stated where an agent reads it after starting the tunnel. Everything
    # here makes staying in `watch` the easy path, and a blocking watch — re-invoked by the harness
    # when a long one is backgrounded — is what re-enters you. The failure this project keeps
    # hitting is a turn that ENDS ON PROSE: answer in prose, end the turn without re-arming, and the
    # person on the phone is talking to nobody. (The watchdog job that used to guard this was
    # retired 2026-09-17.)
    print("  re-arm             end every turn in `watch` — FOREGROUND by default (it blocks and "
          "returns when he speaks); background it only while you have OTHER work or he is away", flush=True)
    print(f"  log                {store.log_path(session)}", flush=True)
    # The banner is what the operator reads before speaking, so it has to say the phrase. Without
    # this line the one thing you must know to use the tool at all was the one thing it never
    # told you — and a name set on a previous run persists, so it is not safely guessable either.
    if gate_enabled:
        print(
            f'  say                "hey {config.wake_name()}"  '
            f"(`command-bridge serve --wake <your name>` to change it)",
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
              f"(`command-bridge download turn` to use prosody instead)", flush=True)
    else:
        print("  turns end on       smart-turn — when you sound finished, not on a timer",
              flush=True)
    print(f"  tts                {tts.available()}", flush=True)
    print(
        f"  voice              speed {config.speech_speed()}x, "
        f"{config.sentence_pause()}s between sentences  (`command-bridge rate` to change and persist)",
        flush=True,
    )
    print(f"  allowlist          {', '.join(security.allowed_cidrs())}", flush=True)
    # THE WAY OUT, on the banner that starts it. This told you how to rename the assistant and how
    # to change the speech rate, and not how to shut the thing down — an audit had to find `stop`
    # by reading the whole `describe` payload. The command that starts a background process should
    # say how it ends, in the same breath.
    print(f"  stop it with       command-bridge stop --session {session}", flush=True)
    print(
        "  (a phone needs HTTPS: `tailscale serve` this port — a LAN IP will NOT work)",
        flush=True,
    )
    # Bring the canvas state up on the same server: load the persisted canvas, start its writer and
    # the voice-lane follower (spec 003). Done here, not in build_app, so it only runs on a real serve.
    from .canvas import aio as canvas_aio
    # follow=False: the canvas no longer POLLS /status for the live lane (spec 004). The voice
    # server drives it in-process from `_set_lane`, so the cross-process Follower would be a second,
    # laggier writer of the same value. Its code stays for the standalone canvas.
    restored = canvas_aio.init_canvas(session, follow=False)
    # Reconcile the default lane (spec 004): a fresh canvas starts on its own default ('main') while a
    # fresh voice session starts on the wake/default lane. Left alone they disagree until the first
    # switch, so an agent that draws to the live lane before anyone switches draws onto a lane the
    # canvas is not showing. Sync the canvas to the voice live lane once, here, where both are known.
    from .canvas import server as _canvas
    _canvas.set_live(app["state"].lanes.current)
    # Liaison mode survives a restart (spec 037): the canvas learns it before any edit arrives.
    _canvas.set_liaisons(app["state"].liaisons)
    print(f"  canvas             {restored} frame(s) restored, on this same server", flush=True)
    if lan:
        # ALSO the Wi-Fi address and only that one (spec 034 FR1): never 0.0.0.0, never the
        # corporate Ethernet or a VPN adapter. Same app, so the LAN gate sees every request.
        print(f"  home wi-fi         {lan['url']}  (key or token required on every route; "
              f"peers in {lan['subnet']} only)", flush=True)
        web.run_app(app, host=[host, lan["ip"]], port=port, print=None)
    else:
        web.run_app(app, host=host, port=port, print=None)
