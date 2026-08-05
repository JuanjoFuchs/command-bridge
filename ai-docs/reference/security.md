---
title: Security model
description: The trust boundary, why it sits on the WebSocket handshake rather than HTTP middleware, and the two mistakes borrowed from VoiceMode's CVE so we don't repeat them.
applies_to: voice_tunnel/security.py, voice_tunnel/server.py
read_before: changing auth, adding an endpoint, or exposing the server beyond loopback
---

# Security model

This server can **turn on a microphone**. Treat every exposure decision as if the failure mode
is "a stranger listens to the room", because it is.

## Two layers, both mandatory

1. **Network layer — who can reach the socket.** An IP allowlist over CIDRs. Loopback always
   allowed; anything else opt-in via `VOICE_TUNNEL_ALLOW_CIDRS`. For the phone that means the Tailscale
   CGNAT range `100.64.0.0/10`, which is only routable inside your tailnet.
2. **Application layer — who can open a session.** A shared token, compared in constant time,
   required on the WebSocket handshake.

Neither is sufficient alone. The allowlist is coarse (any device on the tailnet passes) and the
token is a bearer secret (it leaks if the URL leaks).

## The allowlist decides on the DIRECT TCP PEER

`X-Forwarded-For` is attacker-controllable. It is honored **only** when the direct peer is
itself inside `VOICE_TUNNEL_TRUSTED_PROXIES`, which defaults to empty — meaning the header is ignored
entirely and the peer address decides.

When a trusted proxy *is* configured, walk the forwarded chain **right to left**, skipping hops
that are themselves trusted; the first untrusted address is the real client. The leftmost
entries are the ones an attacker writes.

> **Why this is spelled out.** `mbailey/voicemode` shipped the naive version and it became
> [GHSA-2qvv-vjq9-g5r4](https://github.com/mbailey/voicemode/security/advisories/GHSA-2qvv-vjq9-g5r4),
> CVSS 8.6: sending `X-Forwarded-For: 127.0.0.1` bypassed the allowlist and reached every
> endpoint **including microphone recording and transcription**. We are re-implementing their
> fixed rule, not rediscovering their bug.

## Auth lives on the WebSocket handshake, NOT in HTTP middleware

The audio channel is a WebSocket. ASGI/HTTP middleware conventionally short-circuits on
`scope["type"] != "http"` and passes WebSocket and lifespan scopes straight through — which
means a middleware-only design leaves **the microphone channel completely unauthenticated**
while looking secure.

VoiceMode's `IPAllowlistMiddleware` and `TokenAuthMiddleware` both open with exactly that
early-return. It may be harmless in their deployment; it would be a hole in ours.

**Rule: every WebSocket connection re-checks both the peer CIDR and the token before the first
audio frame is read.** No exceptions, no "the HTTP layer already did it."

## Token comparison

Use `hmac.compare_digest`. A plain `!=` on a secret is timing-attack shaped. This is cheap to
get right and embarrassing to get wrong.

## What is deliberately NOT here

- No user accounts, no OAuth, no roles. One operator, one machine.
- No public exposure path. There is no Funnel/ngrok mode and adding one is a design change.
- No transcript egress. Turns are written to a local file and nowhere else.
