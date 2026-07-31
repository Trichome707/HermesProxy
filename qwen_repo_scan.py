#!/usr/bin/env python3
"""
qwen_repo_scan.py

Scans WowClassicGrindBot source files with a local Ollama model (qwen2.5:3b)
and writes one Markdown analysis file per source file.

Requires Python 3.9+ and a running Ollama instance (http://localhost:11434)
with the qwen2.5:3b model already pulled.

USAGE
-----

Task 1 - targeted extraction against a list of files (from your grep targets.txt):

    python qwen_repo_scan.py --task extract --targets targets.txt ^
        --repo-root C:\WowClassicGrindBot --out analysis

Task 2 - one-line summary of every .cs file in the repo (good filler task):

    python qwen_repo_scan.py --task summarize ^
        --repo-root C:\WowClassicGrindBot --out analysis --ext .cs

DESIGNED TO RUN UNATTENDED FOR HOURS
-------------------------------------
- Resume-safe: if a file's output .md already exists, it's skipped. You can
  kill the script (Ctrl+C) and rerun the same command to pick up where it
  left off.
- Retries each Ollama call up to 3 times with a short delay before giving up
  on a chunk.
- Large files are split into ~6000-character chunks so nothing gets silently
  truncated by the model's context window.
- Writes a running log to analysis/_run.log and a list of any files that
  failed after all retries to analysis/_failures.txt, so you can spot-check
  or rerun just the failures in the morning.

WHAT YOU GET BACK
------------------
One .md file per source file under the --out directory, named after the
file's path with slashes replaced by underscores, e.g.:

    Core_BotController.cs.md
    SharedLib_ClientVersion.cs.md

Bring the whole analysis/ folder back to Claude afterward - that's the raw
fact index the actual reasoning step (does a 2.5.6 port look safe, what
would it touch) will be based on.
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional
from urllib import request, error

OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "qwen2.5:3b"
CHUNK_CHARS = 6000          # conservative chunk size for a 3B model's context
MAX_RETRIES = 3
RETRY_DELAY_SEC = 5
REQUEST_TIMEOUT_SEC = 300

EXTRACT_SYSTEM = (
    "You are extracting facts from source code, not explaining or reasoning about them. "
    "Only output what is literally present in the file. Do not infer intent or assess correctness."
)

EXTRACT_TEMPLATE = """File: {filepath}{chunk_note}

IMPORTANT: The category descriptions below are generic shape descriptions, not
real values. Do not report anything you recall from outside this file, and do
not reuse any sample values you may have seen in other instructions or files.
Only report items you can point to verbatim inside the <file content> block
below. If nothing in a category is present in the actual file text, write
"None found."

Extract, as plain bullet points:
1. Every enum member that looks like a WoW client version tag (a short token
   mixing letters, numbers, and underscores, such as an expansion abbreviation
   plus a version number) - quote it exactly as it appears in the file.
2. Every switch/if statement that branches on a variable or enum named
   something like ClientVersion - list the condition and the branch target
   (method/class name), one line each, quoting the actual condition text.
3. Every literal integer that looks like a client build number (typically
   5 digits) found anywhere in the file - quote the exact number as written
   plus the variable/constant name next to it.
4. Any line containing the literal text "SecureActionButtonTemplate",
   "BindPad", or "2.5.5" - quote the full line verbatim, unmodified.

Before answering, re-check each item against the <file content> block below.
If you cannot find the literal text in that block, remove it from your answer.

<file content>
{content}
</file content>
"""

HERMES_SYSTEM = (
    "You are extracting facts from source code, not explaining or reasoning about them. "
    "Only output what is literally present in the file. Do not infer intent or assess correctness. "
    "Never invent, recall, or reuse values from outside the file text given to you."
)

HERMES_TEMPLATE = """File: {filepath}{chunk_note}

IMPORTANT: Only report items you can point to verbatim inside the <file content>
block below. Do not reuse any values you may have seen in other files, other
instructions, or general knowledge of WoW client builds. If nothing in a
category is present in the actual file text, write "None found."

This is HermesProxy, a protocol-translation proxy between modern WoW clients
and legacy emulator servers. Extract, as plain bullet points:

1. Every enum member, constant, or string literal that names a specific
   supported client build (a pattern like a version number followed by an
   underscore and a 5-digit build number, e.g. a "V" prefix plus dots/underscores
   and digits) - quote it exactly as written in the file.
2. Every place the code compares or switches on the client's build number or
   version against one or more of the values found in #1 - quote the
   condition and the branch target (method/class name) verbatim.
3. Every place the code throws, logs, or returns an error/exception related to
   an unrecognized, unknown, or unsupported client version/build - quote the
   full line verbatim.
4. Any dictionary, table, or array that is indexed or keyed by client
   version/build (e.g. an opcode table, a packet-structure table, a feature
   table) - name the structure (its declared type/variable name) and what it
   maps to, without inventing contents that are not shown in this chunk.

Before answering, re-check each item against the <file content> block below.
If you cannot find the literal text in that block, remove it from your answer.

<file content>
{content}
</file content>
"""

SUMMARIZE_SYSTEM = (
    "You summarize what a source file does in exactly one sentence, "
    "based only on its class/method names and comments. No speculation."
)

SUMMARIZE_TEMPLATE = """File: {filepath}{chunk_note}

In exactly one sentence, state what this file's primary responsibility is,
based only on its class/method names and comments. No speculation about broader architecture.

