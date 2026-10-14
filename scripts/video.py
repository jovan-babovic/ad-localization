"""Video steps with ffmpeg. No paid calls here (the paid ones are in voice.py).

  python3 scripts/video.py prepare output/RUN input/VIDEO.mp4 [--every 2]
      Saves the audio, a frame every 2 seconds (for reading on-screen text) and the English transcript
      if input/VIDEO.transcript.txt exists.
  python3 scripts/video.py dub output/RUN --video NAME --language fr [--music-volume 1.0] [--starts 0.1,6.8,...]
      Lays the recorded translation over the original music, each sentence where the English one starts.
      The picture is copied untouched and the sound is cut to the picture's length.
  python3 scripts/video.py screen output/RUN --video NAME --language fr
      Replaces on-screen English with the checked translation in every frame it shows, including while it
      zooms, types in or fades, then reads every changed frame back to check no English is left.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job import LABEL, ROOT, load, now, option as value_of, save, use_venv  # noqa: E402

MAX_TEMPO = 1.15
SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def tools():
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        raise SystemExit(f"Not installed: {', '.join(missing)}. Install ffmpeg (https://ffmpeg.org/download.html) and run again.")


def run(args):
    done = subprocess.run([str(a) for a in args], capture_output=True, text=True)
    if done.returncode:
        raise SystemExit(f"{Path(str(args[0])).name} failed: {done.stderr.strip()[-800:]}")
    return done.stdout


def probe(path):
    data = json.loads(run(["ffprobe", "-v", "error", "-show_entries",
                           "stream=codec_type,width,height,r_frame_rate,duration,nb_frames:format=duration", "-of", "json", path]))
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if not video:
        raise SystemExit(f"{path} has no video stream.")
    num, den = (int(x) for x in video["r_frame_rate"].split("/"))
    fps = num / den if den else None
    # The picture's own length: some files carry sound a few frames longer than the picture.
    if video.get("duration") not in (None, "N/A"):
        picture = float(video["duration"])
    elif video.get("nb_frames") not in (None, "N/A") and fps:
        picture = int(video["nb_frames"]) / fps
    else:
        picture = float(data["format"]["duration"])
    return dict(duration=round(float(data["format"]["duration"]), 3), picture_seconds=round(picture, 3),
                width=video["width"], height=video["height"], fps=round(fps, 3) if fps else None,
                rate=video["r_frame_rate"], has_audio=any(s["codec_type"] == "audio" for s in data["streams"]))


def stream_hash(path, stream):
    return run(["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", f"0:{stream}:0", "-c", "copy",
                "-f", "hash", "-hash", "sha256", "-"]).strip()


def seconds(path):
    return float(json.loads(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path]))["format"]["duration"])


def loudness(path):
    """Integrated loudness in LUFS (EBU R128), as ffmpeg measures it."""
    done = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
                          capture_output=True, text=True)
    found = re.findall(r"I:\s+(-?[\d.]+) LUFS", done.stderr)
    return float(found[-1]) if found else None


def fit(narration_seconds, video_seconds, start):
    """Tempo needed so the voice ends before the video does. Over 15% faster sounds rushed: shorten the words."""
    room = video_seconds - start - 0.2
    if room <= 0:
        raise ValueError("The start offset leaves no room for the voice.")
    tempo = max(1.0, narration_seconds / room)
    if tempo > MAX_TEMPO:
        raise ValueError(f"The voice runs {narration_seconds:.1f}s but the video leaves {room:.1f}s: it would need "
                         f"{(tempo - 1) * 100:.0f}% faster speech (limit {(MAX_TEMPO - 1) * 100:.0f}%). "
                         "Use a shorter version of the script (voice.py speak --short) and record again.")
    return round(tempo, 4)


# ---------- sentence alignment ----------

def sentences(text):
    return [s for s in SENTENCE_END.split(text.strip()) if s]


def speech_spans(path, noise=-40, gap=0.2):
    """Where someone speaks in a voice-only track: the stretches between silences of at least `gap` seconds."""
    log = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-af", f"silencedetect=noise={noise}dB:d={gap}",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
    total = seconds(path)
    spans, t = [], 0.0
    for n, s in enumerate(starts):
        if s - t > 0.05:
            spans.append((round(max(t, 0.0), 3), round(s, 3)))
        t = ends[n] if n < len(ends) else total
    if total - t > 0.05:
        spans.append((round(t, 3), round(total, 3)))
    return spans


def group_spans(spans, sentence_chars):
    """Group speech stretches into sentences: contiguous groups whose lengths best follow each sentence's share of
    the characters, preferring cuts at longer pauses. Returns each sentence's start time, or None."""
    n, m = len(sentence_chars), len(spans)
    if n == 0 or m < n:
        return None
    speech = spans[-1][1] - spans[0][0]
    expected = [speech * c / sum(sentence_chars) for c in sentence_chars]
    inf = float("inf")
    best = [[inf] * (m + 1) for _ in range(n + 1)]
    best[0][0], back = 0.0, {}
    for i in range(1, n + 1):
        for k in range(i, m + 1):
            for j in range(i - 1, k):
                if best[i - 1][j] == inf:
                    continue
                length = spans[k - 1][1] - spans[j][0]
                cost = ((length - expected[i - 1]) / max(expected[i - 1], 0.5)) ** 2
                if j > 0:
                    cost -= 0.5 * min(spans[j][0] - spans[j - 1][1], 1.0)  # a cut at a long pause is likely a sentence end
                if best[i - 1][j] + cost < best[i][k]:
                    best[i][k], back[i, k] = best[i - 1][j] + cost, j
    if best[n][m] == inf:
        return None
    starts, k = [], m
    for i in range(n, 0, -1):
        k = back[i, k]
        starts.append(spans[k][0])
    return starts[::-1]


