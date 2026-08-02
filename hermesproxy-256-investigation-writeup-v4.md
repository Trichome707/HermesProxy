# HermesProxy + TBC Classic Anniversary 2.5.6 (68941): Connect Handshake Investigation — v4

*Continues from investigation-writeup-v3.md. This version covers the Process Monitor (ProcMon) phase: characterizing the ~45-52 second hang at the OS/process level, since v3 had already ruled out a network-layer cause.*

## Summary of Progress Since v3

v3 established that the post-`ciid`-fix hang is **not network-dependent** — zero meaningful client-server traffic occurs during the ~45-58 second wait. This session used Process Monitor to look for OS-level causes (file I/O, registry access, thread activity) instead.

**Result: also ruled out.** The hang shows **zero file I/O, zero registry access, and zero meaningful OS-visible activity of any kind** during the wait. Combined with v3's network finding, this narrows the cause to something happening **purely inside the client process's own memory/execution** — invisible to both packet capture and OS-activity monitoring.

## Methodology

- Process Monitor run inside the VM (elevated), filtered to `Process Name is WowClassic.exe`.
- Captured a clean, tightly-scoped window (start capture immediately before Arctium launch, stop immediately after the disconnect dialog appears) to avoid the very large, mostly-irrelevant capture from an initial looser attempt.
- Exported to CSV and analyzed locally via Python (deliberately kept off Claude's own compute for the initial bulk pass, per an explicit resource-conservation preference — deterministic filtering doesn't need an LLM at all, and only small, already-narrowed slices were shared for further analysis).

## Finding 1: The largest gap in the capture is a red herring

An initial largest-time-gap search found a 120-second gap, but on inspection this was **not** the login hang — it was the OS tearing down the process (`RegCloseKey`/`CloseFile`/`IRP_MJ_CLOSE` on system DLLs) after the user manually closed the client *following* the disconnect, plus lagging TCP socket cleanup. A corrected search excluding this teardown sequence was needed to find the real window.

## Finding 2: Zero OS-visible activity during the actual hang

With the teardown sequence excluded, the largest gaps in the real hang window were all trivial (~1 second), consisting only of routine `Process Profiling` sampling events (ProcMon's own periodic stats collection, not client activity). **No file reads, no registry access, no network activity of any kind occurs between roughly 8:47:13.66 PM and 8:47:45.80 PM** — a ~32 second genuinely silent window, sitting inside the broader ~45-52 second hang observed on-screen.

## Finding 3: CPU usage during the hang — steady, low, non-zero

Examining the `Process Profiling` events' `User Time`/`Kernel Time` deltas across the window: approximately **3.81s User + 1.91s Kernel CPU time consumed over ~35 wall-clock seconds — roughly 16% average utilization, held steady throughout, with no spikes.**

This is diagnostically useful:
- **Rules out a pure sleep/block** (would show ~0% CPU).
- **Rules out a tight busy-spin/infinite loop** (would show ~100%, pegging a core).
- **Consistent with an in-memory polling loop** — periodically waking, checking some internal condition or flag, sleeping again, repeating — waiting on an internal signal that never arrives, rather than on any external resource.

## Finding 4: A sharp memory deallocation at the exact moment the wait ends

`Private Bytes` drops sharply (~70MB, from `811,737,088` to `741,613,568`) in the very last CPU sample before the window ends — a sudden, one-time deallocation, unlike the flat/gradual pattern throughout the rest of the wait. This is very likely **the exact instant the internal wait gives up and begins tearing down whatever it was trying to do** — a useful marker even without knowing what specifically was being waited on.

## Finding 5: What happens immediately after the wait ends (confirmed unrelated to the cause)

The moment the silent window ends (~8:47:45.80 PM), the client begins a fast (<1 second) burst of routine addon/SavedVariables initialization — iterating alphabetically through every `Blizzard_*.lua` file, checking both account-wide and character-specific `SavedVariables` paths, receiving expected `NAME NOT FOUND` results (fresh account, nothing saved yet), with one `.lua.bak` rename collision. **This is normal, expected startup work that resumes immediately once the internal block releases — not a cause of the hang, just confirmation of what continues right after it.**

## Current Conclusion

Two independent observation methods have now been exhausted for this specific symptom:
- **Network capture (Wireshark, v3):** zero relevant traffic during the hang.
- **OS activity monitoring (ProcMon, this session):** zero file/registry/handle activity during the hang; CPU profile consistent with an internal polling loop, not I/O-bound waiting.

**The remaining unknown — what internal condition the client is polling for — lives entirely inside the process's own memory and call stack.** Neither packet capture nor OS-activity monitoring can see inside a pure CPU-level wait; only a live debugger attached to the process during the hang can show the actual thread call stack and reveal what function is looping and what it's checking.

## What's Needed Next

**Attach a live debugger (WinDbg or x64dbg) to `WowClassic.exe` during the hang window**, specifically to capture the call stack of whichever thread is active during the polling loop (e.g., break in partway through the ~32 second window and inspect all threads). This is a meaningfully bigger step than anything done so far — Wireshark and ProcMon are passive, external observation; a debugger requires attaching to and potentially interrupting a live, running process, and reading raw stack traces/assembly rather than structured logs. Worth treating as a deliberate, separate phase of work rather than a quick extension of tonight's session.

## Reference: Key Data Points (this session)

- Hang window (silent portion): **8:47:13.66 PM to 8:47:45.80 PM**, ~32 seconds.
- CPU utilization during hang: **~16% average, steady** (3.81s User + 1.91s Kernel over ~35s wall-clock).
- Memory: flat at ~811MB Private Bytes throughout, sharp ~70MB drop at the moment the wait ends.
- Result-type breakdown for the whole capture window (`1657` events / `1217` before teardown): almost entirely `SUCCESS`; the `NAME NOT FOUND`/`FAST IO DISALLOWED`/`BUFFER OVERFLOW`/`NAME COLLISION` entries are all either normal startup queries (proxy settings, before the hang) or normal addon SavedVariables initialization (after the hang) — none fall inside the actual silent window itself.
- ProcMon capture files: `C:\Dev\Sysinternals\procmon_capture.PML` (native format), `Logfile.CSV` (analysis format).

## Reference: Key Files (cumulative)

- `HermesProxy/Framework/Proto/ConnectionService.cs` — `Ciid` property (field 9, wire tag 74) — confirmed necessary, working.
- `HermesProxy/HermesProxy/BnetServer/Services/Services/Connection.cs` — `HandleConnect` synthesizes client identity, sets `Ciid`, populates `ContentHandleArray` (kept in place, unproven either way for this specific symptom).
- `C:\Dev\Sysinternals\ProcMon.py` through `ProcMon4.py` — local analysis scripts developed this session for gap-finding and CPU-profile extraction from ProcMon CSV exports.
- **Next tool needed:** WinDbg or x64dbg, for live call-stack inspection of `WowClassic.exe` during the hang.
