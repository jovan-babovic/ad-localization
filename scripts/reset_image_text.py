"""Erase the English text boxes on a flat ad image and set translations in the same boxes.

  python3 reset_image_text.py detect IMAGE NEW_BOXES.json [--fonts FONTS.txt]
  python3 reset_image_text.py apply IMAGE BOXES.json TRANSLATIONS.json NEW_OUT.png

detect  reads the text lines (Apple Vision via ocr_lines.swift, macOS), groups them into blocks and
        measures each block: ink box, text colour(s), closest available font, size, line pitch,
        letter spacing and alignment. Every block starts as "action": "review"; a person or an AI
        sets each one to "replace" or "preserve" (logos, names, text over photos) before apply.
apply   repaints only the pixels inside each "replace" box that differ from the surrounding
        background (smooth fill sampled from a band around the box), then sets the translation in
        the same box, same font, colour and alignment. It may shrink the type to min_scale of the
        original size. If the text still does not fit, the image is written anyway and the block is
        FLAGGED "overflow: needs shorter copy" with an estimated character capacity. Nothing else in
        the image is moved, resized or redrawn. OUT.json records every box, font and flag.

TRANSLATIONS.json: {"language": "French", "blocks": {"<block id>": "text" | {"text": "...", "short": "...", "highlight": ["49 %"]}}}
Optional block keys a reviewer may set: "transform": "upper" (source set in capitals), "highlight" (terms drawn in
"highlight_colour"), "trailing_icon" [x,y,w,h] (an arrow that follows the end of the text), "max_width", "max_lines".
"shape" [x,y,w,h] is found by detect for a one-line label inside a plain filled shape (a button): a label centred in
it stays centred in it, keeps its vertical centre when the type shrinks, and may use some of the side padding.
Requires Pillow and numpy. No network, no paid services, no company data in this file.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

VENV = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"
if __name__ == "__main__" and VENV.exists() and Path(sys.prefix).resolve() != VENV.parent.parent.resolve():
    os.execv(str(VENV), [str(VENV)] + sys.argv)  # run under the folder's .venv (README), where Pillow and numpy live

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

VERSION = "1.3"  # 1.1 natural word spaces, 1.2 glyphs measured without compression halos, 1.3 labels centred in buttons
FONT_DIRS = ["~/Library/Fonts", "/Library/Fonts", "/System/Library/Fonts", "/System/Library/Fonts/Supplemental"]
DEFAULT_FONTS = [  # file names looked up in FONT_DIRS; "name#index" picks a face in a .ttc, "name@Style" a variable style
    "Inter_24pt-Regular.ttf", "Inter_24pt-Medium.ttf", "Inter_24pt-SemiBold.ttf",
    "DMSans-Regular.ttf", "DMSans-Medium.ttf", "DMSans-SemiBold.ttf",
    "Montserrat-Regular.ttf", "Montserrat-Medium.ttf", "Montserrat-SemiBold.ttf",
    "ZalandoSans-Regular.ttf", "ZalandoSans-Medium.ttf", "ZalandoSans_SemiExpanded-Regular.ttf",
    "ZalandoSans_SemiExpanded-Medium.ttf", "ZalandoSans_Expanded-Regular.ttf", "ZalandoSans_Expanded-Medium.ttf",
    "SourceSans3-Regular.ttf", "SourceSans3-SemiBold.ttf",
    "Arial.ttf", "Arial Bold.ttf", "HelveticaNeue.ttc#0", "HelveticaNeue.ttc#10",
    "Avenir Next.ttc#7", "Avenir Next.ttc#5", "SFNS.ttf@Regular", "SFNS.ttf@Medium",
    "SFNSMono.ttf@Regular", "SFNSMono.ttf@Medium", "Menlo.ttc#0", "PTMono.ttc#1", "Andale Mono.ttf", "Courier New.ttf",
]
DIFF = 30          # colour distance (0-441) that counts as "not background"
MIN_SCALE = 0.85   # smallest type size allowed, as a share of the original


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resolve_font(spec):
    """Full path spec for a font name, or None when the file is not installed."""
    spec = str(Path(spec).expanduser()) if spec.startswith("~") else spec
    name = spec.split("#")[0].split("@")[0]
    if Path(name).is_absolute():
        return spec if Path(name).exists() else None
    for folder in FONT_DIRS:
        if (Path(folder).expanduser() / name).exists():
            return str(Path(folder).expanduser() / name) + spec[len(name):]
    return None


def load_font(spec, size):
    path, _, style = spec.partition("@")
    path, _, index = path.partition("#")
    font = ImageFont.truetype(path, size=max(1.0, size), index=int(index or 0))
    if style:
        font.set_variation_by_name(style)
    return font


def to_rect(r):
    x, y, w, h = r
    return [int(round(x)), int(round(y)), int(round(w)), int(round(h))]


def clip(rect, shape):
    x, y, w, h = rect
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(shape[1], x + w), min(shape[0], y + h)
    return [x0, y0, max(0, x1 - x0), max(0, y1 - y0)]


def grow(rect, n, shape):
    x, y, w, h = rect
    return clip([x - n, y - n, w + 2 * n, h + 2 * n], shape)


def smooth1d(a, k=9):
    """Median over a sliding window, so thin crossing lines do not leak into the fill."""
    if len(a) < 3:
        return a
    k = min(k, len(a) - (1 - len(a) % 2))
    pad = k // 2
    padded = np.pad(a, ((pad, pad), (0, 0)), mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(padded, k, axis=0), axis=-1)


def background(img, rect, pad=6):
    """Smooth background estimate for rect: a Coons patch built from the band just outside it.
    Returns (H, W, 3) float array and the band's residual noise (std)."""
    x, y, w, h = rect
    H, W = img.shape[:2]
    bands, resid = {}, []
    if y - pad >= 0:
        bands["t"] = np.median(img[y - pad:y, x:x + w], axis=0)
    if y + h + pad <= H:
        bands["b"] = np.median(img[y + h:y + h + pad, x:x + w], axis=0)
    if x - pad >= 0:
        bands["l"] = np.median(img[y:y + h, x - pad:x], axis=1)
    if x + w + pad <= W:
        bands["r"] = np.median(img[y:y + h, x + w:x + w + pad], axis=1)
    if not bands:
        raise ValueError("No background band around box")
    for key, profile in list(bands.items()):
        smoothed = smooth1d(profile)
        resid.append(float(np.abs(profile - smoothed).mean()))
        bands[key] = smoothed
    # A missing side (image edge) borrows the opposite side, or the mean of what exists.
    mean = np.mean([b.mean(axis=0) for b in bands.values()], axis=0)
    t = bands.get("t", bands.get("b", np.tile(mean, (w, 1))))
    b = bands.get("b", t)
    l = bands.get("l", bands.get("r", np.tile(mean, (h, 1))))
    r = bands.get("r", l)
    u = np.linspace(0, 1, w)[None, :, None]
    v = np.linspace(0, 1, h)[:, None, None]
    corners = ((1 - u) * (1 - v) * t[0] + u * (1 - v) * t[-1] + (1 - u) * v * b[0] + u * v * b[-1])
    patch = (1 - v) * t[None] + v * b[None] + (1 - u) * l[:, None] + u * r[:, None] - corners
    return np.clip(patch, 0, 255), max(resid) if resid else 0.0


