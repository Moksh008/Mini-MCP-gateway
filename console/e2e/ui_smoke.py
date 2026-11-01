"""Browser smoke test for the console, against a running stack.

    python console/e2e/ui_smoke.py --url http://localhost:3000 [--shots docs/screenshots]

Drives the real UI with Playwright: the agent flow in the Playground, every operator page, and
the guided scenarios (which must all PASS). Exits non-zero on any failure or browser console error.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:3000")
    ap.add_argument("--shots", default=None, help="directory to write screenshots to")
    ap.add_argument("--channel", default=None, help="browser channel, e.g. msedge or chrome (default: bundled chromium)")
    args = ap.parse_args()
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []

    def shot(page, name: str) -> None:
        if shots:
            page.mouse.move(0, 0)
            page.wait_for_timeout(400)  # let CSS transitions settle
            page.screenshot(path=str(shots / f"{name}.png"))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel=args.channel) if args.channel else p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
        def on_console(m) -> None:
            # The Auth scenario deliberately sends bad keys; browsers log those 401s on their own.
            if m.type == "error" and "status of 401" not in m.text:
                errors.append(f"console: {m.text}")

        page.on("console", on_console)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        # 1. Playground: incremental search → schema → call
        page.goto(f"{args.url}/#/playground")
        expect(page.get_by_text("Tool context the model carries")).to_be_visible()
        expect(page.locator(".ctx-row").first).to_contain_text("tok", timeout=15000)
        page.get_by_role("button", name="how much does a ride to the airport cost").click()
        first_hit = page.locator(".hit").first
        expect(first_hit).to_contain_text("rides__estimate_fare", timeout=15000)
        first_hit.click()
        expect(page.locator(".tool-name")).to_have_text("rides__estimate_fare")
        page.get_by_role("button", name="Call via gateway").click()
        expect(page.get_by_text("success")).to_be_visible(timeout=15000)
        expect(page.locator(".json").first).to_contain_text("fare_usd")
        # 2× tools/list (context measurement) + search + schema + call; dev-mode StrictMode may double the first two
        methods = page.locator(".wire-method").all_inner_texts()
        assert methods[:3] == ["tools/call · call_tool", "tools/call · get_tool_schema", "tools/call · search_tools"], methods
        assert methods.count("tools/list") >= 2, methods
        print(f"ok  playground: search -> schema -> call, wire inspector shows {len(methods)} real exchanges")
        shot(page, "playground")

        # forbidden tool for the rider is not discoverable
        page.locator(".chips").get_by_role("button", name="refund a charge").click()
        page.wait_for_timeout(800)
        assert "payments__refund_payment" not in page.locator(".hits").inner_text(), "refund leaked to rider"
        print("ok  playground: rider cannot discover refunds")

        # 2. Operator sign-in
        page.goto(f"{args.url}/#/overview")
        page.get_by_role("button", name="Use demo operator key").click()
        expect(page.get_by_role("heading", name="Overview")).to_be_visible()
        expect(page.locator(".upstream")).to_have_count(5, timeout=15000)
        print("ok  overview: 5 upstreams")

        # 3. Scenarios — all must pass
        page.goto(f"{args.url}/#/scenarios")
        page.get_by_role("button", name="Run all").click()
        verdicts = page.locator(".card-actions .badge", has_text=re.compile(r"^(PASS|FAIL)$"))
        expect(verdicts).to_have_count(6, timeout=180000)
        expect(page.get_by_role("button", name="Running…")).to_have_count(0)
        fails = page.locator(".card-actions .badge", has_text=re.compile(r"^FAIL$")).count()
        if fails:
            errors.append(f"{fails} scenario(s) failed:\n" + page.locator(".scenarios").inner_text())
        else:
            print("ok  scenarios: 6/6 PASS")
        shot(page, "scenarios")

        # 4. Remaining operator pages render real data
        checks = {
            "registry": (".table tbody tr", "Registered servers"),
            "catalog": (".catalog-items li", "Tool catalog"),
            "policy": (".matrix tbody tr", "Access policy"),
            "audit": (".collapsible", "Audit log"),
            "experiment": (".hbar-row", "Experiment"),
        }
        for name, (selector, heading) in checks.items():
            page.goto(f"{args.url}/#/{name}")
            expect(page.get_by_role("heading", name=heading).first).to_be_visible()
            expect(page.locator(selector).first).to_be_visible(timeout=15000)
            print(f"ok  {name}: {page.locator(selector).count()} rows")
            shot(page, name)

        # 5. Registry: register + remove a server through the UI
        page.goto(f"{args.url}/#/registry")
        page.get_by_role("button", name="Legacy REST service").click()
        page.get_by_role("button", name="Register server").click()
        expect(page.get_by_text("Registered 'geo-v2'")).to_be_visible(timeout=15000)
        row = page.locator("tr", has_text="geo-v2").first
        row.get_by_role("button", name="Remove").click()
        expect(page.get_by_text("Removed geo-v2")).to_be_visible(timeout=15000)
        print("ok  registry: registered and removed geo-v2 via UI")

        # 6. Overview again, now that the scenarios generated traffic
        page.goto(f"{args.url}/#/overview")
        expect(page.locator(".sparkbars rect").first).to_be_visible(timeout=15000)
        page.wait_for_timeout(3000)
        print("ok  overview: traffic chart populated")
        shot(page, "overview")

        browser.close()

    for e in errors:
        print("FAIL", e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
