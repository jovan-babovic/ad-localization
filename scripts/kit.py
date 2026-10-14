"""Build the review kit: one page per language with every original next to its draft, the checks,
the blind back-translation and the meaning notes. Also one CSV per language for copying text into ad platforms.

  python3 scripts/kit.py output/RUN

It refuses to build when a translation is missing, a check finds an error, or a back-translation or
meaning note belongs to different wording than the current draft. It never uploads or sends anything.
"""
import csv
import hashlib
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import sys
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job import IMAGE_TYPES, LABEL, ROOT, VIDEO_TYPES, check, load, now, save, use_venv, words  # noqa: E402

FIELD_LABELS = {"primary_text": "Text above the ad", "headline": "Headline", "description": "Description",
                "cta": "Button", "path": "Display path", "image_text": "Text in the image",
                "script": "Voice-over script", "screen_text": "Text on screen"}
PLATFORM_NAMES = {"linkedin": "LinkedIn", "google": "Google Ads", "meta": "Meta", "image": "image", "video": "video"}
RESULT_TEXT = {"fits": ("ok", "Fits"), "over_recommended": ("warn", "Longer than recommended"),
               "over_max": ("bad", "Over the platform limit"), "within_estimate": ("ok", "Within timing estimate"),
               "over_budget": ("warn", "Probably too long for its box"),
               "over_estimate": ("warn", "Long for the video"), "no_limit": ("plain", "No character limit")}
CSS = """
:root{--bg:#fbf8f3;--card:#fff;--ink:#161618;--muted:#5f5d58;--line:#e8e1d6;--accent:#f25533;--ok:#1d7a46;--okbg:#e5f4ea;
--warn:#8a5a00;--warnbg:#fdf1d8;--bad:#b42318;--badbg:#fde8e6;--plainbg:#f1ede6}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;overflow-x:hidden}
.banner{position:sticky;top:0;z-index:10;background:var(--ink);color:#fff;text-align:center;padding:10px 16px;font-size:14px;font-weight:600;border-bottom:3px solid var(--accent)}
main{max-width:1040px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:30px;line-height:1.2;margin:8px 0 8px}h2{font-size:22px;margin:40px 0 12px;padding-top:8px;border-top:1px solid var(--line)}
h3{font-size:17px;margin:28px 0 10px}p{margin:8px 0}a{color:#b8361c}
.lead{color:var(--muted);max-width:720px}
nav.kit{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 8px}nav.kit a{padding:6px 14px;border:1px solid var(--line);border-radius:999px;background:var(--card);text-decoration:none;color:var(--ink);font-size:14px}
nav.kit a.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;align-items:start}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;min-width:0}
.card h4{margin:0 0 6px;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600}
.text{white-space:pre-wrap;overflow-wrap:anywhere;font-size:17px;margin:4px 0 8px}
.meta{font-size:13px;color:var(--muted)}
.pill{display:inline-block;font-size:12px;font-weight:600;padding:2px 9px;border-radius:999px;margin:2px 4px 2px 0;background:var(--plainbg)}
.pill.ok{background:var(--okbg);color:var(--ok)}.pill.warn{background:var(--warnbg);color:var(--warn)}.pill.bad{background:var(--badbg);color:var(--bad)}
.check{margin-top:10px;padding:10px 12px;background:var(--bg);border-radius:8px;font-size:14px}
.check b{font-weight:600}.notes{margin:8px 0 0;padding-left:18px;font-size:14px}.notes li{margin:3px 0}
.field{margin:18px 0 26px}.field>.label{font-weight:600;margin-bottom:8px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:15px}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:13px;color:var(--muted);font-weight:600}
.scroll{overflow-x:auto}
img,video{display:block;max-width:100%;height:auto;border-radius:8px;border:1px solid var(--line);background:#fff}
.empty{min-height:120px;display:flex;align-items:center;justify-content:center;text-align:center;color:var(--muted);background:var(--plainbg);border-radius:8px;padding:16px;font-size:14px}
footer{margin-top:48px;font-size:13px;color:var(--muted)}
@media(max-width:700px){.grid{grid-template-columns:1fr}.grid .first{order:-1}h1{font-size:25px}.text{font-size:16px}
table.stack,table.stack tbody,table.stack tr,table.stack td{display:block;width:100%}table.stack tr:first-child{display:none}
table.stack tr{border-bottom:1px solid var(--line);padding:8px 0}table.stack td{border:0;padding:4px 12px;display:flex;justify-content:space-between;gap:12px}
table.stack td::before{content:attr(data-label);color:var(--muted);font-size:13px}}
"""


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        self.links += [v for k, v in attrs if k in ("href", "src") and v]


