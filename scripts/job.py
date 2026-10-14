"""The copy side of the job: check the setup, list what to translate, check the drafts.

  python3 scripts/job.py config [--example]             check config/ (or examples/config/)
  python3 scripts/job.py prepare [--example] [--languages fr,es]
                                                       start a new run in output/
  python3 scripts/job.py prepare output/RUN            rebuild a run's lines after media-text.json changes
  python3 scripts/job.py check output/RUN              check drafts.json, write checks.json
  python3 scripts/job.py words output/RUN NAME CODE    write the checked words for one image or video
                                                       (for scripts/reset_image_text.py apply)

Scripts count and check. Claude translates and judges meaning. Nothing here calls a paid service.
"""
import csv
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import unicodedata

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv" / "bin" / "python"
LABEL = "AI draft, not yet reviewed by a native speaker"
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_TYPES = {".mp4", ".mov", ".m4v", ".webm"}
PLATFORM_ALIASES = {"facebook": "meta", "instagram": "meta", "fb": "meta", "ig": "meta",
                    "google_ads": "google", "adwords": "google", "search": "google",
                    "linkedin_ads": "linkedin", "li": "linkedin"}
FIELD_ALIASES = {"intro": "primary_text", "introductory_text": "primary_text", "intro_text": "primary_text",
                 "primary": "primary_text", "body": "primary_text", "text": "primary_text", "post_text": "primary_text",
                 "title": "headline", "button": "cta", "call_to_action": "cta", "desc": "description",
                 "display_path": "path"}
