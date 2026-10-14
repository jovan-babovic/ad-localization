"""See the review kit at phone width.

  python3 scripts/phone_check.py output/RUN [--width 390]

Writes output/RUN/checks/phone.html: every kit page in a frame 390 pixels wide, side by side. Open it in any browser.
If Chrome (or Chromium, Edge, Brave) is installed, it also loads that page without opening a window, lists anything
wider than the phone (which a phone would cut off or scroll sideways) and saves a screenshot, checks/phone.png.
A frame is used because Chrome will not draw a window narrower than about 500 pixels. Chrome is optional: without
it, open the page yourself.
"""
from html import escape
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job import option  # noqa: E402

BROWSERS = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "google-chrome", "chromium", "chromium-browser", "microsoft-edge"]
MEASURE = """<script>
addEventListener("load", () => setTimeout(() => {
  const out = [];
  for (const frame of document.querySelectorAll("iframe")) {
    try {
      const doc = frame.contentDocument, width = frame.clientWidth;
      const clipped = el => { for (let p = el.parentElement; p && p !== doc.body; p = p.parentElement) {
        const o = getComputedStyle(p).overflowX; if (o === "auto" || o === "scroll" || o === "hidden") return true; } return false; };
      const wide = [...doc.body.querySelectorAll("*")].filter(el => el.getBoundingClientRect().right > width + 0.5 && !clipped(el))
        .slice(0, 8).map(el => el.tagName.toLowerCase() + (el.className ? "." + String(el.className).split(" ")[0] : "") +
        " ends at " + Math.round(el.getBoundingClientRect().right) + "px: " + (el.textContent || "").trim().slice(0, 50));
      out.push({page: frame.dataset.page, width, too_wide: wide});
    } catch (error) { out.push({page: frame.dataset.page, error: String(error)}); }
  }
  document.getElementById("result").textContent = JSON.stringify(out);
}, 300));
</script>"""


def browser():
    return next((b for b in BROWSERS if (Path(b).exists() if b.startswith("/") else shutil.which(b))), None)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        raise SystemExit(__doc__)
    run_dir = Path(argv[0]).resolve()
    kit = run_dir / "kit" if (run_dir / "kit").is_dir() else run_dir
    pages = sorted(kit.glob("*.html"), key=lambda p: (p.name != "index.html", p.name))
    if not pages:
        raise SystemExit(f"No kit pages in {kit}. Build the kit first: python3 scripts/kit.py {run_dir}")
    width = int(option(argv, "--width", "390"))
    checks = (run_dir if kit != run_dir else kit.parent) / "checks"
    checks.mkdir(exist_ok=True)
    frames = "".join(f'<figure><figcaption>{escape(p.name)}</figcaption><iframe data-page="{escape(p.name)}" '
                     f'src="{escape(p.resolve().as_uri())}" width="{width}" height="2400"></iframe></figure>' for p in pages)
    wrapper = checks / "phone.html"
    wrapper.write_text(f'<!doctype html><html><head><meta charset="utf-8"><title>Kit at {width}px</title><style>'
                       'body{margin:16px;font:13px -apple-system,sans-serif;background:#ddd}main{display:flex;gap:16px;'
                       'align-items:flex-start}figure{margin:0}iframe{border:0;background:#fff;display:block}'
                       f'</style></head><body><pre id="result"></pre><main>{frames}</main>{MEASURE}</body></html>',
                       encoding="utf-8")
    found = browser()
    if not found:
        print(json.dumps(dict(page=str(wrapper), browser=None, next="No Chrome found. Open the page in any browser to "
                              "look at each kit page at phone width."), indent=1))
        return
    flags = ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--allow-file-access-from-files",
             "--virtual-time-budget=8000", f"--window-size={len(pages) * (width + 16) + 32},2500"]
    dom = subprocess.run([found, *flags, "--dump-dom", wrapper.as_uri()], capture_output=True, text=True, timeout=120).stdout
    subprocess.run([found, *flags, f"--screenshot={checks / 'phone.png'}", wrapper.as_uri()], capture_output=True, timeout=120)
    match = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
    result = json.loads(match.group(1).replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")) \
        if match and match.group(1).strip() else []
    print(json.dumps(dict(page=str(wrapper), screenshot=str(checks / "phone.png"), pages=result,
                          ok=bool(result) and all(not r.get("too_wide") and not r.get("error") for r in result)),
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
