"""Безопасное построение PDF из уже проверенного Markdown-отчёта.

MathText понимает ограниченное подмножество LaTeX, но не исполняет TeX-команды.
Внешние изображения из Markdown намеренно не загружаются.
"""

from __future__ import annotations

import re
from html import escape
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from urllib.parse import urlparse

from markdown_it import MarkdownIt
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Image, KeepTogether, LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

MAX_REPORT_CHARS = 100_000
MAX_FORMULAS = 64
MAX_FORMULA_CHARS = 500
INLINE_MATH = re.compile(r"(?<!\\)(?<!\$)\$(?!\$)(.+?)(?<!\\)\$(?!\$)")
_FONT_READY = False
_FONT_LOCK = Lock()


def _fonts() -> None:
    global _FONT_READY
    if _FONT_READY:
        return
    with _FONT_LOCK:
        if _FONT_READY:
            return
        from matplotlib import get_data_path

        font_dir = Path(get_data_path()) / "fonts" / "ttf"
        for name, filename in (("ResearchSans", "DejaVuSans.ttf"),
                               ("ResearchSans-Bold", "DejaVuSans-Bold.ttf"),
                               ("ResearchSans-Oblique", "DejaVuSans-Oblique.ttf")):
            pdfmetrics.registerFont(TTFont(name, str(font_dir / filename)))
        pdfmetrics.registerFontFamily(
            "ResearchSans", normal="ResearchSans", bold="ResearchSans-Bold",
            italic="ResearchSans-Oblique", boldItalic="ResearchSans-Bold",
        )
        _FONT_READY = True


def _styles() -> dict[str, ParagraphStyle]:
    body = ParagraphStyle(
        "Body", fontName="ResearchSans", fontSize=9.5, leading=15,
        textColor=colors.HexColor("#172b3a"), spaceAfter=8,
    )
    return {
        "body": body,
        "title": ParagraphStyle("Title", parent=body, fontName="ResearchSans-Bold",
                                fontSize=17, leading=23, spaceAfter=15),
        "h1": ParagraphStyle("H1", parent=body, fontName="ResearchSans-Bold",
                             fontSize=14, leading=19, spaceBefore=15, spaceAfter=7),
        "h2": ParagraphStyle("H2", parent=body, fontName="ResearchSans-Bold",
                             fontSize=11.5, leading=17, spaceBefore=12, spaceAfter=6),
        "h3": ParagraphStyle("H3", parent=body, fontName="ResearchSans-Bold",
                             fontSize=10, leading=15, spaceBefore=10, spaceAfter=5),
        "cell": ParagraphStyle("Cell", parent=body, fontSize=8.5, leading=13,
                               spaceAfter=0, splitLongWords=True),
        "code": ParagraphStyle("Code", parent=body, backColor=colors.HexColor("#f3f6f8"),
                               leftIndent=12, rightIndent=12, spaceBefore=5),
        "math": ParagraphStyle("Math", parent=body, alignment=TA_CENTER, spaceBefore=8,
                               spaceAfter=12),
    }


def _math_png(formula: str) -> bytes | None:
    if len(formula) > MAX_FORMULA_CHARS or not formula.strip():
        return None
    output = BytesIO()
    try:
        from matplotlib import mathtext

        mathtext.math_to_image(f"${formula.strip()}$", output, dpi=180, format="png",
                               color="#172b3a")
    except (ValueError, RuntimeError, TypeError):
        return None
    return output.getvalue()


def _math_image(formula: str, max_width: float) -> Image | None:
    data = _math_png(formula)
    if data is None:
        return None
    width, height = PILImage.open(BytesIO(data)).size
    scale = min(0.7, max_width / width)
    result = Image(BytesIO(data), width=width * scale, height=height * scale)
    result.hAlign = "CENTER"
    return result


def _paragraph_markup(children: list, temp_dir: Path, formula_count: list[int], max_math_width: float = 190) -> str:
    parts: list[str] = []
    links: list[str] = []
    for token in children:
        if token.type == "text":
            cursor = 0
            for match in INLINE_MATH.finditer(token.content):
                parts.append(escape(token.content[cursor:match.start()]))
                if formula_count[0] >= MAX_FORMULAS:
                    parts.append(escape(match.group(0)))
                else:
                    data = _math_png(match.group(1))
                    if data is None:
                        parts.append(escape(match.group(0)))
                    else:
                        formula_count[0] += 1
                        path = temp_dir / f"formula-{formula_count[0]}.png"
                        path.write_bytes(data)
                        width, height = PILImage.open(BytesIO(data)).size
                        scale = min(0.35, max_math_width / width)
                        parts.append(
                            f'<img src="{path}" width="{width * scale:.1f}" '
                            f'height="{height * scale:.1f}" valign="middle"/>'
                        )
                cursor = match.end()
            parts.append(escape(token.content[cursor:]))
        elif token.type == "softbreak":
            parts.append(" ")
        elif token.type == "hardbreak":
            parts.append("<br/>")
        elif token.type == "strong_open":
            parts.append("<b>")
        elif token.type == "strong_close":
            parts.append("</b>")
        elif token.type == "em_open":
            parts.append("<i>")
        elif token.type == "em_close":
            parts.append("</i>")
        elif token.type == "code_inline":
            parts.append(f'<font color="#315b75">{escape(token.content)}</font>')
        elif token.type == "link_open":
            href = token.attrGet("href") or ""
            if urlparse(href).scheme in {"http", "https"}:
                parts.append(f'<link href="{escape(href, quote=True)}" color="#176a91">')
                links.append("</link>")
            else:
                parts.append('<font color="#176a91">')
                links.append("</font>")
        elif token.type == "link_close":
            parts.append(links.pop() if links else "")
        elif token.type == "image":
            parts.append(escape(token.content or "[изображение]"))
    return "".join(parts)