SURFACE_FIELDS = {"image": "image_text", "spoken": "script", "screen": "screen_text"}
LONGER_THAN_ENGLISH = 1.2  # an English text already over the recommended length: flag a draft only past 20% longer


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def use_venv():
    """Run under this folder's own Python (.venv, see README) when it exists: Pillow and numpy are installed there."""
    if VENV.exists() and Path(sys.prefix).resolve() != VENV.parent.parent.resolve():
        os.execv(str(VENV), [str(VENV)] + sys.argv)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def option(args, flag, default=None):
    """The value after `flag` on the command line, `default` when the flag is absent, a plain message when it's empty."""
    if flag not in args:
        return default
    rest = args[args.index(flag) + 1:]
    if not rest or rest[0].startswith("--"):
        raise SystemExit(f"Add a value after {flag}.")
    return rest[0]


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def slug(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def characters(value, platform):
    """Google counts a double-width character (Chinese, Japanese, Korean) as 2. Others count code points."""
    wide = platform == "google"
    return sum(2 if wide and unicodedata.east_asian_width(c) in "WF" else 1 for c in value)


def name_count(value, name):
    """Exact, case-sensitive matches. A full stop after a name is punctuation; 'name.com' is a different word."""
    return len(re.findall(r"(?<![\w.])" + re.escape(name) + r"(?!\w|\.\w)", value))


MULTIPLIERS = [(r"mil millones", 10 ** 9), (r"[Kk千]", 1000), (r"M", 10 ** 6), (r"B|bn", 10 ** 9), (r"[万萬]", 10 ** 4),
               (r"[亿億]", 10 ** 8), (r"mil", 1000)]
MULTIPLIED = re.compile(r"(?<![\d.,])(\d+)(?:[.,](\d{1,2}))?\s?(" + "|".join(m for m, _ in MULTIPLIERS) + r")(?![A-Za-z])")


def expand_numbers(value):
    """'100K', '10 万', '100 mil' and '1.5M' written out in full, grouped by thousands ('100 000', '1 500 000')."""
    def full(match):
        whole, part, unit = match.groups()
        factor = next(f for m, f in MULTIPLIERS if re.fullmatch(m, unit))
        number = round(float(f"{whole}.{part or 0}") * factor)
        return f"{number:,}".replace(",", " ")
    return MULTIPLIED.sub(full, value)


def digit_groups(value):
    """'1,000' '1 000' and '1.000' all give ['1', '000']; '2.5' and '2,5' both give ['2', '5']; '100K' and '10 万'
    both give ['000', '100']."""
    return sorted(re.findall(r"\d+", expand_numbers(value)))


# ---------- config ----------

def config_dir(example=False):
    return ROOT / ("examples/config" if example else "config")


def never_translate(glossary_text, heading="never translate"):
    """Read the bullet list under '## Never translate' (or another heading) in glossary.md."""
    names, inside = [], False
    for line in glossary_text.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower().startswith(heading)
            continue
        if inside and re.match(r"\s*[-*]\s+\S", line):
            name = re.sub(r"^\s*[-*]\s+", "", line).strip().strip("`").strip()
            if name and name not in names:
                names.append(name)
    return names


def read_config(folder):
    """Return (config, errors, warnings), in plain sentences a marketer can act on. Config is None on errors."""
    folder, problems, warnings = Path(folder), [], []
    files = {name: folder / name for name in ("company.md", "markets.json", "glossary.md")}
    for name, path in files.items():
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            problems.append(f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path} is missing or empty. Run /setup.")
    if problems:
        return None, problems, warnings
    try:
        markets = load(files["markets.json"])
    except json.JSONDecodeError as error:
        return None, [f"markets.json is not valid JSON: {error}"], warnings
    limits = load(ROOT / "scripts/limits.json")
    if not isinstance(markets.get("markets"), list) or not markets["markets"]:
        problems.append("markets.json needs at least one market under \"markets\".")
    codes = []
    for n, market in enumerate(markets.get("markets") or [], 1):
        for key in ("language", "code", "market", "address"):
            if not isinstance(market.get(key), str) or not market[key].strip():
                problems.append(f"Market {n} is missing \"{key}\" (for example language French, code fr, market France, address vous).")
        code = market.get("code", "")
        if code and not re.fullmatch(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8})*", code):
            problems.append(f"Market {n}: code \"{code}\" should look like fr, es, pt-BR or zh-Hant.")
        codes.append(code)
    if len(set(codes)) != len(codes):
        problems.append("Two markets use the same language code.")
    overrides = markets.get("limits") or {}
    for platform in markets.get("platforms") or []:
        if platform not in limits["platforms"] and not any(k.startswith(platform + ".") for k in overrides):
            warnings.append(f"Platform \"{platform}\" has no saved character limits; its text will be marked 'limit not known'. "
                            f"Known: {', '.join(limits['platforms'])}.")
    for key, value in (markets.get("limits") or {}).items():
        if "." not in key or not isinstance(value, dict) or not set(value) <= {"max", "recommended", "note"}:
            problems.append(f"limits \"{key}\" should look like \"linkedin.headline\": {{\"max\": 200, \"recommended\": 70}}.")
    glossary = files["glossary.md"].read_text(encoding="utf-8")
    names = never_translate(glossary)
    # Generic words that are also product names (Connect, Insights): Claude keeps them where they name the product;
    # the script does not count them, since "Connect your fleet" is not the name.
    config = dict(folder=str(folder), source_language=markets.get("source_language", "English"),
                  markets=markets.get("markets") or [], platforms=markets.get("platforms") or [],
                  limit_overrides=markets.get("limits") or {}, never_translate=names,
                  names_in_context=never_translate(glossary, "names in context"), limits=limits)
    if not names:
        warnings.append("glossary.md has no names under '## Never translate'. Brand and product names will not be checked.")
    return (None if problems else config), problems, warnings


# ---------- inputs ----------

def normal_platform(value):
    value = slug(value)
    return PLATFORM_ALIASES.get(value, value)


def normal_field(value):
    value = re.sub(r"_?\d+$", "", slug(value))
    return FIELD_ALIASES.get(value, value)


