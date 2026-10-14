---
name: localize
description: Run the ad-localization job on the ads in input/ (or on the sample with "/localize example"). Drafts every ad for each market in config/, checks lengths, names and numbers, runs a blind back-translation, re-sets the text in image ads, dubs video ads with a stock voice after asking, and builds a review kit. Use when the user types /localize.
argument-hint: "[example] [language codes, e.g. fr,es]"
disable-model-invocation: true
allowed-tools: Edit(./output/**) Edit(./config/markets.json) Bash(python3 scripts/job.py *) Bash(python3 scripts/blind.py *) Bash(python3 scripts/kit.py *) Bash(python3 scripts/video.py *) Bash(python3 scripts/reset_image_text.py *) Bash(python3 scripts/voice.py account) Bash(python3 scripts/voice.py voices *)
---

# /localize: from English ads to a review kit

Arguments: `$ARGUMENTS`. If they include `example`, add `--example` to the config and prepare commands (sample
company and ads from `examples/`). Language codes (for example `fr,es`) go to `--languages`.

Follow `CLAUDE.md` throughout: the AI-draft label everywhere, nothing uploaded, ask before every paid step, stock
voices only, the key never in the chat, blind back-translation stays blind. Run every command exactly as written
here, from this folder, without `cd`. Tell the person what you are doing in one short line per step.

## 1. Start the run

```
python3 scripts/job.py config            (add --example for the sample)
python3 scripts/job.py prepare           (add --example, and --languages fr,es if given)
```

If config is not ready, say so and suggest `/setup`. Prepare prints the run folder, called RUN below
(for example `output/2026-10-20-0930`), and lists the images and videos it found.

Read the company files now: `config/company.md`, `config/markets.json`, `config/glossary.md`
(for the sample: the same names under `examples/config/`).

## 2. Read the text inside images and videos (skip if there are none)

**Images** (needs a Mac, plus Pillow and numpy). For each image NAME.png in the input folder:

1. `python3 scripts/reset_image_text.py detect INPUT/NAME.png RUN/images/NAME.boxes.json`
   (INPUT is `input`, or `examples/input` for the sample. If `config/fonts.txt` exists, add `--fonts config/fonts.txt`
   at the end.) It finds each block of text and measures its box, colour, font and size.
2. Look at the image and at `RUN/images/NAME.boxes.json`. Every block starts as `"action": "review"`. Set ad copy to
   `"replace"`. Set `"preserve"` for logos, names under "Never translate" when the block holds nothing else,
   product screenshots, and any text sitting on a photo. Fix OCR misreads in `source_text`. If a block is set in
   capitals, add `"transform": "upper"`. If `company.md` says which font each kind of text uses, check each
   block's `font` against it and change it to that font's file when the match picked another. A label in a button
   has a `shape` and `"align": "center"`: it stays centred in the button.
3. For each `replace` block add an item to `RUN/media-text.json`, with the block id:

```json
{"items": [
  {"id": "NAME.b2", "file": "NAME.png", "surface": "image", "text": "Stop sorting site photos at your desk.", "block": "b2"}
]}
```

**Videos.** For each video run `python3 scripts/video.py prepare RUN input/NAME.mp4` (for the sample:
`examples/input/NAME.mp4`). It saves the audio, a frame every 2 seconds in `RUN/video/NAME/frames/` and the
transcript if `NAME.transcript.txt` sits next to the video.
- No transcript? Ask the person: add `NAME.transcript.txt` themselves (free), or pay for transcription. For the
  paid route run `python3 scripts/voice.py transcribe RUN --video NAME`, show the estimate, and only after a yes
  run it again with `--confirm`.
- Add the voice-over as one item: `{"id": "NAME.script", "file": "NAME.mp4", "surface": "spoken", "text": "<transcript>"}`.
- Look at the frames. For English text on screen, add one item each with when it shows and where:
  `{"id": "NAME.s1", "file": "NAME.mp4", "surface": "screen", "text": "Introducing Field Mode", "start": 8.0, "end": 12.0, "box": [x, y, w, h], "frame": 10.0}`.
  `text` is the whole line once it is complete. `start` is the last frame before the text appears and `end` the
  first frame after it is gone, so the window covers its zoom, typing and fade; `frame` is a time where the whole
  line sits still; `box` is where it sits then, in the video's pixels. Add `"align": "center"` for centred titles.
  Only one-line text on a plain background can be redrawn; leave product screens, logos, text over footage and
  lines made only of names under "Never translate" in English.

Then refresh the lines: `python3 scripts/job.py prepare RUN`. Text in images and on screen now shows about how
many characters fit its box (`limit.recommended`).

## 3. Translate (you, in this session)

Read `RUN/lines.json`. Each line has `line_id`, `language`, `text`, the platform and field, its `limit` and the
`names` that must stay unchanged. Write `RUN/drafts.json` following "How to translate" in `CLAUDE.md`: one
entry per line and language, a short version with a note whenever the full one is over a limit. For many lines,
work one language at a time.

## 4. Check, back-translate, judge meaning

```
python3 scripts/job.py check RUN
python3 scripts/blind.py RUN
```

Fix every error the check reports (missing lines, changed names) and run it again until it shows 0 errors.
A "fail" (over the platform limit) with a short version that fits is fine; without one, write a short version
or explain in `review_note` why nothing faithful fits.

Then read `RUN/back-translations.json` and write `RUN/meaning.json` as described in `CLAUDE.md`. If a version
changed meaning by mistake, fix the draft, then run `check` and `blind.py` again (it only sends what changed)
and update `meaning.json`. Do at most two repair rounds; after that, leave the flag for the person.

## 5. Image ads

For each image and language:

```
python3 scripts/job.py words RUN NAME CODE
python3 scripts/reset_image_text.py apply INPUT/NAME.png RUN/images/NAME.boxes.json RUN/images/NAME.CODE.words.json RUN/images/NAME.CODE.png
```

The first command writes the checked translation for each block (and the short version as a fallback); it
refuses while the drafts have check errors. The tool erases the English and sets the translation in the same box,
same font, colour and alignment, shrinking the type to 85% at most. Text that still doesn't fit is **flagged, not
squashed**: tell the person which line, and offer a shorter wording (new short version, then check, blind and
meaning again, then `words` and `apply` again; earlier images move to `RUN/images/old/`). Look at every output
image yourself before moving on.

## 6. Video ads (paid: ask first)

For each video and language:

1. **Voice.** If `voice_id` is empty for this language in `config/markets.json`, run
   `python3 scripts/voice.py voices --language CODE`, offer three or four voices that suit the ad (name, accent,
   preview link) and let the person choose. Save the chosen `voice_id` in `config/markets.json`.
2. **Separate the music** once per video: `python3 scripts/voice.py stems RUN --video NAME`. It prints an
   estimate and stops. Ask: "This uses about N ElevenLabs credits (you have M left). Go ahead?" Only after a yes,
   run the same command with `--confirm`.
3. **Record the voice**: `python3 scripts/voice.py speak RUN --video NAME --language CODE` (add `--short` to
   record the short version). Same pattern: estimate, ask, `--confirm` after a yes.
4. **Mix**: `python3 scripts/video.py dub RUN --video NAME --language CODE`. Each translated sentence is laid
   where the English sentence starts (found from the pauses in the separated English voice), over the original
   music at its original level, and the sound is cut to the picture. Read `alignment` and `sentences_moved_late`
   in what it prints. If it says the voice is too long, record the short version (or write one, check and
   back-translate it first), then mix again. If it says a sentence would be cut mid-sound, record again with
   `voice.py speak` (it records one clip per sentence, so every cut falls in silence).
5. **Text on screen**: `python3 scripts/video.py screen RUN --video NAME --language CODE` when the video has
   on-screen text items. Every frame the English shows in is redrawn in the translation, following its zoom,
   typing and fade, and the changed frames are read back for leftover English. Look at the frames it changed
   (`RUN/video/NAME/CODE/screen/<item>/out/`): the first, a typing frame, a held frame and the last.

If the person says no to spending, skip the video; the kit still shows the translated script.

## 7. Build the kit and hand it over

```
python3 scripts/kit.py RUN
```

Then tell the person, briefly:
- where the kit is: `RUN/kit/index.html` (one page per language, plus a CSV per language for copying text)
- per language: lines that fit as drafted, lines that need the short version, lines where nothing fits yet,
  and lines to check (meaning changed or longer than recommended)
- ElevenLabs credits used, if any
- what was not checked: native-speaker review, the ad platform's preview, how images and video look to a person
- that everything is an AI draft, not yet reviewed by a native speaker, and nothing was uploaded
