import sys
from pathlib import Path
from typing import List, Tuple

import fitz  # PyMuPDF
from playwright.sync_api import sync_playwright
import cv2
import numpy as np

from replace_name_pdf import (
    render_text_png,
    run_tesseract_tsv,
    find_phrase_boxes_from_tsv,
    find_matches,
)


def normalize_fa(text: str) -> str:
    if text is None:
        return ""
    # Unify Persian/Arabic variants and strip diacritics, ZWNJ, tatweel, punctuation spacing
    mapping = {
        "\u064A": "\u06CC",  # ARABIC YEH -> FARSI YEH
        "\u0643": "\u06A9",  # ARABIC KAF -> KEHEH
        "\u0629": "\u0647",  # TEH MARBUTA -> HEH
        "\u0649": "\u06CC",  # ALEF MAKSURA -> FARSI YEH
        "\u06C0": "\u0647",  # HEH WITH YEH ABOVE -> HEH
        "\u06C2": "\u06C1",  # HEH GOAL WITH HAMZA -> HEH GOAL
    }
    punctuation_to_strip = set(list(
        ":,.;/\\-_()[]{}<>|!?«»ـ،؛:٫٬\u061B\u060C\u061F\u066B\u066C\u06D4\u2009\u202F\u00A0"
    ))
    res = []
    for ch in text:
        # remove diacritics
        if 0x064B <= ord(ch) <= 0x065F:
            continue
        if ord(ch) in (0x200C, 0x200D, 0x200E, 0x200F):  # ZWNJ, ZWJ, LRM, RLM
            continue
        if ord(ch) == 0x0640:  # tatweel
            continue
        if ch in punctuation_to_strip:
            continue
        res.append(mapping.get(ch, ch))
    normalized = "".join(res)
    # collapse spaces
    normalized = "".join(normalized.split())
    return normalized


def find_phrase_boxes_from_tsv_fuzzy(tsv_rows, phrase: str):
    # Join line words and search with normalized text ignoring spaces
    phrase_norm = normalize_fa(phrase)
    from collections import defaultdict

    lines = defaultdict(list)
    for r in tsv_rows:
        if r.get("level") != "5":
            continue
        key = (r.get("page_num", 1), r.get("block_num", 0), r.get("par_num", 0), r.get("line_num", 0))
        lines[key].append(r)

    results = []
    for key, words in lines.items():
        words_sorted = sorted(words, key=lambda w: w.get("left", 0))
        texts = [w.get("text", "") for w in words_sorted]
        texts_norm = [normalize_fa(t) for t in texts]
        line_norm = "".join(texts_norm)
        idx = line_norm.find(phrase_norm)
        if idx == -1:
            continue
        # Map back to word span
        cum = ""
        start_word = 0
        end_word = 0
        for i, t in enumerate(texts_norm):
            prev = len(cum)
            cum += t
            if prev <= idx < len(cum):
                start_word = i
            if len(cum) >= idx + len(phrase_norm):
                end_word = i
                break
        ws = words_sorted[start_word:end_word + 1]
        left = min(w.get("left", 0) for w in ws)
        top = min(w.get("top", 0) for w in ws)
        right = max(w.get("left", 0) + w.get("width", 0) for w in ws)
        bottom = max(w.get("top", 0) + w.get("height", 0) for w in ws)
        rect = fitz.Rect(left, top, right, bottom)
        page_index = int(ws[0].get("page_num", 1)) - 1
        results.append((page_index, rect))
    return results


