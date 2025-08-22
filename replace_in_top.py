import sys
from pathlib import Path
from typing import List, Tuple, Dict, Any

import fitz  # PyMuPDF
from playwright.sync_api import sync_playwright

from replace_name_pdf import (
    render_text_png,
    run_tesseract_tsv,
    find_phrase_boxes_from_tsv,
    find_matches,
    detect_text_color,
)


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


def replace_in_top_region(
    input_pdf: Path,
    output_pdf: Path,
    old_phrase: str,
    new_phrase: str,
    top_fraction: float = 0.4,
) -> Tuple[int, int]:
    doc = fitz.open(str(input_pdf))
    pages_touched = 0
    replacements = 0

    with sync_playwright() as p:
        for page_index in range(len(doc)):
            page = doc[page_index]
            page_h = page.rect.height
            top_limit = page_h * top_fraction
            top_rect = fitz.Rect(page.rect.x0, page.rect.y0, page.rect.x1, top_limit)

            # Collect matches via native search
            candidate_rects: List[fitz.Rect] = []
            for r in find_matches(page, old_phrase):
                if top_rect.contains(r):
                    candidate_rects.append(r)

            # OCR fallback on the top region
            if not candidate_rects:
                zoom = 6.0
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat, clip=top_rect, alpha=False)
                img_path = output_pdf.parent / f"top_ocr_{page_index+1:03d}.png"
                pix.save(str(img_path))
                rows = None
                for psm in (6, 4, 3, 11):
                    try:
                        rows = run_tesseract_tsv(img_path, lang="fas+ara", psm=psm)
                        break
                    except Exception:
                        rows = None
                if rows is None:
                    rows = run_tesseract_tsv(img_path, lang="fas", psm=6)
                boxes = find_phrase_boxes_from_tsv(rows, old_phrase)
                for _, rpx in boxes:
                    rpdf = fitz.Rect(
                        top_rect.x0 + rpx.x0 / zoom,
                        top_rect.y0 + rpx.y0 / zoom,
                        top_rect.x0 + rpx.x1 / zoom,
                        top_rect.y0 + rpx.y1 / zoom,
                    )
                    candidate_rects.append(rpdf)

            if not candidate_rects:
                continue

            # Render replacement with detected color
            color_hex = detect_text_color(page, candidate_rects[0]) or "#000000"
            tmp_png = output_pdf.parent / f"top_replace_{page_index:03d}.png"
            png_w, png_h = render_text_png(p, new_phrase, tmp_png, font_size_px=28, color_hex=color_hex)

            for r in candidate_rects:
                # paint white under original token and place new text scaled by height
                page.draw_rect(r, fill=(1, 1, 1), color=(1, 1, 1))
                target_h = r.height
                scale = target_h / png_h if png_h > 0 else 1.0
                img_w = png_w * scale
                img_h = png_h * scale
                x0 = r.x0
                y0 = r.y0
                page.insert_image(
                    fitz.Rect(x0, y0, x0 + img_w, y0 + img_h),
                    filename=str(tmp_png),
                    keep_proportion=True,
                )
                replacements += 1
            pages_touched += 1

    doc.save(str(output_pdf))
    doc.close()
    return pages_touched, replacements


def main():
    if len(sys.argv) < 5:
        print(
            "Usage: python replace_in_top.py <input.pdf> <output.pdf> <old> <new> [top_fraction]",
            file=sys.stderr,
        )
        sys.exit(2)
    input_pdf = Path(sys.argv[1])
    output_pdf = Path(sys.argv[2])
    old_phrase = sys.argv[3]
    new_phrase = sys.argv[4]
    top_fraction = float(sys.argv[5]) if len(sys.argv) > 5 else 0.4
    pages, reps = replace_in_top_region(input_pdf, output_pdf, old_phrase, new_phrase, top_fraction=top_fraction)
    print(f"Pages touched: {pages}, replacements: {reps}")


if __name__ == "__main__":
    main()