# HermesProxy + TBC Classic Anniversary 2.5.6 (68941): Connect Handshake Investigation — v3

*Continues from investigation-writeup-v2.md. This version covers all work completed after the `ciid` fix was implemented: confirming it worked, discovering a new downstream symptom, and ruling out three separate hypotheses for it via direct testing.*

## Summary of Progress Since v2

The `ciid` fix (documented in v2) is confirmed working — **the instant disconnect that blocked every attempt all night is gone.** The client now proceeds past `Connect` and holds the connection open for a consistent, reproducible **~45-58 second** period before giving up and disconnecting with a new, different error (`Glue Fatal Error: 3006` / displayed as `BLZ51903006`, or `BLZ51901023` when the wrong Arctium flag was used — see below).

This session's work focused entirely on diagnosing that new hang. **Three specific, well-reasoned hypotheses were tested directly and ruled out.** The most important finding: **during the entire hang window, there is no meaningful network activity from the client at all** — ruling out a network-dependent cause entirely and pointing instead to something internal to the client process itself.

## Hypothesis 1: Arctium's `--skip` flag — ruled out, wrong tool

Tried `--skip` ("allows connection to servers that come with already patched clients") hoping it might bypass whatever the client was waiting on. Instead produced an **instant, different failure** (`BLZ51901023`) with no hang at all.

**Conclusion:** `--skip` is meant for a workflow where the client binary is permanently pre-patched beforehand; ours is patched fresh in-memory by Arctium every launch (the Ed25519 key patches observed all night). Telling Arctium to skip that step on a client that needs it caused the necessary patches to never apply, producing an unrelated, immediate failure. Not informative about the real hang; a red herring caused by picking the wrong flag for our workflow.

## Hypothesis 2: Arctium's `--versionurl`/`--cdnsurl` flags — ruled out

Checked Arctium's full flag list via `--help`, confirming both `--versionurl <url>` and `--cdnsurl <url>` exist. Set up a minimal Python `http.server` on the host serving dummy BPSV-shaped `versions`/`cdns` files, and pointed both flags at it.

**Result:** the Python server logged **zero incoming requests**. `Tact.log` was unchanged, still showing the same hardcoded real Blizzard CDN node list (`level3.blizzard.com`, `us.cdn.blizzard.com`) regardless of these flags.

**Conclusion:** the running game client's own internal TACT/CDN resolution is completely independent of these Arctium launcher flags — they evidently only affect something in Arctium's own pre-launch process, not the client's separate, already-compiled-in TACT networking subsystem.

## Wireshark capture methodology established

Standard approach used for the remainder of this session: capture inside the VM with Wireshark, filtered to relevant hosts/ports, started before login, analyzed afterward via `scapy` (Python) since `tshark` wasn't available in the analysis environment. Three captures were taken and analyzed (`1st-scan.pcapng` through `3rd-scan.pcapng`).

## Hypothesis 3: Missing CDN content-handle data (`ContentHandleArray`) — ruled out

