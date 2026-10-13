package dev.commandbridge.companion

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Spec 044 AC7: the app's half of the primitive protocol — parse, ignore the unknown, say what it can do. */
class PrimitivesTest {
    private fun j(s: String) = JSONObject(s)

    @Test fun theHelloListsTheProtocolAndOps() {
        val h = Primitives.hello("0.8.0")
        assertEquals(1, h.getInt("protocol"))
        assertEquals("0.8.0", h.getString("app"))
        val ops = (0 until h.getJSONArray("ops").length()).map { h.getJSONArray("ops").getString(it) }
        assertEquals(listOf("audio", "notify", "notify_cancel", "awake"), ops)
    }

    @Test fun drivenOnlyWhenTheAckSaysSo() {
        assertTrue(Primitives.driven(j("""{"type":"hello_ack","native":{"protocol":1}}""")))
        assertFalse(Primitives.driven(j("""{"type":"hello_ack","server_sample_rate":16000}""")))
        assertFalse(Primitives.driven(j("""{"type":"hello_ack","native":{"protocol":0}}""")))
    }

    @Test fun theOrbOffDirectiveParses() {                       // the spec 043 fix, server-side
        assertEquals(Primitives.Directive.Audio(mic = false, call = false, device = null),
            Primitives.parse(j("""{"type":"native","op":"audio","mic":false,"call":false,"device":null}""")))
        assertEquals(Primitives.Directive.Audio(mic = true, call = true, device = 7),
            Primitives.parse(j("""{"type":"native","op":"audio","mic":true,"call":true,"device":7}""")))
    }

    @Test fun heardYouParsesAsAQuietNotification() {             // the spec 040 feature, server-side
        val d = Primitives.parse(j("""{"type":"native","op":"notify","key":"heard","style":"quiet",
            "title":"Heard you → Magnus","text":"turn the oven off"}"""))
        assertEquals(Primitives.Directive.Notify("heard", true, "Heard you → Magnus", "turn the oven off"), d)
        assertEquals(4, Primitives.notificationId("heard"))       // the id 0.7.0 used: replaces, never stacks
        assertEquals(2, Primitives.notificationId("speak"))
    }

    @Test fun anUnknownOpOrANonNativeMessageIsIgnored() {
        assertNull(Primitives.parse(j("""{"type":"native","op":"vibrate"}""")))
        assertNull(Primitives.parse(j("""{"type":"turn","text":"x"}""")))
        assertNull(Primitives.parse(j("""{"type":"native","op":"notify","key":""}""")))
    }

    @Test fun awakeAndCancelParse() {
        assertEquals(Primitives.Directive.Awake(true), Primitives.parse(j("""{"type":"native","op":"awake","on":true}""")))
        assertEquals(Primitives.Directive.NotifyCancel("speak"),
            Primitives.parse(j("""{"type":"native","op":"notify_cancel","key":"speak"}""")))
    }

    @Test fun theReportIsWhatTheServerReads() {
        val r = Primitives.Report(false, listOf(2, 7), mic = true, call = true, device = 7, micError = null).toJson()
        assertEquals("native_state", r.getString("type"))
        assertEquals(7, r.getInt("device"))
        assertTrue(r.isNull("mic_error"))
        // Equal reports compare equal: the service sends only on a change.
        assertEquals(Primitives.Report(true, listOf(2), false, false, null, null),
            Primitives.Report(true, listOf(2), false, false, null, null))
    }

    @Test fun aSplitDirectiveReleasesBeforeItAcquires() {
        // Mic off but call kept: only the mic goes.
        assertEquals(listOf(CallHold.Step.RELEASE_MIC),
            CallHold.plan(wantMic = false, wantCall = true, micHeld = true, routed = true))
        assertEquals(listOf(CallHold.Step.ROUTE, CallHold.Step.OPEN_MIC),
            CallHold.plan(wantMic = true, wantCall = true, micHeld = false, routed = false))
    }
}
