import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def download_send_vis(url: str, output_path: str, password: str | None = None) -> None:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-gpu",
            ],
        )
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        page.goto(url, wait_until="load")
        page.wait_for_load_state("networkidle", timeout=30_000)
        try:
            page.wait_for_selector("button, a", timeout=15_000)
        except Exception:
            pass

        # If page requires a password, fill and unlock
        try:
            if page.locator("#password-input").first.is_visible(timeout=2_000):
                if not password:
                    raise RuntimeError("Password is required for this link but none was provided.")
                page.fill("#password-input", password)
                # Click Unlock
                if page.locator("#password-btn").first.is_visible(timeout=2_000):
                    page.click("#password-btn")
                else:
                    page.keyboard.press("Enter")
                # Wait for unlock to process
                page.wait_for_load_state("networkidle", timeout=30_000)
                # Give UI a moment to render download controls
                page.wait_for_timeout(1000)
        except Exception:
            # If any errors happen here, continue; selectors below may still work
            pass

        download = None

        # Try common selectors for the download action
        candidate_selectors = [
            "button:has-text('Decrypt & Download')",
            "button:has-text('Decrypt and Download')",
            "button:has-text('Download')",
            "a:has-text('Download')",
            "text=Decrypt & Download",
            "text=Decrypt and Download",
            "text=Download",
            "text=دانلود",
            "button:has-text('دانلود')",
            "a:has-text('دانلود')",
            "css=a[href*='download']",
            "css=button:has-text('Download')",
        ]

        for selector in candidate_selectors:
            try:
                if page.locator(selector).first.is_visible(timeout=2_000):
                    with page.expect_download(timeout=180_000) as download_info:
                        page.locator(selector).first.click(timeout=10_000)
                    download = download_info.value
                    break
            except Exception:
                pass

        if download is None:
            try:
                # Try scanning all buttons/links and click those with download-ish text
                elements = page.locator("button, a, [role=button]").all()
                # Save debug info: list all button/link texts
                try:
                    texts = []
                    for el in elements:
                        try:
                            t = el.inner_text(timeout=500)
                        except Exception:
                            t = ""
                        t = (t or "").strip().replace("\n", " ")
                        if t:
                            texts.append(t)
                    debug_txt = "\n".join(texts)
                    (output.parent / "send_vis_buttons.txt").write_text(debug_txt)
                except Exception:
                    pass
                for el in elements:
                    try:
                        text = el.inner_text(timeout=1_000).strip().lower()
                    except Exception:
                        continue
                    if any(keyword in text for keyword in [
                        "decrypt & download",
                        "decrypt and download",
                        "download",
                        "دانلود",
                        "بارگیری",
                        "بارگيري",
                    ]):
                        with page.expect_download(timeout=180_000) as download_info:
                            try:
                                el.click(timeout=5_000)
                            except Exception:
                                # Force click via JS
                                page.evaluate("el => el.click()", el)
                        download = download_info.value
                        break
                if download is None:
                    download = page.wait_for_event("download", timeout=180_000)
            except Exception:
                # As a last resort, wait a bit and try clicking any visible button containing 'Download'
                try:
                    with page.expect_download(timeout=180_000) as download_info:
                        page.locator("button:has-text('Download')").first.click(timeout=10_000)
                    download = download_info.value
                except Exception as exc:
                    try:
                        # Save page content for debugging
                        dbg_html = output.parent / "send_vis_page.html"
                        dbg_png = output.parent / "send_vis_screenshot.png"
                        dbg_html.write_text(page.content())
                        try:
                            page.screenshot(path=str(dbg_png), full_page=True)
                        except Exception:
                            pass
                    except Exception:
                        pass
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
        print("Usage: python fetch_send_vis.py <url> [output_path] [password]", file=sys.stderr)
        sys.exit(2)

    url = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else "/workspace/input.pdf"
    password = sys.argv[3] if len(sys.argv) > 3 else None
    download_send_vis(url, output_path, password)
    print(f"Saved to: {output_path}")


if __name__ == "__main__":
    main()