def english_starts(folder, english, override):
    """When each English sentence starts: given by the person (--starts), from a paid word-timed transcript
    (voice.py transcribe), or measured on the separated English voice (stems/vocals.mp3)."""
    parts = sentences(english)
    if override:
        starts = [float(x) for x in override.split(",") if x.strip()]
        return (starts, "given with --starts") if len(starts) == len(parts) else (None, "--starts count differs from the sentences")
    timed = folder / "transcript.json"
    if timed.exists():
        heard = [w for w in load(timed).get("words", []) if w.get("type", "word") == "word"]
        counts = [len(p.split()) for p in parts]
        if len(heard) == sum(counts):
            idx = [sum(counts[:i]) for i in range(len(parts))]
            return [round(heard[i]["start"], 3) for i in idx], "word-timed transcript"
    vocals = folder / "stems" / "vocals.mp3"
    if vocals.exists():
        starts = group_spans(speech_spans(vocals), [len(p) for p in parts])
        if starts:
            return starts, "pauses in the separated English voice (stems/vocals.mp3)"
    return None, "no sentence timing found"


def sentence_times(alignment, text):
    """Start and end of each sentence in a recorded take, from ElevenLabs' character timings."""
    joined = "".join(alignment["characters"])
    times, pos = [], 0
    for part in sentences(text):
        i = joined.find(part, pos)
        if i < 0:
            return None
        j = i + len(part) - 1
        times.append((alignment["character_start_times_seconds"][i], alignment["character_end_times_seconds"][j]))
        pos = j + 1
    return times


def place(spoken, starts, picture, lead=0.05, tail=0.1, gap=0.1):
    """Lay each recorded sentence so its first word lands where the English sentence starts. A sentence that is
    longer than its English slot is sped up, by at most 15%, so the next one can still start on time; past that it
    moves the next one later. Cuts are times in the recording; each piece carries its own tempo.
    Returns (highest tempo, pieces) or raises ValueError when the last sentence runs past the picture."""
    pieces, prev_end = [], 0.0
    for i, ((a, b), start) in enumerate(zip(spoken, starts)):
        cut_in = max(a - lead, (spoken[i - 1][1] + a) / 2 if i else 0.0)
        cut_out = min(b + tail, (b + spoken[i + 1][0]) / 2) if i + 1 < len(spoken) else b + tail
        at = max(start - (a - cut_in), prev_end + gap if i else 0.0)
        room = (starts[i + 1] - lead - gap if i + 1 < len(spoken) else picture - 0.1) - at
        tempo = round(min(MAX_TEMPO, max(1.0, (cut_out - cut_in) / room)) if room > 0 else MAX_TEMPO, 3)
        prev_end = at + (cut_out - cut_in) / tempo
        pieces.append(dict(take_from=round(cut_in, 3), take_to=round(cut_out, 3), at=round(at, 3), tempo=tempo,
                           english_start=start, drift=round(at + (a - cut_in) / tempo - start, 2)))
    if prev_end > picture - 0.1:
        raise ValueError("The translated voice does not fit the picture even 15% faster. Use a shorter version of the "
                         "script (voice.py speak --short) and record again.")
    return max(p["tempo"] for p in pieces), pieces


