"""Save your ElevenLabs API key to .env without it ever showing on screen or passing through Claude.

Run this yourself, in your terminal (not inside Claude Code):

  python3 scripts/save_key.py

The key is typed or pasted at a hidden prompt, written to .env in this folder with owner-only
permissions (600), and never printed. .env is in .gitignore, so git will not pick it up.
"""
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
NAME = "ELEVENLABS_API_KEY"


def save_key(path, key):
    """Write or replace the key line in .env, keep any other lines, atomically, mode 600."""
    if not key or any(c.isspace() for c in key) or "=" in key:
        raise ValueError("The key is empty or has spaces in it. Nothing was changed.")
    path = Path(path)
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if not l.startswith(NAME + "=")] if path.exists() else []
    lines.append(f"{NAME}={key}")
    temporary = None
    try:
        # mkstemp creates the file readable by the owner only (600) before anything is written.
        handle, name = tempfile.mkstemp(dir=path.parent, prefix=".env.")
        temporary = Path(name)
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write("\n".join(lines) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    os.chmod(path, 0o600)


def describe_plan(key):
    """Confirm the key works and show the plan. Prints plan details only, never the key."""
    request = urllib.request.Request("https://api.elevenlabs.io/v1/user/subscription", headers={"xi-api-key": key})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read())
    left = (data.get("character_limit") or 0) - (data.get("character_count") or 0)
    return f"Key works. Plan: {data.get('tier')} ({data.get('status')}), {left:,} credits left this period."


if __name__ == "__main__":
    if not sys.stdin.isatty():
        sys.exit("Run this in your own terminal so the key stays hidden (not through Claude).")
    try:
        key = getpass.getpass("Paste your ElevenLabs API key (it will not show), then press Return: ").strip()
        save_key(ROOT / ".env", key)
    except (KeyboardInterrupt, EOFError):
        sys.exit("\nCancelled. Nothing was changed.")
    except (OSError, ValueError) as error:
        sys.exit(f"Could not save the key: {error}")
    print("Saved to .env (only you can read it). The key was not displayed.")
    try:
        print(describe_plan(key))
    except urllib.error.HTTPError as error:
        print(f"ElevenLabs did not accept the key (HTTP {error.code}). Check it on elevenlabs.io and run this again.")
    except OSError:
        print("Could not reach ElevenLabs to test the key. It is saved; the job will test it again before any paid step.")
    print("Back in Claude Code, say: key saved.")