def dilate(mask, r):
    out = mask.copy()
    p = np.pad(mask, r)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out |= p[r + dy:r + dy + mask.shape[0], r + dx:r + dx + mask.shape[1]]
    return out


def components(mask):
    """Connected pixel groups (8-neighbour) as lists of (row, col)."""
    seen, groups = np.zeros_like(mask), []
    for start in zip(*np.nonzero(mask)):
        if seen[start]:
            continue
        stack, group = [start], []
        seen[start] = True
        while stack:
            r, c = stack.pop()
            group.append((r, c))
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < mask.shape[0] and 0 <= cc < mask.shape[1] and mask[rr, cc] and not seen[rr, cc]:
                        seen[rr, cc] = True
                        stack.append((rr, cc))
        groups.append(group)
    return groups


def ink(img, rect, pad=6, core=None):
    """Tight box of non-background pixels in rect, the mask, and the background patch.
    With core (the OCR line box), pixel groups centred outside its rows are dropped:
    they belong to the line above or below (descenders, ascenders)."""
    rect = clip(rect, img.shape)
    x, y, w, h = rect
    bg, noise = background(img, rect, pad)
    diff = np.linalg.norm(img[y:y + h, x:x + w] - bg, axis=2)
    mask = diff > max(DIFF, 0.3 * diff.max())  # glyph body, not the compression halo around small type
    if core is not None and mask.any():
        top, bottom = core[1] - y, core[1] + core[3] - y
        reach = 0.35 * core[3]
        for group in components(mask):
            rows = [g[0] for g in group]
            centre_out = not top <= (min(rows) + max(rows)) / 2 <= bottom
            from_above = min(rows) == 0 and max(rows) < top + reach
            from_below = max(rows) == h - 1 and min(rows) > bottom - reach
            if centre_out or from_above or from_below:
                for g in group:
                    mask[g] = False
            elif min(rows) == 0 or max(rows) == h - 1:  # touches a neighbouring line: keep only its part in the core rows
                for g in group:
                    if not top <= g[0] <= bottom:
                        mask[g] = False
    if not mask.any():
        return None, mask, bg, diff, noise
    ys, xs = np.nonzero(mask)
    return [x + xs.min(), y + ys.min(), xs.max() - xs.min() + 1, ys.max() - ys.min() + 1], mask, bg, diff, noise


