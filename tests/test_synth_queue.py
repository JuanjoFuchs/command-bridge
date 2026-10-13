"""Spec: the TTS synthesis queue (JJ, 2026-09-22, dev.jsonl turn 6259).

Only ONE synthesis runs at a time, and when several are waiting the LIVE lane's clip goes first,
regular before intent. Concurrent kokoro synthesis across six lanes was the measured CPU spike; this
gate is what serializes it. The gate is tested directly (not through the whole `_speak` path) so the
ordering guarantee is pinned without a resident TTS engine on disk.
"""
import asyncio
import threading

from command_bridge.server import _SynthGate


def test_one_at_a_time_and_priority_order():
    """While one synth holds the slot, four queue out of priority order; they must drain
    live-regular, live-intent, off-regular, off-intent — priority beating arrival order."""
    async def main():
        gate = _SynthGate()
        ran: list[str] = []
        started = threading.Event()
        release = threading.Event()

        def make(label: str, block: bool = False):
            def fn():
                if block:
                    started.set()
                    release.wait(3)
                ran.append(label)
                return label
            return fn

        # The first call takes the slot and blocks INSIDE the executor, so the gate is busy.
        first = asyncio.create_task(gate.run((0, 0), make("first", block=True)))
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 3)

        # Enqueue four while busy, deliberately WORST-priority first, so FIFO would get it wrong.
        waiters = [
            asyncio.create_task(gate.run((1, 1), make("off_intent"))),
            asyncio.create_task(gate.run((1, 0), make("off_regular"))),
            asyncio.create_task(gate.run((0, 1), make("live_intent"))),
            asyncio.create_task(gate.run((0, 0), make("live_regular"))),
        ]
        await asyncio.sleep(0.1)   # let all four reach the heap and park
        release.set()
        await asyncio.gather(first, *waiters)
        return ran

    ran = asyncio.run(main())
    assert ran[0] == "first", ran
    assert ran[1:] == ["live_regular", "live_intent", "off_regular", "off_intent"], ran


def test_ties_break_fifo():
    """Same priority → arrival order (the monotonic seq), so nothing starves within a priority."""
    async def main():
        gate = _SynthGate()
        ran: list[str] = []
        started = threading.Event()
        release = threading.Event()

        def make(label: str, block: bool = False):
            def fn():
                if block:
                    started.set()
                    release.wait(3)
                ran.append(label)
                return label
            return fn

        first = asyncio.create_task(gate.run((0, 0), make("first", block=True)))
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 3)
        waiters = [asyncio.create_task(gate.run((1, 0), make(f"r{i}"))) for i in range(4)]
        await asyncio.sleep(0.1)
        release.set()
        await asyncio.gather(first, *waiters)
        return ran

    ran = asyncio.run(main())
    assert ran == ["first", "r0", "r1", "r2", "r3"], ran


def test_uncontended_runs_immediately():
    """No queue → the call takes the slot directly and returns its result."""
    async def main():
        gate = _SynthGate()
        return await gate.run((0, 0), lambda: "ok")

    assert asyncio.run(main()) == "ok"
