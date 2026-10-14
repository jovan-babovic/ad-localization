# Ad localization

Turn your best English ads into drafts for other markets: text ads, image ads and video ads. You run it inside
[Claude Code](https://code.claude.com/docs/en/setup). Claude interviews you once about your company and markets,
then drafts every ad in every language, checks it, and hands you a review kit: each original next to its
translation, with a character count against the platform's limit and a back-translation into English.

Every output is labelled **AI draft, not yet reviewed by a native speaker**. Nothing gets uploaded or published.
Have someone who speaks the language read the kit before anything runs.

## Why this exists

ElevenLabs' paid marketing team is four English-speaking people. They built an internal workflow to run their ads
in seven languages, and ElevenLabs reports those localized campaigns generated $3.78M in incremental conversion
value at a 7.16 ROAS. Their write-up: [How we solved multilingual advertising internally](https://elevenlabs.io/blog/how-we-solved-multilingual-advertising-internally-and-why-we-are-launching-it-as-ads-engine).

This repo packages the repeat part of that job (drafting, checking, dubbing, laying out the kit) so a B2B
marketing team can run it on its own ads. The results above are ElevenLabs' own, and this repo makes no promise
about yours.

## Quickstart (about 5 minutes)

```sh
git clone https://github.com/jovan-babovic/ad-localization
cd ad-localization
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # only for image and video ads
claude
```

The second line gives this folder its own Python with Pillow and numpy, so it works whatever Python your Mac has
(Homebrew's newest `python3` ships without them). The scripts switch to `.venv` by themselves; you keep typing
`python3 scripts/...`, and `/setup` offers to do this step for you.

Then, inside Claude Code:

1. Type **`/setup`**. Claude asks one question at a time: your company and site, who buys, which platforms you
   advertise on, which markets, how to address readers in each language (French "vous" or "tu", Spanish "tú" or
   "usted", Brazilian Portuguese "você"...), names that must never be translated, and claims that need care.
   It writes `config/`.
   Want to see it work first? Type **`/localize example`** to run it on sample ads for *Field Mode by
   PropFusion*, a field inspection app for reserve study professionals.
2. Drop your ads into **`input/`**: a CSV of text ads (columns `ad, platform, field, text`, see
   [`examples/input/text-ads.csv`](examples/input/text-ads.csv)), image ads (`.png`, `.jpg`), video ads (`.mp4`).
3. Type **`/localize`**. Add language codes to do only some markets: `/localize fr,es`.
4. Open **`output/<date-time>/kit/index.html`**. There is one page per language and one CSV per language for
   copying approved text into your ad platform.

Video ads need an ElevenLabs key. Save it yourself in a separate terminal window, so it never passes through the
chat: `python3 scripts/save_key.py`. It asks for the key at a hidden prompt and writes it to `.env` (readable only
by you, ignored by git).

## What happens in a run

1. **Prepare.** Every piece of text is listed with its platform's character limit: Google headline 30, LinkedIn
   introductory text 150 recommended, and so on ([`scripts/limits.json`](scripts/limits.json), with the official
   source pages and the date they were checked).
2. **Read the media.** Claude reads the text inside your images and the frames of your videos, and takes the
   voice-over from a transcript you provide (or a paid transcription, if you say yes).
3. **Translate.** Claude drafts each line in your session, in the tone you set for that market, keeping your
   brand names and claims exactly as strong as the English. When a translation is too long it also writes a
   short version and says what the short version leaves out.
4. **Check.** A script counts characters the way each platform does, and checks that locked names and numbers
   survived and that the same English line wasn't translated two different ways.
5. **Blind back-translation.** A separate Claude, with no tools and no access to your files, sees only the
   translation and turns it back into English. It can't fill gaps from memory of the original because it never
   saw it. Claude then compares each one with your English and notes where the meaning moved.
6. **Media.** Image ads: the English text is erased and the translation set in the same box, with the design left
   alone; a label in a button stays centred in the button. Each box gets a character budget before translating, so
   long lines come with a short version up front. Text that still doesn't fit is flagged for a shorter line, never
   squashed. Video ads: the music is separated from the voice, the translated script is recorded with a ready-made
   ElevenLabs voice (never a clone of your speaker), and each sentence is laid where the English sentence starts,
   over the original music at its original level, cut to the picture's length. Text on screen is redrawn in every
   frame it shows, following its zoom, typing and fade, and the changed frames are read back for leftover English.
7. **Kit.** The review pages, a CSV per language, and every piece of evidence behind them.

Before every paid step, Claude shows you the estimate in credits and waits for your yes.

## What you need

- Claude Code with a Pro, Max, Team, Enterprise or Console account ([install](https://code.claude.com/docs/en/setup)).
- Python 3.11 or newer. The text side uses only Python's standard library.
- For image ads and text on screen in videos: a Mac (the text detection uses Apple's built-in OCR) and Pillow plus
  numpy in this folder's `.venv` (`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`). If your
  ads use a brand font, list its file path in `config/fonts.txt`. The repo ships no fonts.
- For video ads: [ffmpeg](https://ffmpeg.org/download.html) and a paid ElevenLabs plan. ElevenLabs' free plan
  "does not include a commercial license and cannot be used for any commercial purpose"; all paid plans include
  one ([ElevenLabs help](https://elevenlabs.io/docs/help-center/legal/can-i-publish-the-content-i-generate-on-the-platform),
  [pricing](https://elevenlabs.io/pricing), [terms](https://elevenlabs.io/terms-of-use)).

## What it costs

- Claude: the translating happens in your Claude Code session and counts toward your plan's usage. In our test
  on 2026-10-14, `/localize fr` on the sample's 15 text lines ran in about a minute with Sonnet, and Claude Code
  reported $0.19 for the whole run, of which the blind back-translation was $0.02.
- ElevenLabs (video only): rates from [elevenlabs.io/pricing](https://elevenlabs.io/pricing), checked
  2026-10-14: text to speech 1 credit per character; speech to text 330 credits per minute (skipped when you
  supply a transcript). Splitting voice from music has no published rate. In our test on 2026-10-14 it used 125
  credits for a 10-second clip, about 750 per minute, and recording 141 French characters used 56 credits.
  A rough budget for one 30-second video in one language: about 450 credits for the voice plus about 375 to split
  the music (once per video, shared by all languages). The Starter plan includes 30,000 credits a month.
- Before any paid call, the job checks that your plan is active, that automatic overage billing is off, and that
  at least 10,000 credits stay in reserve. If any check fails, it stops and nothing is spent.

## What it does not do

- It doesn't replace a native speaker. The checks catch length, names, numbers and meaning drift, but they can't
  tell you whether a line sounds natural to a local buyer.
- It doesn't upload or publish anything. You copy approved text into your ad platforms yourself.
- It makes no performance claims. It writes drafts; whether they convert depends on your market, offer and budget.
- It never clones a voice. Video dubs use ready-made ElevenLabs voices only.
- It doesn't build landing pages, check legal wording market by market, or show you the ad platform's preview.

## Folder map

```
.claude/skills/setup/      /setup: the interview that writes config/
.claude/skills/localize/   /localize: the job
CLAUDE.md                  the rules Claude follows in this folder
config/                    your company, markets and glossary (ignored by git)
input/                     your ads (ignored by git)
output/                    one folder per run, with kit/ inside (ignored by git)
examples/                  sample config and ads (Field Mode by PropFusion)
scripts/job.py             check config, list what to translate, check drafts
scripts/blind.py           blind back-translation
scripts/kit.py             the review kit
scripts/voice.py           ElevenLabs: voices, transcription, music split, recording
scripts/video.py           ffmpeg: frames, audio, mixing, on-screen text
scripts/reset_image_text.py  erase and re-set text in image ads (with scripts/ocr_lines.swift)
scripts/save_key.py        save your ElevenLabs key without showing it
scripts/limits.json        platform character limits and their sources
requirements.txt           Pillow and numpy, for image ads and text on screen
tests/                     .venv/bin/python -m unittest discover tests
```

## Credits

Built by Jovan Babovic at [Refrain](https://refrain.team). The code is MIT licensed ([LICENSE](LICENSE)); the example ads in `examples/input/` belong to PropFusion and are not ([examples/README.md](examples/README.md)).
