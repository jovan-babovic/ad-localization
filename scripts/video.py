"""Video steps with ffmpeg. No paid calls here (the paid ones are in voice.py).

  python3 scripts/video.py prepare output/RUN input/VIDEO.mp4 [--every 2] [--music-only]
      Saves the audio, a frame every 2 seconds (for reading on-screen text), a sheet of 12 frames to see the
      whole video at a glance, and the English transcript if input/VIDEO.transcript.txt exists. --music-only
      marks a video with music and no voice-over: there is nothing to transcribe, separate or dub.
  python3 scripts/video.py reject output/RUN --video NAME --language fr --reason "why"
      Marks a made video as rejected after review: it moves to old/ and the kit shows the reason, not the video.
  python3 scripts/video.py dub output/RUN --video NAME --language fr [--music-volume 1.0] [--starts 0.1,6.8,...]
      Lays the recorded translation over the original music, each sentence where the English one starts.
      The picture is copied untouched and the sound is cut to the picture's length.
  python3 scripts/video.py screen output/RUN --video NAME --language fr
      Replaces on-screen English with the checked translation in every frame it shows, then reads every
      changed frame back to check no English is left. A one-line title on a plain background follows its zoom,
      typing and fade; a block of several lines, or text over a gradient or picture, follows its slide and fade.
      Only titles, call-outs and small interface text are changed ("kind" in media-text.json). An animated GIF
      is worked on as the MP4 that prepare made from it.
"""
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job import LABEL, ROOT, SENTENCE_END, TRANSLATED_KINDS, load, now, option as value_of, save, use_venv  # noqa: E402

MAX_TEMPO = 1.15


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
    return [s.strip() for s in SENTENCE_END.split(text.strip()) if s.strip()]


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


# ---------- text on screen: blocks that slide and fade ----------

MASK_DIFF = 28   # colour distance from the fitted background that counts as part of a letter
EDGE = 24        # translated text stays at least this many pixels from the edge of the picture
ROOM = 1.35      # a translated block may grow to 1.35x the English block's width ("max_width" on the item overrides)
VISIBLE = 0.05   # share of a block's letters that must be inside the picture to look for it


def box(a, r):
    """Box blur of radius r over both image axes, edges repeated."""
    import numpy as np
    for axis in (0, 1):
        pad = [(0, 0)] * a.ndim
        pad[axis] = (r + 1, r)
        c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis)
        n = a.shape[axis]
        a = (np.take(c, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis) - np.take(c, np.arange(n), axis=axis)) / (2 * r + 1)
    return a


def blur(a, r):
    return box(box(box(a, r), r), r)


def erase_letters(img, mask, r=4, pad=40):
    """The pixels under mask filled from the pixels around them (normalized convolution), so lines, glows and
    gradients behind the letters carry on. Nothing outside the mask changes."""
    import numpy as np
    if not mask.any():
        return img
    ys, xs = np.nonzero(mask)
    y0, y1, x0, x1 = max(0, ys.min() - pad), ys.max() + pad + 1, max(0, xs.min() - pad), xs.max() + pad + 1
    part, m = img[y0:y1, x0:x1], mask[y0:y1, x0:x1]
    out, todo, w = part.copy(), m.copy(), (~m).astype(float)
    for rr in (r, 2 * r, 4 * r, 8 * r):
        den, num = blur(w, rr), blur(part * w[..., None], rr)
        ok = todo & (den > 1e-4)
        out[ok] = num[ok] / den[ok][:, None]
        todo &= ~ok
        if not todo.any():
            break
    img = img.copy()
    img[y0:y1, x0:x1] = out
    return img


def shifted(mask, dx, dy):
    """A 2-D array moved by whole pixels, zero-filled."""
    import numpy as np
    out, (h, w) = np.zeros_like(mask), mask.shape
    xs, xd = (0, dx) if dx >= 0 else (-dx, 0)
    ys, yd = (0, dy) if dy >= 0 else (-dy, 0)
    if w - abs(dx) > 0 and h - abs(dy) > 0:
        out[yd:yd + h - abs(dy), xd:xd + w - abs(dx)] = mask[ys:ys + h - abs(dy), xs:xs + w - abs(dx)]
    return out


