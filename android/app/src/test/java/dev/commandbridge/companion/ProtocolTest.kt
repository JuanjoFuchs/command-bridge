package dev.commandbridge.companion

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class ProtocolTest {
    @Test fun httpsPageUrlBecomesWssSocket() {
        val e = Protocol.parse("https://abc.ngrok-free.app/?token=testtoken")!!
        assertEquals("wss://abc.ngrok-free.app/ws?token=testtoken", e.socketUrl)
        assertEquals("https://abc.ngrok-free.app/?token=testtoken&spectate=1", e.spectateUrl)
        assertEquals("testtoken", e.token)
    }

    @Test fun httpLocalUrlBecomesWsAndKeepsPort() {
        val e = Protocol.parse("  http://127.0.0.1:8765/?token=t1&x=2 ")!!
        assertEquals("ws://127.0.0.1:8765/ws?token=t1", e.socketUrl)
    }

    @Test fun missingTokenOrBadSchemeIsRejected() {
        assertNull(Protocol.parse("https://host/"))
        assertNull(Protocol.parse("https://host/?token="))
        assertNull(Protocol.parse("ftp://host/?token=t"))
        assertNull(Protocol.parse("not a url"))
    }

    @Test fun aPairLinkYieldsAnHttpsBaseAndTheCode() {         // spec 031 AC6
        val p = Protocol.parsePairLink(
            "commandbridge://pair?base=https%3A%2F%2Fabc.ngrok-free.app&code=Zx_9-k")!!
        assertEquals("https://abc.ngrok-free.app", p.base)
        assertEquals("Zx_9-k", p.code)
    }

    @Test fun aPairLinkWithAPlainHttpBaseOrNoCodeIsRefused() {
        assertNull(Protocol.parsePairLink("commandbridge://pair?base=http%3A%2F%2Fevil.example&code=c"))
        assertNull(Protocol.parsePairLink("commandbridge://pair?base=https%3A%2F%2Fa.app&code="))
        assertNull(Protocol.parsePairLink("commandbridge://pair?code=c"))
        assertNull(Protocol.parsePairLink("https://pair?base=https%3A%2F%2Fa.app&code=c"))
        assertNull(Protocol.parsePairLink("commandbridge://other?base=https%3A%2F%2Fa.app&code=c"))
    }

    @Test fun aPairLinkCarriesTheLanAddressBesideTheTunnel() {           // spec 034 AC4
        val both = Protocol.parsePairLink("commandbridge://pair?base=https%3A%2F%2Fa.app" +
            "&lan=http%3A%2F%2F192.168.1.20%3A8765&code=c")!!
        assertEquals("https://a.app", both.base)
        assertEquals("http://192.168.1.20:8765", both.lan)
        assertEquals("http://192.168.1.20:8765", both.exchangeBase)
        val lanOnly = Protocol.parsePairLink("commandbridge://pair?lan=http%3A%2F%2F10.0.0.4%3A8765&code=c")!!
        assertNull(lanOnly.base)
        assertEquals("http://10.0.0.4:8765", lanOnly.exchangeBase)
        val tunnelOnly = Protocol.parsePairLink("commandbridge://pair?base=https%3A%2F%2Fa.app&code=c")!!
        assertNull(tunnelOnly.lan)
        assertEquals("https://a.app", tunnelOnly.exchangeBase)
    }

    @Test fun aLanAddressMustBePrivateHttpWithAPort() {                    // spec 034 AC4
        for (bad in listOf("http%3A%2F%2F8.8.8.8%3A8765", "https%3A%2F%2F192.168.1.20%3A8765",
                           "http%3A%2F%2F192.168.1.20", "http%3A%2F%2Fevil.example%3A80",
                           "http%3A%2F%2F100.101.1.2%3A8765", "http%3A%2F%2F172.32.0.1%3A8765",
                           "http%3A%2F%2F192.168.01.1%3A8765")) {
            assertNull(bad, Protocol.parsePairLink("commandbridge://pair?base=https%3A%2F%2Fa.app&lan=$bad&code=c"))
        }
        assertNull(Protocol.parsePairLink("commandbridge://pair?code=c"))
        assertTrue(Protocol.isPrivateV4("172.16.0.1") && Protocol.isPrivateV4("172.31.255.255"))
    }

    @Test fun homeWhenItAnswersElseTheTunnel() {                          // spec 034 AC5
        val lan = "http://192.168.1.20:8765"; val tunnel = "https://a.app"
        assertEquals(lan to "home", Protocol.choosePath(lan, tunnel, lanAnswered = true))
        assertEquals(tunnel to "tunnel", Protocol.choosePath(lan, tunnel, lanAnswered = false))
        assertEquals(tunnel to "tunnel", Protocol.choosePath(null, tunnel, lanAnswered = false))
        assertEquals(lan to "home", Protocol.choosePath(lan, null, lanAnswered = false))
        assertNull(Protocol.choosePath(null, null, lanAnswered = false))
    }

    @Test fun aLanEndpointIsPlainWebSocket() {
        val e = Protocol.pairedEndpoint("http://192.168.1.20:8765", "KEY")
        assertEquals("ws://192.168.1.20:8765/ws", e.socketUrl)
    }

    @Test fun aPairedEndpointCarriesTheKeyNotAToken() {
        val e = Protocol.pairedEndpoint("https://abc.ngrok-free.app", "KEY")
        assertEquals("wss://abc.ngrok-free.app/ws", e.socketUrl)
        assertEquals("KEY", e.deviceKey)
        assertNull(e.token)
    }

    @Test fun theLoudspeakerNotTheEarpieceWhenNothingIsConnected() {   // spec 032 AC1
        val phoneOnly = listOf(AudioRoute.TYPE_BUILTIN_EARPIECE, AudioRoute.TYPE_BUILTIN_SPEAKER)
        assertEquals(AudioRoute.TYPE_BUILTIN_SPEAKER, AudioRoute.choose(phoneOnly))
        assertTrue(AudioRoute.speakerphone(phoneOnly))
    }

    @Test fun aHeadsetWinsOverTheSpeaker() {
        val withBt = listOf(AudioRoute.TYPE_BUILTIN_EARPIECE, AudioRoute.TYPE_BUILTIN_SPEAKER, AudioRoute.TYPE_BLUETOOTH_SCO)
        assertEquals(AudioRoute.TYPE_BLUETOOTH_SCO, AudioRoute.choose(withBt))
        assertFalse(AudioRoute.speakerphone(withBt))
        val wired = listOf(AudioRoute.TYPE_BUILTIN_SPEAKER, AudioRoute.TYPE_WIRED_HEADSET)
        assertEquals(AudioRoute.TYPE_WIRED_HEADSET, AudioRoute.choose(wired))
    }

    @Test fun aRememberedOutputWinsWhenPresent() {                       // spec 033 AC1
        val withBt = listOf(AudioRoute.TYPE_BUILTIN_EARPIECE, AudioRoute.TYPE_BUILTIN_SPEAKER, AudioRoute.TYPE_BLUETOOTH_SCO)
        assertEquals(AudioRoute.TYPE_BUILTIN_SPEAKER, AudioRoute.choose(withBt, AudioRoute.TYPE_BUILTIN_SPEAKER))
        assertEquals(AudioRoute.TYPE_BUILTIN_EARPIECE, AudioRoute.choose(withBt, AudioRoute.TYPE_BUILTIN_EARPIECE))
        assertTrue(AudioRoute.speakerphone(withBt, AudioRoute.TYPE_BUILTIN_SPEAKER))
    }

    @Test fun aRememberedOutputThatIsGoneFallsBackToTheRule() {
        val phoneOnly = listOf(AudioRoute.TYPE_BUILTIN_EARPIECE, AudioRoute.TYPE_BUILTIN_SPEAKER)
        assertEquals(AudioRoute.TYPE_BUILTIN_SPEAKER, AudioRoute.choose(phoneOnly, AudioRoute.TYPE_BLUETOOTH_SCO))
    }

    @Test fun choicesAreLabelledAndOnlyWhatIsPresent() {
        val c = AudioRoute.choices(listOf(AudioRoute.TYPE_BLUETOOTH_SCO, AudioRoute.TYPE_BUILTIN_SPEAKER, 99))
        assertEquals(listOf(AudioRoute.TYPE_BUILTIN_SPEAKER to "Speaker", AudioRoute.TYPE_BLUETOOTH_SCO to "Bluetooth"), c)
    }

    @Test fun theLevelIsOnThePageScaleAndClamps() {                      // spec 033 AC2
        assertEquals(0.5, Protocol.pageLevel(0.075), 1e-9)
        assertEquals(1.0, Protocol.pageLevel(0.9), 0.0)
        val shell = File("../../command_bridge/web/shell.html").readText()
        assertTrue(shell.contains("levelTarget = Math.min(1, r / 0.15);"))
    }

    @Test fun noticesParseInServerOrderWithTheirSeq() {                    // spec 036 AC4
        val (items, seq) = Notices.parse("""{"items":[
            {"id":2,"seq":2,"clip":"clip-2","lane":"rigel","text":"Build is green","color":"#7fae2e","resolved":false},
            {"id":1,"seq":3,"clip":"clip-1","lane":"magnus","text":"x","color":null,"resolved":true}],"seq":3}""")
        assertEquals(3, seq)
        assertEquals(listOf("clip-2", "clip-1"), items.map { it.clip })
        assertEquals("#7fae2e", items[0].color)
        assertNull(items[1].color)
        assertTrue(items[1].resolved)
        assertEquals(Pair(emptyList<Notices.Notice>(), 7), Notices.parse("""{"items":[],"seq":7}"""))
    }

    @Test fun noticeBackoffDoublesFromASecondToAMinute() {
        assertEquals(1_000L, Notices.nextBackoff(0))
        assertEquals(2_000L, Notices.nextBackoff(1_000))
        assertEquals(60_000L, Notices.nextBackoff(40_000))
    }

    @Test fun laneColoursAreOpaqueOrNothing() {
        assertEquals(0xFF7FAE2E.toInt(), Notices.argb("#7fae2e"))
        assertNull(Notices.argb("red"))
        assertNull(Notices.argb(null))
        assertEquals(1007, Notices.notificationId(7))
    }

    @Test fun heardYouOnlyForAFinalAddressedTurnWithTheAppBehind() {      // spec 040 AC1
        assertTrue(Notices.heardYou("turn", addressed = true, text = "Hey Magnus, go", appInFront = false))
        assertFalse(Notices.heardYou("partial", addressed = true, text = "Hey Mag", appInFront = false))
        assertFalse(Notices.heardYou("turn", addressed = false, text = "talking to someone else", appInFront = false))
        assertFalse(Notices.heardYou("turn", addressed = true, text = "  ", appInFront = false))
        assertFalse(Notices.heardYou("turn", addressed = true, text = "Hey Magnus", appInFront = true))
    }

    @Test fun pcmIsLittleEndian() {
        val bytes = Protocol.shortsToBytes(shortArrayOf(1, -1, 0x1234))
        assertArrayEquals(byteArrayOf(1, 0, -1, -1, 0x34, 0x12), bytes)
    }

    @Test fun rmsOnThePageScale() {
        assertEquals(0.0, Protocol.rms(ShortArray(10)), 1e-9)
        val full = ShortArray(10) { 16384 }   // 0.5 of full scale
        assertEquals(0.5, Protocol.rms(full), 1e-9)
    }

    @Test fun backoffDoublesAndCaps() {
        assertEquals(500L, Protocol.nextBackoff(0))
        assertEquals(1000L, Protocol.nextBackoff(500))
        assertEquals(8000L, Protocol.nextBackoff(8000))
    }

    @Test fun speakingNeedsMinimumRunThenHangover() {
        val d = SpeakingDetector()
        assertNull(d.update(0.1, 0))          // run starts
        assertNull(d.update(0.1, 100))        // 100 ms < 120 ms minimum
        assertEquals(true, d.update(0.1, 120))
        assertNull(d.update(0.0, 500))        // inside the 700 ms hangover
        assertEquals(false, d.update(0.0, 821))
    }

    @Test fun connectingNeverUnmutesTheSession() {
        val s = SessionFlags()
        assertTrue(s.onConnect().none { it.first == "muted" })
    }

    // --- spec 043: the orb off lets go of the call ------------------------------------------------

    @Test fun theCallIsWantedOnlyWhileRunningWithTheChannelOpen() {           // spec 043 AC1
        assertTrue(CallHold.wanted(running = true, channelOpen = true))
        assertFalse(CallHold.wanted(running = true, channelOpen = false))
        assertFalse(CallHold.wanted(running = false, channelOpen = true))
    }

    @Test fun orbOffReleasesTheMicThenTheRoute() {                            // spec 043 AC1
        assertEquals(listOf(CallHold.Step.RELEASE_MIC, CallHold.Step.UNROUTE),
            CallHold.plan(want = false, micHeld = true, routed = true))
    }

    @Test fun orbOnRoutesThenOpensTheMicLikeTheStartPath() {                  // spec 043 AC1
        assertEquals(listOf(CallHold.Step.ROUTE, CallHold.Step.OPEN_MIC),
            CallHold.plan(want = true, micHeld = false, routed = false))
    }

    @Test fun applyingThePlanTwiceIsANoOp() {                                 // spec 043 AC2
        assertTrue(CallHold.plan(want = true, micHeld = true, routed = true).isEmpty())
        assertTrue(CallHold.plan(want = false, micHeld = false, routed = false).isEmpty())
    }

    @Test fun quickTogglesConvergeOnTheLastState() {                          // spec 043 AC2
        // Simulate the service applying each plan in turn, as syncAudio does on the main thread.
        var mic = true; var routed = true
        fun apply(open: Boolean) {
            for (s in CallHold.plan(CallHold.wanted(true, open), mic, routed)) when (s) {
                CallHold.Step.ROUTE -> { assertFalse(routed); routed = true }
                CallHold.Step.OPEN_MIC -> { assertFalse(mic); mic = true }
                CallHold.Step.RELEASE_MIC -> { assertTrue(mic); mic = false }
                CallHold.Step.UNROUTE -> { assertTrue(routed); routed = false }
            }
        }
        for (open in listOf(false, true, false, false, true, true, false)) apply(open)
        assertFalse(mic); assertFalse(routed)
        apply(true)
        assertTrue(mic); assertTrue(routed)
    }

    @Test fun aPartlyHeldCallIsFinishedEitherWay() {                          // spec 043 AC1
        // A mic that failed to open while routed: OFF still unroutes, ON retries only the mic.
        assertEquals(listOf(CallHold.Step.UNROUTE), CallHold.plan(want = false, micHeld = false, routed = true))
        assertEquals(listOf(CallHold.Step.OPEN_MIC), CallHold.plan(want = true, micHeld = false, routed = true))
    }

    @Test fun reconnectingWithTheOrbOffClaimsNoCapture() {                    // spec 043 AC3
        val s = SessionFlags()
        assertEquals(true, s.onConnect().toMap()["capturing"])
        s.apply("channel", false)
        assertEquals(false, s.onConnect().toMap()["capturing"])
        assertEquals(false, s.onConnect().toMap()["channel"])
    }

    @Test fun aServerMuteStopsFramesLeavingThePhone() {
        val s = SessionFlags()
        assertTrue(s.shouldSend(connected = true))
        s.apply("muted", true)
        assertFalse(s.shouldSend(connected = true))
        s.apply("muted", false)
        s.apply("channel", false)
        assertFalse(s.shouldSend(connected = true))
        assertFalse(SessionFlags().shouldSend(connected = false))
    }

    /** Review NICE 10: the constants are the page's; read the page so a change to one fails here. */
    @Test fun constantsMatchThePage() {
        val shell = File("../../command_bridge/web/shell.html").readText()
        fun num(name: String) = Regex("""\b$name = ([0-9.]+)""").find(shell)!!.groupValues[1].toDouble()
        assertEquals(num("SPEAKING_RMS"), Protocol.SPEAKING_RMS, 1e-12)
        assertEquals(num("SPEAKING_HANGOVER_MS"), Protocol.SPEAKING_HANGOVER_MS.toDouble(), 0.0)
        assertEquals(num("SPEAKING_MIN_RUN_MS"), Protocol.SPEAKING_MIN_RUN_MS.toDouble(), 0.0)
        assertEquals(num("RECONNECT_MIN"), Protocol.RECONNECT_MIN_MS.toDouble(), 0.0)
        assertEquals(num("RECONNECT_MAX"), Protocol.RECONNECT_MAX_MS.toDouble(), 0.0)
    }

    @Test fun aBriefBlipNeverAsserts() {
        val d = SpeakingDetector()
        d.update(0.1, 0)
        d.update(0.0, 50)                     // run broken before the minimum
        assertNull(d.update(0.1, 100))        // a new run starts from here
        assertNull(d.update(0.1, 200))
        assertEquals(true, d.update(0.1, 220))
    }
}
