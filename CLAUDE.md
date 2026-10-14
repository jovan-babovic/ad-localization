# Ad localization: rules for Claude

This folder turns a team's best English ads into drafts for other languages: text ads, image ads and video ads.
The person running it is usually a marketer, not a developer. Use plain words, explain choices through what goes
in and what comes out, and ask one question at a time.

Two commands: `/setup` (interview, writes `config/`) and `/localize` (runs the job on `input/`).

## What never changes

1. **Every output says "AI draft, not yet reviewed by a native speaker".** Pages, CSVs, file names in messages,
   your summaries. Never call a draft final, approved, native-quality or ready to publish.
2. **Nothing is uploaded, published or sent.** No ad-account uploads, no posting, no emails. The kit is a local
   folder the person reviews and shares themselves.
3. **Ask before spending.** Paid steps (ElevenLabs) print an estimate and stop. Show the estimate in credits and
   only add `--confirm` after the person says yes in this conversation. One yes covers one step.
4. **Keys never pass through the chat.** The ElevenLabs key is saved by the person running
   `python3 scripts/save_key.py` in their own terminal. Never ask for the key, never read `.env`, never print it.
   If someone pastes a key into the chat, tell them to delete it in ElevenLabs and make a new one.
5. **Stock voices only.** Video dubs use a ready-made ElevenLabs voice (default voices or Voice Library voices
   added to the account). Never clone, design or imitate the original speaker's voice. `voice.py` refuses
   cloned and account-made voices; do not work around it.
6. **Blind back-translation stays blind.** `scripts/blind.py` sends only the translated text to a separate,
   tool-free Claude. Never add the English, the brief or company files to it. A back-translation counts only for
   the exact wording it was made from; after any edit, run `blind.py` again (it only sends what changed).

## How to translate (you do this in the session)

Read `config/company.md`, `config/markets.json` and `config/glossary.md` before drafting. Then, for each line in
`lines.json`:

- Keep the same promise and say it the way people in that market say it. Translation is not localization:
  idioms, examples and tone follow the market, the claim does not move.
- Use the address and tone set for the market (for example French "vous", Latin American Spanish "tú",
  Brazilian Portuguese "você") consistently across every line.
- Names under "Never translate" stay exactly as written, same capitals. Use the glossary's set wording.
- Keep numbers, units, conditions and qualifiers ("up to", "from", "per seat", "free trial"). Never make a claim
  stronger, add a number, or turn a paid feature into a free one. Watch the claims listed in `company.md`.
- A button (`cta`) on LinkedIn or Meta is chosen from the platform's list. Translate it for reference and say so.
- If the faithful translation is longer than the limit, keep it and also write a `short_version` that fits, with
  a `shortening_note` in English listing exactly what was dropped or changed. If nothing faithful fits, leave
  `short_version` null and explain in `review_note`.
- Text inside an image or on screen must fit the same box; prefer the shorter natural wording. Its line shows about
  how many characters fit (`limit.recommended`); when the translation is longer, also write a short version that fits.
- A voice-over script should take about as long to say as the English. Keep one sentence per English sentence, in
  the same order: the dub lays each sentence where the English one starts. Write a short version when it runs long.
- `review_note`: one short English sentence on any choice a reviewer should know about, else "".

Write the drafts to `output/RUN/drafts.json`:

```json
{"translations": [
  {"line_id": "L66a86418ef", "language": "fr", "translation": "…", "short_version": null,
   "shortening_note": null, "review_note": ""}
]}
```

## Judging meaning

After `blind.py`, compare each English line with the back-translation of each version (full and short) and
write `output/RUN/meaning.json`:

```json
{"reviews": [{"line_id": "…", "language": "fr", "variant": "full", "text": "<exact translation>",
              "verdict": "same", "note": "One sentence."}]}
```

`verdict` is `same` when the promise, numbers, conditions and tone match, otherwise `changed` with the reason.
Small word differences in the back-translation are normal; you are judging the claim, not the wording. When a
version is `changed` and that is not an intended shortening, fix the draft and run the checks again.

## Scripts count, Claude judges

Scripts check counts, limits, names, numbers, coverage and files. They cannot judge meaning, tone or whether an
ad looks right. Look at every image and frame the job makes before calling it done, and say plainly what you
did not check. A script that exits cleanly is not an approval.

## Where things are

- `scripts/job.py` config, prepare and check. `scripts/blind.py` blind back-translation. `scripts/kit.py` the
  review kit. `scripts/voice.py` ElevenLabs. `scripts/video.py` FFmpeg. `scripts/reset_image_text.py` image text.
- `scripts/limits.json` platform character limits with their official sources and the date they were checked.
- Each run lives in `output/<date-time>/`; the kit is `output/<run>/kit/index.html`.
- `config/fonts.txt` (optional): paths of brand fonts installed on this computer, for the image tool. Never copy
  a font file into this folder; most brand fonts are licensed.
- Run scripts exactly as written from this folder (`python3 scripts/...`), without `cd`, so the pre-approved
  commands in `.claude/settings.json` match. When `.venv/` exists, the scripts switch to its Python by themselves.