def letter_mask(img, rects, keep=(), pad=3):
    """The letters' pixels in each line box: further than MASK_DIFF from a colour plane fitted to the ring of pixels
    around the box (so a gradient is not mistaken for letters), grown by 2 pixels. Boxes in `keep` (a logo inside a
    line) are left out."""
    import numpy as np
    H, W = img.shape[:2]
    mask = np.zeros((H, W), bool)
    for x, y, w, h in rects:
        x0, y0, x1, y1 = max(0, x - pad), max(0, y - pad), min(W, x + w + pad), min(H, y + h + pad)
        X0, Y0, X1, Y1 = max(0, x0 - 6), max(0, y0 - 6), min(W, x1 + 6), min(H, y1 + 6)
        yy, xx = np.mgrid[Y0:Y1, X0:X1]
        ring = ~((yy >= y0) & (yy < y1) & (xx >= x0) & (xx < x1))
        v = img[Y0:Y1, X0:X1][ring]
        far = np.linalg.norm(v - np.median(v, axis=0), axis=1)
        calm = far < np.median(far) + 20  # stray letters from a neighbouring line do not tilt the plane
        A = np.c_[np.ones(calm.sum()), xx[ring][calm], yy[ring][calm]]
        coef = np.linalg.lstsq(A, v[calm], rcond=None)[0]
        yy, xx = np.mgrid[y0:y1, x0:x1]
        plane = coef[0] + xx[..., None] * coef[1] + yy[..., None] * coef[2]
        mask[y0:y1, x0:x1] |= np.linalg.norm(img[y0:y1, x0:x1] - plane, axis=2) > MASK_DIFF
    mask = box(mask.astype(float), 2) > 1e-6
    for x, y, w, h in keep:
        mask[max(0, y):y + h, max(0, x):x + w] = False
    return mask


def detail(img):
    """Fine detail of the picture's brightness (what is left after blurring it away), for matching letters."""
    import numpy as np
    lum = img @ np.array([0.299, 0.587, 0.114])
    return (lum - blur(lum, 6)).astype(np.float32)


def follow(frame_detail, ref, lo, hi, template, mask):
    """Where the block is in each frame from lo to hi (dx, dy from the reference frame) and how opaque (0-1),
    followed out from the reference frame both ways by matching the fine detail of its letters, until it fades out
    or leaves the picture."""
    import numpy as np
    ys, xs = np.nonzero(mask)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    T = template[y0:y1, x0:x1]
    norm = float((T ** 2).sum())
    H, W = mask.shape

    def window(dx, dy):
        X0, Y0 = x0 + dx, y0 + dy
        a0, b0, a1, b1 = max(0, -X0), max(0, -Y0), min(x1 - x0, W - X0), min(y1 - y0, H - Y0)
        return (X0, Y0, a0, b0, a1, b1) if a1 > a0 and b1 > b0 else None

    def score(f, dx, dy, step=1):
        """Opacity of the block at (dx, dy) in frame f, and the share of it inside the picture."""
        w = window(dx, dy)
        if not w:
            return 0.0, 0.0
        X0, Y0, a0, b0, a1, b1 = w
        part = T[b0:b1:step, a0:a1:step]
        e = float((part ** 2).sum())
        if e < 1e-6:
            return 0.0, 0.0
        return float((part * frame_detail(f)[Y0 + b0:Y0 + b1:step, X0 + a0:X0 + a1:step]).sum()) / e, e * step * step / norm

    def search(f, cx, cy, plan):
        best = (-1e9, cx, cy)
        for step, grid, span in plan:
            cx, cy, best = best[1], best[2], (-1e9, best[1], best[2])
            for ddy in range(-span, span + 1, grid):
                for ddx in range(-span, span + 1, grid):
                    s, seen = score(f, cx + ddx, cy + ddy, step)
                    if seen > VISIBLE and s > best[0]:
                        best = (s, cx + ddx, cy + ddy)
        return best[1], best[2]

    found = {}
    for direction in (-1, 1):
        dx = dy = vx = vy = 0
        alpha, f = 1.0, ref
        while lo <= f <= hi:
            px, py = dx + vx, dy + vy
            seen = score(f, px, py)[1]
            if seen == 0:
                break  # slid out of the picture
            if seen < VISIBLE:  # nearly all outside: keep moving at the same speed
                dx, dy = px, py
            else:
                wide = ((4, 4, 96), (2, 2, 6), (1, 1, 1))
                plan = ((2, 2, 6), (1, 1, 1)) if alpha < 0.3 else wide if f == ref + direction else ((2, 2, 24), (1, 1, 1))
                nx, ny = (0, 0) if f == ref else search(f, px, py, plan)
                a = score(f, nx, ny)[0]
                if f != ref and alpha >= 0.3 and a < 0.6 * alpha:  # lost it (a repeated frame, then a jump): look wider
                    wx, wy = search(f, px, py, wide)
                    if score(f, wx, wy)[0] > a:
                        nx, ny, a = wx, wy, score(f, wx, wy)[0]
                if a < 0.04:  # faded out: the last, faintest frames carry on at the same rate until nothing is left
                    last = [found[g]["alpha"] for g in (f - direction, f - 2 * direction) if g in found]
                    fade = last[1] - last[0] if len(last) == 2 and last[0] < last[1] else 0.0  # opacity lost per frame
                    alpha = last[0] if last else 0.0
                    while fade > 0 and alpha - fade > 0 and lo <= f <= hi:
                        alpha -= fade
                        dx, dy = dx + vx, dy + vy
                        found.setdefault(f, dict(dx=int(dx), dy=int(dy), alpha=round(float(alpha), 3)))
                        f += direction
                    break
                if f != ref:
                    vx, vy = nx - dx, ny - dy
                dx, dy, alpha = nx, ny, min(1.0, a)
            if f not in found:
                found[f] = dict(dx=int(dx), dy=int(dy), alpha=round(float(alpha), 3))
            f += direction
    return found


