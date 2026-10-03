"""Render the native human client through its published noVNC endpoint.

Run after game status reports a browser URL; requires `playwright install chromium`.
"""

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--screenshot", required=True, type=Path)
    args = parser.parse_args()
    root = args.state_dir.resolve()
    browser = json.loads((root / "browser.json").read_text())
    with sync_playwright() as p:
        client = p.chromium.launch()
        page = client.new_page(viewport={"width": 1400, "height": 900})
        page.goto(browser["url"] + "&autoconnect=true")
        page.locator("#noVNC_password_input").fill((root / browser["password_file"]).read_text().strip())
        page.locator("#noVNC_credentials_button").click()
        page.wait_for_function('document.documentElement.classList.contains("noVNC_connected")')
        canvas = page.locator("#noVNC_container canvas")
        canvas.wait_for(state="visible")
        # Allow an initial complete framebuffer update before capturing the game.
        page.wait_for_timeout(1500)
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)
        canvas.screenshot(path=str(args.screenshot))
        print("Connected to the native game; screenshot:", args.screenshot)
        client.close()


if __name__ == "__main__":
    main()
