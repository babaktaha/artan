import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

import fitz  # PyMuPDF
from playwright.sync_api import sync_playwright

from replace_name_pdf import (
    render_text_png,
    run_tesseract_tsv,
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
        # remove diacritics and tatweel and ZW* marks
        if 0x064B <= ord(ch) <= 0x065F:
            continue
        if ord(ch) in (0x200C, 0x200D, 0x200E, 0x200F):
            continue
        if ord(ch) == 0x0640:
            continue
        res.append(mapping.get(ch, ch))
    return "".join(res)


def group_tsv_rows_by_line(tsv_rows: List[Dict[str, Any]]) -> Dict[Tuple[int, int, int, int], List[Dict[str, Any]]]:
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
    # sort each line by x (left)
    for k in list(groups.keys()):
        groups[k] = sorted(groups[k], key=lambda w: w.get("left", 0))
    return groups


NAME_LABEL_KEYWORDS = [
    "نام",
    "نام:",
    "نامو",
    "نامونامخانوادگی",
    "نامونام خانوادگی",
    "نام و نام خانوادگی",
]


def contains_name_label(text_norm: str) -> bool:
    return any(k.replace(" ", "") in text_norm for k in NAME_LABEL_KEYWORDS)


def find_name_value_boxes(tsv_rows: List[Dict[str, Any]], target_old: str) -> List[Tuple[int, fitz.Rect]]:
    old_norm = normalize_fa(target_old)
    groups = group_tsv_rows_by_line(tsv_rows)
    results: List[Tuple[int, fitz.Rect]] = []
    # iterate lines looking for a line that contains a name label
    for (page_num, block, par, line_no), words in groups.items():
        texts = [normalize_fa((w.get("text") or "").strip()) for w in words]
        # skip empty lines
        if not any(texts):
            continue
        line_norm = "".join(texts)
        if contains_name_label(line_norm):
            # search same line, or within next 1-3 lines in the same block/paragraph
            for look_ahead in range(0, 4):
                key = (page_num, block, par, line_no + look_ahead)
                if key not in groups:
                    continue
                words_la = groups[key]
                for w in words_la:
                    wn = normalize_fa((w.get("text") or ""))
                    if not wn:
                        continue
                    if old_norm in wn or wn == old_norm:
                        left = int(w.get("left", 0))
                        top = int(w.get("top", 0))
                        right = left + int(w.get("width", 0))
                        bottom = top + int(w.get("height", 0))
                        rect = fitz.Rect(left, top, right, bottom)
                        results.append((page_num - 1, rect))
    # Fallback: find any token equal to old name on the page
    if not results:
        for (page_num, block, par, line_no), words in groups.items():
            for w in words:
                wn = normalize_fa((w.get("text") or ""))
                if wn == old_norm:
                    left = int(w.get("left", 0))
                    top = int(w.get("top", 0))
                    right = left + int(w.get("width", 0))
                    bottom = top + int(w.get("height", 0))
                    rect = fitz.Rect(left, top, right, bottom)
                    results.append((page_num - 1, rect))
    return results


def replace_only_name_field(input_pdf: Path, output_pdf: Path, old_name: str, new_name: str) -> Tuple[int, int]:
    doc = fitz.open(str(input_pdf))
    pages_touched = 0
    repls = 0

    with sync_playwright() as p:
        for page_index in range(len(doc)):
            page = doc[page_index]
            # OCR page
            zoom = 6.0
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img_path = output_pdf.parent / f"page_ocr_{page_index+1:03d}.png"
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
            boxes = find_name_value_boxes(rows, old_name)
            # map boxes (pixel) to PDF coords
            mapped: List[fitz.Rect] = []
            for pn, rect_px in boxes:
                if pn != page_index:
                    continue
                rect_pdf = fitz.Rect(
                    rect_px.x0 / zoom,
                    rect_px.y0 / zoom,
                    rect_px.x1 / zoom,
                    rect_px.y1 / zoom,
                )
                mapped.append(rect_pdf)
            if not mapped:
                continue
            # Render replacement text once per page (color from around the field)
            tmp_png = output_pdf.parent / f"name_replace_{page_index:03d}.png"
            color_hex = detect_text_color(page, mapped[0]) or "#000000"
            png_w, png_h = render_text_png(p, new_name, tmp_png, font_size_px=28, color_hex=color_hex)
            for rect in mapped:
                # paint background under exact rect (white for safety)
                page.draw_rect(rect, fill=(1, 1, 1), color=(1, 1, 1))
                target_h = rect.height
                scale = target_h / png_h if png_h > 0 else 1.0
                img_w = png_w * scale
                img_h = png_h * scale
                x0 = rect.x0
                y0 = rect.y0
                page.insert_image(
                    fitz.Rect(x0, y0, x0 + img_w, y0 + img_h),
                    filename=str(tmp_png),
                    keep_proportion=True,
                )
                repls += 1
            pages_touched += 1

    doc.save(str(output_pdf))
    doc.close()
    return pages_touched, repls


def main():
    if len(sys.argv) < 5:
        print("Usage: python replace_name_in_specs.py <input.pdf> <output.pdf> <old_name> <new_name>", file=sys.stderr)
        sys.exit(2)
    input_pdf = Path(sys.argv[1])
    output_pdf = Path(sys.argv[2])
    old_name = sys.argv[3]
    new_name = sys.argv[4]
    pages, repls = replace_only_name_field(input_pdf, output_pdf, old_name, new_name)
    print(f"Pages touched: {pages}, replacements: {repls}")


if __name__ == "__main__":
    main()