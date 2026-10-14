"""Checks for the job's scripts. No paid calls, no network, no Claude calls.

Run from the repo folder:  .venv/bin/python -m unittest discover tests  (plain python3 skips the image test)
"""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import blind  # noqa: E402
import job  # noqa: E402
import kit  # noqa: E402
import save_key  # noqa: E402
import video  # noqa: E402
import voice  # noqa: E402


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), encoding="utf-8")


def make_config(folder, markets=None, platforms=("linkedin", "google"), names=("Acme", "Acme Pro")):
    write(folder / "company.md", "# Acme\nSells things.\n")
    write(folder / "markets.json", {"source_language": "English", "platforms": list(platforms),
                                    "markets": markets or [{"language": "French", "code": "fr", "market": "France", "address": "vous"}]})
    write(folder / "glossary.md", "# Glossary\n\n## Never translate\n" + "".join(f"- {n}\n" for n in names) + "\n## Terms\n- not a name\n")


def make_run(tmp, rows, markets=None):
    """A run folder built the way prepare does it, from CSV rows."""
    config, inputs, run = tmp / "config", tmp / "input", tmp / "run"
    make_config(config, markets)
    write(inputs / "ads.csv", "ad,platform,field,text\n" + "".join(
        ",".join('"' + str(v).replace('"', '""') + '"' for v in r) + "\n" for r in rows))
    run.mkdir()
    cfg, errors, _ = job.read_config(config)
    assert not errors, errors
    job.save(run / "lines.json", job.build_lines(cfg, inputs, run))
    return run


def draft_all(run, text_for, short_for=lambda line: None):
    lines = job.load(run / "lines.json")["lines"]
    rows = []
    for line in lines:
        short = short_for(line)
        rows.append(dict(line_id=line["line_id"], language=line["language"], translation=text_for(line),
                         short_version=short, shortening_note="Drops a word." if short else None, review_note=""))
    job.save(run / "drafts.json", {"translations": rows})
    return rows


class Counting(unittest.TestCase):
    def test_google_counts_wide_characters_twice(self):
        self.assertEqual(job.characters("A中", "google"), 3)
        self.assertEqual(job.characters("A中", "meta"), 2)

    def test_names_are_exact_words(self):
        self.assertEqual(job.name_count("Acme. Acme.com SuperAcme AcmeX acme", "Acme"), 1)
        self.assertEqual(job.name_count("Acme Pro, Acme Professional", "Acme Pro"), 1)

    def test_numbers_survive_local_formats(self):
        self.assertEqual(job.digit_groups("1,000 users, 2.5x"), job.digit_groups("1 000 utilisateurs, 2,5x"))
        self.assertNotEqual(job.digit_groups("30 days"), job.digit_groups("14 jours"))


class Config(unittest.TestCase):
    def test_example_config_is_valid(self):
        config, errors, _ = job.read_config(ROOT / "examples/config")
        self.assertEqual(errors, [])
        self.assertEqual([m["code"] for m in config["markets"]], ["fr", "es", "pt-BR"])
        self.assertEqual(config["never_translate"], ["PropFusion", "Field Mode", "propfusion.com/demo"])

    def test_problems_are_reported_in_plain_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.assertIsNone(job.read_config(folder)[0])
            make_config(folder, [{"language": "French", "code": "French", "market": "France", "address": ""}],
                        platforms=("linkedin", "tiktok"))
            config, errors, warnings = job.read_config(folder)
            self.assertIsNone(config)
            self.assertTrue(any("address" in e for e in errors) and any("code" in e for e in errors))
            self.assertTrue(any("tiktok" in w for w in warnings))

    def test_any_language_code_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_config(Path(tmp), [{"language": "Japanese", "code": "ja", "market": "Japan", "address": "polite (desu/masu)"},
                                    {"language": "Chinese", "code": "zh-Hant", "market": "Taiwan", "address": "您"}])
            self.assertEqual(job.read_config(Path(tmp))[1], [])


