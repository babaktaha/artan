import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def download_send_vis(url: str, output_path: str) -> None:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        page.goto(url, wait_until="load")

        download = None

        # Try common selectors for the download action
        candidate_selectors = [
            "text=Download",
            "role=button[name='Download']",
            "text=دانلود",
            "role=button[name='دانلود']",
            "css=a[href*='download']",
            "css=button:has-text('Download')",
        ]

        for selector in candidate_selectors:
            try:
                if page.is_visible(selector):
                    with page.expect_download(timeout=120_000) as download_info:
                        page.click(selector, timeout=5_000)
                    download = download_info.value
                    break
            except Exception:
                pass

        if download is None:
            try:
                download = page.wait_for_event("download", timeout=120_000)
            except Exception:
                # As a last resort, wait a bit and try clicking any visible button containing 'Download'
                try:
                    with page.expect_download(timeout=120_000) as download_info:
                        page.locator("button:has-text('Download')").first.click(timeout=5_000)
                    download = download_info.value
                except Exception as exc:
                    browser.close()
                    raise RuntimeError(
                        f"Could not trigger download from send.vis.ee page: {exc}"
                    )

        download.save_as(str(output))
        # Small delay to ensure file is flushed to disk
        time.sleep(0.5)
        browser.close()


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python fetch_send_vis.py <url> [output_path]", file=sys.stderr)
        sys.exit(2)

    url = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else "/workspace/input.pdf"
    download_send_vis(url, output_path)
    print(f"Saved to: {output_path}")


if __name__ == "__main__":
    main()