def hexcolor(c):
    return "#%02x%02x%02x" % tuple(int(round(v)) for v in c)


def text_colours(img, rect, mask, diff):
    """Main text colours: strong ink pixels binned coarsely, near bins merged, shares of the ink."""
    x, y, w, h = rect
    if not mask.any():
        return []
    strong = mask & (diff > 0.6 * diff[mask].max())
    pixels = img[y:y + h, x:x + w][strong]
    if len(pixels) < 4:
        return []
    bins = {}
    for key, pixel in zip(map(tuple, (pixels // 24).astype(int)), pixels):
        bins.setdefault(key, []).append(pixel)
    groups = []
    for members in sorted(bins.values(), key=len, reverse=True):
        centre = np.mean(members, axis=0)
        near = [g for g in groups if np.linalg.norm(g[1] - centre) < 70]
        if near:
            near[0][0].extend(members)
        else:
            groups.append([list(members), centre])
    total = len(pixels)
    return [{"colour": hexcolor(np.median(g[0], axis=0)), "share": round(len(g[0]) / total, 3)}
            for g in groups if len(g[0]) / total >= 0.03]


def draw_line(draw, xy, text, font, colour, tracking=0.0, highlights=(), highlight_colour=None):
    """Draw one line at a left-baseline point; returns its advance width."""
    x, y = xy
    spans = [(text, colour)]
    for term in highlights:
        out = []
        for chunk, c in spans:
            parts = chunk.split(term)
            for i, part in enumerate(parts):
                if part:
                    out.append((part, c))
                if i < len(parts) - 1:
                    out.append((term, highlight_colour or c))
        spans = out
    start = x
    for chunk, c in spans:
        if tracking:
            for ch in chunk:
                if draw is not None:
                    draw.text((x, y), ch, font=font, fill=c, anchor="ls")
                x += font.getlength(ch) + (0 if ch == " " else tracking)  # word spaces keep their natural width
        else:
            if draw is not None:
                draw.text((x, y), chunk, font=font, fill=c, anchor="ls")
            x += font.getlength(chunk)
    return x - start - (tracking if tracking and text else 0)


def wrap(text, font, width, tracking):
    lines, current = [], ""
    for word in text.split():
        trial = (current + " " + word).strip()
        if not current or draw_line(None, (0, 0), trial, font, None, tracking) <= width:
            current = trial
        else:
            lines.append(current)
            current = word
    return lines + ([current] if current else [])


def match_font(img, line, fonts):
    """Closest candidate font for one measured line: same text, same ink height, best shape overlap."""
    box, mask = line["ink"], line["_mask"]
    if box is None or len(line["text"].replace(" ", "")) < 3:
        return None
    x, y, w, h = box
    lx, ly = line["_rect"][:2]
    src = mask[y - ly:y - ly + h, x - lx:x - lx + w]
    best = None
    for spec in fonts:
        try:
            font = load_font(spec, 100)
        except OSError:
            continue
        l0, t0, r0, b0 = font.getbbox(line["text"], anchor="ls")
        if b0 - t0 <= 0:
            continue
        size = 100 * h / (b0 - t0)
        font = load_font(spec, size)
        natural = draw_line(None, (0, 0), line["text"], font, None)
        tracking = (w - natural) / max(1, len(line["text"]) - 1)
        if abs(tracking) > 0.5 * size:
            continue
        canvas = Image.new("L", (int(w * 1.5) + 20, int(h * 2) + 20), 0)
        d = ImageDraw.Draw(canvas)
        draw_line(d, (10, 10 + size), line["text"], font, 255, tracking if abs(tracking) > 0.01 * size else 0)
        arr = np.array(canvas) > 96
        if not arr.any():
            continue
        ys, xs = np.nonzero(arr)
        crop = Image.fromarray((arr[ys.min():ys.max() + 1, xs.min():xs.max() + 1] * 255).astype(np.uint8)).resize((w, h))
        rendered = np.array(crop) > 127
        iou = (rendered & src).sum() / max(1, (rendered | src).sum())
        score = iou - 0.1 * abs(tracking) / size
        if best is None or score > best["score"]:
            best = {"font": spec, "size": round(size, 2), "tracking": round(tracking if abs(tracking) > 0.01 * size else 0, 2),
                    "shape_overlap": round(float(iou), 3), "score": float(score)}
    return best


def filled_shape(img, rect, tol=DIFF):
    """The plain shape a one-line label sits in (a button, a tag): walk out from the text along its middle row
    and column while the colour around the text continues. None when that colour runs to the image edge or the
    shape is much taller than the text (a panel, not a button)."""
    x, y, w, h = rect
    H, W = img.shape[:2]
    if x < 3 or y < 3 or x + w + 3 > W or y + h + 3 > H:
        return None
    ring = np.concatenate([img[y - 3, x:x + w], img[y + h + 2, x:x + w], img[y:y + h, x - 3], img[y:y + h, x + w + 2]])
    fill = np.median(ring, axis=0)
    if np.abs(ring - fill).mean() > 6:  # not a flat fill
        return None
    cx, cy = x + w // 2, y + h // 2

    def walk(line, i, step):
        while 0 <= i < len(line):
            if np.linalg.norm(line[i] - fill) > tol:
                return i
            i += step
        return None
    left, right = walk(img[cy], x - 2, -1), walk(img[cy], x + w + 1, 1)
    top, bottom = walk(img[:, cx], y - 2, -1), walk(img[:, cx], y + h + 1, 1)
    if None in (left, right, top, bottom) or bottom - top > 4 * h or right - left > 0.9 * W:
        return None
    return [int(left + 1), int(top + 1), int(right - left - 1), int(bottom - top - 1)]


def group_lines(lines):
    """Same-baseline fragments become one line; stacked lines of one style become one block."""
    lines = sorted(lines, key=lambda l: (l["rect"][1], l["rect"][0]))
    merged = []
    for line in lines:
        x, y, w, h = line["rect"]
        for m in merged:
            mx, my, mw, mh = m["rect"]
            overlap = min(y + h, my + mh) - max(y, my)
            gap = x - (mx + mw)
            if overlap > 0.6 * min(h, mh) and -0.5 * min(h, mh) <= gap < 2.5 * max(h, mh):
                m["text"] += " " + line["text"]
                m["rect"] = [mx, min(y, my), x + w - mx, max(y + h, my + mh) - min(y, my)]
                break
        else:
            merged.append({"text": line["text"], "rect": list(line["rect"])})
    blocks = []
    for line in merged:
        x, y, w, h = line["rect"]
        last = blocks[-1]["lines"][-1] if blocks else None
        if last:
            lx, ly, lw, lh = last["rect"]
            same_size = 0.8 <= h / lh <= 1.25
            close = -0.3 * lh <= y - (ly + lh) < 0.9 * lh
            aligned = abs(x - lx) < 0.6 * lh or abs((x + w / 2) - (lx + lw / 2)) < 0.6 * lh
            if same_size and close and aligned:
                blocks[-1]["lines"].append(line)
                continue
        blocks.append({"lines": [line]})
    return blocks


def detect(image_path, out_path, fonts=None):
    image_path, out_path = Path(image_path).resolve(), Path(out_path)
    if out_path.exists():
        raise FileExistsError(out_path)
    swift = Path(__file__).with_name("ocr_lines.swift")
    result = subprocess.run(["swift", str(swift), str(image_path)], capture_output=True, text=True, check=True)
    ocr = json.loads(result.stdout)
    img = np.asarray(Image.open(image_path).convert("RGB"), dtype=np.float64)
    fonts = [f for f in (resolve_font(x) for x in (fonts or DEFAULT_FONTS)) if f]
    blocks = []
    for n, group in enumerate(group_lines([{"text": l["text"], "rect": l["rect"]} for l in ocr["lines"]]), 1):
        lines = []
        for line in group["lines"]:
            rect = grow(to_rect(line["rect"]), 4, img.shape)
            box, mask, bg, diff, noise = ink(img, rect, core=to_rect(line["rect"]))
            baseline = None
            if box:
                counts = mask.sum(axis=1)
                first, last = box[1] - rect[1], box[1] + box[3] - rect[1] - 1
                lower = range(first + int(0.4 * (last - first)), last + 1)
                drops = [(counts[r] - (counts[r + 1] if r + 1 < len(counts) else 0), r) for r in lower]
                baseline = int(rect[1] + max(drops)[1] + 1)
            lines.append({"text": line["text"], "ocr_rect": to_rect(line["rect"]), "ink": [int(v) for v in box] if box else None,
                          "baseline": baseline, "_mask": mask, "_rect": rect, "_diff": diff})
        inks = [l["ink"] for l in lines if l["ink"]]
        if not inks:
            continue
        x0, y0 = min(b[0] for b in inks), min(b[1] for b in inks)
        x1, y1 = max(b[0] + b[2] for b in inks), max(b[1] + b[3] for b in inks)
        matches = [m for m in (match_font(img, l, fonts) for l in lines) if m]
        votes = {}
        for m in matches:
            votes[m["font"]] = votes.get(m["font"], 0) + m["shape_overlap"]
        font = max(votes, key=votes.get) if votes else fonts[0]
        sizes = [m for m in matches if m["font"] == font] or matches
        lefts = [b[0] for b in inks]
        centres = [b[0] + b[2] / 2 for b in inks]
        rights = [b[0] + b[2] for b in inks]
        spread = lambda v: max(v) - min(v)
        align = "left" if len(inks) == 1 or spread(lefts) <= 6 else "center" if spread(centres) <= 6 else "right" if spread(rights) <= 6 else "left"
        found = {}
        for l in lines:
            for c in text_colours(img, l["_rect"], l["_mask"], l["_diff"]):
                found[c["colour"]] = found.get(c["colour"], 0) + c["share"] / len(lines)
        merged = []
        for colour, share in sorted(found.items(), key=lambda kv: -kv[1]):
            rgb = np.array([int(colour[i:i + 2], 16) for i in (1, 3, 5)])
            near = [m for m in merged if np.linalg.norm(m["_rgb"] - rgb) < 60]
            if near:
                near[0]["share"] += share
            else:
                merged.append({"colour": colour, "share": share, "_rgb": rgb})
        colours = [{"colour": m["colour"], "share": round(m["share"], 3)} for m in merged if m["share"] >= 0.03]
        size = float(np.median([m["size"] for m in sizes])) if sizes else float(inks[0][3])
        bases = [l["baseline"] for l in lines if l["baseline"]]
        pitch = float(np.median(np.diff(bases))) if len(bases) > 1 else round(size * 1.2, 2)
        font_obj = load_font(font, size)
        rect = [int(x0), int(y0), int(x1 - x0), int(y1 - y0)]
        shape = filled_shape(img, rect) if len(lines) == 1 else None
        max_width = rect[2]
        if shape:  # a label in a button: centred in the shape if it was, and may use some of the side padding
            pad = min(rect[0] - shape[0], shape[0] + shape[2] - rect[0] - rect[2])
            if abs(rect[0] + rect[2] / 2 - (shape[0] + shape[2] / 2)) <= max(3, 0.06 * shape[2]):
                align = "center"
                max_width = max(rect[2], int(shape[2] - 2 * 0.6 * pad))
        blocks.append({
            "id": f"b{n}", "action": "review", "source_text": " ".join(l["text"] for l in lines),
            "lines": [{"text": l["text"], "ocr_rect": l["ocr_rect"], "ink": l["ink"], "baseline": l["baseline"]} for l in lines],
            "rect": rect, "shape": shape, "max_width": max_width, "max_lines": len(lines), "align": align,
            "colour": colours[0]["colour"] if colours else "#ffffff", "colours_found": colours,
            "highlight": [], "highlight_colour": colours[1]["colour"] if len(colours) > 1 else None,
            "font": font, "font_size": round(size, 2), "line_pitch": round(pitch, 2),
            "cap_top_offset": round(-font_obj.getbbox("H", anchor="ls")[1], 2),
            "tracking": float(np.median([m["tracking"] for m in sizes])) if sizes else 0.0,
            "font_match": sizes[0] if sizes else None, "trailing_icon": None, "note": ""})
    out = {"tool": "reset_image_text.py", "version": VERSION, "image": str(image_path), "image_sha256": sha256(image_path),
           "size": [int(img.shape[1]), int(img.shape[0])], "ocr_engine": ocr.get("engine"),
           "min_scale": MIN_SCALE, "fonts_considered": fonts, "blocks": blocks,
           "instructions": "Set each block's action to replace or preserve. Check rect, max_width, max_lines, colour, "
                           "highlight terms and trailing_icon ([x,y,w,h] of an arrow or icon that follows the text) before apply."}
    with out_path.open("x") as handle:
        handle.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"blocks": len(blocks), "texts": [b["source_text"] for b in blocks]}, ensure_ascii=False))
    return out