class Prepare(unittest.TestCase):
    def test_repeated_text_is_translated_once_and_fields_get_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), [("A", "LinkedIn", "Intro Text", "Try Acme Pro"), ("B", "linkedin", "primary_text", "Try Acme Pro"),
                                       ("C", "Google Ads", "Headline 1", "Fast"), ("C", "google", "headline 2", "Fast"),
                                       ("C", "google", "cta", "Go"), ("D", "tiktok", "headline", "Hi")],
                           markets=[{"language": "French", "code": "fr", "market": "France", "address": "vous"},
                                    {"language": "Spanish", "code": "es", "market": "Mexico", "address": "usted"}])
            data = job.load(run / "lines.json")
            ids = [f["id"] for f in data["fields"]]
            self.assertIn("c.google.headline_2", ids)
            self.assertEqual(len(data["lines"]), 2 * 4)
            intro = next(l for l in data["lines"] if l["text"] == "Try Acme Pro" and l["language"] == "fr")
            self.assertEqual(intro["field_ids"], ["a.linkedin.primary_text", "b.linkedin.primary_text"])
            self.assertEqual(intro["names"], ["Acme", "Acme Pro"])
            self.assertEqual(intro["limit"]["recommended"], 150)
            kinds = {f["id"]: f["limit"]["kind"] for f in data["fields"]}
            self.assertEqual(kinds["c.google.headline"], "limit")
            self.assertEqual(kinds["d.tiktok.headline"], "unknown")

    def test_text_in_a_box_gets_a_character_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), [("A", "google", "headline", "Fast")])
            write(run / "images" / "promo.boxes.json", {"blocks": [{"id": "b1", "max_width": 200, "max_lines": 1,
                                                                    "lines": [{"text": "Book a demo", "ink": [0, 0, 110, 20]}]}]})
            write(run / "media-text.json", {"items": [{"id": "promo.b1", "file": "promo.png", "surface": "image",
                                                       "text": "Book a demo", "block": "b1"}]})
            cfg = job.read_config(Path(tmp) / "config")[0]
            job.save(run / "lines.json", job.build_lines(cfg, Path(tmp) / "input", run))
            line = next(l for l in job.load(run / "lines.json")["lines"] if l["text"] == "Book a demo")
            self.assertEqual(line["limit"]["recommended"], 23)  # 200 px / (10 px per character x 0.85)
            draft_all(run, lambda l: "Réservez une démonstration gratuite" if l["text"] == "Book a demo" else "Vite")
            self.assertIn("over_budget", {f["code"] for f in job.check(run)["findings"]})

    def test_media_text_becomes_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), [("A", "google", "headline", "Fast")])
            write(run / "media-text.json", {"items": [
                {"id": "promo.t1", "file": "input/promo.png", "surface": "image", "text": "Big sale", "box": [1, 2, 3, 4]},
                {"id": "clip.script", "file": "input/clip.mp4", "surface": "spoken", "text": "Hello there, this is Acme."}]})
            cfg = job.read_config(Path(tmp) / "config")[0]
            data = job.build_lines(cfg, Path(tmp) / "input", run)
            by_id = {f["id"]: f for f in data["fields"]}
            self.assertEqual(by_id["promo.t1"]["limit"]["kind"], "layout")
            self.assertEqual(by_id["clip.script"]["limit"]["kind"], "timing")
            self.assertEqual(by_id["clip.script"]["names"], ["Acme"])
            write(run / "media-text.json", {"items": [{"file": "x.png", "surface": "image", "text": " "}]})
            with self.assertRaises(ValueError):
                job.build_lines(cfg, Path(tmp) / "input", run)


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = make_run(Path(self.tmp.name), [("A", "google", "headline", "Acme Pro saves 30 minutes"),
                                                  ("A", "google", "description", "Acme Pro saves 30 minutes"),
                                                  ("B", "linkedin", "headline", "Try it")])

    def tearDown(self):
        self.tmp.cleanup()

    def codes(self):
        return {f["code"]: f["severity"] for f in job.check(self.run)["findings"]}

    def test_clean_drafts_pass(self):
        draft_all(self.run, lambda l: "Acme Pro : 30 minutes" if "Acme" in l["text"] else "Essayez")
        report = job.check(self.run)
        self.assertEqual((report["summary"]["errors"], report["summary"]["fails"]), (0, 0))

    def test_missing_extra_and_duplicate_rows_are_errors(self):
        rows = draft_all(self.run, lambda l: "x")
        del rows[1]
        rows += [dict(rows[0]), dict(rows[0], line_id="Lnope")]
        job.save(self.run / "drafts.json", {"translations": rows})
        codes = self.codes()
        self.assertEqual(codes["missing"], "error")
        self.assertEqual(codes["duplicate"], "error")
        self.assertEqual(codes["unexpected"], "error")

    def test_over_the_limit_needs_a_short_version_that_fits(self):
        long = "Acme Pro vous fait gagner 30 minutes chaque jour"
        draft_all(self.run, lambda l: long if "Acme" in l["text"] else "Essayez")
        self.assertIn("no_version_fits", self.codes())
        draft_all(self.run, lambda l: long if "Acme" in l["text"] else "Essayez",
                  lambda l: "Acme Pro : 30 min gagnées" if "Acme" in l["text"] else None)
        codes = self.codes()
        self.assertEqual(codes["over_max"], "fail")
        self.assertNotIn("no_version_fits", codes)

    def test_changed_names_and_numbers_are_caught(self):
        draft_all(self.run, lambda l: "acme pro : 20 minutes" if "Acme" in l["text"] else "Essayez")
        codes = self.codes()
        self.assertEqual(codes["name_changed"], "error")
        self.assertEqual(codes["numbers_differ"], "review")

    def test_short_version_needs_its_note(self):
        rows = draft_all(self.run, lambda l: "Acme Pro : 30 minutes" if "Acme" in l["text"] else "Essayez")
        rows[0]["short_version"] = "Acme Pro"
        job.save(self.run / "drafts.json", {"translations": rows})
        self.assertEqual(self.codes()["short_without_note"], "error")

    def test_same_english_translated_two_ways_is_flagged(self):
        draft_all(self.run, lambda l: ("Acme Pro : 30 minutes" if l["field"] == "headline" else "Acme Pro, 30 minutes")
                  if "Acme" in l["text"] else "Essayez")
        self.assertIn("same_english_different_translation", self.codes())


