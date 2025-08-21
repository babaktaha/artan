import sys
import subprocess
import csv
from pathlib import Path
from typing import List, Tuple, Dict, Any

import fitz  # PyMuPDF
from playwright.sync_api import sync_playwright


def render_text_png(playwright_ctx, text: str, out_png: Path, font_size_px: int = 24, color_hex: str = "#000") -> Tuple[int, int]:
    chromium = playwright_ctx.chromium
    browser = chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 800, "height": 200}, device_scale_factor=2)
    page = context.new_page()

    html = f"""
    <html dir=\"rtl\">
    <head>
      <meta charset=\"utf-8\" />
      <style>
        body {{
          margin: 0;
          padding: 0;
          background: transparent;
          font-family: 'Noto Naskh Arabic','Noto Sans Arabic','Tahoma','Arial','sans-serif';
        }}
        #t {{
          display: inline-block;
          white-space: nowrap;
          font-size: {font_size_px}px;
          color: {color_hex};
          padding: 2px 4px;
          background: transparent;
        }}
      </style>
    </head>
    <body>
      <div id=\"t\">{text}</div>
    </body>
    </html>
    """

    page.set_content(html, wait_until="load")
    page.wait_for_selector("#t")
    element = page.locator("#t")
    bounding = element.bounding_box()
    if not bounding:
        raise RuntimeError("Failed to measure rendered text bounding box")
    # Expand viewport if needed to fit text
    width = int(bounding["width"]) + 8
    height = int(bounding["height"]) + 8
    page.set_viewport_size({"width": max(100, width), "height": max(50, height)})
    # Re-measure after viewport change
    element = page.locator("#t")
    bounding = element.bounding_box()
    clip = {
        "x": max(0, int(bounding["x"]) - 2),
        "y": max(0, int(bounding["y"]) - 2),
        "width": int(bounding["width"]) + 4,
        "height": int(bounding["height"]) + 4,
    }
    out_png.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out_png), clip=clip, omit_background=True)
    browser.close()
    return clip["width"], clip["height"]