def add_text_near_phrase(
    input_pdf: Path,
    output_pdf: Path,
    phrase: str,
    text_to_add: str,
    position: str = "left",
    gap_pt: float = 6.0,
) -> Tuple[int, int]:
    doc = fitz.open(str(input_pdf))
    additions = 0
    pages_touched = 0

    with sync_playwright() as p:
        # Render the number to PNG; we'll scale to match label height
        tmp_png = output_pdf.parent / "add_text.png"
        png_w, png_h = render_text_png(p, text_to_add, tmp_png, font_size_px=26)
        # Render the phrase itself for template matching fallback
        phrase_png = output_pdf.parent / "phrase_template.png"
        tpl_w, tpl_h = render_text_png(p, phrase, phrase_png, font_size_px=28)

        # First try native search with common variants
        matches: List[Tuple[int, fitz.Rect]] = []
        variants = [phrase, phrase.replace(" ", ""), phrase.replace(" ", "\u200c")]  # with no-space and with ZWNJ
        for page_index in range(len(doc)):
            page = doc[page_index]
            rects = []
            for v in variants:
                rects.extend(find_matches(page, v))
            for r in rects:
                matches.append((page_index, r))

        if not matches:
            # Fallback to OCR
            for page_index in range(len(doc)):
                page = doc[page_index]
                zoom = 6.0
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img_path = output_pdf.parent / f"ocr_page_{page_index+1:03d}.png"
                pix.save(str(img_path))
                try:
                    rows = None
                    for psm in (6, 4, 3, 11):
                        try:
                            rows = run_tesseract_tsv(img_path, lang="fas+ara", psm=psm)
                            break
                        except Exception:
                            rows = None
                    if rows is None:
                        rows = run_tesseract_tsv(img_path, lang="fas", psm=6)
                except Exception:
                    rows = run_tesseract_tsv(img_path, lang="fas", psm=6)
                ocr_matches = find_phrase_boxes_from_tsv(rows, phrase)
                if not ocr_matches:
                    ocr_matches = find_phrase_boxes_from_tsv_fuzzy(rows, phrase)
                for _, rect_px in ocr_matches:
                    rect_pdf = fitz.Rect(
                        rect_px.x0 / zoom,
                        rect_px.y0 / zoom,
                        rect_px.x1 / zoom,
                        rect_px.y1 / zoom,
                    )
                    matches.append((page_index, rect_pdf))

        if not matches:
            # Fallback to template matching with OpenCV
            page_images = list(sorted(output_pdf.parent.glob("ocr_page_*.png")))
            if not page_images:
                # Render now if OCR step didn't run
                for page_index in range(len(doc)):
                    page = doc[page_index]
                    zoom = 6.0
                    mat = fitz.Matrix(zoom, zoom)
                    pix = page.get_pixmap(matrix=mat, alpha=False)
                    img_path = output_pdf.parent / f"ocr_page_{page_index+1:03d}.png"
                    pix.save(str(img_path))
                page_images = list(sorted(output_pdf.parent.glob("ocr_page_*.png")))

            tpl = cv2.imread(str(phrase_png), cv2.IMREAD_GRAYSCALE)
            if tpl is not None and tpl.size > 0:
                for img_path in page_images:
                    page_num = int(img_path.stem.split("_")[-1]) - 1 if "_" in img_path.stem else 0
                    img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
                    if img is None:
                        continue
                    # Try multiple scales
                    for scale in [0.8, 0.9, 1.0, 1.1, 1.2]:
                        tpl_resized = cv2.resize(tpl, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                        if tpl_resized.shape[0] >= img.shape[0] or tpl_resized.shape[1] >= img.shape[1]:
                            continue
                        res = cv2.matchTemplate(img, tpl_resized, cv2.TM_CCOEFF_NORMED)
                        loc = np.where(res >= 0.65)
                        h, w = tpl_resized.shape
                        for pt_y, pt_x in zip(*loc):
                            # pt in numpy is (row, col) => (y, x)
                            x0_px, y0_px = int(pt_x), int(pt_y)
                            rect_px = fitz.Rect(x0_px, y0_px, x0_px + w, y0_px + h)
                            rect_pdf = fitz.Rect(
                                rect_px.x0 / 6.0,
                                rect_px.y0 / 6.0,
                                rect_px.x1 / 6.0,
                                rect_px.y1 / 6.0,
                            )
                            matches.append((page_num, rect_pdf))
                            break
                        if matches:
                            break
                    if matches:
                        break

        for page_index, rect in matches:
            page = doc[page_index]
            pages_touched += 1
            # Desired height similar to label height
            target_h = rect.height * 0.9
            scale = target_h / png_h if png_h > 0 else 1.0
            img_w = png_w * scale
            img_h = png_h * scale

            if position == "left":
                x1 = rect.x0 - gap_pt
                x0 = x1 - img_w
                y0 = rect.y0 + (rect.height - img_h) / 2
            else:  # right
                x0 = rect.x1 + gap_pt
                y0 = rect.y0 + (rect.height - img_h) / 2
            x1_final = x0 + img_w
            y1_final = y0 + img_h

            page.insert_image(
                fitz.Rect(x0, y0, x1_final, y1_final),
                filename=str(tmp_png),
                keep_proportion=True,
            )
            additions += 1

    doc.save(str(output_pdf))
    doc.close()
    return pages_touched, additions


def main():
    if len(sys.argv) < 5:
        print(
            "Usage: python add_text_near_phrase.py <input.pdf> <output.pdf> <phrase> <text_to_add> [left|right]",
            file=sys.stderr,
        )
        sys.exit(2)
    input_pdf = Path(sys.argv[1])
    output_pdf = Path(sys.argv[2])
    phrase = sys.argv[3]
    text_to_add = sys.argv[4]
    position = sys.argv[5] if len(sys.argv) > 5 else "left"
    pages, adds = add_text_near_phrase(input_pdf, output_pdf, phrase, text_to_add, position=position)
    print(f"Pages touched: {pages}, annotations: {adds}")


if __name__ == "__main__":
    main()