class Blind(unittest.TestCase):
    def test_only_the_translation_goes_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                blind.run_batch([{"id": "t1", "language": "fr", "text": "Bonjour", "source": "Hello"}], Path(tmp) / "r", "sonnet")

    def test_call_is_isolated_and_tool_free(self):
        cmd = blind.command("sonnet")
        for flag in ("--tools", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence", "--json-schema"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "")

    def test_results_must_be_complete_and_tool_free(self):
        items = [{"id": "t1", "language": "fr", "text": "Bonjour"}]
        good = {"subtype": "success", "is_error": False, "permission_denials": [],
                "structured_output": {"results": [{"id": "t1", "back_translation": "Hello", "uncertainty": ""}]}}
        self.assertIn("t1", blind.validate(items, good))
        for bad in (dict(good, permission_denials=[{"tool_name": "Read"}]), dict(good, is_error=True),
                    dict(good, structured_output={"results": []}),
                    dict(good, structured_output={"results": [{"id": "t1", "back_translation": " ", "uncertainty": ""}]})):
            with self.assertRaises(ValueError):
                blind.validate(items, bad)

    def test_only_new_wording_is_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            job.save(run / "drafts.json", {"translations": [
                dict(line_id="a", language="fr", translation="Un", short_version="U", shortening_note="n", review_note=""),
                dict(line_id="b", language="fr", translation="Un", short_version=None, shortening_note=None, review_note="")]})
            self.assertEqual(blind.wanted(run), [("fr", "Un"), ("fr", "U")])


class Kit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = make_run(Path(self.tmp.name), [("A", "google", "headline", "Try Acme Pro")])
        draft_all(self.run, lambda l: "Essayez Acme Pro")
        job.save(self.run / "back-translations.json", {"results": [
            {"language": "fr", "text": "Essayez Acme Pro", "back_translation": "Try Acme Pro", "uncertainty": "", "run": "blind/run-01"}], "runs": []})
        line_id = job.load(self.run / "lines.json")["lines"][0]["line_id"]
        self.meaning = {"reviews": [{"line_id": line_id, "language": "fr", "variant": "full", "text": "Essayez Acme Pro",
                                     "verdict": "same", "note": "Same."}]}
        job.save(self.run / "meaning.json", self.meaning)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self):
        with mock.patch("sys.stdout"):
            kit.build(self.run)

    def test_builds_labelled_pages_and_csv(self):
        self.build()
        for name in ("index.html", "fr.html"):
            self.assertIn(job.LABEL, (self.run / "kit" / name).read_text(encoding="utf-8"))
        self.assertIn(job.LABEL, (self.run / "kit/fr.csv").read_text(encoding="utf-8-sig"))

    def test_old_back_translation_cannot_vouch_for_new_wording(self):
        draft_all(self.run, lambda l: "Essayez donc Acme Pro")
        with self.assertRaises(SystemExit) as stop:
            self.build()
        self.assertIn("back-translation", str(stop.exception))

    def test_meaning_note_must_match_current_wording(self):
        self.meaning["reviews"][0]["text"] = "Old wording"
        job.save(self.run / "meaning.json", self.meaning)
        with self.assertRaises(SystemExit) as stop:
            self.build()
        self.assertIn("meaning", str(stop.exception))

    def test_check_errors_block_the_kit(self):
        draft_all(self.run, lambda l: "Essayez acme")
        with self.assertRaises(SystemExit):
            self.build()

    def test_broken_links_are_caught(self):
        folder = Path(self.tmp.name) / "k"
        write(folder / "index.html", '<a href="missing.html">x</a><a href="https://example.com">ok</a>')
        with self.assertRaises(ValueError):
            kit.verify_links(folder)


