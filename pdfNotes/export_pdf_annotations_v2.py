#!/usr/bin/env python3
"""
Export PDF annotations to Markdown (.md) with color-grouped sections and strict file naming.

Features:
- Highlights: extract underlying text, remove ALL whitespace, group by color categories
  (yellow=重点, red=问题, blue=方法, green=具体实验细节, purple=论文概括)
- Notes: export as callout blocks using Obsidian/Markdown syntax:
    > [!note] 批注
    > line1
    > line2
- Rectangles/Circles: crop region as images with strictly sanitized filenames; group by color

CLI:
  python export_pdf_annotations_v2.py input.pdf [--out output.md] [--img-dir images] [--min-dpi 144]

Requires:
  pip install PyMuPDF
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

try:
    import fitz  # PyMuPDF
except Exception as exc:  # pragma: no cover
    print("PyMuPDF is required. Install with: pip install PyMuPDF", file=sys.stderr)
    raise


# ----------------------------- Filename & Color ---------------------------- #


_FILENAME_ALLOWED_RE = re.compile(r"[^A-Za-z0-9_-]+")


def make_safe_filename(*parts: str) -> str:
    base = "_".join(parts)
    base = base.replace(" ", "")
    return _FILENAME_ALLOWED_RE.sub("", base)


def categorize_color(rgb: Tuple[int, int, int]) -> str:
    refs: Dict[str, Tuple[int, int, int]] = {
        "yellow": (255, 212, 0),
        "red": (230, 0, 0),
        "blue": (0, 112, 192),
        "green": (0, 176, 80),
        "purple": (128, 0, 128),
    }
    r, g, b = rgb
    best_key = "yellow"
    best_d = float("inf")
    for k, (rr, gg, bb) in refs.items():
        d = (r - rr) ** 2 + (g - gg) ** 2 + (b - bb) ** 2
        if d < best_d:
            best_d = d
            best_key = k
    return best_key


COLOR_TITLES: Dict[str, str] = {
    "yellow": "黄色（重点）",
    "red": "红色（问题）",
    "blue": "蓝色（方法）",
    "green": "绿色（具体实验细节）",
    "purple": "紫色（论文概括）",
}


# --------------------------------- Helpers -------------------------------- #


def rgb_from_annot(annot) -> Tuple[int, int, int]:
    try:
        color = annot.colors.get("stroke") or annot.colors.get("fill")
    except Exception:
        color = None
    if not color:
        return (255, 212, 0)
    r, g, b = color
    return (int(r * 255), int(g * 255), int(b * 255))


def normalize_highlight_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def extract_highlight_text(page: "fitz.Page", annot) -> str:
    try:
        quads = annot.vertices or []
    except Exception:
        quads = []
    texts: List[str] = []
    if quads:
        for i in range(0, len(quads), 4):
            rect = fitz.Quad(quads[i : i + 4]).rect
            texts.append(page.get_text("text", clip=rect).strip())
    else:
        texts.append(page.get_text("text", clip=annot.rect).strip())
    return " ".join([t for t in texts if t]).strip()


def save_rect_image(page: "fitz.Page", rect: "fitz.Rect", out_dir: str, base_name: str, dpi: int) -> Optional[str]:
    try:
        os.makedirs(out_dir, exist_ok=True)
        pix = page.get_pixmap(clip=rect, dpi=dpi)
        out_path = os.path.join(out_dir, f"{base_name}.png")
        pix.save(out_path)
        return out_path
    except Exception:
        return None


# ---------------------------------- Core ---------------------------------- #


def export_pdf_annotations(
    pdf_path: str,
    out_md: Optional[str] = None,
    img_dir: str = "images",
    min_dpi: int = 144,
) -> str:
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(f"File not found: {pdf_path}")

    if out_md is None:
        out_md = os.path.splitext(pdf_path)[0] + ".annotations.md"

    out_dir = os.path.dirname(out_md) or os.getcwd()
    image_dir = os.path.join(out_dir, img_dir)

    doc = fitz.open(pdf_path)

    # Grouping containers
    highlight_groups: Dict[str, List[str]] = defaultdict(list)  # color -> [text]
    notes_blocks: List[str] = []
    rect_groups: Dict[str, List[Dict[str, Optional[str]]]] = defaultdict(list)  # color -> list of {image_path, rect_text}

    for page_index in range(len(doc)):
        page = doc[page_index]
        annot = page.first_annot
        rect_index = 0
        while annot:
            subtype = (annot.type[0] if isinstance(annot.type, tuple) else annot.type) or ""
            color = rgb_from_annot(annot)
            try:
                if subtype == fitz.PDF_ANNOT_HIGHLIGHT:
                    txt = extract_highlight_text(page, annot)
                    txt = normalize_highlight_text(txt)
                    if txt:
                        highlight_groups[categorize_color(color)].append(txt)

                elif subtype in (fitz.PDF_ANNOT_TEXT, fitz.PDF_ANNOT_FREE_TEXT):
                    content = (annot.info or {}).get("content") or ""
                    content = "\n".join(ln.strip() for ln in content.splitlines() if ln.strip())
                    if content:
                        notes_blocks.append(content)

                elif subtype in (fitz.PDF_ANNOT_SQUARE, fitz.PDF_ANNOT_CIRCLE):
                    rect_index += 1
                    rect = annot.rect
                    clip_text = page.get_text("text", clip=rect).strip() or None
                    base_name = make_safe_filename(
                        os.path.splitext(os.path.basename(pdf_path))[0],
                        f"p{page_index+1}",
                        f"rect{rect_index}",
                    )
                    img_path = save_rect_image(page, rect, image_dir, base_name, dpi=min_dpi)
                    rect_groups[categorize_color(color)].append({"image_path": img_path, "rect_text": clip_text})

                elif subtype in (fitz.PDF_ANNOT_UNDERLINE, fitz.PDF_ANNOT_STRIKEOUT):
                    txt = extract_highlight_text(page, annot)
                    txt = normalize_highlight_text(txt)
                    if txt:
                        highlight_groups[categorize_color(color)].append(txt)
            except Exception:
                # skip broken annotation gracefully
                pass

            annot = annot.next

    doc.close()

    # Build Markdown
    lines: List[str] = []
    title = os.path.splitext(os.path.basename(pdf_path))[0]
    lines.append(f"# Annotations for {title}")
    lines.append("")

    order = ["yellow", "red", "blue", "green", "purple"]

    # Highlights section (content only; whitespace removed; grouped by color)
    if any(highlight_groups.get(k) for k in order):
        lines.append("## Highlights")
        lines.append("")
        for k in order:
            items = [t for t in highlight_groups.get(k, []) if t]
            if not items:
                continue
            lines.append(f"### {COLOR_TITLES[k]}")
            lines.append("")
            for txt in items:
                lines.append(f"- > {txt}")
            lines.append("")

    # Notes section as callouts
    if notes_blocks:
        lines.append("## Notes")
        lines.append("")
        for block in notes_blocks:
            lines.append("> [!note] 注释")
            for ln in block.splitlines():
                if ln.strip():
                    lines.append(f"> {ln.strip()}")
            lines.append("")

    # Rectangles / Figures (grouped by color)
    if any(rect_groups.get(k) for k in order):
        lines.append("## Rectangles / Figures")
        lines.append("")
        for k in order:
            items = rect_groups.get(k, [])
            if not items:
                continue
            lines.append(f"### {COLOR_TITLES[k]}")
            lines.append("")
            for it in items:
                if it.get("image_path"):
                    rel = it["image_path"].replace("\\", "/")
                    lines.append(f"![]({rel})")
                lines.append("")

    markdown = "\n".join(lines).rstrip() + "\n"

    os.makedirs(os.path.dirname(out_md) or ".", exist_ok=True)
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(markdown)

    return out_md


# ----------------------------------- CLI ---------------------------------- #


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Export PDF annotations to Markdown with color grouping.")
    parser.add_argument("--pdf", dest="pdf", default=r"D:\codes\tools\pdfNotes\Jingming - 基于PINN的复合材料自动铺放轨迹整体规划.pdf", help="Path to input PDF file")
    parser.add_argument("--out", dest="out", default=None, help="Output .md path (default: alongside PDF)")
    parser.add_argument(
        "--img-dir",
        dest="img_dir",
        default=r"D:\\jianguo\\我的坚果云\\obsidian\\Research\\00Inbox\\001Attachment\\PDFimgs",
        help="Directory for cropped images (absolute or relative to MD)",
    )
    parser.add_argument("--min-dpi", dest="min_dpi", type=int, default=144, help="DPI for cropped images")

    args = parser.parse_args(argv)
    pdf_path = os.path.abspath(args.pdf)
    out_md = os.path.abspath(args.out) if args.out else None

    try:
        result = export_pdf_annotations(pdf_path=pdf_path, out_md=out_md, img_dir=args.img_dir, min_dpi=args.min_dpi)
    except Exception as e:  # pragma: no cover
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Saved: {result}")
    if os.path.isdir(os.path.join(os.path.dirname(result) or os.getcwd(), args.img_dir)):
        print(f"Images: {os.path.join(os.path.dirname(result) or os.getcwd(), args.img_dir)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


