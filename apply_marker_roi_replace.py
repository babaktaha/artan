import sys
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any

import fitz  # PyMuPDF
import cv2
import numpy as np

from replace_name_pdf import (
    render_text_png,
    run_tesseract_tsv,
    detect_text_color,
)


def replace_in_marker_roi(
    input_pdf: Path,
    output_pdf: Path,
    marker_img: Path,
    old_phrase: str,
    new_phrase: str,
) -> Tuple[int, int]:
    doc = fitz.open(str(input_pdf))
    pages_touched = 0
    replacements = 0

    marker = cv2.imread(str(marker_img), cv2.IMREAD_GRAYSCALE)
    if marker is None or marker.size == 0:
        raise RuntimeError(f"Cannot read marker image: {marker_img}")

    # Preprocess marker to enhance shapes regardless of color
    marker_blur = cv2.GaussianBlur(marker, (3, 3), 0)
    m_edges = cv2.Canny(marker_blur, 50, 150)

    for page_index in range(len(doc)):
        page = doc[page_index]
        zoom = 4.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        page_png = output_pdf.parent / f"marker_page_{page_index+1:03d}.png"
        pix.save(str(page_png))

        img_color = cv2.imread(str(page_png), cv2.IMREAD_COLOR)
        if img_color is None:
            continue
        img = cv2.cvtColor(img_color, cv2.COLOR_BGR2GRAY)
        img_blur = cv2.GaussianBlur(img, (3, 3), 0)
        i_edges = cv2.Canny(img_blur, 50, 150)

        found = False
        best_rect_px = None
        # Try several scales of the marker to be robust
        for scale in [0.6, 0.7, 0.8, 0.9, 1.0, 1.1]:
            mh, mw = m_edges.shape[:2]
            new_w = max(10, int(mw * scale))
            new_h = max(10, int(mh * scale))
            tpl = cv2.resize(m_edges, (new_w, new_h), interpolation=cv2.INTER_AREA)
            if tpl.shape[0] >= i_edges.shape[0] or tpl.shape[1] >= i_edges.shape[1]:
                continue
            res = cv2.matchTemplate(i_edges, tpl, cv2.TM_CCOEFF_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
            if max_val >= 0.45:  # moderate threshold due to annotations
                top_left = max_loc
                h, w = tpl.shape[:2]
                best_rect_px = (top_left[0], top_left[1], top_left[0] + w, top_left[1] + h)
                found = True
                break
        if not found or best_rect_px is None:
            continue

        # Define ROI around the matched area (inflate a bit)
        x0, y0, x1, y1 = best_rect_px
        pad = int(0.08 * max(img.shape[0], img.shape[1]))
        rx0 = max(0, x0 - pad)
        ry0 = max(0, y0 - pad)
        rx1 = min(img.shape[1] - 1, x1 + pad)
        ry1 = min(img.shape[0] - 1, y1 + pad)

        # Map ROI back to PDF coordinates
        roi_pdf = fitz.Rect(rx0 / zoom, ry0 / zoom, rx1 / zoom, ry1 / zoom)

        # OCR within ROI to find old_phrase tokens
        roi_pix = page.get_pixmap(matrix=mat, clip=roi_pdf, alpha=False)
        roi_png = output_pdf.parent / f"marker_roi_{page_index+1:03d}.png"
        roi_pix.save(str(roi_png))
        try:
            rows = run_tesseract_tsv(roi_png, lang="fas+ara", psm=6)
        except Exception:
            rows = run_tesseract_tsv(roi_png, lang="fas", psm=6)

        def normalize_fa(s: str) -> str:
            if s is None:
                return ""
            mapping = {"\u064A": "\u06CC", "\u0643": "\u06A9", "\u0629": "\u0647", "\u0649": "\u06CC"}
            out = []
            for ch in s:
                if 0x064B <= ord(ch) <= 0x065F:  # diacritics
                    continue
                if ord(ch) in (0x200C, 0x200D, 0x200E, 0x200F) or ord(ch) == 0x0640:
                    continue
                out.append(mapping.get(ch, ch))
            return "".join(out)

        old_norm = normalize_fa(old_phrase)
        matches_px: List[fitz.Rect] = []
        for r in rows:
            if r.get("level") != "5":
                continue
            t = normalize_fa((r.get("text") or "").strip())
            if not t:
                continue
            if old_norm and old_norm in t:
                l = int(r.get("left", 0))
                ttop = int(r.get("top", 0))
                w = int(r.get("width", 0))
                h = int(r.get("height", 0))
                rect_px = fitz.Rect(l, ttop, l + w, ttop + h)
                # Map into page PDF coords (offset by roi origin)
                rect_pdf = fitz.Rect(
                    roi_pdf.x0 + rect_px.x0 / zoom,
                    roi_pdf.y0 + rect_px.y0 / zoom,
                    roi_pdf.x0 + rect_px.x1 / zoom,
                    roi_pdf.y0 + rect_px.y1 / zoom,
                )
                matches_px.append(rect_pdf)

        if not matches_px:
            continue

        # Prepare replacement rendering with local color
        color_hex = detect_text_color(page, matches_px[0]) or "#000000"
        tmp_png = output_pdf.parent / f"marker_replace_{page_index:03d}.png"
        png_w, png_h = render_text_png(None, new_phrase, tmp_png, font_size_px=28, color_hex=color_hex)  # type: ignore
        # Note: render_text_png expects playwright context; we can call once per page using a small with-block
        # Use a small context to render with color
        from playwright.sync_api import sync_playwright as sp
        with sp() as pp:
            png_w, png_h = render_text_png(pp, new_phrase, tmp_png, font_size_px=28, color_hex=color_hex)  # type: ignore

        for mr in matches_px:
            # Paint white background just under the match and insert replacement scaled by height
            page.draw_rect(mr, fill=(1, 1, 1), color=(1, 1, 1))
            target_h = mr.height
            scale = target_h / png_h if png_h > 0 else 1.0
            img_w = png_w * scale
            img_h = png_h * scale
            x0i = mr.x0
            y0i = mr.y0
            page.insert_image(
                fitz.Rect(x0i, y0i, x0i + img_w, y0i + img_h),
                filename=str(tmp_png),
                keep_proportion=True,
            )
            replacements += 1
        pages_touched += 1

    doc.save(str(output_pdf))
    doc.close()
    return pages_touched, replacements


def main():
    if len(sys.argv) < 6:
        print(
            "Usage: python apply_marker_roi_replace.py <input.pdf> <output.pdf> <marker.png> <old> <new>",
            file=sys.stderr,
        )
        sys.exit(2)
    input_pdf = Path(sys.argv[1])
    output_pdf = Path(sys.argv[2])
    marker_path = Path(sys.argv[3])
    old_phrase = sys.argv[4]
    new_phrase = sys.argv[5]
    pages, reps = replace_in_marker_roi(input_pdf, output_pdf, marker_path, old_phrase, new_phrase)
    print(f"Pages touched: {pages}, replacements: {reps}")


if __name__ == "__main__":
    main()