def erase(img, rect, pad=6):
    """Repaint the non-background pixels of rect with the smooth background around it."""
    rect = clip(rect, img.shape)
    x, y, w, h = rect
    bg, noise = background(img, rect, pad)
    region = img[y:y + h, x:x + w]
    if noise < 4:  # smooth background: repaint the whole box, which also removes compression halos
        mask = np.ones(region.shape[:2], dtype=bool)
    else:  # textured: repaint only what differs, so lines or texture crossing the box survive
        mask = dilate(np.linalg.norm(region - bg, axis=2) > DIFF * 0.4, 3)
    rng = np.random.default_rng(0)
    grain = rng.normal(0, min(noise, 3.0), region.shape) if noise > 0.5 else 0
    region[mask] = np.clip(bg + grain, 0, 255)[mask]
    return {"erased_pixels": int(mask.sum()), "band_noise": round(noise, 2),
            "fill": "whole box from surrounding band" if noise < 4 else "differing pixels only, from surrounding band",
            "fill_confidence": "high" if noise < 4 else "check" if noise < 10 else "low: textured background, inspect closely"}


def sentence_lines(block, text):
    """When every English line of a block ends a sentence ("Capture it on site." / "It lands in the study."), the
    translation keeps one sentence per line, if it has as many sentences. Otherwise None (normal wrapping)."""
    source = [l["text"].strip() for l in block.get("lines", [])]
    parts = [t for t in re.split(r"(?<=[.!?])\s+", text.strip()) if t]
    if len(source) > 1 and all(s[-1:] in ".!?" for s in source) and len(parts) == len(source):
        return parts
    return None


