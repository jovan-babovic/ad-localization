# Drop your ads here

- **Text ads:** a `.csv` (or `.json`) with the columns `ad, platform, field, text`. One row per piece of text.
  `platform` is `linkedin`, `google` or `meta`; `field` is `primary_text`, `headline`, `description` or `cta`.
  See `examples/input/text-ads.csv`.
- **Image ads:** `.png`, `.jpg` or `.webp`. Claude reads the text in them.
- **Video ads:** `.mp4` or `.mov`. If you have the voice-over script, save it next to the video as
  `NAME.transcript.txt` (saves a paid transcription).

Everything in this folder except this file is ignored by git, so your ads never end up in a commit.