def speech_bounds(lv, spoken, step=0.01, below=35):
    """Narrow each sentence's (start, end) to where it actually sounds: character timings can run on into silence."""
    out = []
    for a, b in spoken:
        window = lv[int(a / step):int(b / step) + 1]
        loud = [k for k, v in enumerate(window) if v > max(window, default=-120) - below]
        out.append((a + loud[0] * step, a + (loud[-1] + 1) * step) if loud else (a, b))
    return out


def levels(path, step=0.01):
    """Loudness of a mono decode in 10 ms steps, as dB (RMS against full scale; digital silence is -120)."""
    from array import array
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    samples, n = array("h"), int(16000 * step)
    samples.frombytes(raw[:len(raw) - len(raw) % 2])
    out = []
    for i in range(0, len(samples), n):
        chunk = samples[i:i + n]
        power = sum(v * v for v in chunk) / max(1, len(chunk)) / 32768 ** 2
        out.append(10 * math.log10(power) if power > 1e-12 else -120.0)
    return out


def level_db(lv, a, b, step=0.01):
    """Average loudness (power mean, dB) of the 10 ms steps between a and b seconds."""
    window = lv[max(0, int(round(a / step))):max(0, int(round(b / step))) + 1] or [-120.0]
    power = sum(10 ** (v / 10) for v in window) / len(window)
    return 10 * math.log10(power) if power > 1e-12 else -120.0


def clean_cuts(lv, pieces, reach=0.15, edge=0.03, margin=30.0):
    """Move every cut to the quietest point within 150 ms (measured over +-30 ms, nearest first), shifting the piece so
    its words keep their place, then check each piece: its edges must be at least 30 dB below its own average, or a
    word is being cut (heard as a click or a clipped syllable). Raises ValueError naming the piece."""
    edge_level = lambda t: level_db(lv, t - edge, t + edge)  # noqa: E731
    out = []
    for n, piece in enumerate(pieces, 1):
        piece = dict(piece)
        for key in ("take_from", "take_to"):
            planned = piece[key]
            options = [planned + k * 0.01 for k in range(-int(reach / 0.01), int(reach / 0.01) + 1) if planned + k * 0.01 >= 0]
            best = min(options, key=lambda t: (round(edge_level(t), 1), abs(t - planned)))
            if key == "take_from":
                piece["at"] = round(max(0.0, piece["at"] + (best - planned) / piece["tempo"]), 3)
            piece[key] = round(best, 3)
        start, end = piece["take_from"], piece["take_to"]
        average = level_db(lv, start, end)
        piece["levels_db"] = dict(average=round(average, 1), edge_in=round(edge_level(start), 1), edge_out=round(edge_level(end), 1))
        cuts = [piece["levels_db"]["edge_in"]] if start > 0.0 else []  # the take's own start and end are not cuts
        cuts += [piece["levels_db"]["edge_out"]] if end < len(lv) * 0.01 - 0.02 else []
        if cuts and max(cuts) > average - margin:
            raise ValueError(f"Sentence {n} would be cut mid-sound: its edges are {piece['levels_db']['edge_in']} and "
                             f"{piece['levels_db']['edge_out']} dB against an average of {round(average, 1)} dB (they must be "
                             f"{margin:.0f} dB quieter). Record it again with voice.py speak (one clip per sentence).")
        out.append(piece)
    return out


