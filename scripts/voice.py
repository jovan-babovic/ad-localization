"""ElevenLabs steps for video ads. Only ready-made stock voices: this job never clones the speaker.

  python3 scripts/voice.py account                                   plan and credits left (free)
  python3 scripts/voice.py voices --language fr                      stock voices for a language (free)
  python3 scripts/voice.py transcribe output/RUN --video NAME        English transcript (paid: speech to text)
  python3 scripts/voice.py stems output/RUN --video NAME             split voice from music (paid)
  python3 scripts/voice.py speak output/RUN --video NAME --language fr [--voice ID] [--short]
                                                                     record the translated script, one clip per
                                                                     sentence (paid)

Paid steps first print an estimate and stop. Add --confirm only after the person running the job says yes.
Before any paid call: the plan must be active, automatic overage billing must be off, and at least
10,000 credits must stay in reserve after the estimate. The key comes from ELEVENLABS_API_KEY or .env
(written by scripts/save_key.py) and is never printed.
"""
import base64
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job import LABEL, ROOT, load, now, save, use_venv  # noqa: E402

API = "https://api.elevenlabs.io"
RESERVE = 10_000
TTS_MODEL = "eleven_v4"
STT_MODEL = "scribe_v2"
# Official rates from https://elevenlabs.io/pricing, checked 2026-10-14. Stem separation has no published
# rate there, so the Eleven Music rate (900 credits/minute) is used as an upper-bound estimate.
CREDITS = {"tts_per_character": 1, "stt_per_minute": 330, "stems_per_minute_upper_bound": 900}


def api_key():
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    env = ROOT / ".env"
    if not key and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("ELEVENLABS_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        raise SystemExit("No ElevenLabs key found. In your own terminal (not in Claude), run: python3 scripts/save_key.py")
    return key


class Client:
    def __init__(self, key):
        self.key = key

    def call(self, path, body=None, content_type=None, timeout=300):
        headers = {"xi-api-key": self.key}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(API + path, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                kept = {k.lower(): v for k, v in response.headers.items()
                        if k.lower() in ("content-type", "character-cost", "request-id", "history-item-id")}
                return response.read(), kept
        except urllib.error.HTTPError as error:
            message = error.read().decode(errors="replace").replace(self.key, "[key hidden]")[:600]
            raise SystemExit(f"ElevenLabs returned HTTP {error.code} for {path.split('?')[0]}: {message}") from None

    def json(self, path):
        return json.loads(self.call(path, timeout=60)[0])

    def account(self):
        data = self.json("/v1/user/subscription")
        keys = ("tier", "status", "character_count", "character_limit", "can_extend_character_limit",
                "allowed_to_extend_character_limit", "next_character_count_reset_unix")
        out = {k: data.get(k) for k in keys}
        out["credits_left"] = (data.get("character_limit") or 0) - (data.get("character_count") or 0)
        return out


def spending_gate(account, estimate):
    """The safeguards kept from the original job. Unknown values stop the job too."""
    if account.get("status") != "active":
        raise ValueError(f"The ElevenLabs plan is not active (status: {account.get('status')}). A paid plan is needed "
                         "for commercial use: https://elevenlabs.io/pricing")
    if account.get("can_extend_character_limit") is not False or account.get("allowed_to_extend_character_limit") is not False:
        raise ValueError("Usage-based billing (overage) may be on, so a call could cost extra money. Turn it off in "
                         "ElevenLabs (Subscription settings) and run this again.")
    left = (account.get("character_limit") or 0) - (account.get("character_count") or 0)
    if left - estimate < RESERVE:
        raise ValueError(f"Only {left:,} credits left; this step needs about {estimate:,} and the job keeps "
                         f"{RESERVE:,} in reserve. Nothing was spent.")


def stock_voice(voice):
    """Ready-made voices only: ElevenLabs default voices, or Voice Library voices added to the account.
    A voice the account made (instant or professional clone, or a designed voice) is refused."""
    if voice.get("category") == "premade":
        return True
    sharing = voice.get("sharing") or {}
    return (voice.get("category") not in ("cloned", "generated") and voice.get("is_owner") is not True
            and sharing.get("status") == "copied")


def multipart(fields, file_field, filename, data, mime):
    boundary = "job-" + uuid.uuid4().hex
    out = io.BytesIO()
    for name, value in fields.items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
              f"Content-Type: {mime}\r\n\r\n".encode())
    out.write(data)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return out.getvalue(), "multipart/form-data; boundary=" + boundary


def duration(path):
    done = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                          check=True, capture_output=True, text=True)
    return float(json.loads(done.stdout)["format"]["duration"])


