# HermesProxy + TBC Classic Anniversary (2.5.6, build 68941): Connect Handshake Investigation

## Summary

TBC Classic Anniversary client, version 2.5.6, build **68941**, fails to authenticate through HermesProxy (Xian55 fork, .NET 10) against a CMaNGOS-TBC (2.4.3/8606) legacy backend. The client completes the Battle.net TLS handshake and the initial `ConnectionService.Connect` RPC call successfully, then **voluntarily disconnects** (`RequestDisconnect`, `ErrorCode=0`) before ever attempting `AuthenticationService.Logon`. This happens on every single attempt, consistently, in under 200ms.

Through a systematic elimination process (raw packet logging, a differential test against a known-working client generation, and full-transcript relaunch testing), **every plausible external cause has been ruled out**. The failure is isolated to something in the 2.5.6 client binary's own internal validation logic — logic that is not among the small set of checks the Arctium launcher currently knows how to patch.

## Environment

- **HermesProxy**: Xian55 fork, .NET 10, `V2_5_6_68941` added to `ClientVersionBuild.cs`/`VersionChecker.cs` as Phase 1 scaffolding (registers the build, reuses `V2_5_3_41750`'s opcode/field tables — noted as an unverified assumption, since this scaffolding work predates the current investigation and turned out not to be the blocker).
- **Legacy backend**: CMaNGOS-TBC, protocol 2.4.3/build 8606, confirmed fully operational (realmd + mangosd, live multi-client tested independently of HermesProxy).
- **Client**: TBC Classic Anniversary, version 2.5.6, build 68941 (released ~3 weeks prior to this investigation).
- **Launcher**: Arctium Game Launcher, `--staticseed --version=Classic --dev`.
- **Topology**: HermesProxy + CMaNGOS-TBC on host PC (static LAN IP), WoW client in a NAT-mode VM. `Config.wtf` portal and HermesProxy's `ProxyNetworkOptions.ExternalAddress` both set to the host's LAN IP (not `127.0.0.1`, which would resolve to the VM itself).

## Exact Symptom

```
ConnectionService/m:1 (Connect)      -> client sends, server responds OK
ConnectionService/m:7 (RequestDisconnect, ErrorCode=0) -> client sends immediately after
```

No `AuthenticationService` call is ever made. Client-side `Connection.log`:

```
Glue Start Login
BattleNet Attempt Logon
Glue Fatal Error: 2147483988   (0x80000154; masked = 0x154 = 340 decimal)
BattleNet Defer Disconnect
```

Displayed to the user as the generic disconnect dialog, `WOW51900340`.

## Investigation Methodology & Elimination Chain

Each hypothesis below was tested directly rather than assumed, in roughly chronological order:

### 1. TLS certificate rejection — ruled out
**Test:** Added `--dev` flag to force Arctium's certificate-bypass patching for the local IP.
**Result:** No change in behavior. Also, the client successfully completes a full RPC round-trip (`Connect` request/response) after TLS — a cert-level rejection would prevent any RPC exchange from happening at all.
**Conclusion:** TLS is not the problem; the failure is above the transport layer.

### 2. Silently-dropped packets between Connect and RequestDisconnect — ruled out
**Test:** Added unconditional raw-packet-header logging (`ServiceId`, `ServiceHash`, `MethodId`, `Token`, `PayloadLen`) directly in `BnetTcpSession.cs`'s dispatch loop, bypassing all Serilog level filtering, to catch anything that might be silently skipped by the `ServiceId != 0xFE && ServiceHash != 0` dispatch guard.
**Result:** Exactly two packets observed per attempt — `Connect` (62 bytes) and `RequestDisconnect` (2 bytes) — nothing in between, nothing dropped.
**Conclusion:** The client genuinely sends only these two calls. No missing/unlogged intermediate step.

### 3. Server-side rejection — ruled out
**Test:** Added logging of the client-reported `ErrorCode` field inside `HandleRequestDisconnect`.
**Result:** `ErrorCode = 0` — a clean, voluntary disconnect, not a server-reported error.
**Conclusion:** The disconnect is a decision made entirely client-side.

### 4. REST API login path (port 8081) — ruled out
**Test:** Added logging to `BnetRestApiSession.ReadHandler` and `HandleLoginRequest`, which contains its own independent build-validation check (`ModernVersion.Build != (ClientVersionBuild) globalSession.Build` -> `FAIL_WRONG_MODERN_VER`).
**Result:** Zero REST activity logged during any 2.5.6 attempt.
**Conclusion:** The client never touches this endpoint; it is not involved in the failure.

### 5. General HermesProxy defect in the shared Connect/Auth handshake code — ruled out
**Test:** Differential test using an actual WotLK Classic 3.4.3 (build 54261) client against the exact same HermesProxy codebase (temporarily switching `appsettings.json`'s `ClientBuild`), through the identical, non-version-branched `Connection.cs`/`Authentication.cs` handler code.
**Result:** WotLK 3.4.3 completed the entire flow cleanly: `Connect` -> `AuthenticationService.Logon` -> real CMaNGOS SRP authentication succeeded -> realm list retrieved -> `AccountService`/`GameUtilitiesService` calls succeeded -> reached realm selection.
**Conclusion:** The shared handshake code is correct and functional in general. The problem is specific to something about the 2.5.6 client build itself, not a HermesProxy regression or general defect. (Note: this also confirms the request/response bytes for `Connect` are byte-length-identical between the two client generations — 62 bytes, same service hash, same method — and `HandleConnect`'s response body does not reference the configured client build at all, so the server-side response content is effectively identical in both cases.)

### 6. Arctium launcher failing to apply its runtime patches for this build — ruled out
**Initial finding:** Arctium's log showed `[GameCrypto Ed25519 PublicKey 1] No result found` (a known-benign warning pattern also seen, for a different key, during the successful WotLK test) followed by an apparent hard failure (`Game initialization failed... The handle is invalid`) when output was captured via PowerShell's `*>` stream redirection.
**Correction:** The apparent hard failure was an artifact of the capture method — redirecting a launched process's OS-level stdio handles via `*>` can break child-process handle inheritance. Re-tested using `Start-Transcript`/`Stop-Transcript` (which captures console output without touching the process's actual handles).
**Result with correct capture method:** Arctium's patching completed successfully - `Static auth seed function not found. Skipping... Done :) You can login now. RE-LAUNCH ATTEMPT 3/3 SUCCESSFUL`. The HermesProxy console for that same session shows the identical `Connect` -> `RequestDisconnect(ErrorCode=0)` failure regardless.
**Conclusion:** Arctium's patching is not the cause. The failure persists even when Arctium's own launch/patch process completes with no errors.

## Current Conclusion

By elimination, the cause is **something in the 2.5.6 client binary's own internal validation logic that fires after receiving a valid `Connect` response and before attempting `Logon`** — logic that is not among the small, currently-known set of things Arctium patches (two Ed25519 public keys, a static-auth-seed function). This is not visible from the server side: the request and response bytes are unremarkable and match a working client generation almost exactly. It is very likely either:

- A new client-side check specific to this build/client generation that Arctium's current patch set doesn't yet address, or
- A structurally different validation path introduced in the "Anniversary" client line (2.5.4+) relative to both the older TBC Classic line (2.5.2/2.5.3) and WotLK Classic (3.4.3) — both of which are confirmed working through this exact HermesProxy code.

Community reports (a public RaGEZONE thread, plus an unverified secondary reference to Xian55/HermesProxy issue #108) independently suggest the 2.5.5+ Anniversary generation may run on a materially different underlying client engine (described as MoP Classic/5.5.x-descended) than earlier TBC Classic builds — though this specific claim has not been independently confirmed via primary source in this investigation.

## What's Needed Next

This requires either:
1. **An Arctium launcher update** that adds a new patch signature for whatever check this build performs (the Arctium team/community would need to identify the new check first), or
2. **Binary-level reverse engineering** of the 2.5.6 client executable directly, to locate the specific validation logic that differs from the working client generations.

Both are specialized, non-trivial undertakings distinct from anything fixable in HermesProxy's own C# source.

## Reference: Key Files (HermesProxy, Xian55 fork)

- `HermesProxy/BnetServer/Services/Services/Connection.cs` — `HandleConnect`, `HandleRequestDisconnect`
- `HermesProxy/BnetServer/Services/Services/Authentication.cs` — `HandleLogon` (never reached in the failing case)
- `HermesProxy/BnetServer/Networking/BnetTcpSession.cs` — raw RPC dispatch loop
- `HermesProxy/BnetServer/Networking/BnetRestApiSession.cs` — REST login path (confirmed unused in this failure)
- `HermesProxy/VersionChecker.cs` — build registration, `IsSupportedModernVersion`, `GetUpdateFieldsDefiningBuild` (confirmed irrelevant to this specific failure — this logic only matters post-authentication)

## Key Error Reference

- Client-side: `Glue Fatal Error: 2147483988` (`0x80000154`) -> displayed as `WOW51900340`. The `WOW519xx` family is Blizzard's generic "disconnected from server" class; the specific `340`/`0x154` suffix has no independently confirmed documented meaning as of this writing — its diagnostic value comes entirely from *when* it fires (immediately post-Connect, pre-Logon), not from the number itself.