def layout(block, text, scale_floor):
    """Largest scale (1.0 down to scale_floor) at which text fits max_width x max_lines. Sentence-per-line breaks
    are tried first when the English block is set that way; greedy wrapping is the fallback."""
    tracking = block.get("tracking", 0.0)
    icon = block.get("trailing_icon")
    icon_room = (icon[2] + block.get("_icon_gap", 0)) if icon else 0
    sentences = sentence_lines(block, text)
    if sentences:
        scale = 1.0
        while True:
            font = load_font(block["font"], block["font_size"] * scale)
            widths = [draw_line(None, (0, 0), l, font, None, tracking * scale) for l in sentences]
            if all(w <= block["max_width"] + 1 for w in widths) and (not icon or widths[-1] + icon_room <= block["max_width"] + 1):
                return True, scale, font, sentences, widths
            if scale <= scale_floor + 1e-9:
                break
            scale = max(scale_floor, round(scale - 0.01, 4))
    scale = 1.0
    while True:
        font = load_font(block["font"], block["font_size"] * scale)
        lines = wrap(text, font, block["max_width"], tracking * scale)
        widths = [draw_line(None, (0, 0), l, font, None, tracking * scale) for l in lines]
        fits = len(lines) <= block["max_lines"] and all(w <= block["max_width"] + 1 for w in widths) \
            and (not icon or widths[-1] + icon_room <= block["max_width"] + 1)
        if fits or scale <= scale_floor + 1e-9:
            return fits, scale, font, lines, widths
        scale = max(scale_floor, round(scale - 0.01, 4))