def sample_background_color(page: fitz.Page, rect: fitz.Rect) -> Tuple[float, float, float]:
    # Render a small pixmap of the rect and compute average RGB
    clip = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y1)
    # Use modest zoom so we have enough pixels
    mat = fitz.Matrix(2, 2)
    try:
        pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
    except Exception:
        return (1.0, 1.0, 1.0)
    if pix is None or pix.n < 3:
        return (1.0, 1.0, 1.0)
    data = pix.samples  # bytes
    n = pix.n  # channels, expect 3 or 4
    width = pix.width
    height = pix.height
    # Sample every few pixels to speed up
    step = max(1, min(width, height) // 20)
    total_r = total_g = total_b = count = 0
    for y in range(0, height, step):
        row_start = y * width * n
        for x in range(0, width, step):
            idx = row_start + x * n
            r = data[idx]
            g = data[idx + 1]
            b = data[idx + 2]
            total_r += r
            total_g += g
            total_b += b
            count += 1
    if count == 0:
        return (1.0, 1.0, 1.0)
    avg_r = total_r / count / 255.0
    avg_g = total_g / count / 255.0
    avg_b = total_b / count / 255.0
    # Clamp
    avg_r = max(0.0, min(1.0, avg_r))
    avg_g = max(0.0, min(1.0, avg_g))
    avg_b = max(0.0, min(1.0, avg_b))
    return (avg_r, avg_g, avg_b)


def sample_background_color_around(page: fitz.Page, rect: fitz.Rect, margin: float = 3.0) -> Tuple[float, float, float]:
    # Sample thin strips around the rectangle and average their colors
    strips = []
    m = margin
    # top, bottom, left, right
    strips.append(fitz.Rect(rect.x0, rect.y0 - m, rect.x1, rect.y0))
    strips.append(fitz.Rect(rect.x0, rect.y1, rect.x1, rect.y1 + m))
    strips.append(fitz.Rect(rect.x0 - m, rect.y0, rect.x0, rect.y1))
    strips.append(fitz.Rect(rect.x1, rect.y0, rect.x1 + m, rect.y1))
    colors = []
    for s in strips:
        colors.append(sample_background_color(page, s))
    # average
    if not colors:
        return (1.0, 1.0, 1.0)
    r = sum(c[0] for c in colors) / len(colors)
    g = sum(c[1] for c in colors) / len(colors)
    b = sum(c[2] for c in colors) / len(colors)
    return (r, g, b)


def detect_text_color(page: fitz.Page, rect: fitz.Rect) -> str:
    # Try to get text color from overlapping spans; return hex string
    try:
        info = page.get_text("dict")
        spans = []
        for block in info.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    bbox = span.get("bbox")
                    if not bbox:
                        continue
                    srect = fitz.Rect(*bbox)
                    if srect.intersects(rect):
                        color = span.get("color") or span.get("fill")
                        if isinstance(color, list) and len(color) >= 3:
                            r, g, b = color[:3]
                            # color might be 0..1 or 0..255
                            if r <= 1 and g <= 1 and b <= 1:
                                r, g, b = int(r * 255), int(g * 255), int(b * 255)
                            else:
                                r, g, b = int(r), int(g), int(b)
                            return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        pass
    return "#000000"


def find_matches(page: fitz.Page, needle: str) -> List[fitz.Rect]:
    rects: List[fitz.Rect] = []
    # Try exact phrase
    for quad in page.search_for(needle, quads=True) or []:
        rects.append(quad.rect)
    if rects:
        return rects
    # Try token-wise approximate matching
    tokens = [t for t in needle.split() if t.strip()]
    for token in tokens:
        for quad in page.search_for(token, quads=True) or []:
            rects.append(quad.rect)
    return rects


def run_tesseract_tsv(image_path: Path, lang: str = "fas+ara", psm: int = 6) -> List[Dict[str, Any]]:
    tsv_path = image_path.with_suffix(".tsv")
    cmd = [
        "tesseract",
        str(image_path),
        str(image_path.with_suffix("")),  # output base
        "-l",
        lang,
        "--oem",
        "1",
        "--psm",
        str(psm),
        "tsv",
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    rows: List[Dict[str, Any]] = []
    with open(tsv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            try:
                if row.get("text") is None:
                    continue
                row["left"] = int(row.get("left", 0))
                row["top"] = int(row.get("top", 0))
                row["width"] = int(row.get("width", 0))
                row["height"] = int(row.get("height", 0))
                row["page_num"] = int(row.get("page_num", 0))
                row["block_num"] = int(row.get("block_num", 0))
                row["par_num"] = int(row.get("par_num", 0))
                row["line_num"] = int(row.get("line_num", 0))
                row["word_num"] = int(row.get("word_num", 0))
                row["conf"] = int(float(row.get("conf", 0)))
            except Exception:
                pass
            rows.append(row)
    return rows


def find_phrase_boxes_from_tsv(tsv_rows: List[Dict[str, Any]], phrase: str) -> List[Tuple[int, fitz.Rect]]:
    phrase_tokens = [t for t in phrase.split() if t.strip()]
    results: List[Tuple[int, fitz.Rect]] = []
    # Group by (block,line)
    groups: Dict[Tuple[int, int, int], List[Dict[str, Any]]] = {}
    for r in tsv_rows:
        if r.get("level") != "5":
            continue
        key = (r.get("block_num", 0), r.get("par_num", 0), r.get("line_num", 0))
        groups.setdefault(key, []).append(r)
    for key, words in groups.items():
        # Sort by horizontal position (RTL languages may still come ordered)
        words_sorted = sorted(words, key=lambda w: w.get("left", 0))
        texts = [ (w.get("text", "").strip(), w) for w in words_sorted if (w.get("text") or "").strip() ]
        # Build a sliding window to match tokens regardless of spacing
        tokens = [t for t,_ in texts]
        for i in range(0, max(0, len(tokens) - len(phrase_tokens) + 1)):
            window = tokens[i:i+len(phrase_tokens)]
            if window == phrase_tokens:
                ws = [texts[i+j][1] for j in range(len(phrase_tokens))]
                left = min(w.get("left", 0) for w in ws)
                top = min(w.get("top", 0) for w in ws)
                right = max(w.get("left", 0) + w.get("width", 0) for w in ws)
                bottom = max(w.get("top", 0) + w.get("height", 0) for w in ws)
                rect = fitz.Rect(left, top, right, bottom)
                results.append((int(ws[0].get("page_num", 1)) - 1, rect))
        # Also try reversed order for RTL quirks
        tokens_rev = list(reversed(tokens))
        for i in range(0, max(0, len(tokens_rev) - len(phrase_tokens) + 1)):
            window = tokens_rev[i:i+len(phrase_tokens)]
            if window == phrase_tokens:
                # Map back to original indices to compute rect
                idxs = list(range(len(tokens)))
                idxs_rev = list(reversed(idxs))
                start_idx = idxs_rev[i]
                end_idx = idxs_rev[i + len(phrase_tokens) - 1]
                ws = [texts[j][1] for j in range(min(start_idx, end_idx), max(start_idx, end_idx) + 1)]
                left = min(w.get("left", 0) for w in ws)
                top = min(w.get("top", 0) for w in ws)
                right = max(w.get("left", 0) + w.get("width", 0) for w in ws)
                bottom = max(w.get("top", 0) + w.get("height", 0) for w in ws)
                rect = fitz.Rect(left, top, right, bottom)
                results.append((int(ws[0].get("page_num", 1)) - 1, rect))
    return results


def replace_name_in_pdf(input_pdf: Path, output_pdf: Path, old_name: str, new_name: str, scale_multiplier: float = 1.0) -> Tuple[int, int]:
    doc = fitz.open(str(input_pdf))
    total_replacements = 0
    pages_touched = 0

    with sync_playwright() as p:
        # Render the replacement text once at a reasonably large size; we'll scale per match
        tmp_png = output_pdf.parent / "replacement_text.png"
        png_w, png_h = render_text_png(p, new_name, tmp_png, font_size_px=28)

        # First try native text search
        matches_found: List[Tuple[int, fitz.Rect]] = []
        for page_index in range(len(doc)):
            page = doc[page_index]
            rects = find_matches(page, old_name)
            for r in rects:
                matches_found.append((page_index, r))

        if not matches_found:
            # Fallback to OCR: render each page and run tesseract to find phrase
            for page_index in range(len(doc)):
                page = doc[page_index]
                zoom = 3.0
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img_path = output_pdf.parent / f"page_{page_index+1:03d}.png"
                pix.save(str(img_path))
                try:
                    tsv_rows = run_tesseract_tsv(img_path, lang="fas+ara")
                except Exception:
                    # Retry with Persian only
                    tsv_rows = run_tesseract_tsv(img_path, lang="fas")
                ocr_matches = find_phrase_boxes_from_tsv(tsv_rows, old_name)
                for _, rect_px in ocr_matches:
                    # Map pixel rect back to PDF coordinates
                    rect_pdf = fitz.Rect(
                        rect_px.x0 / zoom,
                        rect_px.y0 / zoom,
                        rect_px.x1 / zoom,
                        rect_px.y1 / zoom,
                    )
                    matches_found.append((page_index, rect_pdf))

        # Apply overlays
        for page_index, rect in matches_found:
            page = doc[page_index]
            pages_touched += 1
            # Draw rectangle to cover original text using sampled background color around for seamless look
            bg_color = sample_background_color_around(page, rect, margin=2.5)
            # Compute scale to fit width
            target_w = rect.width * (scale_multiplier if scale_multiplier and scale_multiplier > 0 else 1.0)
            scale = target_w / png_w if png_w > 0 else 1.0
            img_w = target_w
            img_h = png_h * scale
            # Center vertically within rect
            y0 = rect.y0 + (rect.height - img_h) / 2
            # Center horizontally relative to original rect
            x0 = rect.x0 - (img_w - rect.width) / 2
            # Paint background under the (possibly larger) overlay
            cover_rect = fitz.Rect(x0, y0, x0 + img_w, y0 + img_h)
            page.draw_rect(cover_rect, fill=bg_color, color=bg_color)
            # Render replacement text with detected color to match surrounding
            color_hex = detect_text_color(page, rect)
            tmp_png2 = output_pdf.parent / "replacement_text_color.png"
            rw, rh = render_text_png(p, new_name, tmp_png2, font_size_px=28, color_hex=color_hex)
            # Rescale based on new raster metrics
            scale = target_w / rw if rw > 0 else 1.0
            img_w = target_w
            img_h = rh * scale
            y0 = rect.y0 + (rect.height - img_h) / 2
            page.insert_image(
                fitz.Rect(x0, y0, x0 + img_w, y0 + img_h),
                filename=str(tmp_png2),
                keep_proportion=True,
            )
            total_replacements += 1

    doc.save(str(output_pdf))
    doc.close()
    return pages_touched, total_replacements


def main():
    if len(sys.argv) < 4:
        print("Usage: python replace_name_pdf.py <input.pdf> <output.pdf> <old_name> [new_name]", file=sys.stderr)
        sys.exit(2)
    input_pdf = Path(sys.argv[1])
    output_pdf = Path(sys.argv[2])
    old_name = sys.argv[3]
    new_name = sys.argv[4] if len(sys.argv) > 4 else "بابک طاهای ابدی"
    scale = 1.0
    if len(sys.argv) > 5:
        try:
            scale = float(sys.argv[5])
        except Exception:
            scale = 1.0
    pages, repls = replace_name_in_pdf(input_pdf, output_pdf, old_name, new_name, scale_multiplier=scale)
    print(f"Pages touched: {pages}, replacements: {repls}")


if __name__ == "__main__":
    main()

