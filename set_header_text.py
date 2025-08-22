import sys
from pathlib import Path
from typing import Tuple, Optional

import fitz  # PyMuPDF
from playwright.sync_api import sync_playwright

from replace_name_pdf import (
    render_text_png,
    detect_text_color,
)


def compute_header_rect(page: fitz.Page, top_fraction: float = 0.35) -> Optional[fitz.Rect]:
    try:
        info = page.get_text("dict")
    except Exception:
        return None
    page_h = page.rect.height
    header_spans = []
    for block in info.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                bbox = span.get("bbox")
                if not bbox:
                    continue
                srect = fitz.Rect(*bbox)
                if srect.y1 <= page_h * top_fraction:
                    header_spans.append(srect)
    if not header_spans:
        return None
    # Union of header spans
    x0 = min(r.x0 for r in header_spans)
    y0 = min(r.y0 for r in header_spans)
    x1 = max(r.x1 for r in header_spans)
    y1 = max(r.y1 for r in header_spans)
    return fitz.Rect(x0, y0, x1, y1)


def set_header_text(input_pdf: Path, output_pdf: Path, header_text: str) -> Tuple[int, int]:
    doc = fitz.open(str(input_pdf))
    pages_touched = 0

    with sync_playwright() as p:
        for page_index in range(len(doc)):
            page = doc[page_index]
            header_rect = compute_header_rect(page, top_fraction=0.35)
            if header_rect is None:
                # fallback: top band of the page
                page_rect = page.rect
                header_rect = fitz.Rect(
                    page_rect.x0 + page_rect.width * 0.1,
                    page_rect.y0 + page_rect.height * 0.04,
                    page_rect.x1 - page_rect.width * 0.1,
                    page_rect.y0 + page_rect.height * 0.12,
                )

            # Choose text color similar to existing text in header (fallback black)
            color_hex = detect_text_color(page, header_rect) or "#000000"

            # Render text once; we will scale to fit inside header rect with margins
            tmp_png = output_pdf.parent / f"header_text_{page_index:03d}.png"
            png_w, png_h = render_text_png(p, header_text, tmp_png, font_size_px=28, color_hex=color_hex)

            # Compute scale to fit inside header rect with margin
            margin_ratio = 0.9
            max_w = header_rect.width * margin_ratio
            max_h = header_rect.height * margin_ratio
            scale_w = max_w / png_w if png_w > 0 else 1.0
            scale_h = max_h / png_h if png_h > 0 else 1.0
            scale = min(scale_w, scale_h)
            img_w = png_w * scale
            img_h = png_h * scale

            # Clear previous content in header area (white background)
            page.draw_rect(header_rect, fill=(1, 1, 1), color=(1, 1, 1))

            # Center text inside header rect
            x0 = header_rect.x0 + (header_rect.width - img_w) / 2
            y0 = header_rect.y0 + (header_rect.height - img_h) / 2

            page.insert_image(
                fitz.Rect(x0, y0, x0 + img_w, y0 + img_h),
                filename=str(tmp_png),
                keep_proportion=True,
            )
            pages_touched += 1

    doc.save(str(output_pdf))
    doc.close()
    return pages_touched, pages_touched


def main():
    if len(sys.argv) < 4:
        print("Usage: python set_header_text.py <input.pdf> <output.pdf> <header_text>", file=sys.stderr)
        sys.exit(2)
    input_pdf = Path(sys.argv[1])
    output_pdf = Path(sys.argv[2])
    header_text = sys.argv[3]
    pages, _ = set_header_text(input_pdf, output_pdf, header_text)
    print(f"Pages updated: {pages}")


if __name__ == "__main__":
    main()