def read_text_ads(path):
    """CSV or JSON rows with ad, platform, field, text. Extra columns are ignored."""
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
    else:
        data = load(path)
        rows = data.get("ads", data.get("rows")) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"{path.name}: expected a list of rows with ad, platform, field and text.")
    out = []
    for n, row in enumerate(rows, 1):
        row = {slug(k): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
        missing = [k for k in ("ad", "platform", "field", "text") if not row.get(k)]
        if missing:
            raise ValueError(f"{path.name} row {n}: missing {', '.join(missing)}.")
        out.append(dict(ad=str(row["ad"]), platform=normal_platform(row["platform"]),
                        field=normal_field(row["field"]), text=str(row["text"]), file=path.name))
    return out


def limit_for(config, platform, field, text):
    if field == "script":
        estimate = round(len(text) * 1.15)
        return dict(kind="timing", recommended=estimate,
                    note="Timing estimate: about the English length plus 15%. The real test is the recorded length.")
    if field in ("image_text", "screen_text"):
        return dict(kind="layout", note="Must fit the same text box; the image tool flags text that overflows.")
    spec = dict(config["limits"]["platforms"].get(platform, {}).get("fields", {}).get(field, {}))
    spec.update(config["limit_overrides"].get(f"{platform}.{field}", {}))
    if spec.get("choose_from_list"):
        return dict(kind="choose_from_list", note=spec.get("note", "Pick this button from the platform's own list."))
    if not spec.get("max") and not spec.get("recommended"):
        return dict(kind="unknown", note="No saved limit for this platform and field. This does not mean unlimited.")
    sources = [config["limits"]["sources"][s] for s in spec.get("sources", []) if s in config["limits"]["sources"]]
    return dict(kind="limit", max=spec.get("max"), recommended=spec.get("recommended"), note=spec.get("note", ""),
                sources=[dict(title=s["title"], url=s["url"], checked=s["checked"]) for s in sources])


def layout_budget(item, run_dir):
    """About how many characters fit a text box in an image or on screen at 85% type size, measured from the English
    in it. A target for the translator; the image tool and the video step still measure the real words."""
    if item["surface"] == "image" and item.get("block"):
        path = Path(run_dir) / "images" / f"{Path(item['file']).stem}.boxes.json"
        block = next((b for b in load(path)["blocks"] if b["id"] == item["block"]), None) if path.exists() else None
        inked = [l for l in (block or {}).get("lines", []) if l.get("ink")]
        if not inked:
            return None
        per_char = sum(l["ink"][2] for l in inked) / max(1, sum(len(l["text"]) for l in inked))
        icon = block.get("trailing_icon")
        width, lines = block["max_width"] - (icon[2] * 1.5 if icon else 0), block["max_lines"]
    elif item["surface"] == "screen" and item.get("box"):
        per_char = item["box"][2] / max(1, len(item["text"]))
        width, lines = item.get("max_width", item["box"][2]), 1
    else:
        return None
    per_line = width / (per_char * 0.85)
    return int(per_line if lines == 1 else lines * per_line * 0.9)


def build_lines(config, input_dir, run_dir, languages=None):
    """Turn text ads plus media-text.json into fields (each place text appears) and lines (what Claude translates)."""
    input_dir, run_dir = Path(input_dir), Path(run_dir)
    files = sorted(p for p in input_dir.iterdir() if p.is_file() and not p.name.startswith(".") and p.name != "README.md")
    rows = []
    for path in files:
        if path.suffix.lower() in (".csv", ".json"):
            rows += read_text_ads(path)
    media_path = run_dir / "media-text.json"
    media_items = load(media_path).get("items", []) if media_path.exists() else []
    for n, item in enumerate(media_items, 1):
        if item.get("surface") not in SURFACE_FIELDS or not str(item.get("text", "")).strip() or not item.get("file"):
            raise ValueError(f"media-text.json item {n}: needs file, surface (image, spoken or screen) and text.")
        rows.append(dict(ad=Path(item["file"]).stem, platform="image" if item["surface"] == "image" else "video",
                         field=SURFACE_FIELDS[item["surface"]], text=item["text"].strip(), file=Path(item["file"]).name,
                         media_id=item.get("id"), surface=item["surface"], budget=layout_budget(item, run_dir)))
    markets = [m for m in config["markets"] if not languages or m["code"] in languages]
    if languages and len(markets) != len(set(languages)):
        raise ValueError(f"Unknown language code in --languages. Configured: {', '.join(m['code'] for m in config['markets'])}.")
    fields, seen = [], Counter()
    for row in rows:
        base = f"{slug(row['ad'])}.{row['platform']}.{row['field']}"
        seen[base] += 1
        field_id = base if seen[base] == 1 else f"{base}_{seen[base]}"
        names = [n for n in config["never_translate"] if name_count(row["text"], n)]
        limit = limit_for(config, row["platform"], row["field"], row["text"])
        if row.get("budget"):
            limit.update(recommended=row["budget"], note=f"About {row['budget']} characters fit this box at 85% type size "
                         "(estimated from the English). The image tool flags text that still overflows.")
        fields.append(dict(id=row.get("media_id") or field_id, ad=row["ad"], platform=row["platform"], field=row["field"],
                           surface=row.get("surface", "ad"), text=row["text"], file=row["file"], names=names, limit=limit))
    if len({f["id"] for f in fields}) != len(fields):
        raise ValueError("Two texts share the same id. Give every media-text.json item its own id.")
    lines = {}
    for field in fields:
        signature = json.dumps([field["text"], field["platform"], field["field"], field["limit"], field["names"]],
                               sort_keys=True, ensure_ascii=False)
        line_id = "L" + hashlib.sha256(signature.encode()).hexdigest()[:10]
        field["line_id"] = line_id
        for market in markets:
            key = line_id, market["code"]
            if key not in lines:
                lines[key] = dict(line_id=line_id, language=market["code"], text=field["text"], platform=field["platform"],
                                  field=field["field"], surface=field["surface"], limit=field["limit"],
                                  names=field["names"], field_ids=[])
            lines[key]["field_ids"].append(field["id"])
    media = dict(images=[p.name for p in files if p.suffix.lower() in IMAGE_TYPES],
                 videos=[p.name for p in files if p.suffix.lower() in VIDEO_TYPES])
    return dict(label=LABEL, built_at=now(), config=config["folder"], input=str(input_dir),
                source_language=config["source_language"], markets=markets, never_translate=config["never_translate"],
                names_in_context=config.get("names_in_context", []), fields=fields, lines=list(lines.values()), media=media)


def new_run_dir(example):
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    base = ROOT / "output" / (stamp + ("-example" if example else ""))
    path, n = base, 1
    while path.exists():
        n += 1
        path = base.with_name(f"{base.name}-{n}")
    path.mkdir(parents=True)
    return path


def prepare(args):
    example = "--example" in args
    languages = None
    if "--languages" in args:
        value = (args[args.index("--languages") + 1:] or [""])[0]
        languages = [c.strip() for c in value.split(",") if c.strip()] if not value.startswith("-") else []
        if not languages:
            raise SystemExit("Add the language codes after --languages, for example --languages fr,es")
    runs = [a for a in args if a.startswith("output") or Path(a).is_dir()]
    if runs:
        run_dir = Path(runs[0]).resolve()
        old = load(run_dir / "lines.json")
        config, problems, warnings = read_config(old["config"])
        languages = languages or [m["code"] for m in old["markets"]]
        input_dir = Path(old["input"])
    else:
        config, problems, warnings = read_config(config_dir(example))
        input_dir = ROOT / ("examples/input" if example else "input")
        run_dir = None
    if config is None:
        raise SystemExit("\n".join(problems))
    data = build_lines(config, input_dir, run_dir or ROOT / "output/.none", languages)
    if not data["fields"] and not data["media"]["images"] and not data["media"]["videos"]:
        raise SystemExit(f"Nothing to localize in {input_dir}. Drop text ads (.csv/.json), images or videos there.")
    run_dir = run_dir or new_run_dir(example)
    if data["media"]["images"]:
        (run_dir / "images").mkdir(exist_ok=True)  # the image tool writes here and will not create folders
    save(run_dir / "lines.json", data)
    print(json.dumps(dict(run=str(run_dir.relative_to(ROOT)), languages=[m["code"] for m in data["markets"]],
                          text_fields=len(data["fields"]), lines_to_translate=len(data["lines"]),
                          images=data["media"]["images"], videos=data["media"]["videos"],
                          warnings=warnings), ensure_ascii=False, indent=1))


# ---------- check ----------

def check(run_dir):
    """Check drafts.json against lines.json. Returns the report; severity error blocks the kit."""
    run_dir = Path(run_dir)
    data = load(run_dir / "lines.json")
    drafts = load(run_dir / "drafts.json") if (run_dir / "drafts.json").exists() else {"translations": []}
    findings, variants = [], []

    def flag(code, severity, detail, **refs):
        findings.append(dict(code=code, severity=severity, detail=detail, **refs))

    expected = {(l["line_id"], l["language"]): l for l in data["lines"]}
    rows = {}
    for n, row in enumerate(drafts.get("translations", []), 1):
        key = row.get("line_id"), row.get("language")
        refs = dict(line_id=key[0], language=key[1])
        if key in rows:
            flag("duplicate", "error", "This line and language appear twice in drafts.json.", **refs)
        elif key not in expected:
            flag("unexpected", "error", "drafts.json has a line that is not in lines.json (old run or typo in line_id).", **refs)
        else:
            rows[key] = row
    for key in expected.keys() - rows.keys():
        flag("missing", "error", "No translation yet.", line_id=key[0], language=key[1])

    for key, row in rows.items():
        line, refs = expected[key], dict(line_id=key[0], language=key[1])
        short, note = row.get("short_version"), row.get("shortening_note")
        if (short is None) != (note is None) or (short is not None and not (str(short).strip() and str(note).strip())):
            flag("short_without_note", "error", "A short version and a note on what it leaves out must come together.", **refs)
        for variant, value in (("full", row.get("translation")), ("short", short)):
            if variant == "short" and value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                flag("empty", "error", f"The {variant} translation is empty.", variant=variant, **refs)
                continue
            count, limit = characters(value, line["platform"]), line["limit"]
            result = "no_limit"
            if limit["kind"] == "limit":
                if limit.get("max") and count > limit["max"]:
                    result = "over_max"
                    flag("over_max", "fail", f"{count} characters; the platform allows {limit['max']}.", variant=variant, **refs)
                elif limit.get("recommended") and count > limit["recommended"]:
                    english = characters(line["text"], line["platform"])
                    if english > limit["recommended"] and count <= english * LONGER_THAN_ENGLISH:
                        result = "over_recommended_like_english"  # the English is cut off there too
                    else:
                        result = "over_recommended"
                        flag("over_recommended", "review", f"{count} characters; the platform recommends {limit['recommended']} or "
                             f"fewer so it is not cut off" + (f", and the English has {english}." if english > limit["recommended"]
                                                              else "."), variant=variant, **refs)
                else:
                    result = "fits"
            elif limit["kind"] == "layout" and limit.get("recommended"):
                result = "fits" if count <= limit["recommended"] else "over_budget"
                if result == "over_budget":
                    flag("over_budget", "review", f"{count} characters; about {limit['recommended']} fit this box. "
                         "Write a short version that fits.", variant=variant, **refs)
            elif limit["kind"] == "timing":
                result = "over_estimate" if count > limit["recommended"] else "within_estimate"
                if result == "over_estimate":
                    flag("long_for_timing", "review", f"{count} characters against an estimate of {limit['recommended']}. "
                         "It may not fit the video; a shorter version may be needed.", variant=variant, **refs)
            for name in line["names"]:
                need, found = name_count(line["text"], name), name_count(value, name)
                if found < need:
                    flag("name_changed", "error", f"\"{name}\" must stay exactly as written ({need}x in English, {found}x here).",
                         variant=variant, **refs)
                elif found > need:
                    flag("name_added", "review", f"\"{name}\" appears more often than in the English.", variant=variant, **refs)
            if variant == "full" and digit_groups(value) != digit_groups(line["text"]):
                flag("numbers_differ", "review", "The numbers differ from the English. Check amounts, units and dates.",
                     variant=variant, **refs)
            if variant == "short" and short_numbers_lost(line["text"], value):
                flag("numbers_differ", "review", "The short version drops or changes a number. Check it still says the same thing.",
                     variant=variant, **refs)
            variants.append(dict(line_id=key[0], language=key[1], variant=variant, text=value, characters=count,
                                 limit=limit, result=result))
        if short is not None and note:
            flag("shortened", "review", "Shortened version leaves out: " + str(note), variant="short", **refs)
        if row.get("review_note"):
            flag("translator_note", "review", str(row["review_note"]), **refs)
        if line["limit"]["kind"] in ("choose_from_list", "unknown") or (line["limit"]["kind"] == "layout" and not line["limit"].get("recommended")):
            flag(line["limit"]["kind"], "review", line["limit"]["note"], **refs)
        full_over = any(v["result"] == "over_max" for v in variants if (v["line_id"], v["language"], v["variant"]) == (*key, "full"))
        short_fits = any(v["result"] == "fits" for v in variants if (v["line_id"], v["language"], v["variant"]) == (*key, "short"))
        if full_over and not short_fits:
            flag("no_version_fits", "fail", "No version fits the platform limit. Write a short version or rethink the line.", **refs)

    wording = defaultdict(lambda: defaultdict(list))
    for key, row in rows.items():
        if isinstance(row.get("translation"), str):
            wording[expected[key]["text"], key[1]][row["translation"]].append(key[0])
    for (english, language), versions in wording.items():
        if len(versions) > 1:
            flag("same_english_different_translation", "review",
                 f"\"{english}\" is translated {len(versions)} different ways. Pick one unless the difference is on purpose.",
                 language=language, line_ids=sorted(i for ids in versions.values() for i in ids))

    severities = Counter(f["severity"] for f in findings)
    report = dict(label=LABEL, checked_at=now(), summary=dict(
        lines=len(expected), translated=len(rows), variants=len(variants), errors=severities["error"],
        fails=severities["fail"], to_review=severities["review"], by_code=dict(Counter(f["code"] for f in findings))),
        findings=findings, variants=variants,
        not_checked=["Meaning: read the blind back-translation and the meaning notes", "Native-speaker review",
                     "How the ad looks in the ad platform's preview", "Image and video text fit (see the media checks)",
                     "Voice timing and pronunciation (listen to the video)"])
    return report


def short_numbers_lost(english, short):
    """A short version may drop words, but a number it keeps must still be in the English."""
    english_groups = Counter(digit_groups(english))
    return any(count > english_groups[g] for g, count in Counter(digit_groups(short)).items())


def check_command(run_dir):
    report = check(run_dir)
    save(Path(run_dir) / "checks.json", report)
    print(json.dumps(report["summary"], ensure_ascii=False))
    for finding in report["findings"]:
        if finding["severity"] in ("error", "fail"):
            print(f"- {finding['severity'].upper()} {finding['code']} {finding.get('language', '')} "
                  f"{finding.get('line_id', '')} {finding.get('variant', '')}: {finding['detail']}")
    if report["summary"]["errors"]:
        raise SystemExit(f"{report['summary']['errors']} error(s) to fix in drafts.json before the kit can be built.")


def words(run_dir, name, code, surface=None):
    """The checked translation (full, plus short when there is one) for each text block of one media file,
    keyed by the tool's block id. Refuses while the drafts have check errors."""
    run_dir = Path(run_dir)
    report = check(run_dir)
    if report["summary"]["errors"]:
        raise SystemExit("Fix the check errors first: python3 scripts/job.py check " + str(run_dir))
    data = load(run_dir / "lines.json")
    market = next((m for m in data["markets"] if m["code"] == code), None)
    if not market:
        raise SystemExit(f"{code} is not a language in this run.")
    media = {i["id"]: i for i in load(run_dir / "media-text.json")["items"]}
    drafts = {(r["line_id"], r["language"]): r for r in load(run_dir / "drafts.json")["translations"]}
    blocks = {}
    for field in data["fields"]:
        item = media.get(field["id"])
        if not item or Path(item["file"]).stem != name or (surface and item["surface"] != surface):
            continue
        row = drafts[field["line_id"], code]
        entry = {"text": row["translation"]}
        if row.get("short_version"):
            entry["short"] = row["short_version"]
        blocks[item.get("block") or item["id"]] = entry
    if not blocks:
        raise SystemExit(f"No text items for {name} in media-text.json.")
    return {"language": market["language"], "code": code, "label": LABEL, "blocks": blocks}


def main(argv):
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return
    if not argv or argv[0] not in ("config", "prepare", "check", "words"):
        raise SystemExit(__doc__)
    if argv[0] == "config":
        config, problems, warnings = read_config(config_dir("--example" in argv))
        for line in problems + warnings:
            print("- " + line)
        if config is None:
            raise SystemExit("Config is not ready: fix the points above (or run /setup again).")
        print(json.dumps(dict(ok=True, languages=[f"{m['language']} ({m['code']}, {m['market']}, {m['address']})"
                                                  for m in config["markets"]],
                              platforms=config["platforms"], never_translate=config["never_translate"],
                              names_in_context=config["names_in_context"]),
                         ensure_ascii=False, indent=1))
    elif argv[0] == "prepare":
        prepare(argv[1:])
    elif argv[0] == "words":
        if len(argv) != 4:
            raise SystemExit("Usage: python3 scripts/job.py words output/RUN NAME CODE")
        out = Path(argv[1]) / "images" / f"{argv[2]}.{argv[3]}.words.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H%M%S")
        for old in (out, out.with_name(f"{argv[2]}.{argv[3]}.png"), out.with_name(f"{argv[2]}.{argv[3]}.json")):
            if old.exists():  # an earlier attempt is kept in images/old/, never deleted
                (old.parent / "old").mkdir(exist_ok=True)
                old.rename(old.parent / "old" / f"{stamp}-{old.name}")
        save(out, words(argv[1], argv[2], argv[3], surface="image"))
        print(json.dumps(dict(words=str(out), blocks=len(load(out)["blocks"]))))
    else:
        if len(argv) != 2:
            raise SystemExit("Usage: python3 scripts/job.py check output/RUN")
        check_command(argv[1])


if __name__ == "__main__":
    use_venv()
    try:
        main(sys.argv[1:])
    except (ValueError, FileNotFoundError) as error:
        raise SystemExit(str(error)) from None
