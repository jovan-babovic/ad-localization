# Drop your ads here

- **Text ads:** a `.csv` (or `.json`) with the columns `ad, platform, field, text`, and optionally `source` (the
  ad's link, shown in the kit). One row per piece of text. `platform` is `linkedin`, `google` or `meta`; `field`
  is `primary_text`, `headline`, `description`, `cta` or `path` (Google's display path).
  See `examples/input/text-ads.csv`.
- **Image ads:** `.png`, `.jpg`, `.webp`, or a one-picture `.gif`. Claude reads the text in them.
- **Video ads:** `.mp4` or `.mov`. If you have the voice-over script, save it next to the video as
  `NAME.transcript.txt` (saves a paid transcription). A video with only music needs no script.
- **Animated GIFs:** `.gif`. They are handled like a silent video, and the draft comes out as an MP4. LinkedIn
  image ads take GIFs up to 5 MB; video ads take MP4.

## Ads from an ad library

Save the files and text yourself rather than copying them from an ad library:

- **Media:** use your own original files, or download them from your ad account. LinkedIn's Ad Library plays its
  own re-saved copy, often bigger than your upload and not the original.
- **Text:** paste it into the CSV. Ad libraries cut long text short with "..." (the job warns you when a text
  ends that way). Put the ad's link in the `source` column.
- **Google search ads:** export the headlines and descriptions from your Google Ads account. The Ads Transparency
  Center shows only one combination of a responsive search ad.

Everything in this folder except this file is ignored by git, so your ads never end up in a commit.