class ElevenLabs(unittest.TestCase):
    account = dict(status="active", character_count=0, character_limit=40000,
                   can_extend_character_limit=False, allowed_to_extend_character_limit=False)

    def test_spending_gate(self):
        voice.spending_gate(self.account, 500)
        for changed in (dict(self.account, status="free"), dict(self.account, can_extend_character_limit=True),
                        dict(self.account, allowed_to_extend_character_limit=None), dict(self.account, character_count=35000)):
            with self.assertRaises(ValueError):
                voice.spending_gate(changed, 500)
        missing = {k: v for k, v in self.account.items() if k != "can_extend_character_limit"}
        with self.assertRaises(ValueError):
            voice.spending_gate(missing, 500)
        with self.assertRaises(ValueError):
            voice.spending_gate(self.account, 30001)

    def test_only_stock_voices(self):
        self.assertTrue(voice.stock_voice({"category": "premade"}))
        self.assertTrue(voice.stock_voice({"category": "professional", "is_owner": None, "sharing": {"status": "copied"}}))
        for made in ({"category": "cloned"}, {"category": "generated", "sharing": {"status": "copied"}},
                     {"category": "professional", "is_owner": True, "sharing": {"status": "copied"}},
                     {"category": "professional", "sharing": None}):
            self.assertFalse(voice.stock_voice(made))

    def test_multipart_body(self):
        body, kind = voice.multipart({"model_id": "m"}, "file", "a.mp3", b"bytes", "audio/mpeg")
        boundary = kind.split("boundary=")[1]
        self.assertEqual(body.count(boundary.encode()), 3)
        self.assertIn(b'name="model_id"', body)
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))

    def test_key_comes_from_env_or_dotenv_and_is_never_shown(self):
        with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "abc123"}):
            self.assertEqual(voice.api_key(), "abc123")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(voice, "ROOT", Path(tmp)):
            with self.assertRaises(SystemExit) as stop:
                voice.api_key()
            self.assertIn("save_key.py", str(stop.exception))
            write(Path(tmp) / ".env", "OTHER=1\nELEVENLABS_API_KEY=xyz789\n")
            self.assertEqual(voice.api_key(), "xyz789")