def apply(image_path, boxes_path, translations_path, out_path):
    image_path, out_path = Path(image_path).resolve(), Path(out_path)
    if out_path.exists() or out_path.with_suffix(".json").exists():
        raise FileExistsError(out_path)
    boxes = json.loads(Path(boxes_path).read_text())
    words = json.loads(Path(translations_path).read_text())
    if sha256(image_path) != boxes["image_sha256"]:
        raise ValueError("Image differs from the one the box map was measured on")
    pending = [b["id"] for b in boxes["blocks"] if b["action"] not in {"replace", "preserve"}]
    if pending:
        raise ValueError("Review these blocks first (action must be replace or preserve): " + ", ".join(pending))
    missing = [b["id"] for b in boxes["blocks"] if b["action"] == "replace" and b["id"] not in words["blocks"]]
    if missing:
        raise ValueError("No translation for: " + ", ".join(missing))
    original = Image.open(image_path).convert("RGB")
    img = np.asarray(original, dtype=np.float64).copy()
    floor = boxes.get("min_scale", MIN_SCALE)
    records, plans = [], []
    for block in boxes["blocks"]:
        record = {k: block[k] for k in ("id", "action", "source_text", "rect")}
        if block["action"] == "preserve":
            record["status"] = "preserved (left as in the original)"
            record["note"] = block.get("note", "")
            records.append(record)
            continue
        entry = words["blocks"][block["id"]]
        entry = dict({"text": entry} if isinstance(entry, str) else entry)
        if block.get("transform") == "upper":  # source set in capitals: keep the style, not the source words
            entry = {k: (v.upper() if k in ("text", "short") and v else v) for k, v in entry.items()}
        block = dict(block, highlight=entry.get("highlight", block.get("highlight", [])))
        icon = block.get("trailing_icon")
        if icon:
            text_right = max(l["ink"][0] + l["ink"][2] for l in block["lines"] if l["ink"])
            block["_icon_gap"] = icon[0] - text_right
        erase_rect = grow(block["rect"], 3, img.shape)
        record.update(erase(img, erase_rect))
        if icon:
            sprite_rect = grow(icon, 2, img.shape)
            sx, sy, sw, sh = sprite_rect
            bg, _ = background(np.asarray(original, dtype=np.float64), sprite_rect)
            src = np.asarray(original, dtype=np.float64)[sy:sy + sh, sx:sx + sw]
            alpha = np.clip(np.linalg.norm(src - bg, axis=2) / max(1.0, np.linalg.norm(src - bg, axis=2).max()), 0, 1)
            sprite = Image.fromarray(np.dstack([src, alpha * 255]).astype(np.uint8), "RGBA")
            record.update({"icon_" + k: v for k, v in erase(img, sprite_rect).items() if k == "erased_pixels"})
        variant, fits, scale, font, lines, widths = None, False, 1.0, None, [], []
        for name in ("text", "short"):
            if entry.get(name):
                fits, scale, font, lines, widths = layout(block, entry[name], floor)
                variant = name
                if fits:
                    break
        plans.append((block, entry[variant], variant, fits, scale, font, lines, widths, sprite if icon else None, record))
    canvas = Image.fromarray(img.astype(np.uint8))
    draw = ImageDraw.Draw(canvas)
    for block, text, variant, fits, scale, font, lines, widths, sprite, record in plans:
        first = block["lines"][0]
        top = first["ink"][1]
        # Keep the first line's cap top where it was: shrinking type moves its baseline up, never the top.
        rise = (first["baseline"] - top) if first.get("baseline") else -load_font(block["font"], block["font_size"]).getbbox(first["text"], anchor="ls")[1]
        baseline = top + rise * scale
        shape = block.get("shape")
        if shape:  # a label in a button keeps its vertical centre when the type shrinks
            baseline = top + rise / 2 + rise * scale / 2
        left = block["rect"][0]
        centre = shape[0] + shape[2] / 2 if shape and block["align"] == "center" else left + block["rect"][2] / 2
        tracking = block.get("tracking", 0.0) * scale
        ends = []
        for i, (line, width) in enumerate(zip(lines, widths)):
            if block["align"] == "center":  # centre the ink, not the advance box (side bearings differ per letter)
                ink_left = font.getbbox(line[0], anchor="ls")[0]
                ink_right = width - font.getlength(line[-1]) + font.getbbox(line[-1], anchor="ls")[2]
                x = centre - (ink_left + ink_right) / 2
            elif block["align"] == "right":
                x = left + block["rect"][2] - width
            else:
                x = left - font.getbbox(line, anchor="ls")[0]  # side bearing: ink starts where it did
            y = baseline + i * block["line_pitch"] * scale
            draw_line(draw, (x, y), line, font, block["colour"], tracking, block.get("highlight", []), block.get("highlight_colour"))
            ends.append((x + width, y))
        if sprite is not None and ends:
            ex, ey = ends[-1]
            dx = int(round(ex + block["_icon_gap"])) - block["trailing_icon"][0]
            dy = int(round(ey - baseline))
            canvas.paste(sprite, (block["trailing_icon"][0] - 2 + dx, block["trailing_icon"][1] - 2 + dy), sprite)
            record["icon_moved_px"] = [dx, dy]
        avg = sum(widths) / max(1, sum(len(l) for l in lines))
        capacity = int(block["max_lines"] * block["max_width"] / max(avg, 1e-6))
        record.update({
            "translation": text, "variant_used": variant, "language": words.get("language"),
            "font": block["font"], "font_size_original": block["font_size"], "font_size_used": round(block["font_size"] * scale, 2),
            "scale": round(scale, 3), "lines_used": lines, "max_lines": block["max_lines"], "max_width": block["max_width"],
            "colour": block["colour"], "highlight": block.get("highlight", []), "align": block["align"],
            "estimated_capacity_chars": capacity, "characters": len(text),
            "status": "fits" if fits and scale >= 0.999 else f"fits at {round(scale * 100)}% type size" if fits
            else f"FLAGGED overflow: needs shorter copy (about {capacity} characters fit at {round(floor * 100)}% size)"})
        records.append(record)
    canvas.save(out_path)
    flagged = [r["id"] for r in records if str(r.get("status", "")).startswith("FLAGGED") or str(r.get("fill_confidence", "")).startswith("low")]
    report = {"tool": "reset_image_text.py", "version": VERSION, "image": str(image_path), "image_sha256": boxes["image_sha256"],
              "boxes": str(Path(boxes_path).resolve()), "boxes_sha256": sha256(boxes_path),
              "translations": str(Path(translations_path).resolve()), "translations_sha256": sha256(translations_path),
              "output": str(out_path.resolve()), "output_sha256": sha256(out_path), "language": words.get("language"),
              "label": "AI draft, not yet reviewed by a native speaker", "flagged_blocks": flagged, "blocks": records}
    out_path.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(out_path), "flagged": flagged,
                      "blocks": {r["id"]: r["status"] for r in records}}, ensure_ascii=False))
    return report


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["detect"] and len(args) in (3, 5):
        fonts = Path(args[4]).read_text().split("\n") if len(args) == 5 and args[3] == "--fonts" else None
        detect(args[1], args[2], [f for f in fonts or [] if f.strip()] or None)
    elif args[:1] == ["apply"] and len(args) == 5:
        apply(*args[1:])
    else:
        raise SystemExit(__doc__)
