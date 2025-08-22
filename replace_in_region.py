import sys
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any

import fitz  # PyMuPDF
from playwright.sync_api import sync_playwright

from replace_name_pdf import (
    render_text_png,
    run_tesseract_tsv,
    find_phrase_boxes_from_tsv,
    find_matches,
    sample_background_color_around,
    detect_text_color,
)


def find_block_rect_by_phrase(page: fitz.Page, phrase: str) -> Optional[fitz.Rect]:
    try:
        info = page.get_text("dict")
    except Exception:
        return None
    phrase_norm = phrase.strip()
    best_rect: Optional[fitz.Rect] = None
    for block in info.get("blocks", []):
        block_rects: List[fitz.Rect] = []
        contains = False
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                txt = (span.get("text") or "").strip()
                bbox = span.get("bbox")
                if not bbox:
                    continue
                srect = fitz.Rect(*bbox)
                block_rects.append(srect)
                if phrase_norm and phrase_norm in txt:
                    contains = True
        if contains and block_rects:
            x0 = min(r.x0 for r in block_rects)
            y0 = min(r.y0 for r in block_rects)
            x1 = max(r.x1 for r in block_rects)
            y1 = max(r.y1 for r in block_rects)
            best_rect = fitz.Rect(x0, y0, x1, y1)
            break
    if best_rect is None:
        # fallback: try native search to get a rect, then expand
        rects = find_matches(page, phrase)
        if rects:
            best_rect = rects[0]
    return best_rect


def expand_region(page: fitz.Page, anchor: fitz.Rect, expand: Tuple[float, float, float, float]) -> fitz.Rect:
    px, py = page.rect.width, page.rect.height
    left_ratio, top_ratio, right_ratio, bottom_ratio = expand
    return fitz.Rect(
        max(page.rect.x0, anchor.x0 - px * left_ratio),
        max(page.rect.y0, anchor.y0 - py * top_ratio),
        min(page.rect.x1, anchor.x1 + px * right_ratio),
        min(page.rect.y1, anchor.y1 + py * bottom_ratio),
    )


def replace_phrase_in_region(
    input_pdf: Path,
    output_pdf: Path,
    region_label: str,
    old_phrase: str,
    new_phrase: str,
) -> Tuple[int, int]:
    doc = fitz.open(str(input_pdf))
    total_replacements = 0
    pages_touched = 0

    with sync_playwright() as p:
        # Pre-render replacement text once; will re-render per page to match color if needed
        tmp_png = output_pdf.parent / "region_replacement.png"
        png_w, png_h = render_text_png(p, new_phrase, tmp_png, font_size_px=28)

        for page_index in range(len(doc)):
            page = doc[page_index]
            anchor = find_block_rect_by_phrase(page, region_label)
            if anchor is None:
                continue
            # Expand region heuristically downward and sideways to cover the details table/fields
            region = expand_region(page, anchor, expand=(0.05, 0.01, 0.05, 0.25))

            # Find matches of old_phrase
            rects = find_matches(page, old_phrase)
            # If none, try OCR in this region only
            if not rects:
                zoom = 4.0
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat, clip=region, alpha=False)
                img_path = output_pdf.parent / f"region_ocr_{page_index+1:03d}.png"
                pix.save(str(img_path))
                try:
                    tsv_rows = run_tesseract_tsv(img_path, lang="fas+ara")
                except Exception:
                    tsv_rows = run_tesseract_tsv(img_path, lang="fas")
                ocr_matches = find_phrase_boxes_from_tsv(tsv_rows, old_phrase)
                for _, rect_px in ocr_matches:
                    rect_pdf = fitz.Rect(
                        region.x0 + rect_px.x0 / zoom,
                        region.y0 + rect_px.y0 / zoom,
                        region.x0 + rect_px.x1 / zoom,
                        region.y0 + rect_px.y1 / zoom,
                    )
                    rects.append(rect_pdf)

            # Filter rects to those inside region
            rects_in_region = [r for r in rects if region.contains(r)]
            if not rects_in_region:
                continue

            # For each match, overlay replacement text matching height and color
            for match_rect in rects_in_region:
                pages_touched += 1
                bg_color = sample_background_color_around(page, match_rect, margin=2.0)
                page.draw_rect(match_rect, fill=bg_color, color=bg_color)
                # Scale by height to maintain layout
                target_h = match_rect.height
                scale = target_h / png_h if png_h > 0 else 1.0
                img_w = png_w * scale
                img_h = png_h * scale
                # Align to left of match rect
                x0 = match_rect.x0
                y0 = match_rect.y0
                # Re-render with detected text color for this area
                color_hex = detect_text_color(page, match_rect)
                if color_hex:
                    png_w2, png_h2 = render_text_png(p, new_phrase, tmp_png, font_size_px=28, color_hex=color_hex)
                    scale = target_h / png_h2 if png_h2 > 0 else 1.0
                    img_w = png_w2 * scale
                    img_h = png_h2 * scale
                page.insert_image(
                    fitz.Rect(x0, y0, x0 + img_w, y0 + img_h),
                    filename=str(tmp_png),
                    keep_proportion=True,
                )
                total_replacements += 1

    doc.save(str(output_pdf))
    doc.close()
    return pages_touched, total_replacements


def main():
    if len(sys.argv) < 6:
        print(
            "Usage: python replace_in_region.py <input.pdf> <output.pdf> <region_label> <old> <new>",
            file=sys.stderr,
        )
        sys.exit(2)
    input_pdf = Path(sys.argv[1])
    output_pdf = Path(sys.argv[2])
    region_label = sys.argv[3]
    old_phrase = sys.argv[4]
    new_phrase = sys.argv[5]
    pages, repls = replace_phrase_in_region(input_pdf, output_pdf, region_label, old_phrase, new_phrase)
    print(f"Pages touched: {pages}, replacements: {repls}")


if __name__ == "__main__":
    main()