def cmd_dub(run_dir, name, code, music_volume, starts_override):
    folder = Path(run_dir) / "video" / name
    video = source_video(run_dir, name)
    narration, music = folder / code / "narration.wav", folder / "stems" / "instrumental.mp3"
    for path, how in ((narration, f"voice.py speak {run_dir} --video {name} --language {code}"),
                      (music, f"voice.py stems {run_dir} --video {name}")):
        if not path.exists():
            raise SystemExit(f"{path} not found. Run first: python3 scripts/{how}")
    if not 0 <= music_volume <= 2:
        raise SystemExit("--music-volume must be between 0 and 2 (1 keeps the music as loud as in the original).")
    info = probe(video)
    picture = info["picture_seconds"]
    take = seconds(narration)
    script = (folder / code / "script.txt").read_text(encoding="utf-8").strip()
    english = (folder / "transcript.txt").read_text(encoding="utf-8").strip() if (folder / "transcript.txt").exists() else ""
    starts, starts_from = english_starts(folder, english, starts_override) if english else (None, "no English transcript")
    alignment = load(folder / code / "alignment.json") if (folder / code / "alignment.json").exists() else None
    lv = levels(narration)
    spoken = sentence_times(alignment, script) if alignment else None
    spoken = speech_bounds(lv, spoken) if spoken else None
    aligned = bool(starts and spoken and len(starts) == len(spoken))
    try:
        if aligned:
            tempo, pieces = place(spoken, starts, picture)
            method = f"each of {len(pieces)} sentences placed where the English one starts ({starts_from})"
        else:
            first = starts[0] if starts else 0.3
            tempo = fit(take, picture, first)
            pieces = [dict(take_from=0.0, take_to=round(take, 3), at=first, tempo=tempo, english_start=first, drift=0.0)]
            method = ("one take from the first English word: " + (
                f"{len(spoken)} recorded sentences vs {len(starts)} English" if starts and spoken else
                "no character timings for the recording (record it again with voice.py speak)" if not spoken else starts_from))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    try:
        pieces = clean_cuts(lv, pieces)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    english_voice = loudness(folder / "stems" / "vocals.mp3") if (folder / "stems" / "vocals.mp3").exists() else None
    target = english_voice if english_voice is not None else -16.0
    gain = round(target - (loudness(narration) or target), 2)  # the new voice as loud as the English voice was
    source_level = loudness(video) if info["has_audio"] else None
    out = folder / code / "dubbed.mp4"
    split = f"[1:a]asplit={len(pieces)}" + "".join(f"[s{i}]" for i in range(len(pieces)))
    parts = [f"[s{i}]atrim=start={p['take_from']}:end={p['take_to']},asetpts=PTS-STARTPTS,atempo={p['tempo']},afade=t=in:d=0.01,"
             f"afade=t=out:st={max(0.0, round((p['take_to'] - p['take_from']) / p['tempo'] - 0.02, 3))}:d=0.02,"
             f"adelay=delays={int(p['at'] * 1000)}:all=1[p{i}]" for i, p in enumerate(pieces)]
    voice_mix = "".join(f"[p{i}]" for i in range(len(pieces))) + f"amix=inputs={len(pieces)}:duration=longest:normalize=0,volume={gain}dB[voice]"
    correction = 0.0
    for _ in range(2):
        filters = ";".join([split] + parts + [voice_mix, f"[2:a]volume={music_volume}[music]",
                                              f"[voice][music]amix=inputs=2:duration=longest:normalize=0,volume={correction}dB,"
                                              "alimiter=limit=0.89:level=false[mix]"])
        run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", video, "-i", narration, "-i", music, "-filter_complex", filters,
             "-map", "0:v:0", "-map", "[mix]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
             "-t", picture, "-movflags", "+faststart", out])
        level = loudness(out)
        if source_level is None or level is None or abs(level - source_level) <= 1.0:
            break
        correction = round(correction + source_level - level, 2)  # match the original's overall loudness
    made = probe(out)
    same_picture = stream_hash(video, "v") == stream_hash(out, "v")
    speak = load(folder / code / "run.json") if (folder / code / "run.json").exists() else {}
    drifted = [i + 1 for i, p in enumerate(pieces) if abs(p["drift"]) > 0.6]
    record = dict(label=LABEL, made_at=now(), output=f"{code}/dubbed.mp4", video_seconds=picture, picture_seconds=picture,
                  container_seconds=info["duration"], output_seconds=made["duration"], voice_seconds=round(take, 3),
                  tempo=tempo, faster_percent=round((tempo - 1) * 100, 1), alignment=method, pieces=pieces,
                  sentences_moved_late=drifted, loudness_lufs=dict(original=source_level, english_voice=english_voice,
                                                                     output=level, voice_gain_db=gain, final_correction_db=correction),
                  music_volume=music_volume, picture_unchanged=same_picture, voice=speak.get("voice"),
                  script_variant=speak.get("variant"),
                  still_to_check=["Watch and listen: timing against the picture, pronunciation of names, any English "
                                  "left in the music"] + ([f"Sentence(s) {drifted} start more than 0.6 s after the English one"]
                                                         if drifted else []) + ["Native-speaker review"])
    save(folder / code / "dub.json", record)
    if not same_picture:
        raise SystemExit("The picture changed during mixing; do not use this file.")
    if made["duration"] > picture + 1.5 / (info["fps"] or 30):
        raise SystemExit(f"The dub runs {made['duration']}s, longer than the picture ({picture}s).")
    print(json.dumps({k: record[k] for k in ("output", "picture_seconds", "output_seconds", "faster_percent", "alignment",
                                             "sentences_moved_late", "loudness_lufs", "picture_unchanged")}, ensure_ascii=False))


# ---------- text on screen ----------

def overlap(a, b):
    """Share of the smaller box covered by the other one."""
    w = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    h = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return w * h / max(1, min(a[2] * a[3], b[2] * b[3]))