def text_style(img, plate, mask):
    """The letters' colour, and a highlight colour (left and right end of its gradient) when some words are in a
    second colour. Every letter pixel is the background behind it (the erased picture) mixed with one of the two
    ink colours; a pixel belongs to the ink that explains it best, so the soft edges of white letters on blue are
    not mistaken for a pale highlight. The main colour is the one whose letters cover more of the block."""
    import numpy as np
    diff = np.linalg.norm(img - plate, axis=2)
    top = np.percentile(diff[mask], 98)
    use = mask & (diff > 0.25 * top)
    px, bg = img[use], plate[use]
    ys, xs = np.nonzero(use)
    strong = px[diff[use] > 0.6 * top]
    a, c = strong[strong.sum(1).argmin()], strong[strong.sum(1).argmax()]
    for _ in range(10):  # two colours among the strongest pixels
        near = np.linalg.norm(strong - a, axis=1) <= np.linalg.norm(strong - c, axis=1)
        a, c = strong[near].mean(0) if near.any() else a, strong[~near].mean(0) if (~near).any() else c
    for _ in range(5):
        fits = []
        for ink in (a, c):
            d = ink - bg
            share = np.clip(((px - bg) * d).sum(1) / np.maximum((d * d).sum(1), 1e-6), 0, 1.2)
            fits.append((np.linalg.norm(px - bg - share[:, None] * d, axis=1), share))
        near_a = fits[0][0] <= fits[1][0]
        core_a, core_c = near_a & (fits[0][1] > 0.8), ~near_a & (fits[1][1] > 0.8)
        a, c = px[core_a].mean(0) if core_a.any() else a, px[core_c].mean(0) if core_c.any() else c
    def cover(group):
        """How many letters are in this colour: separate shapes of solid pixels, after closing gaps of a pixel or two
        (the speckle of a dithered gradient). Thick bold strokes count no extra, unlike a count of pixels."""
        from reset_image_text import components
        if not group.any():
            return 0
        y0, x0 = ys[group].min(), xs[group].min()
        solid = np.zeros((ys[group].max() - y0 + 1, xs[group].max() - x0 + 1), bool)
        solid[ys[group] - y0, xs[group] - x0] = True
        solid = box(solid.astype(float), 1) > 0.34  # a pixel with a few solid neighbours joins them
        return sum(1 for shape in components(solid) if len(shape) >= 15)
    (main, core_main), (other, core_other) = (((near_a, core_a), (~near_a, core_c)) if cover(core_a) >= cover(core_c)
                                              else ((~near_a, core_c), (near_a, core_a)))
    colour = tuple(int(v) for v in px[core_main if core_main.any() else main].mean(0))
    if not core_other.any() or cover(core_other) < 0.03 * cover(core_main | core_other) or \
            np.linalg.norm(px[core_main].mean(0) - px[core_other].mean(0)) < 60:
        return tuple(int(v) for v in strong.mean(0)), None  # one colour: the strongest pixels, free of soft edges
    hx, hp = xs[core_other], px[core_other]
    lo, hi = np.percentile(hx, 8), np.percentile(hx, 92)
    return colour, (tuple(int(v) for v in hp[hx <= lo].mean(0)), tuple(int(v) for v in hp[hx >= hi].mean(0)))


def runs(line):
    """'journey is *unique*' -> [('journey is ', False), ('unique', True)]"""
    return [(p, i % 2 == 1) for i, p in enumerate(line.split("*")) if p]


