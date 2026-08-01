# HermesProxy + TBC Classic Anniversary (2.5.6, build 68941): Connect Handshake Investigation

## Summary

TBC Classic Anniversary client, version 2.5.6, build **68941**, initially failed to authenticate through HermesProxy (Xian55 fork, .NET 10) against a CMaNGOS-TBC (2.4.3/8606) legacy backend. The client completed the Battle.net TLS handshake and the initial `ConnectionService.Connect` RPC call, then **voluntarily disconnected** before ever attempting `AuthenticationService.Logon`, every single time, in under 200ms.

**Root cause identified and fixed:** HermesProxy's hand-ported `ConnectResponse` protobuf message was missing the `ciid` (client-instance-id) field entirely — not merely unset, but structurally absent from the vendored C# class, because it was generated against an older version of Blizzard's `bgs-sdk` schema than what the 2.5.6 client expects. Adding this field (confirmed via TrinityCore's actively-maintained source as `optional string ciid = 9;`) and generating a synthetic client identity when the client's own request omits one (which it does, for this build) **eliminated the instant disconnect.**

**Current state:** the client now proceeds past `Connect` and hangs for a fixed ~45 seconds before failing with a *different*, more generic error (`BLZ51903006` / `Glue Fatal Error: 3006`), with zero further communication attempted with HermesProxy during that wait. This points to a **separate, downstream problem**: the client attempting to verify itself against real Blizzard CDN/version infrastructure over the actual internet (because Arctium's custom CDN redirect has no data for this brand-new build), which cannot succeed and times out on its own. This is very likely an **Arctium-side gap**, not a HermesProxy problem, and is the next phase of investigation.

## Environment

- **HermesProxy**: Xian55 fork, .NET 10, `V2_5_6_68941` registered in `ClientVersionBuild.cs`/`VersionChecker.cs`.
- **Legacy backend**: CMaNGOS-TBC, protocol 2.4.3/build 8606, fully operational and independently validated.
- **Client**: TBC Classic Anniversary, version 2.5.6, build 68941.
- **Launcher**: Arctium Game Launcher (full-featured, closed-source), `--staticseed --version=Classic --dev`.
- **Topology**: HermesProxy + CMaNGOS-TBC on host PC (static LAN IP `192.168.0.6`), WoW client in a NAT-mode VM. `Config.wtf` portal and HermesProxy's `ProxyNetworkOptions.ExternalAddress` both set to the host's LAN IP.

## Investigation Timeline & Elimination Chain

### Phase 1 — Ruling out external causes of the instant disconnect

1. **TLS certificate rejection — ruled out.** `--dev` flag made no difference; the client completes a full RPC round-trip after TLS, which a cert-level rejection would prevent entirely.
2. **Silently-dropped packets — ruled out.** Unconditional raw-packet-header logging added to `BnetTcpSession.cs`'s dispatch loop showed exactly two packets per attempt (`Connect`, then `RequestDisconnect`) — nothing missed, nothing dropped.
3. **Server-side rejection — ruled out.** Logging added to `HandleRequestDisconnect` showed `ErrorCode = 0` — a clean, voluntary, client-initiated disconnect, not a server error.
4. **REST API login path (port 8081) — ruled out.** Logging added to `BnetRestApiSession.cs` showed zero REST activity during any 2.5.6 attempt.
5. **General HermesProxy defect — ruled out.** A live WotLK Classic 3.4.3 client, using the exact same shared `Connection.cs`/`Authentication.cs` code, completed the entire BNet flow cleanly (Connect → real CMaNGOS SRP auth → realm list → Account/GameUtilities services), proving the shared handshake code works in general.
6. **Arctium's runtime patching (Ed25519 keys, static-auth-seed) — ruled out.** Verified via `Start-Transcript` (avoiding a `*>` stdio-redirection artifact that initially produced a misleading "invalid handle" failure): Arctium patches successfully every time (`Done :) You can login now.`), independent of whether the BNet handshake then succeeds or fails.

### Phase 2 — Root cause: missing `ciid` field

- Extracted printable strings directly from the client binary (`WowClassic.exe`, 65,459,408 bytes) via a PowerShell byte-scan, after working around two unrelated tooling snags: PowerShell's default execution policy blocking `.ps1` files (fixed with `-ExecutionPolicy Bypass`), and `[System.Text.Encoding]::Latin1` not existing in Windows PowerShell 5.1's .NET Framework runtime (fixed using `GetEncoding(28591)`, the ISO-8859-1 codepage, instead).
- The extracted strings confirmed the full `bgs.protocol.connection.v1` method list (`Connect`, `Bind`, `Echo`, `ForceDisconnect`, `KeepAlive`, `Encrypt`, `RequestDisconnect`) and revealed `ciid` and `connected_region` as fields referenced in the client's own protobuf schema.
- Confirmed via HermesProxy's own `Framework/Proto/ConnectionService.cs` (a hand-ported, non-auto-regenerated file) that `ConnectResponse` genuinely has no `Ciid` property — attempting to set one produced a clean compiler error (`CS1061`), not a runtime failure, confirming the field is structurally absent rather than merely unset.
- Obtained the authoritative field definition directly from TrinityCore's actively-maintained `connection_service.pb.h` (they support current retail, so their schema is current): `optional string ciid = 9;`, wire tag `74` (`(9 << 3) | 2`).
- **Implemented properly** in `Framework/Proto/ConnectionService.cs`: added the `Ciid` property (matching the existing null-as-absence pattern used by other string fields like `Reason`), and wired it into the constructor copy, `WriteTo`, `CalculateSize`, and both `MergeFrom` overloads.
- **First test attempt was inconclusive**, not negative: the live capture showed `request.ClientId was null - could not set Ciid` — the 2.5.6 client's own `Connect` request doesn't supply a `client_id`, and the original code only populated `Ciid` when one was present, so `Ciid` was never actually sent in that test.
- **Fixed `HandleConnect`** to synthesize a client identity (matching real Battle.net/TrinityCore behavior, which generates one "from the session ID and session creation time" when the client omits it) regardless of whether the request supplies one, then derive `Ciid` from it unconditionally.
- **Result: the instant disconnect stopped.** This is a genuine, reproducible behavioral change — confirmed across two separate login attempts.

### Phase 3 — New, downstream symptom: reproducible ~45-second hang

With `Ciid` now populated, `Connection.log` shows a **new event order**, reproduced across two consecutive attempts:

```
Glue Start Login
BattleNet Attempt Logon
[~45 second gap, no further client-server communication of any kind]
BattleNet Front Disconnected     <- network session drop happens FIRST now
Glue Fatal Error: 3006            <- Glue error is now a consequence, not the cause
BattleNet Defer Disconnect
```

This is the **opposite order** from every pre-fix attempt, where `Glue Fatal Error` fired immediately and *caused* the disconnect. The new error (`BLZ51903006` / plain integer `3006`) is also a different, simpler code than the pre-fix `0x80000154` (masked `340`) — consistent with a generic network-timeout class rather than a specific client-side pre-flight rejection.

**Confirmed via `BnetTcpSession.cs`'s raw packet logging and the client's own `WowConnection.log`:** zero communication of any kind occurs between client and HermesProxy during the ~45-second wait, on either side. The client isn't retrying or waiting on us — it's blocked on something entirely separate.

**Investigated and ruled out as the cause of this specific hang:** a one-off `Tact.log`/`Client.log` stall (~5 minutes) observed on the first post-fix attempt was **not reproduced** on a second attempt (which showed a clean 5ms DNS resolution and no stall) — confirming that particular stall was an unrelated fluke, not a symptom of the real problem.

**Leading hypothesis:** the client is attempting to verify itself against **real Blizzard CDN/version-check infrastructure** (not a local/custom redirect) during this window. Arctium's own startup log explicitly documents this: `https://ngdp.arctium.io/EU/wow/68941/versions not reachable. Falling back to: https://%s.version.battle.net/v2/products/%s/%s` — because Arctium's custom CDN dataset (`http://ngdp.arctium.io/customs/wow/cdns`) has no entry for build 68941 yet, being so new. A verification attempt against genuine Blizzard servers, for a session that isn't actually a genuine Blizzard-issued session, would predictably fail after its own internal timeout (~45 seconds), independent of anything HermesProxy does.

## Current Conclusion

Two separate, previously-stacked problems have been identified:

1. **HermesProxy's `ConnectResponse` schema gap (SOLVED).** The `ciid` field was missing, causing the client to reject the connection instantly. Fixed by adding the field with the correct wire-format details and populating it (with a synthesized client identity, since this client doesn't supply one) in every `Connect` response.
2. **A CDN/version-verification gap, likely on Arctium's side (OPEN, new phase of investigation).** With the BNet-layer problem solved, the client now proceeds far enough to attempt a real-Blizzard-infrastructure verification step that cannot succeed for an unlisted build, and times out on its own after ~45 seconds with a generic disconnect. This is very likely fixable by supplying Arctium with (or configuring it to use) valid CDN/version data for build 68941, rather than anything requiring further HermesProxy changes.

## What's Needed Next

Move to investigating Arctium's CDN/version-redirect mechanism specifically:
- Whether Arctium's launcher supports manually supplying custom CDN/version manifest data for a build not in its own dataset.
- Whether this is a known, documented limitation for very new builds, and whether Arctium's community has a workaround.
- Whether the ~45 second window matches any known TACT/Ribbit protocol timeout that could be independently confirmed.

## Reference: Key Files & Values

- `HermesProxy/BnetServer/Services/Services/Connection.cs` — `HandleConnect` (now synthesizes client identity + populates `Ciid`), `HandleRequestDisconnect`.
- `HermesProxy/Framework/Proto/ConnectionService.cs` — hand-ported protobuf messages; `Ciid` property added here (field 9, wire tag 74).
- `HermesProxy/BnetServer/Networking/BnetTcpSession.cs` — raw RPC dispatch loop (temp debug logging added).
- `HermesProxy/BnetServer/Networking/BnetRestApiSession.cs` — REST login path (confirmed unused in this investigation).
- **`ciid` field**: `optional string ciid = 9;` in `bgs.protocol.connection.v1.ConnectResponse`, confirmed via TrinityCore's current source.
- **Pre-fix error**: `Glue Fatal Error: 2147483988` (`0x80000154`) → `WOW51900340`.
- **Post-fix error**: `Glue Fatal Error: 3006` → `BLZ51903006`, occurring after a reproducible ~45 second hang with no client-server communication.
- **Arctium CDN redirect** (relevant to Phase 3): `http://ngdp.arctium.io/customs/wow/cdns` — has no entry for build 68941 as of this writing, causing fallback to real Blizzard version-check endpoints.