def verify_links(kit):
    """Every local link must point to a file inside the kit, so the folder can be zipped and shared."""
    for page in kit.glob("*.html"):
        parser = Links()
        parser.feed(page.read_text(encoding="utf-8"))
        for url in parser.links:
            parts = urlsplit(url)
            if parts.scheme or parts.netloc or not parts.path:
                continue
            target = (page.parent / unquote(parts.path)).resolve()
            if not target.is_relative_to(kit.resolve()) or not target.exists():
                raise ValueError(f"Broken link in {page.name}: {url}")


def page(title, body):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,'
            f'initial-scale=1"><title>{escape(title)}</title><style>{CSS}</style></head><body>'
            f'<div class="banner">{LABEL}</div><main>{body}<footer>Made with the ad-localization job '
            f'(github.com/jovan-babovic/ad-localization). {LABEL}. Nothing here has been uploaded to an ad account.'
            f'</footer></main></body></html>')


def count_text(variant, limit):
    n = variant["characters"]
    if limit["kind"] == "limit":
        parts = [f"{n} characters"]
        if limit.get("max"):
            parts.append(f"limit {limit['max']}")
        if limit.get("recommended"):
            parts.append(f"recommended {limit['recommended']}")
        return " · ".join(parts)
    if limit["kind"] == "timing":
        return f"{n} characters · timing estimate {limit['recommended']}"
    if limit["kind"] == "layout" and limit.get("recommended"):
        return f"{n} characters · about {limit['recommended']} fit the box"
    return f"{n} characters"


def pill(kind, text):
    return f'<span class="pill {kind}">{escape(text)}</span>'


def result_pill(result, limit):
    if limit["kind"] == "choose_from_list":
        return pill("plain", "Choose from the platform's button list")
    if limit["kind"] == "layout":
        return pill(*RESULT_TEXT["over_budget"]) if result == "over_budget" else pill("plain", "Must fit the same text box")
    if limit["kind"] == "unknown":
        return pill("warn", "Limit not known for this platform and field")
    return pill(*RESULT_TEXT.get(result, ("plain", result)))


def image_note(run_dir, stem, code):
    """Read the image tool's report. An image made from older wording is refused, like a stale back-translation."""
    info = run_dir / "images" / f"{stem}.{code}.json"
    if not info.exists():
        return "<p>" + pill("warn", "No report from the image tool: check this image by eye") + "</p>"
    report = load(info)
    current = words(run_dir, stem, code, surface="image")["blocks"]
    for block in report.get("blocks", []):
        if block.get("action") == "replace":
            entry = current.get(block["id"], {})
            if block.get("translation", "").upper() not in (entry.get("text", "").upper(), (entry.get("short") or "").upper()):
                raise SystemExit(f"{stem}.{code}.png was made from older wording (block {block['id']}). Make it again with "
                                 "scripts/reset_image_text.py apply, then rebuild the kit.")
    flagged = [b for b in report.get("blocks", []) if b["id"] in (report.get("flagged_blocks") or [])]
    smaller = [b for b in report.get("blocks", []) if str(b.get("status", "")).startswith("fits at")]
    parts = []
    if flagged:
        parts.append(pill("bad", "Text does not fit: needs shorter copy") + '<ul class="notes">' + "".join(
            f'<li>"{escape(b.get("translation", ""))}": {escape(b["status"])}</li>' for b in flagged) + "</ul>")
    else:
        parts.append(pill("ok", "Translated text fits its boxes"))
    if smaller:
        parts.append('<p class="meta">' + escape("; ".join(f'"{b.get("translation", "")}" {b["status"]}' for b in smaller)) + "</p>")
    used_short = [b for b in report.get("blocks", []) if b.get("variant_used") == "short"]
    if used_short:
        parts.append('<p class="meta">Short version used for: ' + escape(", ".join(f'"{b["translation"]}"' for b in used_short)) + "</p>")
    return "".join(parts)


