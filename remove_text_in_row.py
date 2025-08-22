import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

import fitz  # PyMuPDF

from replace_name_pdf import (
    run_tesseract_tsv,
    sample_background_color_around,
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
        # remove diacritics and tatweel and ZW marks
        if 0x064B <= ord(ch) <= 0x065F:
            continue
        if ord(ch) in (0x200C, 0x200D, 0x200E, 0x200F):
            continue
        if ord(ch) == 0x0640:
            continue
        res.append(mapping.get(ch, ch))
    return "".join(res)


def group_lines(tsv_rows: List[Dict[str, Any]]) -> Dict[Tuple[int, int, int, int], List[Dict[str, Any]]]:
    groups: Dict[Tuple[int, int, int, int], List[Dict[str, Any]]] = {}
    for r in tsv_rows:
        if r.get("level") != "5":
            continue
        key = (
            int(r.get("page_num", 1)),
            int(r.get("block_num", 0)),
            int(r.get("par_num", 0)),
            int(r.get("line_num", 0)),
        )
        groups.setdefault(key, []).append(r)
    # sort by x (left)
    for k in list(groups.keys()):
        groups[k] = sorted(groups[k], key=lambda w: w.get("left", 0))
    return groups


def remove_babak_on_row7(input_pdf: Path, output_pdf: Path) -> Tuple[int, int]:
    doc = fitz.open(str(input_pdf))
    pages_touched = 0
    removals = 0

    for page_index in range(len(doc)):
        page = doc[page_index]
        zoom = 6.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img_path = output_pdf.parent / f"ocr_row_{page_index+1:03d}.png"
        pix.save(str(img_path))
        try:
            rows = run_tesseract_tsv(img_path, lang="fas+ara", psm=6)
        except Exception:
            rows = run_tesseract_tsv(img_path, lang="fas", psm=6)
        groups = group_lines(rows)
        to_cover_pdf_rects: List[fitz.Rect] = []
        for (pn, b, p, ln), words in groups.items():
            if pn - 1 != page_index:
                continue
            texts = [normalize_fa((w.get("text") or "").strip()) for w in words]
            line_text = "".join(texts)
            # consider a line relevant if it contains a 7 marker in Persian or Latin
            if ("۷" not in line_text) and ("7" not in line_text):
                continue
            # find tokens that are exactly 'بابک' (normalized)
            for w in words:
                wn = normalize_fa((w.get("text") or ""))
                if wn == "بابک":
                    left = int(w.get("left", 0))
                    top = int(w.get("top", 0))
                    right = left + int(w.get("width", 0))
                    bottom = top + int(w.get("height", 0))
                    rect_px = fitz.Rect(left, top, right, bottom)
                    # map to PDF coords
                    rect_pdf = fitz.Rect(
                        rect_px.x0 / zoom,
                        rect_px.y0 / zoom,
                        rect_px.x1 / zoom,
                        rect_px.y1 / zoom,
                    )
                    to_cover_pdf_rects.append(rect_pdf)
        if not to_cover_pdf_rects:
            continue
        for r in to_cover_pdf_rects:
            bg = sample_background_color_around(page, r, margin=2.0)
            page.draw_rect(r, fill=bg, color=bg)
            removals += 1
        pages_touched += 1

    doc.save(str(output_pdf))
    doc.close()
    return pages_touched, removals


def main():
    if len(sys.argv) < 3:
        print("Usage: python remove_text_in_row.py <input.pdf> <output.pdf>", file=sys.stderr)
        sys.exit(2)
    input_pdf = Path(sys.argv[1])
    output_pdf = Path(sys.argv[2])
    pages, removals = remove_babak_on_row7(input_pdf, output_pdf)
    print(f"Pages touched: {pages}, removals: {removals}")


if __name__ == "__main__":
    main()