import sys
import subprocess
import csv
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional

import fitz  # PyMuPDF
from playwright.sync_api import sync_playwright
import cv2
import numpy as np


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


def normalize_fa(text: str) -> str:
    if text is None:
        return ""
    mapping = {
        "\u064A": "\u06CC",
        "\u0643": "\u06A9",
        "\u0629": "\u0647",
        "\u0649": "\u06CC",
        "\u06C0": "\u0647",
        "\u06C2": "\u06C1",
    }
    res = []
    for ch in text:
        if 0x064B <= ord(ch) <= 0x065F:
            continue
        if ord(ch) in (0x200C, 0x200D, 0x200E, 0x200F):
            continue
        if ord(ch) == 0x0640:
            continue
        res.append(mapping.get(ch, ch))
    return "".join(res)


def find_spans_with_phrase(page: fitz.Page, phrase: str, top_fraction: float = 0.35) -> List[fitz.Rect]:
    rects: List[fitz.Rect] = []
    try:
        info = page.get_text("dict")
        page_h = page.rect.height
        phrase_norm = normalize_fa(phrase)
        for block in info.get("blocks", []):
            for line in block.get("lines", []):
                # Skip lines not in top area if requested
                words_rects = []
                for span in line.get("spans", []):
                    bbox = span.get("bbox")
                    if not bbox:
                        continue
                    srect = fitz.Rect(*bbox)
                    if top_fraction is not None and srect.y1 > page_h * top_fraction:
                        continue
                    txt = normalize_fa(span.get("text", ""))
                    if phrase_norm and phrase_norm in txt:
                        rects.append(srect)
    except Exception:
        pass
    return rects


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


def find_container_rect(page: fitz.Page, rect: fitz.Rect, pad: float = 1.5) -> fitz.Rect:
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    candidates = []
    for d in drawings:
        r = d.get("rect")
        if not r:
            continue
        r = fitz.Rect(r)
        # if this drawing rect reasonably contains our rect
        if r.contains(rect):
            candidates.append(r)
    if candidates:
        # choose the smallest area that still contains rect
        candidates.sort(key=lambda r: r.width * r.height)
        return candidates[0]
    # fallback: slightly grow the rect
    return fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)


