import sys
from pathlib import Path
from typing import List, Tuple, Dict, Any

import fitz  # PyMuPDF
import cv2
import numpy as np
from playwright.sync_api import sync_playwright

from replace_name_pdf import (
    render_text_png,
    run_tesseract_tsv,
    detect_text_color,
)


def find_red_rois(image_bgr: np.ndarray) -> List[Tuple[int, int, int, int]]:
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    # Red ranges in HSV
    lower1 = np.array([0, 80, 80])
    upper1 = np.array([12, 255, 255])
    lower2 = np.array([168, 80, 80])
    upper2 = np.array([180, 255, 255])
    mask1 = cv2.inRange(hsv, lower1, upper1)
    mask2 = cv2.inRange(hsv, lower2, upper2)
    mask = cv2.bitwise_or(mask1, mask2)
    # Morphology to connect strokes
    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    rois: List[Tuple[int, int, int, int]] = []
    h, w = image_bgr.shape[:2]
    min_area = max(1000, int(0.001 * w * h))
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        if cw * ch < min_area:
            continue
        # Inflate a bit to cover inside box text area
        pad_x = int(cw * 0.1)
        pad_y = int(ch * 0.1)
        x0 = max(0, x - pad_x)
        y0 = max(0, y - pad_y)
        x1 = min(w - 1, x + cw + pad_x)
        y1 = min(h - 1, y + ch + pad_y)
        rois.append((x0, y0, x1, y1))
    # Merge overlapping ROIs
    merged = True
    while merged:
        merged = False
        new_rois: List[Tuple[int, int, int, int]] = []
        used = [False] * len(rois)
        for i in range(len(rois)):
            if used[i]:
                continue
            x0, y0, x1, y1 = rois[i]
            rx0, ry0, rx1, ry1 = x0, y0, x1, y1
            for j in range(i + 1, len(rois)):
                if used[j]:
                    continue
                a0, b0, a1, b1 = rois[j]
                if not (rx1 < a0 or a1 < rx0 or ry1 < b0 or b1 < ry0):
                    rx0 = min(rx0, a0)
                    ry0 = min(ry0, b0)
                    rx1 = max(rx1, a1)
                    ry1 = max(ry1, b1)
                    used[j] = True
                    merged = True
            used[i] = True
            new_rois.append((rx0, ry0, rx1, ry1))
        rois = new_rois
    return rois


def replace_in_red_boxes(input_pdf: Path, output_pdf: Path, old_phrase: str, new_phrase: str) -> Tuple[int, int]:
    doc = fitz.open(str(input_pdf))
    pages_touched = 0
    replacements = 0

    with sync_playwright() as p:
        for page_index in range(len(doc)):
            page = doc[page_index]
            # Render full page
            zoom = 6.0
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            page_img_path = output_pdf.parent / f"red_page_{page_index+1:03d}.png"
            pix.save(str(page_img_path))
            img = cv2.imread(str(page_img_path), cv2.IMREAD_COLOR)
            if img is None:
                continue
            rois = find_red_rois(img)
            if not rois:
                continue

            for (x0, y0, x1, y1) in rois:
                # Map ROI to PDF coords
                roi_pdf = fitz.Rect(x0 / zoom, y0 / zoom, x1 / zoom, y1 / zoom)
                # OCR in ROI
                roi_pix = page.get_pixmap(matrix=mat, clip=roi_pdf, alpha=False)
                roi_img_path = output_pdf.parent / f"red_roi_{page_index+1:03d}.png"
                roi_pix.save(str(roi_img_path))
                rows = None
                for psm in (6, 4, 3, 11):
                    try:
                        rows = run_tesseract_tsv(roi_img_path, lang="fas+ara", psm=psm)
                        break
                    except Exception:
                        rows = None
                if rows is None:
                    rows = run_tesseract_tsv(roi_img_path, lang="fas", psm=6)
                # Find tokens containing old_phrase
                matches: List[fitz.Rect] = []
                def norm(s: str) -> str:
                    if s is None:
                        return ""
                    mapping = {"\u064A": "\u06CC", "\u0643": "\u06A9", "\u0629": "\u0647", "\u0649": "\u06CC"}
                    out = []
                    for ch in s:
                        if 0x064B <= ord(ch) <= 0x065F or ord(ch) in (0x200C, 0x200D, 0x200E, 0x200F) or ord(ch) == 0x0640:
                            continue
                        out.append(mapping.get(ch, ch))
                    return "".join(out)
                needle = norm(old_phrase)
                for r in rows:
                    if r.get("level") != "5":
                        continue
                    t = norm((r.get("text") or "").strip())
                    if not t:
                        continue
                    if needle and needle in t:
                        l = int(r.get("left", 0))
                        ttop = int(r.get("top", 0))
                        w = int(r.get("width", 0))
                        h = int(r.get("height", 0))
                        rect_pdf = fitz.Rect(
                            roi_pdf.x0 + l / zoom,
                            roi_pdf.y0 + ttop / zoom,
                            roi_pdf.x0 + (l + w) / zoom,
                            roi_pdf.y0 + (ttop + h) / zoom,
                        )
                        matches.append(rect_pdf)
                if not matches:
                    continue

                # Prepare replacement rendering with local color
                color_hex = detect_text_color(page, matches[0]) or "#000000"
                tmp_png = output_pdf.parent / f"red_replace_{page_index:03d}.png"
                png_w, png_h = render_text_png(p, new_phrase, tmp_png, font_size_px=28, color_hex=color_hex)

                for mr in matches:
                    page.draw_rect(mr, fill=(1, 1, 1), color=(1, 1, 1))
                    target_h = mr.height
                    scale = target_h / png_h if png_h > 0 else 1.0
                    img_w = png_w * scale
                    img_h = png_h * scale
                    x_ins = mr.x0
                    y_ins = mr.y0
                    page.insert_image(
                        fitz.Rect(x_ins, y_ins, x_ins + img_w, y_ins + img_h),
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
        print("Usage: python replace_in_red_boxes.py <input.pdf> <output.pdf> <old> <new>", file=sys.stderr)
        sys.exit(2)
    input_pdf = Path(sys.argv[1])
    output_pdf = Path(sys.argv[2])
    old_phrase = sys.argv[3]
    new_phrase = sys.argv[4]
    pages, reps = replace_in_red_boxes(input_pdf, output_pdf, old_phrase, new_phrase)
    print(f"Pages touched: {pages}, replacements: {reps}")


if __name__ == "__main__":
    main()