def reveal(english, shown, translation, names):
    """How much of the translation to show while the English is typing in. Positions map piece by piece between
    the names both lines share (so "Introducing" alone shows the word before "Field Mode", not half a name), and
    a whole English word shown means whole translated words."""
    points, e_at, t_at = [(0, 0)], 0, 0
    for name in sorted({n for n in names if n in english and n in translation}, key=english.index):
        e, t = english.find(name, e_at), translation.find(name, t_at)
        if e < 0 or t < 0:
            continue
        points += [(e, t), (e + len(name), t + len(name))]
        e_at, t_at = e + len(name), t + len(name)
    points.append((len(english), len(translation)))
    n = len(shown)
    for (e0, t0), (e1, t1) in zip(points, points[1:]):
        if e0 <= n <= e1:
            pos = t0 + (t1 - t0) * ((n - e0) / (e1 - e0) if e1 > e0 else 1)
            break
    if n >= len(english) or english[n:n + 1] == " ":  # the English shows whole words: so does the translation
        ends = [i for i in range(len(translation) + 1) if i == len(translation) or translation[i] == " "]
        pos = min(ends, key=lambda i: abs(i - pos))
    return translation[:int(round(pos))].rstrip()


def word_clusters(cols, gap):
    """Runs of inked columns split by gaps of at least `gap` pixels: one run per visible word."""
    runs, start, last = [], None, None
    for c in cols:
        if start is None:
            start = last = c
        elif c - last > gap:
            runs.append((start, last))
            start = last = c
        else:
            last = c
    return runs + ([(start, last)] if start is not None else [])


def ocr_frames(paths):
    if not shutil.which("swift"):
        raise SystemExit("Text on screen needs macOS (Apple's text recognition). On a Mac, swift comes with the Xcode "
                         "Command Line Tools: xcode-select --install")
    swift = Path(__file__).with_name("ocr_lines.swift")
    out = json.loads(run(["swift", swift] + [str(p) for p in paths]))
    return {Path(f["file"]).name: f["lines"] for f in out["frames"]} if "frames" in out else {Path(paths[0]).name: out["lines"]}