def draw_runs(canvas, x, y, line, fonts, colour, highlight):
    """One line at baseline y; highlighted runs in the highlight font, coloured left to right along its gradient."""
    import numpy as np
    from PIL import Image, ImageDraw
    for text, em in runs(line):
        font = fonts[em]
        if not em or not highlight:
            ImageDraw.Draw(canvas).text((x, y), text, font=font, fill=colour + (255,), anchor="ls")
        else:
            shape = Image.new("L", canvas.size, 0)
            ImageDraw.Draw(shape).text((x, y), text, font=font, fill=255, anchor="ls")
            l, t, r, b = shape.getbbox() or (0, 0, 1, 1)
            ramp = np.linspace(0, 1, max(1, r - l))[None, :, None]
            paint = np.zeros((canvas.size[1], canvas.size[0], 3))
            paint[:, l:r] = np.array(highlight[0]) * (1 - ramp) + np.array(highlight[1]) * ramp
            layer = Image.fromarray(paint.astype(np.uint8)).convert("RGBA")
            layer.putalpha(shape)
            canvas.alpha_composite(layer)
        x += font.getlength(text)
    return x


def ink_edges(line, fonts):
    """Left and right ink edge of a line drawn from x=0."""
    from PIL import Image
    size = int(max(f.size for f in fonts.values()))
    width = int(sum(fonts[em].getlength(t) for t, em in runs(line))) + 4 * size
    canvas = Image.new("RGBA", (width, 3 * size), (0, 0, 0, 0))
    draw_runs(canvas, 2 * size, 2 * size, line, fonts, (255, 255, 255), None)
    box = canvas.getchannel("A").getbbox() or (2 * size, 0, 2 * size + 1, 1)
    return box[0] - 2 * size, box[2] - 2 * size


def _place(part, where, shape):
    """A cut-out (rows y0:y1, columns x0:x1 of a picture) put back at its place in an empty picture of that shape."""
    import numpy as np
    y0, y1, x0, x1 = where
    out = np.zeros(shape, dtype=part.dtype)
    h, w = min(y1, shape[0]) - y0, min(x1, shape[1]) - x0
    out[y0:y0 + h, x0:x0 + w] = part[:h, :w]
    return out


