# Your company setup

`/setup` writes three files here:

- `company.md`: who you are, who buys, your voice, and the claims that need care.
- `markets.json`: the languages and markets, how to address the reader (for example "vous" or "tú"), the tone,
  the ad platforms you use (they set the character limits) and the stock voice for each language.
- `glossary.md`: names that must never be translated, and terms with a set translation.
- `fonts.txt` (optional): the file path of each brand font your image ads use, one per line. The fonts stay where
  they are installed; this repo ships no fonts.

You can edit them by hand. Check them with `python3 scripts/job.py config`.
`examples/config/` holds a filled-in example.

These files are in `.gitignore` so your company details stay out of a public fork. If your copy is private
and you want them in git, remove the `config/*` line from `.gitignore`.
