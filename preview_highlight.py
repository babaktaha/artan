import sys
from pathlib import Path
from typing import List, Tuple, Dict, Any

import fitz  # PyMuPDF

from replace_name_pdf import (
    run_tesseract_tsv,
    find_phrase_boxes_from_tsv,
    find_matches,
)


def highlight_phrase(input_pdf: Path, output_pdf: Path, phrase: str) -> Tuple[int, int]:
    doc = fitz.open(str(input_pdf))
    total_marks = 0
    pages_touched = 0

    # First pass: native search boxes
    matches_by_page: Dict[int, List[fitz.Rect]] = {}
    for page_index in range(len(doc)):
        rects = find_matches(doc[page_index], phrase)
        if rects:
            matches_by_page.setdefault(page_index, []).extend(rects)

    # OCR fallback
    for page_index in range(len(doc)):
        page = doc[page_index]
        zoom = 4.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img_path = output_pdf.parent / f"hl_page_{page_index+1:03d}.png"
        pix.save(str(img_path))
        try:
            tsv = run_tesseract_tsv(img_path, lang="fas+ara")
        except Exception:
            tsv = run_tesseract_tsv(img_path, lang="fas")
        boxes = find_phrase_boxes_from_tsv(tsv, phrase)
        for _, rpx in boxes:
            rpdf = fitz.Rect(rpx.x0 / zoom, rpx.y0 / zoom, rpx.x1 / zoom, rpx.y1 / zoom)
            matches_by_page.setdefault(page_index, []).append(rpdf)

    # Draw highlights
    for page_index, rects in matches_by_page.items():
        if not rects:
            continue
        page = doc[page_index]
        for r in rects:
            page.draw_rect(r, color=(1, 0, 0), width=1)
            total_marks += 1
        pages_touched += 1

    doc.save(str(output_pdf))
    doc.close()
    return pages_touched, total_marks


def main():
    if len(sys.argv) < 4:
        print("Usage: python preview_highlight.py <input.pdf> <output.pdf> <phrase>", file=sys.stderr)
        sys.exit(2)
    input_pdf = Path(sys.argv[1])
    output_pdf = Path(sys.argv[2])
    phrase = sys.argv[3]
    pages, marks = highlight_phrase(input_pdf, output_pdf, phrase)
    print(f"Pages highlighted: {pages}, marks: {marks}")


if __name__ == "__main__":
    main()