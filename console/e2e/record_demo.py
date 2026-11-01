"""Record a guided tour of the console and turn it into a README GIF (+ MP4 for social posts).

    python console/e2e/record_demo.py --url http://localhost:3000 [--channel msedge] [--insecure]

Outputs docs/demo.gif and docs/demo.mp4. Requires ffmpeg on PATH. If the AI agent is enabled
on the target gateway, the tour includes a live agent run.
"""

from __future__ import annotations

import argparse
import json
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
W, H = 1440, 900


def agent_available(url: str, insecure: bool) -> bool:
    ctx = ssl._create_unverified_context() if insecure else None  # noqa: S323 - local self-signed only
    with urllib.request.urlopen(f"{url}/v1/meta", context=ctx, timeout=10) as r:
        return bool(json.load(r)["agent"]["available"])


def tour(page, url: str, with_agent: bool) -> None:
    pause = page.wait_for_timeout

    # 1. Agent run (the hero moment)
    if with_agent:
        page.goto(f"{url}/#/agent")
        pause(1200)
        page.get_by_role("button", name="Book me a comfort ride from the Ferry Building to SFO and tell me the driver ETA.").click()
        pause(600)
        page.get_by_role("button", name="Run agent").click()
        expect(page.locator(".final-answer")).to_be_visible(timeout=120000)
        page.locator(".final-answer").scroll_into_view_if_needed()
        pause(3500)
        page.mouse.wheel(0, -2000)
        pause(400)
        page.get_by_role("button", name="Re-run in full catalog mode").click()
        expect(page.locator(".final-answer")).to_be_visible(timeout=120000)
        pause(800)
        page.locator(".table").first.scroll_into_view_if_needed()
        pause(3500)

    # 2. Playground: incremental discovery with the wire inspector
    page.goto(f"{url}/#/playground")
    pause(1500)
    box = page.locator(".search-row input")
    box.fill("")
    box.type("how much does a ride to the airport cost", delay=35)
    page.get_by_role("button", name="Search", exact=True).click()
    expect(page.locator(".hit").first).to_be_visible(timeout=15000)
    pause(1500)
    page.locator(".hit").first.click()
    pause(1500)
    page.get_by_role("button", name="Call via gateway").click()
    expect(page.get_by_text("success")).to_be_visible(timeout=15000)
    pause(2500)

    # 3. Scenarios: RBAC + circuit breaker
    page.goto(f"{url}/#/scenarios")
    pause(800)
    cards = page.locator(".card")
    cards.filter(has_text="Role-based access control").get_by_role("button", name="Run").click()
    expect(cards.filter(has_text="Role-based access control").get_by_text("PASS", exact=True)).to_be_visible(timeout=60000)
    pause(2000)
    circuit = cards.filter(has_text="Circuit breaker on a failing upstream")
    circuit.scroll_into_view_if_needed()
    circuit.get_by_role("button", name="Run").click()
    expect(circuit.get_by_text("PASS", exact=True)).to_be_visible(timeout=90000)
    pause(2500)

    # 4. Operator views
    page.goto(f"{url}/#/overview")
    if page.get_by_role("button", name="Use demo operator key").count():
        page.get_by_role("button", name="Use demo operator key").click()
    expect(page.locator(".upstream").first).to_be_visible(timeout=15000)
    pause(3000)
    page.goto(f"{url}/#/policy")
    pause(2500)
    page.goto(f"{url}/#/experiment")
    pause(3000)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:3000")
    ap.add_argument("--channel", default=None)
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "docs"))
    ap.add_argument("--no-agent", action="store_true", help="skip the live agent segment")
    args = ap.parse_args()
    if not shutil.which("ffmpeg"):
        print("ffmpeg not found on PATH", file=sys.stderr)
        return 2
    with_agent = not args.no_agent and agent_available(args.url, args.insecure)
    print(f"agent {'enabled' if with_agent else 'not enabled'} — recording tour")

    tmp = Path(tempfile.mkdtemp())
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=args.channel) if args.channel else p.chromium.launch()
        ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(tmp),
                                  record_video_size={"width": W, "height": H}, ignore_https_errors=args.insecure)
        page = ctx.new_page()
        tour(page, args.url, with_agent)
        video = page.video.path()
        ctx.close()
        browser.close()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    mp4, gif = out / "demo.mp4", out / "demo.gif"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-vf", "fps=30,scale=1440:-2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", "-movflags", "+faststart", str(mp4)], check=True)
    palette = tmp / "palette.png"
    vf = "fps=8,scale=960:-1:flags=lanczos"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-vf", f"{vf},palettegen=stats_mode=diff", str(palette)], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-i", str(palette), "-lavfi",
                    f"{vf}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle", str(gif)], check=True)
    for f in (mp4, gif):
        print(f"wrote {f}  ({f.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
