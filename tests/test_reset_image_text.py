"""One runnable check for reset_image_text.py: python3 test_reset_image_text.py

Builds a synthetic ad (vertical gradient, white headline, a dark line crossing nowhere near the text),
then checks: detect finds the headline (macOS Vision; skipped elsewhere), apply refuses unreviewed
blocks, erases the English, sets the translation inside the box, leaves every pixel outside the box
untouched, and flags a translation too long for the box instead of squashing it. A one-line label in a
button ("Book" -> "Réserver") must come out centred in the button, not against its left edge.
"""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
try:
    import numpy as np
    from PIL import Image, ImageDraw
    import reset_image_text as tool
except ImportError:  # image ads need Pillow and numpy; the rest of the job does not
    tool = None


BUTTON_BOX = (500, 180, 720, 244)  # x0, y0, x1, y1
GREEN = (0, 133, 97)


def synthetic(folder):
    h, w = 400, 800
    ramp = np.linspace(0, 1, h)[:, None, None]
    img = (np.array([12, 18, 40]) * (1 - ramp) + np.array([20, 60, 120]) * ramp) * np.ones((h, w, 3))
    image = Image.fromarray(img.astype(np.uint8))
    font_spec = next(f for f in (tool.resolve_font(x) for x in tool.DEFAULT_FONTS) if f)
    draw = ImageDraw.Draw(image)
    draw.text((60, 120), "Grow your app", font=tool.load_font(font_spec, 64), fill="white", anchor="ls")
    draw.line([(0, 330), (800, 330)], fill=(240, 240, 120), width=3)  # artwork outside the text box
    draw.rounded_rectangle(BUTTON_BOX, radius=8, fill=GREEN)
    font = tool.load_font(font_spec, 30)
    left, top, right, bottom = font.getbbox("Book", anchor="ls")
    bx, by, bw, bh = BUTTON_BOX[0], BUTTON_BOX[1], BUTTON_BOX[2] - BUTTON_BOX[0], BUTTON_BOX[3] - BUTTON_BOX[1]
    draw.text((bx + (bw - (right - left)) / 2 - left, by + (bh - (bottom - top)) / 2 - top), "Book", font=font,
              fill="white", anchor="ls")
    path = folder / "ad.png"
    image.save(path)
    return path, font_spec


def boxes_for(path, font_spec, folder):
    if shutil.which("swift"):
        detected = folder / "detected.json"
        out = tool.detect(path, detected)
        assert any("Grow your app" in b["source_text"] for b in out["blocks"]), out["blocks"]
        boxes = json.loads(detected.read_text())
    else:  # no Vision OCR: describe the same block by hand
        print("swift not found: detect skipped, box written by hand")
        img = np.asarray(Image.open(path).convert("RGB"), dtype=np.float64)
        box = tool.ink(img, [50, 60, 500, 90])[0]
        boxes = {"image_sha256": tool.sha256(path), "min_scale": 0.85, "blocks": [{
            "id": "b1", "action": "review", "source_text": "Grow your app", "rect": [int(v) for v in box],
            "lines": [{"text": "Grow your app", "ink": [int(v) for v in box], "baseline": 120}],
            "max_width": int(box[2]), "max_lines": 1, "align": "left", "colour": "#ffffff",
            "font": font_spec, "font_size": 64.0, "line_pitch": 76.8, "tracking": 0.0}]}
    return boxes


def main():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        path, font_spec = synthetic(folder)
        boxes = boxes_for(path, font_spec, folder)
        block = next(b for b in boxes["blocks"] if "Grow" in b["source_text"])
        boxes["blocks"] = [block]
        (folder / "boxes.json").write_text(json.dumps(boxes))
        (folder / "words.json").write_text(json.dumps({"language": "French", "blocks": {block["id"]: "Grandir"}}))
        try:
            tool.apply(path, folder / "boxes.json", folder / "words.json", folder / "unreviewed.png")
            raise AssertionError("an unreviewed block was applied")
        except ValueError:
            pass
        block["action"] = "replace"
        (folder / "boxes.json").write_text(json.dumps(boxes))
        report = tool.apply(path, folder / "boxes.json", folder / "words.json", folder / "fr.png")
        before = np.asarray(Image.open(path).convert("RGB"), dtype=int)
        after = np.asarray(Image.open(folder / "fr.png").convert("RGB"), dtype=int)
        x, y, w, h = tool.grow(block["rect"], 12, before.shape)  # erase band + halo allowance
        outside = np.ones(before.shape[:2], dtype=bool)
        outside[y:y + h, x:x + w] = False
        assert np.array_equal(before[outside], after[outside]), "pixels outside the box changed"
        bx, by, bw, bh = block["rect"]
        tail = after[by:by + bh, bx + int(0.75 * bw):bx + bw]  # where English ran on but "Grandir" stops
        assert tail.max() < 150, "English glyphs remain after erase"
        assert report["blocks"][0]["status"] == "fits", report["blocks"][0]["status"]
        ink = tool.ink(after.astype(np.float64), tool.grow(block["rect"], 4, after.shape))[0]
        assert ink and ink[0] >= bx - 4 and ink[0] + ink[2] <= bx + bw + 4, "new text left its box"
        (folder / "long-words.json").write_text(json.dumps({"blocks": {block["id"]: "Faites grandir votre application mobile aujourd'hui"}}))
        long = tool.apply(path, folder / "boxes.json", folder / "long-words.json", folder / "long.png")
        assert long["flagged_blocks"] == [block["id"]] and long["blocks"][0]["status"].startswith("FLAGGED overflow"), long
        check_button(path, font_spec, folder)
        check_sentence_lines(font_spec)
    print("PASS: detect finds the line; unreviewed blocks refused; English erased; translation set inside its box; "
          "pixels outside the box unchanged; too-long copy flagged, not squashed; button label centred in its button; one sentence per line kept")