**First capture (`1st-scan.pcapng`)** revealed the client making real HTTP requests to genuine Blizzard CDN IPs (`137.221.64.x`, `23.35.26.x`, matching `Tact.log`'s node list) for:
```
GET /tpr/wow/config/00/00/00000000000000000000000000000000
```
An all-zero placeholder hash — these connections completed cleanly at the TCP level (proving the VM has real internet access and these aren't network-blocked), but received `403`/`404` from Blizzard's real servers, as expected for a garbage hash.

**Investigated:** `ConnectResponse.ContentHandleArray`/`BinaryContentHandleArray` (fields 5/8) were confirmed never populated by `HandleConnect` — both null. `ContentHandle`'s structure (`Framework/Proto/ContentHandleTypes.cs`) was read directly: `region` (uint32), `usage` (uint32, unknown enum), `hash` (bytes), `proto_url` (optional override string).

**Fix attempted:** populated `ContentHandleArray` with the client's own real, already-cached `CDN Key` from its local `.build.info` file (`72c730bef365effe8a1373203e9c8c56`), reasoning that giving the client back a value it already trusts locally was better than fabricating one.

**Result, confirmed via timestamp analysis of `2nd-scan.pcapng`:** the all-zero CDN requests **still occurred**, but critically, **at the wrong time** — they happen during Arctium's own pre-launch startup (matching its logged CDN-reachability checks), consistently *before* the client ever connects to HermesProxy's `Connect` handler at all. Confirmed by comparing packet timestamps directly against HermesProxy's own connection-accepted log line.

**Conclusion:** these all-zero requests are very likely Arctium's own generic CDN-latency-probing behavior at startup (deliberately probing multiple mirrors with a placeholder hash to measure reachability) — a normal, expected, unrelated process. `ContentHandleArray` was a reasonable, well-motivated hypothesis, but is not the cause of the post-login hang, since the requests it might have influenced happen before `HandleConnect` is ever invoked. The fix remains in place (harmless, potentially still correct for a different reason) but is not the answer to this symptom.

## Hypothesis 4: Windows' own certificate trust-list validation — ruled out

Filtering `2nd-scan.pcapng` to the actual post-login hang window (after the real `Connect` timestamp, not before) revealed a different, genuinely new lead: a request during the hang to `ctldl.windowsupdate.com` for `disallowedcertstl.cab` — a **Windows OS-level** certificate "disallowed/trust list" download, unrelated to WoW's own application logic. Cross-referenced against the client binary's own string dump from earlier in the investigation, which confirmed the client uses Windows SChannel/CryptoAPI for certificate validation (`CertGetCertificateChain`, `CERT_TRUST_IS_UNTRUSTED_ROOT` strings present).

**Hypothesis:** Windows itself, independent of the WoW client's or Arctium's own logic, might be performing a slow background trust-chain validation on HermesProxy's self-signed TLS certificate, blocking the connection for the duration of that check.

**Fix applied:** enabled Group Policy `Computer Configuration → Administrative Templates → System → Internet Communication Management → Internet Communication settings → "Turn off Automatic Root Certificates Update"`, applied via `gpupdate /force` (no restart required).

**Result, confirmed via `3rd-scan.pcapng`:** the `disallowedcertstl.cab`/`ctldl.windowsupdate.com` traffic is **completely gone** — confirming the Group Policy change took effect correctly. **However, the hang duration was unchanged (~53 seconds)**, and the disconnect still occurred at the same relative timing.

**Conclusion:** this hypothesis correctly identified a real, genuine side-effect (the OS-level cert check was really happening) but incorrectly identified it as the *cause* — it was coincidental, not causal. Good example of why each hypothesis in this investigation has been tested directly rather than assumed correct from a single correlating observation.

## Key Finding: The hang is not network-dependent at all

Analyzing **all** packets (not just to known hosts) during the `3rd-scan.pcapng` hang window (`07:07:06` to `07:07:59` UTC) revealed:
- Our own BNet TCP connection to the client: completely idle, zero bytes exchanged in either direction, for the entire ~53 seconds, until the client sends a clean `FIN`.
- The only other traffic during this window: routine OS background noise (a DHCP lease renewal, NetBIOS/mDNS broadcasts) and one already-established, otherwise-idle connection to `34.110.236.123:443` receiving occasional passive keepalive-style bytes with no corresponding outgoing request.
- **Nothing WoW-related, nothing CDN-related, nothing directed at HermesProxy, occurs during this window.**

**This is a decisive result.** Three real, well-motivated network-layer hypotheses have now been tested and ruled out in a row (`ContentHandleArray`, Windows cert validation, and implicitly the earlier CDN/TACT theories from v2). The client appears to be waiting on **something purely internal to its own process** — a local timer, a stuck thread/mutex, a slow local computation — not on any network exchange.

## What's Needed Next

**Pivot from network-level to process-level tracing.** Wireshark has told us what it can for this specific symptom; the next tool is **Process Monitor (ProcMon, Sysinternals)**, run against `WowClassic.exe` during the hang window, to observe file I/O, registry access, and thread activity that packet capture cannot see. This is the planned next step for the following session.

## Current State of HermesProxy Code Changes

All changes remain in place and confirmed unchanged/intact as of this writeup:
- `HermesProxy/Framework/Proto/ConnectionService.cs`: `Ciid` property added (field 9, wire tag 74) — **confirmed necessary and working.**
- `HermesProxy/HermesProxy/BnetServer/Services/Services/Connection.cs`: `HandleConnect` synthesizes a client identity when the request omits one (this client always omits it), sets `Ciid` unconditionally, and additionally populates `ContentHandleArray` with the client's real cached CDN Key (harmless, unproven either way — kept in place pending further investigation).
- Temp debug logging (`[CONNECT DEBUG]`, `[DISCONNECT DEBUG]`, `[RAW PACKET]`) remains in `Connection.cs` and `BnetTcpSession.cs`.
- `appsettings.json`: `ClientOptions.ClientBuild` = `V2_5_6_68941` (confirmed correct/unchanged).

## Reference: Error Codes Observed This Session

- `BLZ51901023` — instant failure, caused by incorrect use of `--skip` (wrong flag; not a real finding about the client/server).
- `BLZ51903006` (`Glue Fatal Error: 3006`) — the current, reproducible symptom: occurs after a consistent ~45-58 second hang with no network activity, following a successful `Connect` exchange with a properly-populated `ciid`.

## Reference: Key Files & Tools

- `Framework/Proto/ContentHandleTypes.cs` — `ContentHandle` structure (`region`, `usage`, `hash`, `proto_url`).
- `.build.info` (WoW install root) — contains the client's real, valid `Build Key`/`CDN Key` hashes (not corrupted; ruled out as a cause).
- Wireshark + `scapy` (Python) — packet capture and analysis methodology established this session; `tshark` was not available in the analysis sandbox, `scapy` (`pip install scapy --break-system-packages`) was used instead.
- Group Policy: `gpedit.msc` → ... → "Turn off Automatic Root Certificates Update" (Enabled) + `gpupdate /force` — successfully eliminates Windows' background cert trust-list check, confirmed via capture, though not the root cause of the hang.
- **Next tool needed:** Process Monitor (ProcMon), Sysinternals — for process-level (not network-level) tracing of `WowClassic.exe` during the hang window.
