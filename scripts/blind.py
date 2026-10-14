"""Blind back-translation: a fresh Claude, with no tools, sees only the translated text and turns it back into English.

  python3 scripts/blind.py output/RUN [--model sonnet]

It never sees the English source, the brief or the company files, so it cannot "remember" what the ad
should say. Results are saved by exact language + text: when a translation changes, only the changed text
is sent again, and an old back-translation can never vouch for new wording.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job import LABEL, load, now, save, use_venv  # noqa: E402

BATCH = 25  # small enough to finish well inside the time limit; a batch that fails is split in two and sent again
SYSTEM = ("You translate advertising text into English for a quality check. You have no source text and must not guess "
          "what an advertisement 'should' say. Translate what the words actually say, including omissions, awkward "
          "wording, ambiguity, numbers, conditions and tone (formal or informal address). Keep names as written. Do not "
          "improve or fill in missing meaning. If something is ambiguous, give the most literal reading and say why in "
          "uncertainty; otherwise leave uncertainty empty.")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["results"], "properties": {"results": {
    "type": "array", "items": {"type": "object", "additionalProperties": False,
                               "required": ["id", "back_translation", "uncertainty"],
                               "properties": {"id": {"type": "string"}, "back_translation": {"type": "string"},
                                              "uncertainty": {"type": "string"}}}}}}


def wanted(run_dir):
    """Every current (language, text) pair in drafts.json, full and short versions."""
    drafts = load(Path(run_dir) / "drafts.json")
    pairs = []
    for row in drafts.get("translations", []):
        for value in (row.get("translation"), row.get("short_version")):
            if isinstance(value, str) and value.strip() and (row["language"], value) not in pairs:
                pairs.append((row["language"], value))
    return pairs


def command(model):
    """Lean, isolated call: no tools, no MCP servers, no skills, no settings, no saved session."""
    return ["claude", "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
            "--tools", "", "--disallowedTools", "mcp__*", "--strict-mcp-config", "--disable-slash-commands",
            "--setting-sources", "", "--no-session-persistence", "--system-prompt", SYSTEM, "--model", model]


def validate(items, result):
    """The run must succeed, use no tools and return exactly one non-empty answer per id."""
    if result.get("is_error") or result.get("subtype") != "success":
        raise ValueError(f"Claude run failed: {result.get('subtype')} {result.get('result', '')[:300]}")
    if result.get("permission_denials"):
        raise ValueError("The blind run tried to use a tool; result rejected.")
    rows = (result.get("structured_output") or {}).get("results")
    if not isinstance(rows, list):
        raise ValueError("No structured results returned.")
    ids = [r.get("id") for r in rows]
    if sorted(ids) != sorted(i["id"] for i in items):
        raise ValueError("Missing, extra or duplicate back-translations.")
    if any(not str(r.get("back_translation", "")).strip() for r in rows):
        raise ValueError("Empty back-translation.")
    return {r["id"]: r for r in rows}


def run_batch(items, folder, model):
    """items: [{id, language, text}] only. Runs in an empty temporary folder so no project file is loaded."""
    for item in items:
        if set(item) != {"id", "language", "text"}:
            raise ValueError("Blind input may hold only id, language and text.")
    folder.mkdir(parents=True)
    prompt = ("Translate each text into English. The language code tells you the source language.\n\n"
              + json.dumps(items, ensure_ascii=False, indent=1))
    save(folder / "input.json", items)
    (folder / "prompt.txt").write_text(prompt, encoding="utf-8")
    record = dict(started_at=now(), model=model, items=len(items), source_supplied=False, tools="none")
    with tempfile.TemporaryDirectory(prefix="blind-") as empty:
        try:
            done = subprocess.run(command(model), input=prompt, text=True, capture_output=True, cwd=empty, timeout=600)
        except subprocess.TimeoutExpired:
            record.update(status="failed", error="no answer within 600 s")
            save(folder / "run.json", record)
            raise RuntimeError("The blind run took longer than 600 s.") from None
    (folder / "stderr.txt").write_text(done.stderr, encoding="utf-8")
    try:
        result = json.loads(done.stdout)
    except json.JSONDecodeError:
        record.update(status="failed", error=f"claude exited {done.returncode}; output was not JSON")
        save(folder / "run.json", record)
        raise RuntimeError(f"claude exited {done.returncode}; see {folder}/stderr.txt") from None
    save(folder / "response.json", result)
    usage = result.get("usage") or {}
    record.update(ended_at=now(), num_turns=result.get("num_turns"), cost_usd_estimate=result.get("total_cost_usd"),
                  input_tokens=sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                              "cache_read_input_tokens")))
    try:
        rows = validate(items, result)
        record["status"] = "complete"
        return rows, record
    except ValueError as error:
        record.update(status="failed", error=str(error))
        raise
    finally:
        save(folder / "run.json", record)


def main(argv):
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return
    if not argv or argv[0].startswith("-"):
        raise SystemExit(__doc__)
    run_dir = Path(argv[0]).resolve()
    model = argv[argv.index("--model") + 1] if "--model" in argv else "sonnet"
    if not shutil.which("claude"):
        raise SystemExit("The claude command is not installed or not on PATH; no blind check was run.")
    store_path = run_dir / "back-translations.json"
    store = load(store_path) if store_path.exists() else dict(label=LABEL, results=[], runs=[])
    have = {(r["language"], r["text"]) for r in store["results"]}
    todo = [p for p in wanted(run_dir) if p not in have]
    if not todo:
        print(json.dumps(dict(new=0, saved=len(store["results"]), note="Every current translation already has its blind back-translation.")))
        return
    blind_dir = run_dir / "blind"
    queue = [todo[start:start + BATCH] for start in range(0, len(todo), BATCH)]
    while queue:
        batch = queue.pop(0)
        n = len(list(blind_dir.glob("run-*"))) + 1 if blind_dir.exists() else 1
        items = [dict(id=f"t{i + 1:03d}", language=lang, text=text) for i, (lang, text) in enumerate(batch)]
        try:
            rows, record = run_batch(items, blind_dir / f"run-{n:02d}", model)
        except (RuntimeError, ValueError) as error:
            if len(batch) < 4:
                raise
            print(json.dumps(dict(retry=f"blind/run-{n:02d} failed ({error}); sending it again in two halves")))
            half = len(batch) // 2
            queue[:0] = [batch[:half], batch[half:]]
            continue
        for item in items:
            row = rows[item["id"]]
            store["results"].append(dict(language=item["language"], text=item["text"], back_translation=row["back_translation"],
                                         uncertainty=row["uncertainty"], run=f"blind/run-{n:02d}"))
        store["runs"].append(dict(folder=f"blind/run-{n:02d}", **record))
        save(store_path, store)
    print(json.dumps(dict(new=len(todo), saved=len(store["results"]), runs=len(store["runs"]),
                          cost_usd_estimate=round(sum(r.get("cost_usd_estimate") or 0 for r in store["runs"]), 4))))


if __name__ == "__main__":
    use_venv()
    try:
        main(sys.argv[1:])
    except (ValueError, RuntimeError, FileNotFoundError) as error:
        raise SystemExit(str(error)) from None