def settle(client, before, wait=60):
    """ElevenLabs can take a minute to show usage; poll so the record holds the real credit change."""
    after = client.account()
    for _ in range(wait // 5):
        if after["character_count"] != before["character_count"]:
            break
        time.sleep(5)
        after = client.account()
    return after


def paid(client, folder, kind, estimate, confirm, details):
    """Print the estimate and stop, unless confirmed. Returns the account snapshot before spending."""
    before = client.account()
    spending_gate(before, estimate)
    summary = dict(step=kind, estimate_credits=estimate, credits_left=before["credits_left"], plan=before["tier"], **details)
    if not confirm:
        print(json.dumps(dict(summary, spent=0, next="Ask the person running the job. If they say yes, rerun with --confirm."),
                         ensure_ascii=False, indent=1))
        raise SystemExit(0)
    folder.mkdir(parents=True, exist_ok=True)
    return before


def finish(client, folder, record, before, headers=None):
    """Credits used: ElevenLabs' own cost header when it sends one (speech does), else the balance change."""
    headers = headers or {}
    cost = str(headers.get("character-cost", ""))
    after = settle(client, before, wait=0 if cost.isdigit() else 60)
    delta = after["character_count"] - before["character_count"]
    used = int(cost) if cost.isdigit() else (delta or None)
    record.update(ended_at=now(), credits_before=before["character_count"], credits_after=after["character_count"],
                  credits_used=used, credits_left=after["credits_left"], response_headers=headers,
                  credits_source="cost header" if cost.isdigit() else "balance change" if delta else
                  "not shown yet: ElevenLabs reports usage late; check with voice.py account")
    save(folder / "run.json", record)
    print(json.dumps({k: record[k] for k in ("step", "output", "credits_used", "credits_left")}, ensure_ascii=False))


def video_folder(run_dir, name):
    folder = Path(run_dir) / "video" / name
    if not (folder / "audio.mp3").exists():
        raise SystemExit(f"{folder}/audio.mp3 not found. Run first: python3 scripts/video.py prepare {run_dir} input/<video file>")
    return folder


def script_for(run_dir, name, code, short):
    """The checked spoken line for this video and language, from drafts.json."""
    data, drafts = load(Path(run_dir) / "lines.json"), load(Path(run_dir) / "drafts.json")
    spoken = [f for f in data["fields"] if f["surface"] == "spoken" and Path(f["file"]).stem == name]
    if len(spoken) != 1:
        raise SystemExit(f"Expected one spoken script for {name} in media-text.json, found {len(spoken)}.")
    rows = [r for r in drafts["translations"] if r["line_id"] == spoken[0]["line_id"] and r["language"] == code]
    if len(rows) != 1:
        raise SystemExit(f"No {code} translation of the {name} script in drafts.json.")
    text = rows[0]["short_version"] if short else rows[0]["translation"]
    if not text:
        raise SystemExit("No short version saved for this script. Write one in drafts.json first.")
    return text.strip(), spoken[0]


def model_language(client, model_id, code):
    base = code.split("-")[0].lower()
    models = {m["model_id"]: m for m in json.loads(client.call("/v1/models", timeout=60)[0])}
    if model_id not in models or not models[model_id].get("can_do_text_to_speech"):
        raise SystemExit(f"Model {model_id} is not available for text to speech on this account.")
    languages = {l.get("language_id", "").lower() for l in models[model_id].get("languages") or []}
    if languages and base not in languages:
        raise SystemExit(f"Model {model_id} does not list language '{base}'. Pick another model with --model "
                         f"(see https://elevenlabs.io/docs/overview/models).")
    return base


def cmd_voices(client, args):
    code = args[args.index("--language") + 1] if "--language" in args else None
    base = code.split("-")[0].lower() if code else None
    found = []
    for kind in ("default", "community"):
        token = ""
        while True:
            page = client.json(f"/v2/voices?voice_type={kind}&page_size=100" + (f"&next_page_token={token}" if token else ""))
            for voice in page.get("voices", []):
                langs = {l.get("language") for l in voice.get("verified_languages") or []} | {(voice.get("labels") or {}).get("language")}
                if stock_voice(voice) and (not base or base in langs):
                    labels = voice.get("labels") or {}
                    found.append(dict(voice_id=voice["voice_id"], name=voice.get("name"), kind=kind,
                                      accent=labels.get("accent"), gender=labels.get("gender"), use_case=labels.get("use_case"),
                                      preview=voice.get("preview_url")))
            token = page.get("next_page_token")
            if not page.get("has_more") or not token:
                break
    print(json.dumps(dict(language=code, stock_voices=found, note=(
        "Listen to the previews and pick one. Default voices are being replaced by ElevenLabs; if one disappears, "
        "add a ready-made voice from the Voice Library (elevenlabs.io/app/voice-library) and run this again.")),
        ensure_ascii=False, indent=1))


def cmd_transcribe(client, run_dir, name, confirm):
    folder = video_folder(run_dir, name)
    audio = folder / "audio.mp3"
    minutes = duration(audio) / 60
    estimate = math.ceil(minutes * CREDITS["stt_per_minute"])
    before = paid(client, folder, "transcribe", estimate, confirm, dict(minutes=round(minutes, 2)))
    body, kind = multipart({"model_id": STT_MODEL, "timestamps_granularity": "word"}, "file", "audio.mp3",
                           audio.read_bytes(), "audio/mpeg")
    record = dict(step="transcribe", started_at=now(), model=STT_MODEL, output="transcript.json")
    raw, headers = client.call("/v1/speech-to-text", body, kind)
    result = json.loads(raw)
    save(folder / "transcript.json", result)
    (folder / "transcript.txt").write_text(result.get("text", "").strip() + "\n", encoding="utf-8")
    finish(client, folder, record, before, headers)


def cmd_stems(client, run_dir, name, confirm):
    folder = video_folder(run_dir, name)
    audio = folder / "audio.mp3"
    minutes = duration(audio) / 60
    estimate = math.ceil(minutes * CREDITS["stems_per_minute_upper_bound"])
    out = folder / "stems"
    if (out / "instrumental.mp3").exists():
        raise SystemExit(f"{out}/instrumental.mp3 already exists; nothing to do (delete the folder to redo it).")
    before = paid(client, out, "stems", estimate, confirm, dict(minutes=round(minutes, 2), estimate_basis="upper bound"))
    body, kind = multipart({"stem_variation_id": "two_stems_v1"}, "file", "audio.mp3", audio.read_bytes(), "audio/mpeg")
    record = dict(step="stems", started_at=now(), output="stems/instrumental.mp3")
    raw, headers = client.call("/v1/music/stem-separation?output_format=mp3_44100_128", body, kind, timeout=600)
    (out / "stems.zip").write_bytes(raw)
    with zipfile.ZipFile(out / "stems.zip") as archive:
        names = archive.namelist()
        for entry in names:
            if "/" in entry or "\\" in entry or ".." in entry or not entry.lower().endswith(".mp3"):
                continue
            target = "vocals.mp3" if "vocal" in entry.lower() else "instrumental.mp3" if "instrument" in entry.lower() else None
            if target:
                (out / target).write_bytes(archive.read(entry))
    record["zip_entries"] = names
    if not (out / "instrumental.mp3").exists():
        record["status"] = "no instrumental stem found in the response"
    finish(client, out, record, before, headers)


def cmd_speak(client, run_dir, name, code, args, confirm):
    markets = {m["code"]: m for m in load(Path(run_dir) / "lines.json")["markets"]}
    if code not in markets:
        raise SystemExit(f"{code} is not a language in this run.")
    voice_id = args[args.index("--voice") + 1] if "--voice" in args else markets[code].get("voice_id")
    if not voice_id:
        raise SystemExit(f"No voice chosen for {code}. Run: python3 scripts/voice.py voices --language {code}, pick one, "
                         "and save its voice_id in config/markets.json (or pass --voice ID).")
    voice = client.json(f"/v1/voices/{voice_id}")
    if not stock_voice(voice):
        raise SystemExit(f"Voice {voice.get('name')} ({voice.get('category')}) is not a ready-made stock voice. "
                         "This job never uses cloned or account-made voices.")
    model_id = args[args.index("--model") + 1] if "--model" in args else TTS_MODEL
    language = model_language(client, model_id, code)
    text, spoken = script_for(run_dir, name, code, "--short" in args)
    if len(text) > 5000:
        raise SystemExit("The script is longer than 5,000 characters; split the video first.")
    folder = Path(run_dir) / "video" / name / code
    estimate = math.ceil(len(text) * CREDITS["tts_per_character"])
    before = paid(client, folder, "speak", estimate, confirm, dict(characters=len(text), voice=voice.get("name"),
                                                                   model=model_id, language=code))
    record = dict(step="speak", started_at=now(), output=f"{code}/narration.wav", label=LABEL, model=model_id,
                  voice=dict(voice_id=voice_id, name=voice.get("name"), category=voice.get("category"),
                             sharing_status=(voice.get("sharing") or {}).get("status")),
                  script=text, script_characters=len(text), variant="short" if "--short" in args else "full",
                  line_id=spoken["line_id"], cloning="none: stock voice")
    # One request per sentence, so every sentence starts and ends in its own silence and the dub never has to cut a
    # take mid-word. Request stitching keeps the voice consistent from sentence to sentence: previous_request_ids
    # (up to 3, from each response's request-id header) and next_text, per
    # https://elevenlabs.io/docs/developers/guides/cookbooks/text-to-speech/request-stitching. The with-timestamps
    # route also returns when each character is spoken, for placing each sentence where the English one starts.
    parts = [p for p in re.split(r"(?<=[.!?…])\s+", text) if p]
    clips_dir = folder / "sentences"
    clips_dir.mkdir(parents=True, exist_ok=True)
    ids, cost, clips = [], 0, []
    for i, part in enumerate(parts):
        payload = {"text": part, "model_id": model_id, "language_code": language}
        if ids:
            payload["previous_request_ids"] = ids[-3:]
        if i + 1 < len(parts):
            payload["next_text"] = parts[i + 1]
        raw, headers = client.call(f"/v1/text-to-speech/{voice_id}/with-timestamps?output_format=mp3_44100_128",
                                   json.dumps(payload, ensure_ascii=False).encode(), "application/json")
        result = json.loads(raw or b"{}")
        if not result.get("audio_base64") or not result.get("alignment"):
            raise SystemExit(f"ElevenLabs returned no audio or timings for sentence {i + 1}.")
        clip = clips_dir / f"{i + 1:02d}.mp3"
        clip.write_bytes(base64.b64decode(result["audio_base64"]))
        if headers.get("request-id"):
            ids.append(headers["request-id"])
        cost += int(headers["character-cost"]) if str(headers.get("character-cost", "")).isdigit() else 0
        clips.append(dict(text=part, file=f"sentences/{clip.name}", seconds=round(duration(clip), 3),
                          request_id=headers.get("request-id"), credits=headers.get("character-cost"),
                          alignment=result["alignment"]))
    # Join the clips with real silence between them (0.4 s, and 0.3 s at both ends) and merge their timings.
    pad, gap, at, chars, starts, ends, delays = 0.3, 0.4, 0.3, [], [], [], []
    for i, clip in enumerate(clips):
        if i:
            chars.append(" ")
            starts.append(ends[-1])
            ends.append(at + clip["alignment"]["character_start_times_seconds"][0])
        chars += clip["alignment"]["characters"]
        starts += [at + t for t in clip["alignment"]["character_start_times_seconds"]]
        ends += [at + t for t in clip["alignment"]["character_end_times_seconds"]]
        delays.append(int(round(at * 1000)))
        at += clip["seconds"] + gap
    inputs = [str(folder / c["file"]) for c in clips]
    mix = ";".join(f"[{i}:a]aformat=sample_rates=44100:channel_layouts=mono,adelay={d}:all=1[c{i}]" for i, d in enumerate(delays))
    mix += ";" + "".join(f"[c{i}]" for i in range(len(clips))) + f"amix=inputs={len(clips)}:duration=longest:normalize=0,apad=pad_dur={pad}[out]"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y"] + [x for f in inputs for x in ("-i", f)] +
                   ["-filter_complex", mix, "-map", "[out]", "-c:a", "pcm_s16le", str(folder / "narration.wav")], check=True)
    save(folder / "alignment.json", dict(characters=chars, character_start_times_seconds=starts, character_end_times_seconds=ends))
    (folder / "script.txt").write_text(text + "\n", encoding="utf-8")
    record.update(narration_seconds=round(duration(folder / "narration.wav"), 3), sentences=[
        {k: v for k, v in c.items() if k != "alignment"} for c in clips], stitching="previous_request_ids + next_text")
    headers = {"character-cost": str(cost)} if cost else {}
    finish(client, folder, record, before, headers)


def main(argv):
    if not argv or argv[0] not in ("account", "voices", "transcribe", "stems", "speak"):
        raise SystemExit(__doc__)
    client = Client(api_key())
    step, confirm = argv[0], "--confirm" in argv
    try:
        if step == "account":
            print(json.dumps(client.account(), indent=1))
        elif step == "voices":
            cmd_voices(client, argv)
        else:
            if len(argv) < 2 or "--video" not in argv:
                raise SystemExit(f"Usage: python3 scripts/voice.py {step} output/RUN --video NAME ...")
            run_dir, name = argv[1], argv[argv.index("--video") + 1]
            if step == "transcribe":
                cmd_transcribe(client, run_dir, name, confirm)
            elif step == "stems":
                cmd_stems(client, run_dir, name, confirm)
            else:
                if "--language" not in argv:
                    raise SystemExit("Add --language CODE (for example --language fr).")
                cmd_speak(client, run_dir, name, argv[argv.index("--language") + 1], argv, confirm)
    except ValueError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    use_venv()
    main(sys.argv[1:])