def cmd_screen(run_dir, name, code):
    """Every frame the English shows in is re-drawn in the target language with the same size, place, letters
    shown and fade, measured on that frame. Works for one-line text on a plain background (titles, cards);
    text over footage is flagged and left alone."""
    import numpy as np
    from PIL import Image, ImageDraw
    import reset_image_text as tool
    from job import words
    run_dir = Path(run_dir)
    items = [i for i in load(run_dir / "media-text.json")["items"] if i["surface"] == "screen" and Path(i["file"]).stem == name]
    if not items:
        raise SystemExit(f"No on-screen text items for {name} in media-text.json.")
    for item in items:
        if not all(k in item for k in ("start", "end", "box")):
            raise SystemExit(f"Screen item {item.get('id')} needs start, end and box [x, y, w, h].")
    source = source_video(run_dir, name)
    info = probe(source)
    fps, W, H = info["fps"], info["width"], info["height"]
    total = int(round(info["picture_seconds"] * fps))
    folder = run_dir / "video" / name / code
    base = folder / "dubbed.mp4" if (folder / "dubbed.mp4").exists() else source
    work = folder / "screen"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    checked = words(run_dir, name, code, surface="screen")
    names = load(run_dir / "lines.json").get("never_translate", [])
    fonts = ROOT / "config" / "fonts.txt"
    edits, records = {}, []
    for item in items:
        record = dict(id=item["id"], text=item["text"])
        records.append(record)
        f0, f1 = max(0, int((item["start"] - 0.5) * fps)), min(total - 1, int(math.ceil((item["end"] + 0.5) * fps)))
        frames = work / item["id"]
        frames.mkdir()
        run(["ffmpeg", "-nostdin", "-v", "error", "-i", source, "-vf", f"select='between(n\\,{f0}\\,{f1})'", "-fps_mode",
             "passthrough", "-start_number", f0, "-frames:v", f1 - f0 + 1, frames / "f%05d.png"])
        paths = sorted(frames.glob("f*.png"))
        ocr = ocr_frames(paths)
        ref_n = min(max(int(round(item.get("frame", (item["start"] + item["end"]) / 2) * fps)), f0), f1)
        boxes = work / f"{item['id']}.boxes.json"
        tool.detect(frames / f"f{ref_n:05d}.png", boxes, [l for l in fonts.read_text().splitlines() if l.strip()] if fonts.exists() else None)
        found = load(boxes)["blocks"]
        block = max(found, key=lambda b: overlap(b["rect"], item["box"])) if found else None
        if not block or overlap(block["rect"], item["box"]) < 0.3 or len(block["lines"]) != 1:
            record["status"] = "FLAGGED: not one line of text in its box on the reference frame; left in English"
            continue
        english, entry = item["text"], checked["blocks"][item["id"]]
        align = item.get("align", block["align"])
        size, tracking = block["font_size"], block.get("tracking", 0.0)
        ink = np.array([int(block["colour"][i:i + 2], 16) for i in (1, 3, 5)], dtype=float)
        rect = block["rect"]
        allowed = item.get("max_width", rect[2])

        def ink_extent(text, font, track):
            width = tool.draw_line(None, (0, 0), text, font, None, track)
            return font.getbbox(text[0], anchor="ls")[0], width - font.getlength(text[-1]) + font.getbbox(text[-1], anchor="ls")[2]

        ref_font = tool.load_font(block["font"], size)
        variant, text, fit_scale = None, None, None
        for key in ("text", "short"):
            if entry.get(key):
                left, right = ink_extent(entry[key], ref_font, tracking)
                variant, text, fit_scale = key, entry[key], min(1.0, allowed / max(1.0, right - left))
                if fit_scale >= tool.MIN_SCALE:
                    break
        record.update(translation=text, variant=variant, type_size=f"{round(fit_scale * 100)}% of the English")
        if fit_scale < tool.MIN_SCALE:
            record["status"] = f"FLAGGED: set at {round(fit_scale * 100)}% type size to fit; needs shorter copy"
        grow_x, grow_y = rect[2] * 0.6, rect[3] * 1.2  # room for text that zooms in from larger
        search = tool.clip([int(rect[0] - grow_x), int(rect[1] - grow_y), int(rect[2] + 2 * grow_x), int(rect[3] + 2 * grow_y)], (H, W))
        sx, sy, sw, sh = search
        english_words, shown_before, seen, skipped = english.split(), "", {}, []

        def measure(path):
            """The English line on one frame: ink box, letters shown, fade per word. None when it is not there."""
            img = np.asarray(Image.open(edits.get(int(path.stem[1:]), path)).convert("RGB"), dtype=np.float64)
            band = np.concatenate([img[sy, sx:sx + sw], img[sy + sh - 1, sx:sx + sw], img[sy:sy + sh, sx], img[sy:sy + sh, sx + sw - 1]])
            bg_colour = np.median(band, axis=0)
            if np.abs(band - bg_colour).mean() > 8:  # footage or a busy background: the English cannot be measured here
                if any(overlap(l["rect"], search) > 0.5 and len(l["text"].strip()) >= 4 and l["text"].strip() in english
                       for l in ocr.get(path.name, [])):
                    skipped.append(int(path.stem[1:]))
                return None
            bg, _ = tool.background(img, search, pad=3)
            axis = ink - bg_colour
            alpha = np.clip(((img[sy:sy + sh, sx:sx + sw] - bg) * axis).sum(axis=2) / max(1.0, (axis ** 2).sum()), 0, 1.5)
            mask = alpha > 0.08
            if mask.sum() < 20 and (alpha > 0.025).sum() > 0.002 * alpha.size:  # the last, faintest frames of a fade
                mask = alpha > 0.025
            rows, cols = np.nonzero(mask.sum(axis=1) >= 2)[0], np.nonzero(mask.sum(axis=0) >= 2)[0]
            if not len(rows) or not len(cols):
                return None
            box = [sx + cols.min(), sy + rows.min(), cols.max() - cols.min() + 1, rows.max() - rows.min() + 1]
            read = [l["text"].strip() for l in ocr.get(path.name, []) if overlap(l["rect"], box) > 0.3]
            read = next((r for r in read if r and english.startswith(r)), None)
            runs = word_clusters(cols, max(3, 0.18 * box[3]))
            return dict(img=img, bg=bg, bg_colour=bg_colour, axis=axis, alpha=alpha, mask=mask, box=box, read=read, runs=runs)

        found_on = {}
        for path in paths:
            m = measure(path)
            if not m:
                continue
            read, runs = m["read"], m["runs"]
            if read and not english[len(read):len(read) + 1].strip():  # whole words shown (OCR may miss one faint new word)
                shown = " ".join(english_words[:min(len(english_words), max(len(read.split()), min(len(runs), len(read.split()) + 1)))])
            elif read:
                shown = read  # letter-by-letter typing
            else:
                shown = shown_before or " ".join(english_words[:min(len(runs), len(english_words))])
            shown_before = shown
            alpha, mask = m["alpha"], m["mask"]
            if len(runs) == len(shown.split()):  # runs are columns inside the search box, like alpha and mask
                fades = [float(np.percentile(alpha[:, a:b + 1][mask[:, a:b + 1]], 97)) for a, b in runs]
            else:
                fades = [float(np.percentile(alpha[mask], 97))] * len(shown.split())
            found_on[int(path.stem[1:])] = dict(box=m["box"], read=bool(read), shown=shown, fades=fades)
        # Keep only unbroken runs of frames that OCR confirmed at least once, so a logo fading in next is left alone.
        keep, run_frames = [], []
        for n in sorted(found_on) + [None]:
            if run_frames and (n is None or n != run_frames[-1] + 1):
                if any(found_on[f]["read"] for f in run_frames):
                    keep += run_frames
                run_frames = []
            if n is not None:
                run_frames.append(n)
        done = []
        for n in keep:
            path, f = frames / f"f{n:05d}.png", found_on[n]
            m = measure(path)
            img, bg, bg_colour, axis = m["img"], m["bg"], m["bg_colour"], m["axis"]
            bx, by, bw, bh = f["box"]
            shown, fades = f["shown"], f["fades"]
            l0, t0, r0, b0 = ref_font.getbbox(shown, anchor="ls")
            scale = bh / max(1, b0 - t0)
            baseline = by - t0 * scale
            visible = reveal(english, shown, text, names)
            pad = int(max(4, 0.3 * bh))
            ex, ey, ew, eh = tool.clip([bx - pad, by - pad, bw + 2 * pad, bh + 2 * pad], img.shape)
            img[ey:ey + eh, ex:ex + ew] = bg[ey - sy:ey - sy + eh, ex - sx:ex - sx + ew] if (sx <= ex and sy <= ey and ex + ew <= sx + sw
                                                                                             and ey + eh <= sy + sh) else bg_colour
            canvas = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
            if visible:
                k = scale * fit_scale
                font = tool.load_font(block["font"], size * k)
                left, right = ink_extent(visible, font, tracking * k)
                x = (bx + bw / 2 - (left + right) / 2) if align == "center" else (bx + bw - right) if align == "right" else bx - left
                cap = -ref_font.getbbox("H", anchor="ls")[1] * scale
                y = baseline - cap / 2 * (1 - fit_scale)  # smaller type keeps the line's vertical centre
                draw = ImageDraw.Draw(canvas)
                for i, word in enumerate(visible.split(" ")):
                    english_at = len(reveal(text, visible[:len(" ".join(visible.split(" ")[:i]))], english, names).split())
                    fade = fades[min(english_at, len(fades) - 1)] if fades else 1.0
                    colour = tuple(int(round(v)) for v in np.clip(bg_colour + min(fade, 1.0) * axis, 0, 255))
                    x += tool.draw_line(draw, (x, y), word, font, colour, tracking * k) + tracking * k + font.getlength(" ")
            out = frames / "out" / path.name
            out.parent.mkdir(exist_ok=True)
            canvas.save(out)
            edits[n] = out
            done.append(n)
        if not done:
            record["status"] = "FLAGGED: the English was not found on a plain background; left in English"
            continue
        record.update(frames=[round(done[0] / fps, 3), round(done[-1] / fps, 3)], frames_changed=len(done))
        if skipped:
            record["status"] = (f"FLAGGED: English over a busy background in {len(skipped)} frame(s) from "
                                f"{round(skipped[0] / fps, 2)}s; left in English there")
        if np.abs(ink - bg_colour).max() < 40:  # the reference frame caught the text mid-fade: the redraw is as faint
            record["status"] = (f"FLAGGED: low contrast, text {block['colour']} on background {tool.hexcolor(bg_colour)} at the "
                                f"reference frame ({round(ref_n / fps, 2)}s). Set \"frame\" on this item in media-text.json to a time "
                                "where the text is fully shown and opaque, then run screen again")
        record.setdefault("status", "replaced in every frame it shows")
    if not edits:
        raise SystemExit("No on-screen text could be replaced; see the flags above. The dubbed video is unchanged.")
    read_back = ocr_frames(sorted(edits.values()))  # no English word left in any changed frame
    for record in records:
        if not record.get("translation"):
            continue
        keep = {w.strip(".,:;!?").lower() for w in record["translation"].split()} | {w.lower() for n in names for w in n.split()}
        english_only = {w.strip(".,:;!?").lower() for w in record["text"].split()} - keep
        left_in = sorted({p.name for p in edits.values() for l in read_back.get(p.name, [])
                          if english_only & {w.strip(".,:;!?").lower() for w in l["text"].split()}})
        record["english_found_in_changed_frames"] = left_in
        if left_in:
            record["status"] = f"FLAGGED: English still read in {len(left_in)} changed frame(s)"
    out_video = folder / "localized.mp4"
    decoder = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", str(base), "-map", "0:v:0", "-f", "rawvideo",
                                "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    encoder = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                                "-r", info["rate"], "-i", "-", "-i", str(base), "-map", "0:v:0", "-map", "1:a?", "-c:v", "libx264",
                                "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p", "-c:a", "copy", "-t",
                                str(info["picture_seconds"]), "-movflags", "+faststart", str(out_video)], stdin=subprocess.PIPE)
    n, size = 0, W * H * 3
    while True:
        frame = decoder.stdout.read(size)
        if len(frame) < size:
            break
        encoder.stdin.write(Image.open(edits[n]).convert("RGB").tobytes() if n in edits else frame)
        n += 1
    encoder.stdin.close()
    decoder.stdout.close()
    if decoder.wait() or encoder.wait():
        raise SystemExit("ffmpeg failed while writing the video with the new on-screen text.")
    record = dict(label=LABEL, made_at=now(), output=f"{code}/localized.mp4", base=base.name, frames_written=n, items=records,
                  flagged=[r["id"] for r in records if str(r.get("status", "")).startswith("FLAGGED")],
                  still_to_check=["Watch each changed moment at full size: zooms, typing and fades are measured per frame "
                                  "and redrawn, so check they move like the original", "Native-speaker review"])
    save(folder / "screen.json", record)
    print(json.dumps(dict(output=record["output"], items={r["id"]: r.get("status") for r in records},
                          frames_changed=len(edits), flagged=record["flagged"]), ensure_ascii=False))