class Video(unittest.TestCase):
    def test_voice_must_fit_with_at_most_15_percent_speed_up(self):
        self.assertEqual(video.fit(20, 26, 0.3), 1.0)
        self.assertLessEqual(video.fit(28.7, 26.0, 0.3), 1.15)
        for args in ((33.8, 26.0, 0.3), (10, 0.4, 0.3)):
            with self.assertRaises(ValueError):
                video.fit(*args)

    def test_english_sentence_starts_from_pauses(self):
        spans = [(0.1, 2.0), (2.3, 4.0), (5.0, 7.0), (7.9, 9.0), (9.2, 12.0)]  # short pauses inside sentences
        self.assertEqual(video.group_spans(spans, [40, 20, 30]), [0.1, 5.0, 7.9])
        self.assertIsNone(video.group_spans(spans[:2], [10, 10, 10]))

    def test_each_sentence_lands_where_the_english_starts(self):
        alignment = {"characters": list("Un. Deux."), "character_start_times_seconds": [0, .1, .2, .3, .4, .5, .6, .7, .8],
                     "character_end_times_seconds": [.1, .2, .3, .4, .5, .6, .7, .8, .9]}
        spoken = video.sentence_times(alignment, "Un. Deux.")
        self.assertEqual(spoken, [(0, .3), (.4, .9)])
        tempo, pieces = video.place(spoken, [1.0, 5.0], picture=10)
        self.assertEqual(tempo, 1.0)
        self.assertEqual([p["drift"] for p in pieces], [0.0, 0.0])
        tempo, pieces = video.place([(0, 3.0), (3.4, 4.0)], [0.0, 3.0], picture=10)  # first sentence a bit long
        self.assertEqual((round(pieces[0]["tempo"], 2), pieces[1]["drift"]), (1.09, 0.0))  # sped up 9%: the next starts on time
        tempo, pieces = video.place([(0, 3.0), (3.4, 4.0)], [0.0, 1.0], picture=10)  # far too long: capped at 15%
        self.assertEqual(pieces[0]["tempo"], 1.15)
        self.assertGreater(pieces[1]["at"], 2.5)
        with self.assertRaises(ValueError):
            video.place([(0, 9.8)], [0.5], picture=8)

    def test_typing_reveals_whole_translated_words_around_names(self):
        names = ["Field Mode"]
        self.assertEqual(video.reveal("Introducing Field Mode", "Introducing", "Conoce Field Mode", names), "Conoce")
        self.assertEqual(video.reveal("Introducing Field Mode", "Introducing Field", "Découvrez Field Mode", names), "Découvrez Field")
        self.assertEqual(video.reveal("Introducing Field Mode", "Introducing Field Mode", "Découvrez Field Mode", names),
                         "Découvrez Field Mode")
        self.assertEqual(video.word_clusters([1, 2, 3, 9, 10, 20], gap=4), [(1, 3), (9, 10), (20, 20)])

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_dub_is_cut_to_the_picture_not_the_longer_sound(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            gen = lambda *a: subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *map(str, a)], check=True)  # noqa: E731
            (tmp / "input").mkdir()
            gen("-f", "lavfi", "-i", "testsrc=size=160x90:rate=30:duration=2", "-f", "lavfi", "-i", "sine=f=220:duration=2.3",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", tmp / "input" / "clip.mp4")  # sound outlasts picture
            run = make_run(tmp, [("A", "google", "headline", "Hi")])
            data = job.load(run / "lines.json")
            job.save(run / "lines.json", dict(data, input=str(tmp / "input"), media=dict(images=[], videos=["clip.mp4"])))
            folder = run / "video" / "clip"
            (folder / "fr").mkdir(parents=True)
            (folder / "stems").mkdir()
            gen("-f", "lavfi", "-i", "sine=f=440:duration=1", folder / "fr" / "narration.wav")
            gen("-f", "lavfi", "-i", "sine=f=330:duration=2.3", folder / "stems" / "instrumental.mp3")
            write(folder / "fr" / "script.txt", "Bonjour.")
            info = video.probe(tmp / "input" / "clip.mp4")
            self.assertLess(info["picture_seconds"], info["duration"])
            with mock.patch("sys.stdout"):
                video.cmd_dub(run, "clip", "fr", 1.0, "")
            made = video.probe(folder / "fr" / "dubbed.mp4")
            self.assertLessEqual(made["duration"], info["picture_seconds"] + 1 / 30 + 1e-3)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_screen_title_away_from_the_left_edge_is_replaced(self):
        try:
            from PIL import Image, ImageDraw
            import reset_image_text as tool
        except ImportError:
            self.skipTest("Pillow and numpy are not installed (only needed for image and on-screen text)")
        font = next((f for f in map(tool.resolve_font, tool.DEFAULT_FONTS) if f), None)
        if not font:
            self.skipTest("none of the default fonts is installed")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            card = Image.new("RGB", (320, 180), "white")  # a short title in the middle: its search box starts far from x=0
            draw, face = ImageDraw.Draw(card), tool.load_font(font, 28)
            draw.text((160, 100), "Try it", font=face, fill=(20, 20, 20), anchor="ms")
            x0, y0, x1, y1 = (int(v) for v in draw.textbbox((160, 100), "Try it", font=face, anchor="ms"))
            rect = [x0, y0, x1 - x0, y1 - y0]
            card.save(tmp / "card.png")
            (tmp / "input").mkdir()
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-loop", "1", "-i", str(tmp / "card.png"), "-t", "1", "-r", "10",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp / "input" / "clip.mp4")], check=True)
            run = make_run(tmp, [("A", "google", "headline", "Hi")])
            write(run / "media-text.json", {"items": [{"id": "clip.s1", "file": "clip.mp4", "surface": "screen", "text": "Try it",
                                                       "start": 0.2, "end": 0.8, "box": rect, "max_width": 300}]})
            job.save(run / "lines.json", job.build_lines(job.read_config(tmp / "config")[0], tmp / "input", run))
            draft_all(run, lambda l: "Essayez" if l["text"] == "Try it" else "Salut")
            block = dict(rect=rect, lines=[{"text": "Try it"}], align="center", font=font, font_size=28.0, tracking=0.0, colour="#141414")
            ocr = lambda paths: {Path(p).name: [{"text": "Try it", "rect": rect}] for p in paths if Path(p).parent.name != "out"}  # noqa: E731
            with mock.patch("sys.stdout"), mock.patch.object(video, "ocr_frames", ocr), \
                    mock.patch.object(tool, "detect", lambda image, out, fonts: job.save(out, {"blocks": [block]})):
                video.cmd_screen(run, "clip", "fr")
            self.assertEqual(job.load(run / "video" / "clip" / "fr" / "screen.json")["items"][0]["status"],
                             "replaced in every frame it shows")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class DubCuts(unittest.TestCase):
    """Two spoken sentences (tones) with a 0.1 s pause, and character timings that put the planned cuts inside the sound,
    like a fast stock voice that barely pauses. Every cut must land in silence (+-30 ms clear of sound), or stop."""
    SOUND = [(0.3, 1.0), (1.1, 1.9)]

    def dub(self, gaps=True):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            gen = lambda *a: subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *map(str, a)], check=True)  # noqa: E731
            (tmp / "input").mkdir()
            gen("-f", "lavfi", "-i", "testsrc=size=160x90:rate=30:duration=3", "-f", "lavfi", "-i", "sine=f=220:duration=3",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", tmp / "input" / "clip.mp4")
            run = make_run(tmp, [("A", "google", "headline", "Hi")])
            data = job.load(run / "lines.json")
            job.save(run / "lines.json", dict(data, input=str(tmp / "input"), media=dict(images=[], videos=["clip.mp4"])))
            folder = run / "video" / "clip"
            (folder / "fr").mkdir(parents=True)
            (folder / "stems").mkdir()
            sound = "+".join(f"between(t\\,{a}\\,{b})" for a, b in (self.SOUND if gaps else [(0.3, 1.9)]))
            gen("-f", "lavfi", "-i", f"aevalsrc=0.5*sin(2*PI*300*t)*({sound}):s=44100:d=2.3", folder / "fr" / "narration.wav")
            gen("-f", "lavfi", "-i", "sine=f=330:duration=3", "-af", "volume=0.05", folder / "stems" / "instrumental.mp3")
            write(folder / "transcript.txt", "One. Two.")
            write(folder / "fr" / "script.txt", "Un. Deux.")
            write(folder / "fr" / "alignment.json", {"characters": list("Un. Deux."),
                  "character_start_times_seconds": [0.35, 0.5, 0.7, 0.8, 1.25, 1.4, 1.55, 1.7, 1.8],
                  "character_end_times_seconds": [0.5, 0.7, 0.8, 1.25, 1.4, 1.55, 1.7, 1.8, 1.85]})
            with mock.patch("sys.stdout"):
                video.cmd_dub(run, "clip", "fr", 1.0, "0.3,1.6")
            return job.load(folder / "fr" / "dub.json")["pieces"]

    def test_cuts_land_in_silence_not_mid_word(self):
        pieces = self.dub()
        for piece in pieces:
            for cut in (piece["take_from"], piece["take_to"]):
                self.assertFalse(any(a - 0.03 < cut < b + 0.03 for a, b in self.SOUND), f"cut at {cut}s is inside the sound")
        self.assertEqual([p["drift"] for p in pieces], [0.0, 0.0])

    def test_no_silence_to_cut_in_stops_the_dub(self):
        with self.assertRaises(SystemExit) as stop:
            self.dub(gaps=False)
        self.assertIn("cut mid-sound", str(stop.exception))


class SaveKey(unittest.TestCase):
    def test_writes_owner_only_file_and_keeps_other_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("OTHER=1\nELEVENLABS_API_KEY=old\n")
            save_key.save_key(env, "sk_new_value")
            self.assertEqual(env.read_text().splitlines(), ["OTHER=1", "ELEVENLABS_API_KEY=sk_new_value"])
            self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)
            for bad in ("", "has space", "a=b"):
                with self.assertRaises(ValueError):
                    save_key.save_key(env, bad)
            self.assertEqual(len(list(Path(tmp).iterdir())), 1, "no temporary file left behind")


class Repo(unittest.TestCase):
    def test_secrets_and_work_stay_out_of_git(self):
        ignored = (ROOT / ".gitignore").read_text().splitlines()
        for entry in (".env", "input/*", "output/*", "config/*"):
            self.assertIn(entry, ignored)

    def test_every_skill_has_frontmatter(self):
        for name in ("setup", "localize"):
            text = (ROOT / ".claude/skills" / name / "SKILL.md").read_text()
            self.assertTrue(text.startswith("---\n"))
            self.assertIn(f"name: {name}\n", text)
            self.assertIn("description:", text)


if __name__ == "__main__":
    unittest.main()