def check_sentence_lines(font_spec):
    """A two-line headline set one sentence per line keeps that break in translation (not "local. Vai / direto")."""
    block = {"font": font_spec, "font_size": 40.0, "tracking": 0.0, "max_width": 520, "max_lines": 2,
             "lines": [{"text": "Capture it on site."}, {"text": "It lands in the study."}]}
    fits, scale, font, lines, widths = tool.layout(block, "Registre no local. Vai direto para o estudo.", 0.85)
    assert fits and lines == ["Registre no local.", "Vai direto para o estudo."], lines
    block["lines"] = [{"text": "Field Mode links every photo and note"}, {"text": "while you are on site."}]
    assert tool.layout(block, "Registre no local. Vai direto para o estudo.", 0.85)[3] != ["Registre no local.", "Vai direto para o estudo."]


def check_button(path, font_spec, folder):
    """The label is re-set centred in its button ("Réserver"), not against the button's left edge."""
    x0, y0, x1, y1 = BUTTON_BOX
    if shutil.which("swift"):
        out = tool.detect(path, folder / "button-detected.json")
        button = next(b for b in out["blocks"] if b["source_text"] == "Book")
        assert button["shape"] and abs(button["shape"][0] - x0) <= 2 and abs(button["shape"][2] - (x1 - x0)) <= 3, button
        assert button["align"] == "center", button["align"]
    else:
        img = np.asarray(Image.open(path).convert("RGB"), dtype=np.float64)
        box = tool.ink(img, [x0 + 4, y0 + 4, x1 - x0 - 8, y1 - y0 - 8])[0]
        button = {"id": "b9", "source_text": "Book", "rect": [int(v) for v in box], "shape": tool.filled_shape(img, box),
                  "lines": [{"text": "Book", "ink": [int(v) for v in box], "baseline": int(box[1] + box[3])}],
                  "max_width": 150, "max_lines": 1, "align": "center", "colour": "#ffffff",
                  "font": font_spec, "font_size": 30.0, "line_pitch": 36.0, "tracking": 0.0}
    button["action"] = "replace"
    boxes = {"image_sha256": tool.sha256(path), "min_scale": 0.85, "blocks": [button]}
    (folder / "button-boxes.json").write_text(json.dumps(boxes))
    (folder / "button-words.json").write_text(json.dumps({"blocks": {button["id"]: "Réserver"}}))
    tool.apply(path, folder / "button-boxes.json", folder / "button-words.json", folder / "button.png")
    after = np.asarray(Image.open(folder / "button.png").convert("RGB"), dtype=int)
    inside = after[y0 + 6:y1 - 6, x0 + 6:x1 - 6]
    cols = np.nonzero((inside.min(axis=2) > 200).any(axis=0))[0]  # white label pixels
    label_centre = x0 + 6 + (cols.min() + cols.max()) / 2
    assert abs(label_centre - (x0 + x1) / 2) <= 2, f"label centre {label_centre} vs button centre {(x0 + x1) / 2}"


@unittest.skipIf(tool is None, "Pillow and numpy are not installed (only needed for image ads)")
class ResetImageText(unittest.TestCase):
    def test_synthetic_ad(self):
        with mock.patch("sys.stdout"):
            main()

    def test_chinese_breaks_between_characters_never_before_closing_punctuation(self):
        self.assertEqual([p for p, _ in tool.pieces("转向 AI，现在「开始」。")], ["转", "向", "AI，", "现", "在", "「开", "始」。"])
        self.assertEqual(tool.pieces("Every AI journey"), [["Every", False], ["AI", True], ["journey", True]])
        font = next((tool.load_font(f, 40) for f in map(tool.resolve_font, ("PingFang.ttc#3", "Hiragino Sans GB.ttc#0")) if f), None)
        if not font:
            self.skipTest("no Chinese font installed")
        lines = tool.wrap("您在未来几年做出的决策，影响将不断累积。", font, 300, 0)
        self.assertGreater(len(lines), 1)
        self.assertEqual("".join(lines), "您在未来几年做出的决策，影响将不断累积。")
        self.assertFalse(any(l[0] in tool.NO_LINE_START for l in lines))

    def test_text_the_font_cannot_draw_gets_a_font_of_the_same_weight(self):
        arial, light = tool.resolve_font("Arial.ttf"), tool.resolve_font("HelveticaNeue.ttc#7")
        if not (arial and light and tool.resolve_font("Hiragino Sans GB.ttc")):
            self.skipTest("macOS fonts not installed")
        self.assertEqual(tool.script_font(arial, "Décision", "fr"), (arial, 1.0))
        spec, scale = tool.script_font(arial, "决策", "zh-Hans")
        self.assertNotEqual(spec, arial)
        self.assertEqual(scale, tool.CJK_SCALE)
        self.assertTrue(tool.has_glyphs(spec, "决策"))
        self.assertEqual(tool.weight_class(light), "light")
        self.assertEqual(tool.weight_class(tool.script_font(light, "决策", "zh-Hans")[0]), "light")

    def test_brand_fonts_come_first_and_keep_the_defaults(self):
        arial = tool.resolve_font("Arial.ttf")
        if not arial:
            self.skipTest("Arial not installed")
        brand, fonts = tool.candidate_fonts([arial, "/no/such/font.ttf"])
        self.assertEqual((brand, fonts[0]), ([arial], arial))
        self.assertEqual(fonts, list(dict.fromkeys(fonts)))
        self.assertGreater(len(fonts), 1)


if __name__ == "__main__":
    main()