def _normal_blocks(markdown: str, temp_dir: Path, styles: dict, formula_count: list[int]) -> list:
    story: list = []
    heading: str | None = None
    list_depth = 0
    list_item = 0
    table_rows = None
    table_row = []
    tokens = MarkdownIt("default").parse(markdown)
    table_math_width = 190
    for index, token in enumerate(tokens):
        if token.type == "table_open":
            table_rows = []
            columns = 0
            for following in tokens[index + 1:]:
                if following.type == "th_open":
                    columns += 1
                if following.type == "tr_close":
                    break
            table_math_width = max(1, (A4[0] - 98) / max(1, columns) - 14)
        elif token.type == "tr_open":
            table_row = []
        elif token.type == "tr_close" and table_rows is not None:
            table_rows.append(table_row)
        elif token.type == "table_close" and table_rows:
            width = A4[0] - 98
            table = LongTable(table_rows, colWidths=[width / len(table_rows[0])] * len(table_rows[0]),
                              repeatRows=1, hAlign="LEFT", splitByRow=1, splitInRow=1,
                              spaceBefore=6, spaceAfter=12)
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8f0f4")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cddbe2")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            story.append(table)
            table_rows = None
        elif token.type == "heading_open":
            heading = token.tag if token.tag in {"h1", "h2", "h3"} else "h3"
        elif token.type == "heading_close":
            heading = None
        elif token.type in {"bullet_list_open", "ordered_list_open"}:
            list_depth += 1
            list_item = 0
        elif token.type in {"bullet_list_close", "ordered_list_close"}:
            list_depth = max(0, list_depth - 1)
        elif token.type == "list_item_open":
            list_item += 1
        elif token.type == "inline":
            markup = _paragraph_markup(token.children or [], temp_dir, formula_count,
                                       table_math_width if table_rows is not None else 190)
            if table_rows is not None:
                if not table_rows:
                    markup = f"<b>{markup}</b>"
                table_row.append(Paragraph(markup, styles["cell"]))
            elif markup:
                prefix = "• " if list_depth else ""
                story.append(Paragraph(prefix + markup, styles[heading or "body"]))
        elif token.type in {"fence", "code_block"}:
            for line in token.content.rstrip().splitlines()[:100]:
                story.append(Paragraph(escape(line).replace(" ", "&nbsp;"), styles["code"]))
        elif token.type == "hr":
            story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    return story


def _blocks(markdown: str, temp_dir: Path, styles: dict) -> list:
    story: list = []
    normal: list[str] = []
    formula_count = [0]
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if line.startswith("$$"):
            closing_line = next((position for position in range(index + 1, len(lines))
                                 if "$$" in lines[position]), None)
            if not (line[2:].endswith("$$") and line[2:]) and closing_line is None:
                normal.append(lines[index])
                index += 1
                continue
            if normal:
                story.extend(_normal_blocks("\n".join(normal), temp_dir, styles, formula_count))
                normal.clear()
            expression = line[2:]
            if expression.endswith("$$") and expression:
                expression = expression[:-2]
            else:
                index += 1
                parts = [expression] if expression else []
                while index < len(lines) and "$$" not in lines[index]:
                    parts.append(lines[index])
                    index += 1
                if index < len(lines):
                    parts.append(lines[index].split("$$", 1)[0])
                expression = "\n".join(parts).strip()
            if formula_count[0] < MAX_FORMULAS:
                image = _math_image(expression, 480)
                if image is not None:
                    formula_count[0] += 1
                    story.append(KeepTogether([Spacer(1, 5), image, Spacer(1, 8)]))
                else:
                    story.append(Paragraph(escape(f"$$ {expression} $$"), styles["math"]))
            else:
                story.append(Paragraph(escape(f"$$ {expression} $$"), styles["math"]))
        else:
            normal.append(lines[index])
        index += 1
    if normal:
        story.extend(_normal_blocks("\n".join(normal), temp_dir, styles, formula_count))
    return story


def render_report_pdf(title: str, markdown: str) -> bytes:
    """Собрать PDF без обращения к сети или исполнения TeX из отчёта."""
    if len(markdown) > MAX_REPORT_CHARS:
        raise ValueError("Отчёт слишком велик для PDF-экспорта")
    _fonts()
    buffer = BytesIO()
    styles = _styles()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=49, leftMargin=49, topMargin=58,
        bottomMargin=55, title=title[:200], author="Платформа Deep Research",
    )

    def footer(canvas, doc) -> None:
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#dae2e8"))
        canvas.line(49, 44, A4[0] - 49, 44)
        canvas.setFont("ResearchSans", 8)
        canvas.setFillColor(colors.HexColor("#607988"))
        canvas.drawString(49, 31, "Deep Research · отчёт")
        canvas.drawRightString(A4[0] - 49, 31, str(doc.page))
        canvas.restoreState()

    with TemporaryDirectory(prefix="research-pdf-") as directory:
        story = [Paragraph(escape(title), styles["title"])]
        story.extend(_blocks(markdown, Path(directory), styles))
        document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
