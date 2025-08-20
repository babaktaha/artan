import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def upload_to_send_vis(file_path: str, expire_downloads: int | None = None, expire_time: str | None = None) -> str:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"]) 
        context = browser.new_context()
        page = context.new_page()

        page.goto("https://send.vis.ee/", wait_until="load")
        page.wait_for_load_state("networkidle", timeout=30_000)

        # Find a file input and set files
        input_loc = page.locator("input[type='file']").first
        if input_loc.count() == 0:
            # Try clicking an upload area to reveal file chooser
            candidates = [
                "text=Upload files",
                "text=Click to upload",
                "css=[for='file']",
                "css=.dropzone",
                "role=button[name='Upload files']",
            ]
            for sel in candidates:
                try:
                    if page.locator(sel).first.is_visible(timeout=2_000):
                        with page.expect_file_chooser(timeout=5_000) as fc_info:
                            page.locator(sel).first.click()
                        fc = fc_info.value
                        fc.set_files(str(path))
                        break
                except Exception:
                    pass
        else:
            input_loc.set_input_files(str(path))

        # Optionally set expiration (best-effort; selectors may change)
        if expire_downloads:
            try:
                # Look for a select/dropdown near 'downloads'
                dl = page.locator("select:has(option[value='1'])").first
                if dl.count() > 0:
                    dl.select_option(str(expire_downloads))
            except Exception:
                pass
        if expire_time:
            try:
                tm = page.locator("select:has(option[value='1d'])").first
                if tm.count() > 0:
                    tm.select_option(expire_time)
            except Exception:
                pass

        # Wait for upload to finish and link to appear
        link = None
        # Common selectors for link field or copy button
        for _ in range(240):  # up to ~120s
            try:
                # Try input field containing the link
                link_inputs = page.locator("input[type='text']").all()
                for li in link_inputs:
                    val = li.input_value()
                    if val and val.startswith("https://send.vis.ee/"):
                        link = val
                        break
                if link:
                    break
                # Try reading any anchor with send.vis.ee
                anchors = page.locator("a[href^='https://send.vis.ee/']").all()
                for a in anchors:
                    href = a.get_attribute("href")
                    if href and href.startswith("https://send.vis.ee/"):
                        link = href
                        break
                if link:
                    break
                # Try copy button then read clipboard via DOM hack
                btn = page.get_by_role("button", name="Copy link")
                if btn and btn.count() > 0 and btn.first.is_visible():
                    btn.first.click()
                    try:
                        copied = page.evaluate("navigator.clipboard.readText && navigator.clipboard.readText()")
                        if copied and isinstance(copied, str) and copied.startswith("https://send.vis.ee/"):
                            link = copied
                            break
                    except Exception:
                        pass
                # Some UIs show link in textarea
                tas = page.locator("textarea").all()
                for ta in tas:
                    val = ta.input_value()
                    if val and val.startswith("https://send.vis.ee/"):
                        link = val
                        break
                if link:
                    break
            except Exception:
                pass
            time.sleep(0.5)

        browser.close()

        if not link:
            # Save debug artifacts
            try:
                dbg_html = Path(file_path).with_suffix(".send_upload.html")
                dbg_png = Path(file_path).with_suffix(".send_upload.png")
                dbg_html.write_text(page.content())
                try:
                    page.screenshot(path=str(dbg_png), full_page=True)
                except Exception:
                    pass
            except Exception:
                pass
            raise RuntimeError("Failed to retrieve upload link from send.vis.ee UI")
        return link


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python upload_send_vis.py <file_path>", file=sys.stderr)
        sys.exit(2)
    file_path = sys.argv[1]
    link = upload_to_send_vis(file_path)
    print(link)


if __name__ == "__main__":
    main()