def screen_block(item, entry, code, paths, ref_n, found, edits, W, H, fps, ocr_frames_fn):
    """Redraw one block of on-screen text (one or more lines, any background) in every frame it shows, following
    how it slides and fades. Returns the record fields. Zoom, rotation and letter-by-letter animation are flagged."""
    import numpy as np
    from PIL import Image
    import reset_image_text as tool
    from job import joined_lines
    lines = sorted((l for b in found for l in b["lines"] if overlap(l["ocr_rect"], item["box"]) >= 0.5),
                   key=lambda l: l["ocr_rect"][1])
    if not lines:
        return dict(status="FLAGGED: no text found in its box on the reference frame; left in English")
    owners = [b for b in found if any(l in lines for l in b["lines"])]
    font_spec = tool.resolve_font(item["font"]) if item.get("font") else max(owners, key=lambda b: len(b["lines"]))["font"]
    by_n = {int(p.stem[1:]): p for p in paths}
    cache = {}

    def frame(n):
        return np.asarray(Image.open(by_n[n]).convert("RGB"), dtype=np.float64)

    def frame_detail(n):
        if n not in cache:
            if len(cache) > 8:
                cache.pop(next(iter(cache)))
            cache[n] = detail(frame(n))
        return cache[n]

    ref = frame(ref_n)
    mask = letter_mask(ref, [l["ocr_rect"] for l in lines], item.get("keep_boxes", ()))
    if not mask.any():
        return dict(status="FLAGGED: the letters could not be told apart from the background; left in English")
    track = follow(frame_detail, ref_n, min(by_n), max(by_n), detail(ref) * mask, mask)
    english = item["text"]
    plate = erase_letters(ref, mask)
    colour, highlight = text_style(ref, plate, mask)
    bands = []  # each line's letters: (top row of its box, rows of letter pixels, columns of letter pixels)
    for l in lines:
        x, y, w, h = l["ocr_rect"]
        top, lft = max(0, y - 3), max(0, x - 3)
        part = mask[top:y + h + 3, lft:x + w + 3]
        bands.append((top, part.sum(1), np.nonzero(part.any(0))[0] + lft))
    if not all(rows.any() and len(cols) for _, rows, cols in bands):
        return dict(status="FLAGGED: a line's letters could not be measured; left in English")
    baselines = [top + int(np.nonzero(rows > 0.3 * rows.max())[0].max()) + 1 for top, rows, _ in bands]
    cols = [c for _, _, c in bands]
    left, right = min(c.min() for c in cols), max(c.max() + 1 for c in cols)
    centre = float(np.mean([(c.min() + c.max() + 1) / 2 for c in cols]))
    base = tool.load_font(font_spec, 100)
    widths = [(c.max() + 1 - c.min(), ink_edges(l["text"], {False: base, True: base})) for c, l in zip(cols, lines)]
    size = float(np.median([100 * w / max(1, r - l) for w, (l, r) in widths]))
    pitch = float(np.mean(np.diff(baselines))) if len(baselines) > 1 else 1.2 * size
    spreads = {"left": np.ptp([c.min() for c in cols]), "right": np.ptp([c.max() for c in cols]),
               "center": np.ptp([(c.min() + c.max()) / 2 for c in cols])}
    align = item.get("align") or ("left" if len(cols) == 1 else min(spreads, key=spreads.get))

    if align == "left":
        margin = W - EDGE - left
    elif align == "right":
        margin = right - EDGE
    else:
        margin = 2 * (min(centre, W - centre) - EDGE)
    room = min(item.get("max_width") or ROOM * (right - left), margin)
    plan = None
    for key, lines_key in (("text", "lines"), ("short", "short_lines")):
        if not entry.get(key):
            continue
        tr_spec, factor = tool.script_font(font_spec, entry[key], code)
        em_spec = (tool.script_font(font_spec, entry[key], code, weight="bold")[0] if tr_spec != font_spec
                   else tool.resolve_font(item["highlight_font"]) if item.get("highlight_font") else tr_spec)
        fonts = {False: tool.load_font(tr_spec, size * factor), True: tool.load_font(em_spec, size * factor)}
        layout = entry.get(lines_key) or tool.wrap(entry[key], fonts[False], room, 0.0)
        if joined_lines(layout).replace(" ", "") != entry[key].replace(" ", ""):
            return dict(status=f"FLAGGED: the line breaks do not spell out the checked {key}; left in English")
        edges = [ink_edges(l, fonts) for l in layout]
        scale = min(1.0, room / max(r - l for l, r in edges))
        plan = dict(variant=key, layout=layout, tr_spec=tr_spec, em_spec=em_spec, factor=factor, scale=scale)
        if scale >= tool.MIN_SCALE:
            break
    k = plan["scale"] * plan["factor"]
    fonts = {False: tool.load_font(plan["tr_spec"], size * k), True: tool.load_font(plan["em_spec"], size * k)}
    layout = plan["layout"]
    edges = [ink_edges(l, fonts) for l in layout]
    if len(layout) == len(baselines):
        ys = baselines
    else:
        mid = float(np.mean(baselines))
        ys = [mid + (i - (len(layout) - 1) / 2) * pitch * plan["scale"] for i in range(len(layout))]
    sprite = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for line, (l, r), y in zip(layout, edges, ys):
        x = left - l if align == "left" else right - r if align == "right" else centre - (l + r) / 2
        x = min(max(x, EDGE - l), W - EDGE - r)
        draw_runs(sprite, x, y, line, fonts, colour, highlight)
    record = dict(engine="block", translation=entry[plan["variant"]], variant=plan["variant"], lines_drawn=layout,
                  type_size=f"{round(k * 100)}% of the English", colour=tool.hexcolor(colour),
                  highlight_colours=[tool.hexcolor(c) for c in highlight] if highlight else None,
                  font=Path(plan["tr_spec"].split("#")[0].split("@")[0]).name)
    shown = sorted(n for n, t in track.items() if t["alpha"] > 0.01)
    ys_, xs_ = np.nonzero(mask)
    reach = (max(0, ys_.min() - 24), ys_.max() + 25, max(0, xs_.min() - 24), xs_.max() + 25)

    def letters(before, after, where):
        """The detail the erase took away, on the letters' pixels only: the letters as they are in this frame."""
        y0, y1, x0, x1 = where
        return (detail(before[y0:y1, x0:x1]) - detail(after[y0:y1, x0:x1])) * shifted_mask[y0:y1, x0:x1]

    shifted_mask = mask
    own = letters(ref, plate, reach)
    own_whole, own_energy = _place(own, reach, (H, W)), float((own ** 2).sum())
    odd, made = [], {}
    for n in shown:
        t = track[n]
        img = np.asarray(Image.open(edits.get(n, by_n[n])).convert("RGB"), dtype=np.float64)
        shifted_mask = shifted(mask, t["dx"], t["dy"])
        erased = erase_letters(img, shifted_mask)
        if t["alpha"] > 0.3:  # the same letters at this strength, or changed shape (a zoom, a turn, letters on their own)?
            where = (max(0, reach[0] + t["dy"]), max(0, reach[1] + t["dy"]), max(0, reach[2] + t["dx"]), max(0, reach[3] + t["dx"]))
            seen = letters(img, erased, where)
            expected = t["alpha"] * shifted(own_whole, t["dx"], t["dy"])[where[0]:where[1], where[2]:where[3]]
            seen_energy = float((seen ** 2).sum())
            if seen.shape == expected.shape and seen_energy > 0.1 * own_energy and \
                    float(((seen - expected) ** 2).sum()) > 0.5 * seen_energy:
                odd.append(n)
        img = erased
        layer = sprite if t["alpha"] >= 0.999 else sprite.copy()
        if t["alpha"] < 0.999:
            layer.putalpha(layer.getchannel("A").point(lambda v, a=t["alpha"]: int(v * a)))
        moved = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        moved.paste(layer, (t["dx"], t["dy"]))
        canvas = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).convert("RGBA")
        canvas.alpha_composite(moved)
        out = by_n[n].parent / "out" / by_n[n].name
        out.parent.mkdir(exist_ok=True)
        canvas.convert("RGB").save(out)
        made[n] = out
    if not shown:
        return dict(status="FLAGGED: the text could not be followed from its reference frame; left in English")
    if len(odd) >= 3:
        return dict(status=f"FLAGGED: the text zooms, turns or animates letter by letter around {round(odd[0] / fps, 2)}s "
                           "(only slides and fades are redrawn); left in English", frames_odd=len(odd))
    edits.update(made)
    record.update(frames=[round(shown[0] / fps, 3), round(shown[-1] / fps, 3)], frames_changed=len(shown))
    # English still showing in a frame this tool did not change: it moved in a way that could not be followed.
    english_only = {w.strip(".,:;!?¿¡").lower() for w in english.split()} - {
        w.strip(".,:;!?¿¡").lower() for w in record["translation"].replace("*", "").split()}
    untouched = [by_n[n] for n in sorted(by_n) if n not in edits]
    grown = [item["box"][0] - item["box"][2] // 2, item["box"][1] - item["box"][3] // 2, item["box"][2] * 2, item["box"][3] * 2]
    left_over = sorted(name for name, found_lines in (ocr_frames_fn(untouched).items() if untouched else [])
                       for l in found_lines if overlap(l["rect"], grown) > 0.3
                       and english_only & {w.strip(".,:;!?¿¡").lower() for w in l["text"].split()})
    if left_over:
        record["status"] = (f"FLAGGED: English still shows in {len(set(left_over))} frame(s) the tool could not follow "
                            f"(first: {sorted(set(left_over))[0]}); check the start and end of this text")
    elif plan["scale"] < tool.MIN_SCALE:
        record["status"] = f"FLAGGED: set at {round(plan['scale'] * 100)}% type size to fit; needs shorter copy"
    else:
        record["status"] = "replaced in every frame it shows"
    return record


def plain_around(img, box, pad=12):
    """True when the ring of pixels just outside the box is one flat colour (a card, a plain title background)."""
    import numpy as np
    x, y, w, h = box
    H, W = img.shape[:2]
    x0, y0, x1, y1 = max(0, x - pad), max(0, y - pad), min(W - 1, x + w + pad), min(H - 1, y + h + pad)
    ring = np.concatenate([img[y0, x0:x1], img[y1, x0:x1], img[y0:y1, x0], img[y0:y1, x1]])
    return float(np.abs(ring - np.median(ring, axis=0)).mean()) <= 8


def cmd_screen(run_dir, name, code):
    """Every frame the English shows in is re-drawn in the target language. One line on a plain background (a title
    card) follows its size, place, letters shown and fade, measured on each frame. Several lines, or text over a
    gradient or picture, are followed as one block as they slide and fade. Dashboards, logos and footage text are
    never changed; zooms and turns of a block are flagged and left alone."""
    import numpy as np
    from PIL import Image, ImageDraw
    import reset_image_text as tool
    from job import words
    run_dir = Path(run_dir)
    items = [i for i in load(run_dir / "media-text.json")["items"] if i["surface"] == "screen" and Path(i["file"]).stem == name]
    if not items:
        raise SystemExit(f"No on-screen text items for {name} in media-text.json.")
    for item in items:
        if item.get("kind", "title") in TRANSLATED_KINDS and not all(k in item for k in ("start", "end", "box")):
            raise SystemExit(f"Screen item {item.get('id')} needs start, end and box [x, y, w, h].")
    source = source_video(run_dir, name)
    info = probe(source)
    fps, W, H = info["fps"], info["width"], info["height"]
    total = int(round(info["picture_seconds"] * fps))
    folder = run_dir / "video" / name / code
    set_aside(folder, ("localized.mp4", "screen.json", "rejected.json"))  # a failed run must not leave an old result behind
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
        if item.get("kind", "title") not in TRANSLATED_KINDS:
            record["status"] = f"left as in the original ({item['kind']}: never translated)"
            continue
        f0, f1 = max(0, int((item["start"] - 0.5) * fps)), min(total - 1, int(math.ceil((item["end"] + 0.5) * fps)))
        frames = work / item["id"]
        frames.mkdir()
        run(["ffmpeg", "-nostdin", "-v", "error", "-i", source, "-vf", f"select='between(n\\,{f0}\\,{f1})'", "-fps_mode",
             "passthrough", "-start_number", f0, "-frames:v", f1 - f0 + 1, frames / "f%05d.png"])
        paths = sorted(frames.glob("f*.png"))
        ref_n = min(max(int(round(item.get("frame", (item["start"] + item["end"]) / 2) * fps)), f0), f1)
        boxes = work / f"{item['id']}.boxes.json"
        tool.detect(frames / f"f{ref_n:05d}.png", boxes, [l for l in fonts.read_text().splitlines() if l.strip()] if fonts.exists() else None)
        found = load(boxes)["blocks"]
        in_box = [l for b in found for l in b["lines"] if overlap(l.get("ocr_rect") or b["rect"], item["box"]) >= 0.5]
        ref_img = np.asarray(Image.open(frames / f"f{ref_n:05d}.png").convert("RGB"), dtype=np.float64)
        engine = item.get("engine") or ("line" if len(in_box) == 1 and plain_around(ref_img, item["box"]) else "block")
        if engine == "block":  # several lines, or a gradient or picture behind: follow the block as it slides and fades
            record.update(screen_block(item, checked["blocks"][item["id"]], code, paths, ref_n, found, edits, W, H, fps,
                                       ocr_frames))
            continue
        record["engine"] = "line"  # one line on a plain background: follows zoom, typing and fade
        ocr = ocr_frames(paths)
        block = max(found, key=lambda b: overlap(b["rect"], item["box"])) if found else None
        if not block or overlap(block["rect"], item["box"]) < 0.3 or len(block["lines"]) != 1:
            record["status"] = "FLAGGED: not one line of text in its box on the reference frame; left in English"
            continue
        english, entry = item["text"], checked["blocks"][item["id"]]
        if item.get("font"):  # pinned in media-text.json (company.md names the font): never re-matched
            block["font"] = tool.resolve_font(item["font"]) or item["font"]
        align = item.get("align", block["align"])
        size, tracking = block["font_size"], block.get("tracking", 0.0)
        tr_spec, tr_factor = tool.script_font(block["font"], entry["text"], code)
        tr_tracking = tracking if tr_spec == block["font"] else 0.0
        ink = np.array([int(block["colour"][i:i + 2], 16) for i in (1, 3, 5)], dtype=float)
        rect = block["rect"]
        allowed = item.get("max_width", rect[2])

        def ink_extent(text, font, track):
            width = tool.draw_line(None, (0, 0), text, font, None, track)
            return font.getbbox(text[0], anchor="ls")[0], width - font.getlength(text[-1]) + font.getbbox(text[-1], anchor="ls")[2]

        ref_font = tool.load_font(block["font"], size)
        tr_font = tool.load_font(tr_spec, size * tr_factor)
        variant, text, fit_scale = None, None, None
        for key in ("text", "short"):
            if entry.get(key):
                left, right = ink_extent(entry[key], tr_font, tr_tracking)
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
                font = tool.load_font(tr_spec, size * tr_factor * k)
                left, right = ink_extent(visible, font, tr_tracking * k)
                x = (bx + bw / 2 - (left + right) / 2) if align == "center" else (bx + bw - right) if align == "right" else bx - left
                cap = -ref_font.getbbox("H", anchor="ls")[1] * scale
                y = baseline - cap / 2 * (1 - fit_scale)  # smaller type keeps the line's vertical centre
                draw = ImageDraw.Draw(canvas)
                for i, word in enumerate(visible.split(" ")):
                    english_at = len(reveal(text, visible[:len(" ".join(visible.split(" ")[:i]))], english, names).split())
                    fade = fades[min(english_at, len(fades) - 1)] if fades else 1.0
                    colour = tuple(int(round(v)) for v in np.clip(bg_colour + min(fade, 1.0) * axis, 0, 255))
                    x += tool.draw_line(draw, (x, y), word, font, colour, tr_tracking * k) + tr_tracking * k + font.getlength(" ")
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


GIF_RATE = "30000/1001"  # an animated GIF becomes a 29.97 fps MP4: LinkedIn video ads take "less than 30 FPS"


def set_aside(folder, names):
    """Earlier results move to old/ with the time in their name, never deleted."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for name in names:
        if (folder / name).exists():
            (folder / "old").mkdir(exist_ok=True)
            (folder / name).rename(folder / "old" / f"{stamp}-{name}")


def cmd_reject(run_dir, name, code, reason):
    """A made video that does not pass review: set aside, with the reason, so the kit never shows it as a draft."""
    folder = Path(run_dir) / "video" / name / code
    made = [n for n in ("localized.mp4", "dubbed.mp4") if (folder / n).exists()]
    if not made:
        raise SystemExit(f"No made video for {name} in {code} to reject.")
    if not reason.strip():
        raise SystemExit('Say why with --reason "...", for example --reason "the title is half replaced in the fade".')
    set_aside(folder, made + ["screen.json"])
    save(folder / "rejected.json", dict(label=LABEL, rejected_at=now(), files=made, reason=reason.strip()))
    print(json.dumps(dict(rejected=made, moved_to=str(folder / "old"), reason=reason.strip()), ensure_ascii=False))


def source_video(run_dir, name):
    """The video to work from: the input file, or for an animated GIF the MP4 that prepare made from it."""
    data = load(Path(run_dir) / "lines.json")
    matches = [Path(data["input"]) / v for v in data["media"]["videos"] if Path(v).stem == name]
    if len(matches) != 1:
        raise SystemExit(f"Video {name} is not in this run's input folder.")
    if matches[0].suffix.lower() == ".gif":
        made = Path(run_dir) / "video" / name / "source.mp4"
        if not made.exists():
            raise SystemExit(f"Run first: python3 scripts/video.py prepare {run_dir} {matches[0]}")
        return made
    return matches[0]


def cmd_prepare(run_dir, video, every, music_only=False):
    video = Path(video).resolve()
    if not video.is_file():
        raise SystemExit(f"{video} not found.")
    folder = Path(run_dir) / "video" / video.stem
    frames = folder / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    original = video
    if video.suffix.lower() == ".gif":  # silent MP4, same picture and timing, for every later step and as the output
        run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", video, "-vf",
             f"fps={GIF_RATE},scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p", "-c:v", "libx264", "-crf", "16",
             "-preset", "medium", "-an", "-movflags", "+faststart", folder / "source.mp4"])
        video = folder / "source.mp4"
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
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", video, "-vf",  # the whole video at a glance, free
         f"fps=12/{max(info['picture_seconds'], 0.5)},scale=320:-2,tile=4x3", "-frames:v", "1", "-q:v", "3", frames / "sheet.jpg"])
    transcript = original.with_name(original.stem + ".transcript.txt")
    if transcript.exists():
        shutil.copy2(transcript, folder / "transcript.txt")
    voice = "none" if music_only or not info["has_audio"] else None
    record = dict(prepared_at=now(), video=original.name, **info, voice=voice, sheet="frames/sheet.jpg", frames_every_seconds=every,
                  frames=[f"frames/t{t:07.2f}.jpg" for t in times],
                  transcript="transcript.txt" if transcript.exists() else None,
                  sha256=hashlib.sha256(original.read_bytes()).hexdigest())
    if original != video:
        record.update(from_gif=True, works_on="source.mp4", note=(
            "Animated GIF: turned into a silent MP4 at 29.97 fps. The translated version comes out as an MP4 (LinkedIn "
            "image ads take GIFs up to 5 MB; video ads take MP4)." + (" Shorter than 3 seconds: LinkedIn video ads need "
                                                                     "at least 3." if info["picture_seconds"] < 3 else "")))
    save(folder / "video.json", record)
    print(json.dumps(dict(folder=str(folder), duration=info["duration"], size=f"{info['width']}x{info['height']}",
                          has_audio=info["has_audio"], frames=len(times), sheet=str(frames / "sheet.jpg"),
                          voice="none (music only)" if music_only and info["has_audio"] else "none (no sound)" if voice else
                          "ask: a voice-over, or only music?", transcript=record["transcript"] or (
                              "none needed: no voice-over" if voice else
                              "none: write input/<name>.transcript.txt or run voice.py transcribe (paid)"),
                          **({"note": record["note"]} if record.get("note") else {})), indent=1))


def main(argv):
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return
    if not argv or argv[0] not in ("prepare", "dub", "screen", "reject") or len(argv) < 3:
        raise SystemExit(__doc__)
    tools()
    step, run_dir = argv[0], argv[1]
    def option(flag, default):
        try:
            return type(default)(value_of(argv, flag, default))
        except ValueError:
            raise SystemExit(f"{flag} needs a number, for example {flag} 0.5") from None
    if step == "prepare":
        cmd_prepare(run_dir, argv[2], option("--every", 2.0), "--music-only" in argv)
        return
    if "--video" not in argv or "--language" not in argv:
        raise SystemExit(f"Usage: python3 scripts/video.py {step} output/RUN --video NAME --language CODE")
    name, code = option("--video", ""), option("--language", "")
    if step == "dub":
        cmd_dub(run_dir, name, code, option("--music-volume", 1.0), option("--starts", ""))
    elif step == "reject":
        cmd_reject(run_dir, name, code, option("--reason", ""))
    else:
        cmd_screen(run_dir, name, code)


if __name__ == "__main__":
    use_venv()
    try:
        main(sys.argv[1:])
    except (ValueError, FileNotFoundError) as error:
        raise SystemExit(str(error)) from None