def source_video(run_dir, name):
    data = load(Path(run_dir) / "lines.json")
    matches = [Path(data["input"]) / v for v in data["media"]["videos"] if Path(v).stem == name]
    if len(matches) != 1:
        raise SystemExit(f"Video {name} is not in this run's input folder.")
    return matches[0]


def cmd_prepare(run_dir, video, every):
    video = Path(video).resolve()
    if not video.is_file():
        raise SystemExit(f"{video} not found.")
    folder = Path(run_dir) / "video" / video.stem
    frames = folder / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    info = probe(video)
    if info["has_audio"]:
        run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", video, "-vn", "-ac", "2", "-ar", "44100", "-b:a", "192k",
             folder / "audio.mp3"])
    times, t = [], 0.0
    while t < info["duration"]:
        times.append(round(t, 2))
        t += every
    for t in times:
        run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", t, "-i", video, "-frames:v", "1",
             "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", frames / f"t{t:07.2f}.jpg"])
    transcript = video.with_name(video.stem + ".transcript.txt")
    if transcript.exists():
        shutil.copy2(transcript, folder / "transcript.txt")
    record = dict(prepared_at=now(), video=video.name, **info, frames_every_seconds=every,
                  frames=[f"frames/t{t:07.2f}.jpg" for t in times],
                  transcript="transcript.txt" if transcript.exists() else None,
                  sha256=hashlib.sha256(video.read_bytes()).hexdigest())
    save(folder / "video.json", record)
    print(json.dumps(dict(folder=str(folder), duration=info["duration"], size=f"{info['width']}x{info['height']}",
                          has_audio=info["has_audio"], frames=len(times), transcript=record["transcript"] or
                          "none: write input/<name>.transcript.txt or run voice.py transcribe (paid)"), indent=1))


def main(argv):
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return
    if not argv or argv[0] not in ("prepare", "dub", "screen") or len(argv) < 3:
        raise SystemExit(__doc__)
    tools()
    step, run_dir = argv[0], argv[1]
    def option(flag, default):
        try:
            return type(default)(value_of(argv, flag, default))
        except ValueError:
            raise SystemExit(f"{flag} needs a number, for example {flag} 0.5") from None
    if step == "prepare":
        cmd_prepare(run_dir, argv[2], option("--every", 2.0))
        return
    if "--video" not in argv or "--language" not in argv:
        raise SystemExit(f"Usage: python3 scripts/video.py {step} output/RUN --video NAME --language CODE")
    name, code = option("--video", ""), option("--language", "")
    if step == "dub":
        cmd_dub(run_dir, name, code, option("--music-volume", 1.0), option("--starts", ""))
    else:
        cmd_screen(run_dir, name, code)


if __name__ == "__main__":
    use_venv()
    try:
        main(sys.argv[1:])
    except (ValueError, FileNotFoundError) as error:
        raise SystemExit(str(error)) from None