def build(run_dir):
    run_dir = Path(run_dir).resolve()
    data = load(run_dir / "lines.json")
    report = check(run_dir)
    if report["summary"]["errors"]:
        raise SystemExit(f"{report['summary']['errors']} check error(s). Run: python3 scripts/job.py check {run_dir} and fix drafts.json.")
    drafts = {(r["line_id"], r["language"]): r for r in load(run_dir / "drafts.json")["translations"]}
    back_path, meaning_path = run_dir / "back-translations.json", run_dir / "meaning.json"
    back = {(r["language"], r["text"]): r for r in (load(back_path)["results"] if back_path.exists() else [])}
    meaning = {}
    for r in (load(meaning_path).get("reviews", []) if meaning_path.exists() else []):
        meaning[r["line_id"], r["language"], r["variant"]] = r
    variants = {(v["line_id"], v["language"], v["variant"]): v for v in report["variants"]}
    missing_back = [k for k, v in variants.items() if (k[1], v["text"]) not in back]
    if missing_back:
        raise SystemExit(f"{len(missing_back)} translation(s) have no blind back-translation for their exact current wording. "
                         f"Run: python3 scripts/blind.py {run_dir}")
    stale = [k for k, v in variants.items() if k not in meaning or meaning[k].get("text") != v["text"]]
    if stale:
        raise SystemExit(f"{len(stale)} translation(s) have no meaning note for their exact current wording in meaning.json "
                         f"(first: {stale[0]}). Compare each English line with its back-translation and save the notes.")
    findings = {}
    for f in report["findings"]:
        for line_id in ([f["line_id"]] if f.get("line_id") else f.get("line_ids", [])):
            findings.setdefault((line_id, f.get("language")), []).append(f)

    kit = run_dir / "kit"
    if kit.exists():
        shutil.rmtree(kit)
    (kit / "assets").mkdir(parents=True)
    assets = {}

    def asset(path, name=None):
        path = Path(path)
        target = Path("assets") / (name or path.name)
        shutil.copy2(path, kit / target)
        assets[target.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        return target.as_posix()

    input_dir = Path(data["input"])
    markets = data["markets"]
    originals = {name: asset(input_dir / name, "original-" + name) for name in data["media"]["images"] + data["media"]["videos"]}

    def nav(current):
        links = [("Overview", "index.html")] + [(f"{m['language']} ({m['market']})", f"{m['code']}.html") for m in markets]
        return '<nav class="kit">' + "".join(
            f'<a href="{href}"{" class=on" if href == current else ""}>{escape(label)}</a>' for label, href in links) + "</nav>"

    def check_block(key, variant_key):
        v, b, m = variants[variant_key], back[variant_key[1], variants[variant_key]["text"]], meaning[variant_key]
        verdict = pill("ok", "Same meaning") if m.get("verdict") == "same" else pill("warn", "Meaning changed: read the note")
        out = (f'<div class="check"><b>Blind back-translation:</b> {escape(b["back_translation"])}'
               + (f'<br><span class="meta">Uncertain: {escape(b["uncertainty"])}</span>' if b.get("uncertainty") else "")
               + f'<br>{verdict} {escape(m.get("note", ""))}</div>')
        return out

    def field_block(field, code):
        key = field["line_id"], code
        row, full = drafts[key], variants[(*key, "full")]
        english_count = len(field["text"])
        out = [f'<div class="field"><div class="label">{escape(FIELD_LABELS.get(field["field"], field["field"]))} '
               f'<span class="meta">· {escape(PLATFORM_NAMES.get(field["platform"], field["platform"]))}</span></div><div class="grid">',
               f'<div class="card"><h4>Original</h4><p class="text">{escape(field["text"])}</p>'
               f'<p class="meta">{english_count} characters</p></div>',
               f'<div class="card"><h4>{escape(code)} draft</h4><p class="text" lang="{escape(code)}">{escape(row["translation"])}</p>'
               f'<p class="meta">{escape(count_text(full, field["limit"]))}</p>{result_pill(full["result"], field["limit"])}'
               f'{check_block(key, (*key, "full"))}</div></div>']
        if row.get("short_version"):
            short = variants[(*key, "short")]
            out.append(f'<div class="grid" style="margin-top:12px"><div class="card"><h4>What the short version leaves out</h4>'
                       f'<p>{escape(row["shortening_note"])}</p></div><div class="card first"><h4>Short version</h4>'
                       f'<p class="text" lang="{escape(code)}">{escape(row["short_version"])}</p>'
                       f'<p class="meta">{escape(count_text(short, field["limit"]))}</p>{result_pill(short["result"], field["limit"])}'
                       f'{check_block(key, (*key, "short"))}</div></div>')
        notes = [f for f in findings.get(key, []) if f["code"] not in ("over_max", "over_recommended", "shortened", "long_for_timing")]
        if notes:
            out.append('<ul class="notes">' + "".join(f'<li>{escape(n["detail"])}</li>' for n in notes) + "</ul>")
        out.append("</div>")
        return "".join(out)

    media_rows = {}
    rows_csv, summary = {}, {}
    for market in markets:
        code = market["code"]
        body = [f'<h1>{escape(market["language"])} · {escape(market["market"])}</h1>', nav(f"{code}.html"),
                f'<p class="lead">Every original next to its {escape(market["language"])} draft. Address: '
                f'<b>{escape(market["address"])}</b>. {escape(market.get("tone", ""))}</p>',
                f'<p class="meta">Download the text as a spreadsheet: <a href="{code}.csv">{code}.csv</a></p>']
        groups = {}
        for field in data["fields"]:
            groups.setdefault((field["ad"], field["file"]), []).append(field)
        counts = dict(lines=0, fits=0, short=0, none=0, check=0)
        csv_rows = []
        for (ad, file), fields in groups.items():
            body.append(f'<h2>{escape(ad)}</h2><p class="meta">From {escape(file)}</p>')
            stem, suffix = Path(file).stem, Path(file).suffix.lower()
            if suffix in IMAGE_TYPES:
                made = run_dir / "images" / f"{stem}.{code}.png"
                note = image_note(run_dir, stem, code) if made.exists() else ""
                right = (f'<img src="{asset(made, f"{stem}.{code}.png")}" alt="{escape(market["language"])} draft of {escape(ad)}">{note}'
                         if made.exists() else '<div class="empty">The image is not made yet. The translated words are below.</div>')
                body.append(f'<div class="grid"><div class="card"><h4>Original</h4><img src="{originals[file]}" alt="Original {escape(ad)}">'
                            f'</div><div class="card"><h4>{escape(code)} draft</h4>{right}</div></div>')
                media_rows.setdefault(code, []).append((file, "image", made.exists()))
            elif suffix in VIDEO_TYPES:
                folder = run_dir / "video" / stem / code
                prepared = load(run_dir / "video" / stem / "video.json") if (run_dir / "video" / stem / "video.json").exists() else {}
                frames = prepared.get("frames") or []
                poster = (f' poster="{asset(run_dir / "video" / stem / frames[min(1, len(frames) - 1)], f"{stem}-poster.jpg")}"'
                          if frames else "")
                dub = load(folder / "dub.json") if (folder / "dub.json").exists() else {}
                speed = dub.get("faster_percent", 0)
                made = next((folder / n for n in ("localized.mp4", "dubbed.mp4") if (folder / n).exists()), None)
                voice_line = (f'Voice: {escape((dub.get("voice") or {}).get("name", "stock voice"))} (ElevenLabs stock voice, '
                              f'no cloning) · {f"speech {speed}% faster to fit" if speed else "no speed-up needed"} · picture '
                              f'unchanged: {"yes" if dub.get("picture_unchanged") else "check"}' if dub else
                              "Voice: the original English voice. This video was not dubbed in this run.")
                right = (f'<video controls preload="metadata"{poster} src="{asset(made, f"{stem}.{code}.mp4")}"></video>'
                         f'<p class="meta">{voice_line}</p>'
                         if made else '<div class="empty">The video is not made yet. The translated script is below.</div>')
                if made and made.name == "localized.mp4" and (folder / "screen.json").exists():
                    screen = load(folder / "screen.json")
                    right += ('<p class="meta">Text on screen:</p><ul class="notes">' + "".join(
                        f'<li>"{escape(r["text"])}" → "{escape(r.get("translation") or "left in English")}": {escape(r.get("status", ""))}</li>'
                        for r in screen["items"]) + "</ul>" + pill("warn", "Watch the text on screen: it is redrawn frame by frame, including moves and fades"))
                if made and (folder / "script.txt").exists():
                    right += f'<p class="meta">Recorded script ({escape(dub.get("script_variant") or "full")} version):</p>' \
                             f'<p class="text">{escape((folder / "script.txt").read_text(encoding="utf-8").strip())}</p>'
                body.append(f'<div class="grid"><div class="card"><h4>Original</h4><video controls preload="metadata"{poster} '
                            f'src="{originals[file]}"></video></div><div class="card"><h4>{escape(code)} draft</h4>{right}</div></div>')
                media_rows.setdefault(code, []).append((file, "video", bool(made)))
            for field in fields:
                body.append(field_block(field, code))
                key = field["line_id"], code
                full = variants[(*key, "full")]
                row = drafts[key]
                short = variants.get((*key, "short"))
                counts["lines"] += 1
                counts["fits"] += full["result"] in ("fits", "within_estimate", "no_limit", "over_recommended", "over_estimate")
                counts["short"] += full["result"] == "over_max" and bool(short) and short["result"] == "fits"
                counts["none"] += full["result"] == "over_max" and not (short and short["result"] == "fits")
                counts["check"] += (full["result"] in ("over_recommended", "over_estimate", "over_budget") or any(
                    meaning[(*key, v)].get("verdict") != "same" for v in ("full", "short") if (*key, v) in meaning))
                csv_rows.append(dict(ad=field["ad"], platform=field["platform"], field=field["field"], english=field["text"],
                                     translation=row["translation"], characters=full["characters"],
                                     limit_max=field["limit"].get("max") or "", limit_recommended=field["limit"].get("recommended") or "",
                                     result=full["result"], short_version=row.get("short_version") or "",
                                     short_characters=short["characters"] if short else "",
                                     short_result=short["result"] if short else "",
                                     back_translation=back[code, row["translation"]]["back_translation"],
                                     meaning=meaning[(*key, "full")].get("verdict"), status=LABEL))
        summary[code] = counts
        rows_csv[code] = csv_rows
        (kit / f"{code}.html").write_text(page(f"{market['language']} ads · review kit", "".join(body)), encoding="utf-8")
        with (kit / f"{code}.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(csv_rows[0]) if csv_rows else ["status"])
            writer.writeheader()
            writer.writerows(csv_rows)

    credits = []
    for record in sorted(run_dir.glob("video/*/**/run.json")):
        r = load(record)
        if "credits_used" in r:
            credits.append((record.parent.relative_to(run_dir).as_posix(), r.get("step"), r["credits_used"]))
    blind_cost = sum(r.get("cost_usd_estimate") or 0 for r in (load(back_path).get("runs", []) if back_path.exists() else []))
    brief = Path(data["config"])  # config/, or examples/config/ for the sample
    brief = brief.relative_to(ROOT) if brief.is_relative_to(ROOT) else brief
    index = [f'<h1>Ad localization review kit</h1>', nav("index.html"),
             f'<p class="lead">Your ads in {escape(", ".join(m["language"] for m in markets))}, drafted by AI from the brief in '
             f'{escape(brief.as_posix())}/. Every draft sits next to its original with a character check, a blind back-translation (a separate '
             f'Claude that never saw the English turns it back into English) and a note on whether the meaning held.</p>',
             '<h2>Markets</h2><div class="scroll"><table class="stack"><tr><th>Market</th><th>Address</th><th>Lines</th><th>Fit as drafted</th>'
             '<th>Need the short version</th><th>Nothing fits yet</th><th>To check</th><th>Media made</th></tr>']
    for m in markets:
        c, made = summary[m["code"]], media_rows.get(m["code"], [])
        cells = [("Market", f'<a href="{m["code"]}.html">{escape(m["language"])} · {escape(m["market"])}</a>'),
                 ("Address", escape(m["address"])), ("Lines", c["lines"]), ("Fit as drafted", c["fits"]),
                 ("Need the short version", c["short"]), ("Nothing fits yet", c["none"]), ("To check", c["check"]),
                 ("Media made", f'{sum(x[2] for x in made)} of {len(made)}')]
        index.append("<tr>" + "".join(f'<td data-label="{label}">{value}</td>' for label, value in cells) + "</tr>")
    index.append("</table></div>")
    index += ['<h2>How to use this kit</h2><ol><li>Open each language page and read the drafts. Start with anything marked '
              '"over the platform limit" or "meaning changed".</li><li>Have someone who speaks the language read it before it runs. '
              'This kit has not been reviewed by a native speaker.</li><li>Copy approved text from the CSV into your ad platform '
              'yourself. Nothing here was uploaded or published.</li></ol>',
              '<h2>What the checks cover</h2><ul><li>Character counts against the platform limits saved in scripts/limits.json '
              '(Google counts double-width characters as two).</li><li>Names that must never be translated: '
              + escape(", ".join(data["never_translate"]) or "none listed") + '.</li><li>Numbers kept the same as in the English.</li>'
              '<li>Blind back-translation of every exact draft, and a meaning note comparing it with the English.</li></ul>'
              '<p><b>Not covered:</b> native-speaker review, the ad platform\'s own preview, legal or regulatory review in each '
              'market, and any claim about how these ads will perform.</p>',
              '<h2>Cost of this run</h2><ul>']
    index += [f'<li>ElevenLabs {escape(step)} ({escape(where)}): {f"{used:,} credits" if used else "not shown yet by ElevenLabs"}</li>'
              for where, step, used in credits] or \
        ['<li>ElevenLabs: no paid calls in this run.</li>']
    index.append(f'<li>Blind back-translation: about ${blind_cost:.2f} as reported by Claude Code (on a Claude subscription this '
                 'counts toward your plan usage, not a separate bill).</li></ul>')
    index.append(f'<p class="meta">Built {escape(now())}. Evidence for every line: <a href="evidence.json">evidence.json</a> · '
                 f'files and fingerprints: <a href="manifest.json">manifest.json</a></p>')
    (kit / "index.html").write_text(page("Ad localization · review kit", "".join(index)), encoding="utf-8")
    evidence = [dict(**v, back_translation=back[v["language"], v["text"]], meaning=meaning[k]) for k, v in variants.items()]
    save(kit / "evidence.json", dict(label=LABEL, built_at=now(), rows=evidence))
    save(kit / "manifest.json", dict(label=LABEL, built_at=now(), uploaded=False, pages=["index.html"] + [f"{m['code']}.html" for m in markets],
                                     assets=assets, summary=summary))
    verify_links(kit)
    print(json.dumps(dict(kit=str(kit / "index.html"), pages=1 + len(markets), assets=len(assets), summary=summary)))


if __name__ == "__main__":
    use_venv()
    if "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__)
    elif len(sys.argv) != 2:
        raise SystemExit(__doc__)
    else:
        try:
            build(sys.argv[1])
        except (ValueError, FileNotFoundError) as error:
            raise SystemExit(str(error)) from None