<file content>
{content}
</file content>
"""


TASK_PROMPTS = {
    "extract": (EXTRACT_SYSTEM, EXTRACT_TEMPLATE),
    "hermes": (HERMES_SYSTEM, HERMES_TEMPLATE),
    "summarize": (SUMMARIZE_SYSTEM, SUMMARIZE_TEMPLATE),
}


def call_ollama(model: str, system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model": model,
        "system": system_prompt,
        "prompt": user_prompt,
        "stream": False,
        "options": {"temperature": 0.1},
    }
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        OLLAMA_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body.get("response", "").strip()


def call_ollama_with_retries(model: str, system_prompt: str, user_prompt: str,
                              log: logging.Logger, label: str) -> Optional[str]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return call_ollama(model, system_prompt, user_prompt)
        except (error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as e:
            log.warning(f"[{label}] attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC)
    log.error(f"[{label}] giving up after {MAX_RETRIES} attempts")
    return None


def chunk_text(text: str, chunk_chars: int) -> List[str]:
    if len(text) <= chunk_chars:
        return [text]
    return [text[i:i + chunk_chars] for i in range(0, len(text), chunk_chars)]


def output_path_for(repo_root: Path, out_dir: Path, src_path: Path) -> Path:
    rel = src_path.relative_to(repo_root)
    flat_name = str(rel).replace("\\", "_").replace("/", "_") + ".md"
    return out_dir / flat_name


def process_file(src_path: Path, repo_root: Path, out_dir: Path, task: str,
                  model: str, log: logging.Logger) -> bool:
    out_path = output_path_for(repo_root, out_dir, src_path)
    if out_path.exists():
        log.info(f"SKIP (already done): {src_path}")
        return True

    try:
        content = src_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.error(f"Could not read {src_path}: {e}")
        return False

    if not content.strip():
        out_path.write_text(f"# {src_path}\n\n(empty file)\n", encoding="utf-8")
        return True

    chunks = chunk_text(content, CHUNK_CHARS)
    system_prompt, template = TASK_PROMPTS[task]

    results = []
    for idx, chunk in enumerate(chunks, start=1):
        chunk_note = f" (part {idx}/{len(chunks)})" if len(chunks) > 1 else ""
        user_prompt = template.format(filepath=src_path, chunk_note=chunk_note, content=chunk)
        label = f"{src_path.name} part {idx}/{len(chunks)}"
        log.info(f"Processing: {label}")
        response = call_ollama_with_retries(model, system_prompt, user_prompt, log, label)
        if response is None:
            results.append(f"## Part {idx}/{len(chunks)}\n\n**FAILED to process this chunk after {MAX_RETRIES} retries.**\n")
        else:
            results.append(f"## Part {idx}/{len(chunks)}\n\n{response}\n" if len(chunks) > 1 else response)

    out_path.write_text(
        f"# {src_path.relative_to(repo_root)}\n\n" + "\n\n".join(results) + "\n",
        encoding="utf-8",
    )
    return True


def gather_targets(args, repo_root: Path, log: logging.Logger) -> List[Path]:
    if args.task in ("extract", "hermes"):
        targets_file = Path(args.targets)
        if not targets_file.exists():
            log.error(f"Targets file not found: {targets_file}")
            sys.exit(1)
        lines = [l.strip() for l in targets_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        paths = []
        for line in lines:
            p = Path(line)
            if not p.is_absolute():
                p = repo_root / p
            if p.exists():
                paths.append(p)
            else:
                log.warning(f"Listed target not found, skipping: {line}")
        return paths

    # summarize mode: walk repo for matching extension
    ext = args.ext if args.ext.startswith(".") else f".{args.ext}"
    exclude_dirs = {".git", "bin", "obj", "node_modules", "Json", "images", "external"}
    paths = []
    for p in repo_root.rglob(f"*{ext}"):
        if any(part in exclude_dirs for part in p.parts):
            continue
        paths.append(p)
    return sorted(paths)


def main():
    parser = argparse.ArgumentParser(description="Scan repo files with local Ollama model.")
    parser.add_argument("--task", choices=["extract", "hermes", "summarize"], required=True)
    parser.add_argument("--repo-root", required=True, help="Path to the cloned repo root")
    parser.add_argument("--out", default="analysis", help="Output directory for .md files")
    parser.add_argument("--targets", default="targets.txt", help="[extract mode] file listing target paths, one per line")
    parser.add_argument("--ext", default=".cs", help="[summarize mode] file extension to scan")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama model name")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    log_path = out_dir / "_run.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    log = logging.getLogger("qwen_repo_scan")

    repo_root = Path(args.repo_root).resolve()
    if not repo_root.exists():
        log.error(f"Repo root not found: {repo_root}")
        sys.exit(1)

    targets = gather_targets(args, repo_root, log)
    log.info(f"Task: {args.task} | Model: {args.model} | Files to process: {len(targets)}")

    failures = []
    start = time.time()
    for i, src_path in enumerate(targets, start=1):
        log.info(f"[{i}/{len(targets)}] {src_path}")
        ok = process_file(src_path, repo_root, out_dir, args.task, args.model, log)
        if not ok:
            failures.append(str(src_path))

    elapsed = time.time() - start
    log.info(f"Done. {len(targets) - len(failures)}/{len(targets)} succeeded in {elapsed/60:.1f} min.")
    if failures:
        fail_path = out_dir / "_failures.txt"
        fail_path.write_text("\n".join(failures), encoding="utf-8")
        log.warning(f"{len(failures)} file(s) failed. See {fail_path}")


if __name__ == "__main__":
    main()