def find_container_rect_edges(page: fitz.Page, rect: fitz.Rect) -> fitz.Rect | None:
    # Use edge/contour detection around the rect to find a box (table cell/frame)
    zoom = 6.0
    pad = max(rect.width, rect.height) * 0.3
    clip = fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
    except Exception:
        return None
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if img.shape[2] >= 3:
        gray = cv2.cvtColor(img[:, :, :3], cv2.COLOR_RGB2GRAY)
    else:
        gray = img[:, :, 0]
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(blur, 60, 150)
    # Dilate to close gaps in lines
    kernel = np.ones((3, 3), np.uint8)
    edges = cv2.dilate(edges, kernel, iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    # Convert target rect to local (clip) pixel coords
    target_px = fitz.Rect(
        (rect.x0 - clip.x0) * zoom,
        (rect.y0 - clip.y0) * zoom,
        (rect.x1 - clip.x0) * zoom,
        (rect.y1 - clip.y0) * zoom,
    )
    h, w = edges.shape[:2]
    best = None
    best_area = None
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        # Skip too small boxes
        if cw * ch < 500:  # pixels
            continue
        c_rect = fitz.Rect(x, y, x + cw, y + ch)
        # Require it to contain the target area
        if not fitz.Rect(c_rect).contains(target_px):
            continue
        area = cw * ch
        if best is None or area < best_area:
            best = c_rect
            best_area = area
    if best is None:
        return None
    # Map back to PDF coords
    bx0 = best.x0 / zoom + clip.x0
    by0 = best.y0 / zoom + clip.y0
    bx1 = best.x1 / zoom + clip.x1
    by1 = best.y1 / zoom + clip.y1
    return fitz.Rect(bx0, by0, bx1, by1)


def replace_name_in_pdf(
    input_pdf: Path,
    output_pdf: Path,
    old_name: str,
    new_name: str,
    scale_multiplier: float = 1.0,
    force_white_bg: bool = False,
    fit_container: bool = False,
    reference_pdf: Optional[Path] = None,
    reference_phrase: Optional[str] = None,
    header_only: bool = False,
) -> Tuple[int, int]:
    doc = fitz.open(str(input_pdf))
    total_replacements = 0
    pages_touched = 0

    with sync_playwright() as p:
        # Render the replacement text once at a reasonably large size; we'll scale per match
        tmp_png = output_pdf.parent / "replacement_text.png"
        png_w, png_h = render_text_png(p, new_name, tmp_png, font_size_px=28)
        # Also render the old phrase for template matching
        phrase_png = output_pdf.parent / "phrase_match.png"
        _mw, _mh = render_text_png(p, old_name, phrase_png, font_size_px=28)

        # Build matches (optionally header-only)
        matches_found: List[Tuple[int, fitz.Rect]] = []
        if header_only:
            for page_index in range(len(doc)):
                page = doc[page_index]
                span_rects = find_spans_with_phrase(page, old_name, top_fraction=0.35)
                for r in span_rects:
                    matches_found.append((page_index, r))
        else:
            for page_index in range(len(doc)):
                page = doc[page_index]
                rects = find_matches(page, old_name)
                for r in rects:
                    matches_found.append((page_index, r))
            # Also scan spans in top area (header) to catch shaped text
            for page_index in range(len(doc)):
                page = doc[page_index]
                span_rects = find_spans_with_phrase(page, old_name, top_fraction=0.4)
                for r in span_rects:
                    matches_found.append((page_index, r))

        if not matches_found and not header_only:
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

        if not matches_found and not header_only:
            # Fallback to template matching (useful when text is already overlaid as image)
            # Ensure page images exist at higher zoom
            page_images = []
            for page_index in range(len(doc)):
                page = doc[page_index]
                zoom = 6.0
                mat = fitz.Matrix(zoom, zoom)
                img_path = output_pdf.parent / f"tmpl_page_{page_index+1:03d}.png"
                page.get_pixmap(matrix=mat, alpha=False).save(str(img_path))
                page_images.append((page_index, img_path))

            tpl = cv2.imread(str(phrase_png), cv2.IMREAD_GRAYSCALE)
            if tpl is not None and tpl.size > 0:
                for page_index, img_path in page_images:
                    img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
                    if img is None:
                        continue
                    for scale in [0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.4, 1.6, 1.8, 2.0]:
                        tpl_resized = cv2.resize(tpl, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                        if tpl_resized.shape[0] >= img.shape[0] or tpl_resized.shape[1] >= img.shape[1]:
                            continue
                        res = cv2.matchTemplate(img, tpl_resized, cv2.TM_CCOEFF_NORMED)
                        loc = np.where(res >= 0.65)
                        h, w = tpl_resized.shape
                        found = False
                        for pt_y, pt_x in zip(*loc):
                            x0_px, y0_px = int(pt_x), int(pt_y)
                            rect_px = fitz.Rect(x0_px, y0_px, x0_px + w, y0_px + h)
                            rect_pdf = fitz.Rect(
                                rect_px.x0 / 6.0,
                                rect_px.y0 / 6.0,
                                rect_px.x1 / 6.0,
                                rect_px.y1 / 6.0,
                            )
                            matches_found.append((page_index, rect_pdf))
                            found = True
                            break
                        if found:
                            break

        # Optionally find reference rectangles for the original phrase (e.g., 'خرداد') from a reference PDF
        ref_rects_by_page: Dict[int, List[fitz.Rect]] = {}
        if reference_pdf and reference_phrase:
            try:
                ref_doc = fitz.open(str(reference_pdf))
                if header_only:
                    # Prefer header spans for reference
                    for pidx in range(len(ref_doc)):
                        span_rects = find_spans_with_phrase(ref_doc[pidx], reference_phrase, top_fraction=0.35)
                        if span_rects:
                            ref_rects_by_page.setdefault(pidx, []).extend(span_rects)
                # Also native search
                for pidx in range(len(ref_doc)):
                    rects = find_matches(ref_doc[pidx], reference_phrase)
                    if rects:
                        ref_rects_by_page.setdefault(pidx, []).extend(rects)
                # If none found at all and not header-only, do OCR on ref
                if not any(ref_rects_by_page.values()) and not header_only:
                    for pidx in range(len(ref_doc)):
                        page = ref_doc[pidx]
                        zoom = 3.0
                        mat = fitz.Matrix(zoom, zoom)
                        pix = page.get_pixmap(matrix=mat, alpha=False)
                        img_path = output_pdf.parent / f"ref_page_{pidx+1:03d}.png"
                        pix.save(str(img_path))
                        try:
                            tsv_rows = run_tesseract_tsv(img_path, lang="fas+ara")
                        except Exception:
                            tsv_rows = run_tesseract_tsv(img_path, lang="fas")
                        ocr_matches = find_phrase_boxes_from_tsv(tsv_rows, reference_phrase)
                        for _, rect_px in ocr_matches:
                            rect_pdf = fitz.Rect(rect_px.x0 / zoom, rect_px.y0 / zoom, rect_px.x1 / zoom, rect_px.y1 / zoom)
                            ref_rects_by_page.setdefault(pidx, []).append(rect_pdf)
                ref_doc.close()
            except Exception:
                ref_rects_by_page = {}

        # Apply overlays
        for page_index, rect in matches_found:
            page = doc[page_index]
            pages_touched += 1
            # Draw rectangle to cover original text using sampled color (or forced white)
            bg_color = (1.0, 1.0, 1.0) if force_white_bg else sample_background_color_around(page, rect, margin=2.5)
            # Optionally fit to detected container rectangle
            container = find_container_rect_edges(page, rect) or find_container_rect(page, rect, pad=2.0)
            # If reference rectangles exist on the same page, pick the nearest to align and size exactly
            ref_rect: Optional[fitz.Rect] = None
            if ref_rects_by_page.get(page_index):
                cx, cy = rect.x0 + rect.width / 2, rect.y0 + rect.height / 2
                best_d = None
                for rr in ref_rects_by_page[page_index]:
                    rcx, rcy = rr.x0 + rr.width / 2, rr.y0 + rr.height / 2
                    d = (rcx - cx) ** 2 + (rcy - cy) ** 2
                    if best_d is None or d < best_d:
                        best_d = d
                        ref_rect = rr
            base_width = (ref_rect.width if ref_rect else container.width) if fit_container else (ref_rect.width if ref_rect else rect.width)
            # Compute scale to fit both width and height of container with margin
            margin_ratio = 0.9 if fit_container else 1.0
            target_w = base_width * margin_ratio * (scale_multiplier if scale_multiplier and scale_multiplier > 0 else 1.0)
            target_h_limit = ((ref_rect.height if ref_rect else container.height) * margin_ratio) if fit_container else (ref_rect.height if ref_rect else rect.height)
            scale_w = target_w / png_w if png_w > 0 else 1.0
            scale_h = target_h_limit / png_h if png_h > 0 else 1.0
            scale = min(scale_w, scale_h)
            img_w = png_w * scale
            img_h = png_h * scale
            # Center vertically within rect
            y0 = rect.y0 + (rect.height - img_h) / 2
            # Center horizontally relative to container or align to ref rect
            if ref_rect:
                x0 = ref_rect.x0 + (ref_rect.width - img_w) / 2
            else:
                x0 = container.x0 + (container.width - img_w) / 2
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
            if ref_rect:
                y0 = ref_rect.y0 + (ref_rect.height - img_h) / 2
            else:
                y0 = container.y0 + (container.height - img_h) / 2
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
    force_white = False
    fit_container = False
    if len(sys.argv) > 5:
        try:
            scale = float(sys.argv[5])
        except Exception:
            # if not float, maybe it's a flag
            if sys.argv[5].lower() in ("white", "force_white", "white_bg"):
                force_white = True
    if len(sys.argv) > 6:
        if sys.argv[6].lower() in ("white", "force_white", "white_bg"):
            force_white = True
        if sys.argv[6].lower() in ("fit", "fit_container", "fitbox", "fit_box"):
            fit_container = True
    if len(sys.argv) > 7:
        if sys.argv[7].lower() in ("fit", "fit_container", "fitbox", "fit_box"):
            fit_container = True
    pages, repls = replace_name_in_pdf(
        input_pdf,
        output_pdf,
        old_name,
        new_name,
        scale_multiplier=scale,
        force_white_bg=force_white,
        fit_container=fit_container,
    )
    print(f"Pages touched: {pages}, replacements: {repls}")


if __name__ == "__main__":
    main()

