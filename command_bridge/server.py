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
from . import config, cues, security, speech, store, timing, tts, turndetect, voiceprint
from . import lanes as lanes_mod
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
        for _name in store.read_lanes(session):
            if _name != self.lanes.default:
                try:
                    self.lanes.add(_name)
                except lanes_mod.LaneError:
                    pass       # a stale or now-invalid name must not stop the server starting
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
        running, and each firing starts another one.

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

        self.lane_consumed: dict[str, int] = {}
        """How far EACH lane has actually read (spec 015 FR1).

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
        own snapshot calls its impostor and the watchdog prompt says never to use: it counts what
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
        speech = s["speech_active"] or s["speech_pending"]
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
            # watchdog took it for the cursor: 26 against a real last id of 367, which would have
            # replayed three hundred old turns as if they had just been spoken. The two are only
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
            "watch_open": self.watch_open,
            "watching_lanes": sorted(self.watching_lanes),
            "lane_waiting": {name: len(clips) for name, clips in self.lane_held.items() if clips},
            "lane_states": dict(self.lane_states),
            "lane_unanswered": self.lane_unanswered(),
            "lane_read_through": dict(self.lane_read_through),
            "lane_consumed": dict(self.lane_consumed),
            "ambiguous": self.ambiguous,
            "last_ambiguous": list(self.last_ambiguous),
            "agent_holds_turns": self.agent_holds_turns,
            # THE SAME QUESTION PER LANE (spec 018 FR1). `agent_holds_turns` above still answers
            # "is any agent here holding turns", unchanged for every caller that predates lanes;
            # this is the fan-out a lane reads to decide whether ITS next wait is a pre-reply
            # check. Published rather than inferred — the CLI cannot see the other lanes.
            "lane_holds_turns": dict(self.lane_holds_turns),
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
         "waiting": {name: len(clips) for name, clips in state.lane_held.items() if clips}},
    )
    # HIS ATTENTION IS BACK HERE, so what this lane said while he was away stops being an
    # interruption and becomes an answer. Best-effort: a failure to play a held clip must never
    # be able to prevent the switch itself, or a broken clip would trap the conversation.
    try:
        await _flush_lane_held(state, lane)
    except Exception as exc:
        state.fail("transport", f"flush lane hold failed: {exc}")


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
        return web.Response(status=500, text="command_bridge/web/index.html missing")
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
    turns = turns[-config.UNREAD_ON_SAY_MAX:]
    return {"unread": turns, "unread_count": len(turns), "cursor": last_id, "since": since}


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
        return web.json_response(_unread_refusal(state, unread, lane), status=428)

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

    if fire_and_forget:
        # Return before synthesis so the agent can acknowledge and keep working in parallel,
        # instead of the user waiting out a TTS round trip before anything else starts.
        # The lane travels on the fire-and-forget path too. `--now` is exactly the path an
        # agent takes when it is in a hurry, which is when it would otherwise talk over him.
        asyncio.get_running_loop().create_task(_speak(state, text, voice, lane=lane, intent=intent))
        return web.json_response({"queued": True, "async": True, **unread})

    try:
        result = await _speak(state, text, voice, lane=lane, timings=timings, intent=intent)
    except tts.TTSError as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response({**result, **unread})


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


async def _speak(state: TunnelState, text: str, voice: str | None,
                 lane: str | None = None, timings: bool = False,
                 intent: bool = False) -> dict[str, Any]:
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

        pcm, rate, schedule = await asyncio.get_running_loop().run_in_executor(None, _synth)
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

    timing.stamp(state.session, "synthesized", audio_s=round(len(pcm) / 2 / rate, 2),
                 lane=owner)
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
    while waited < 15.0 and not state.muted:
        if state.talking(amplitude=False):
            if not announced:
                await _set_agent_state(state, "waiting", owner)
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
            if state.talking(amplitude=False):
                break
        if not state.talking(amplitude=False):
            break
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
    if deliverable:
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
        state.lane_inflight.setdefault(owner, []).append(state.hand_to_client(
            {"header": dict(header), "pcm": pcm, "at": time.time()}))
        await _push_cue(state, "speaking")
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
                                      "waiting": len(state.lane_held.get(str(lane), []))})
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
    return {
        "queued": True,
        "id": clip_id,
        "seconds": round(len(pcm) / 2 / rate, 2),
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
        # SPEC 012: how many of this agent's still-held INTENT announcements this clip superseded (0
        # when none, or when it played live). Lets the agent see its "about to" was dropped as stale.
        "superseded": header.get("superseded", 0),
        "lane": lane,
        # Say WHICH kind of not-delivered, so the agent can tell "his phone dropped" from "he
        # closed the channel deliberately" without a second call.
        "reason": None if deliverable else (
            "off_lane" if off_lane else ("channel_closed" if state.clients else "no_client")
        ),
    }


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
        # The hand comes down on OPTIMISM, and that is deliberate: he has switched to this lane
        # and the audio is on its way, so leaving it up would be the page telling him something is
        # still waiting while it plays. If the clips come back (barge-in), so does the hand.
        await _broadcast_json(state, {"type": "lane_waiting", "lane": lane, "waiting": 0})
    return len(queued)


def _clip_played(state: TunnelState, clip_id: str) -> None:
    """A clip reached his ears, so the server can finally let go of its copy (spec 022 FR2).

    Matched by id rather than popped positionally: clips can be acknowledged out of order when a
    lane switch interleaves two flushes, and dropping the wrong one would keep a played clip
    forever while discarding an unplayed one.
    """
    for lane, clips in list(state.lane_inflight.items()):
        kept = [c for c in clips if (c.get("header") or {}).get("id") != clip_id]
        if len(kept) == len(clips):
            continue
        if kept:
            state.lane_inflight[lane] = kept
        else:
            state.lane_inflight.pop(lane, None)
        return


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
                                      "waiting": len(state.lane_held.get(lane, []))})
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
    """
    _prune_undelivered(state)
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
                                      "waiting": len(state.lane_held.get(lane, []))})
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

    if action in ("add", "remove"):
        # The set changed even though the live lane may not have. Publish it, or the page keeps
        # offering a lane that is gone.
        await _set_lane(state, state.lanes.current, why=action)
    return web.json_response({
        "lane": state.lanes.current,
        "lanes": list(state.lanes.names),
        "default_lane": state.lanes.default,
        "broadcast": lanes_mod.BROADCAST,
        "watching_lanes": sorted(state.watching_lanes),
    })


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
    return web.json_response({"agent_state": state.agent_state, "verbose": state.verbose,
                              "lane": lane, "watching_lanes": sorted(state.watching_lanes),
                              **({"expired": expired} if expired else {})})


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
    store.write_consumed_cursor(state.session, cursor)
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
    # `verbose` rides on the ready frame so the client ADOPTS it rather than pushing its own.
    # `muted` deliberately does NOT — that is a fact about this device's microphone, and a phone
    # muting a desktop in another room would be wrong.
    # The wake name travels too, so the page's prompt cannot drift from what the gate accepts.
    # Hardcoding "hey claude" in the copy is how a renamed assistant keeps telling people to say
    # the old name.
    await ws.send_json(
        {"type": "ready", "session": state.session, "verbose": state.verbose,
         "wake": config.wake_name(), "wake_phrases": list(state.wake.phrases),
         # THE WHOLE LANE PICTURE ON CONNECT. A page that learned about lanes only from the next
         # change event would show an empty room until somebody moved — and a phone reconnects
         # every time it locks, so that is the common case rather than the edge one.
         "lane": state.lanes.current, "lanes": list(state.lanes.names),
         "broadcast": lanes_mod.BROADCAST,
         "lane_states": dict(state.lane_states),
         # A page that reconnects must be able to tell "listening" from "working" immediately
         # (015 FR2), not only after the next state change — a phone reconnects every time it
         # locks, so that is the common case rather than the edge one.
         "watching_lanes": sorted(state.watching_lanes),
         "lane_consumed": dict(state.lane_consumed),
         "default_lane": state.lanes.default,
         "waiting": {n: len(c) for n, c in state.lane_held.items() if c}}
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
        await _drop_client(state, ws)
        # Flush whatever was mid-sentence when the socket dropped, so a disconnect never
        # silently eats the last thing that was said.
        try:
            await _flush(state, loop)
        except Exception as exc:
            state.fail("transport", f"flush failed: {exc}")
        state.close_capture()
    return ws


async def _drop_client(state: TunnelState, ws) -> None:
    """Forget a page, and everything that was only true because it existed.

    A named function rather than a block inside a `finally`, because what happens when the last
    page dies is now load-bearing enough to need a test — and a test that re-implemented this
    block would prove nothing about the server.
    """
    state.clients.discard(ws)
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
        await ws.send_json({"type": "hello_ack", "server_sample_rate": config.TARGET_SR})
    elif kind == "played":
        state.last_played = str(msg.get("id") or "")
        # THE RECEIPT IS WHAT LETS THE SERVER FORGET IT (spec 022 FR2). Until this arrives the
        # clip is still the server's problem, because everything between here and his ear can
        # still drop it.
        _clip_played(state, state.last_played)
        # 🔴 THE LANE THIS CLIP ACTUALLY BELONGED TO (spec 024), looked up by its own id rather
        # than read from a slot the next clip may already have overwritten.
        owner = state.clip_owner.pop(state.last_played, None) \
            or state.speaking_lane or state.lanes.current
        timing.stamp(state.session, "played", clip=state.last_played, lane=owner)
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

    # 🔴 WHICH LANE HE STARTED SPEAKING TO (spec 023). Latched on the silence→speech edge and
    # held until the turn is routed, because everything between those two moments takes seconds —
    # ASR, the voiceprint, the turn model — and he can move the conversation inside that window.
    # Reading the live lane at the END of that pipeline delivers his words to whoever he switched
    # TO, which is an agent he was never talking to and cannot un-hear them.
    speaking_before = bool(state.buffer.speech_active)
    completed = state.buffer.feed(samples)
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
        _maybe_partial(state, loop)
        return
    # RAISED BEFORE THE FIRST AWAIT, and that ordering is the whole point. Between the buffer
    # emptying and the turn reaching the log, both speech signals read false while he has in fact
    # just spoken — and `_emit` awaits the recognizer, so `/status` IS served inside that window.
    # Counting from here closes it. See TunnelState.speech_pending.
    state.speech_pending += 1
    try:
        await _emit(state, completed, loop)
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
        # Same accounting as the ingest path: a flush is a close, and its turn is pending until
        # it is in the log. This is the path a mute, an orb tap or a dropped socket takes, which
        # is exactly when an agent is most likely to be asking whether he has finished.
        state.speech_pending += 1
        try:
            await _emit(state, completed, loop)
        finally:
            state.speech_pending = max(0, state.speech_pending - 1)


async def _emit(state: TunnelState, completed, loop: asyncio.AbstractEventLoop) -> None:
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
    # ASR is CPU-bound; keep it off the event loop or audio ingest stalls.
    text = await loop.run_in_executor(None, state.recognizer.transcribe, samples)
    timing.stamp(state.session, "transcribed", chars=len(text or ""))
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
    moved = state.lanes.apply(routing) if addressed else False
    if moved:
        await _set_lane(state, state.lanes.current, why="wake")

    turn = store.append_turn(
        session=state.session,
        text=agent_text,
        t_start=t_start,
        t_end=t_end,
        addressed=addressed,
        reason=reason,
        # FR3: the turn carrying the wake word belongs to the NEW lane, instruction and all —
        # switching on turn N and routing on N+1 would lose the sentence he cared about.
        lane=lane,
        stamp_lane=True,
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


def build_app(session: str, token: str | None, gate_enabled: bool = True) -> web.Application:
    app = web.Application()
    app["state"] = TunnelState(session, token, gate_enabled)
    app.add_routes(
        [
            web.get("/", handle_index),
            web.get("/meeting", handle_meeting),
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
            web.post("/lane", handle_lane),
            web.get("/ws", handle_ws),
        ]
    )
    # The canvas rides the same server (spec 003): /events (SSE), the canvas page, and the frame-op
    # routes. Additive only — the voice routes above are untouched. Routes are safe at app-build
    # time; the store/follower threads start on serve (init_canvas), not here, so tests stay inert.
    from .canvas import aio as canvas_aio
    canvas_aio.setup(app)
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
        token = os.environ.get("COMMAND_BRIDGE_TOKEN") or security.generate_token()

    is_loopback = security.ip_in_cidrs(host, config.LOOPBACK_CIDRS) or host in ("localhost",)
    if not is_loopback and not token:
        raise SystemExit(
            "refusing to bind a non-loopback interface without a token; set COMMAND_BRIDGE_TOKEN"
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
    print(f"command-bridge serving   http://{host}:{port}/?token={token}", flush=True)
    # GIVE THIS TO THE HUMAN, WHOLE (JJ 2026-09-07). The line above is not just a status message —
    # it is the one thing the operator needs to open the page, and the `?token=` is load-bearing:
    # the page loads without it but cannot connect the microphone, so a URL handed over without it
    # looks like the tool is broken. An agent that restarts DETACHED will not see this banner and
    # must read the same URL back from `command-bridge doctor` / `status.url` instead.
    print("  ^ GIVE THIS URL to whoever will talk — including the ?token=, which the mic needs",
          flush=True)
    print(f"  session            {session}", flush=True)
    # The FIRST thing an agent reads after starting the tunnel, because it is the one instruction
    # this CLI cannot enforce on its own. Everything else here makes staying in `watch` the easy
    # path; nothing makes an agent RE-ENTER once it has fallen out, and that is the failure this
    # project keeps hitting — five times in one session, always the same shape: answer in prose,
    # end the turn, and the person on the phone is talking to nobody.
    print("  watchdog           register a ~1/min job in YOUR harness that re-arms `watch` "
          "(see `command-bridge describe` -> watchdog)", flush=True)
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
    print(f"  canvas             {restored} frame(s) restored, on this same server", flush=True)
    web.run_app(app, host=host, port=port, print=None)
