#!/usr/bin/env python3
"""Build the product overview with an index, author, footer, and screenshots."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

SKILL_BUILD = Path(
    "/Users/shivanksharma/.cursor/skills/product-overview-pdf/scripts/build_pdf.py"
)
HERE = Path(__file__).resolve().parent

spec = importlib.util.spec_from_file_location("overview_build", SKILL_BUILD)
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)

FIGURES = [
    (
        HERE / "ControlScreen.PNG",
        "Telegram. Ask the agents for the work, then open the live desktop when they need you.",
    ),
    (
        HERE / "TelegramScreen.PNG",
        "The live link. The phone shows the computer, and the keyboard types into it.",
    ),
]
AUTHOR = "Shivank.sharma@wipro.com"
FOOTER = "AI-Council Switzerland"


class OverviewDoc(SimpleDocTemplate):
    def __init__(self, *args, section_pages, **kwargs):
        super().__init__(*args, **kwargs)
        self.section_pages = section_pages

    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name == "SectionHeading":
            self.section_pages.setdefault(flowable.getPlainText(), self.page)


def extra_styles(styles):
    styles.add(ParagraphStyle(
        "AuthorLine", parent=styles["Normal"], fontSize=12, leading=16,
        textColor=build.DARK, alignment=TA_CENTER, spaceBefore=14,
    ))
    styles.add(ParagraphStyle(
        "IndexIntro", parent=styles["BodyTextX"], fontSize=11, leading=16,
        spaceAfter=12,
    ))
    styles.add(ParagraphStyle(
        "IndexTitle", parent=styles["Normal"], fontSize=12, leading=15,
        textColor=build.DARK, spaceBefore=0, spaceAfter=0,
    ))
    styles.add(ParagraphStyle(
        "IndexPage", parent=styles["Normal"], fontSize=12, leading=15,
        textColor=build.ACCENT, alignment=TA_RIGHT,
    ))
    styles.add(ParagraphStyle(
        "IndexBlurb", parent=styles["Normal"], fontSize=10, leading=14,
        textColor=build.MUTED, leftIndent=8, spaceBefore=1, spaceAfter=8,
    ))
    return styles


def format_labels(collected, total, order):
    labels = {}
    for index, heading in enumerate(order):
        start = collected.get(heading)
        if start is None:
            labels[heading] = ""
            continue
        if index + 1 < len(order) and order[index + 1] in collected:
            end = collected[order[index + 1]] - 1
        else:
            end = total
        if end <= start:
            labels[heading] = str(start)
        else:
            labels[heading] = f"{start}–{end}"
    return labels


def screenshot_pair(width, styles):
    gap = 0.5 * cm
    col_w = (width - gap) / 2
    cells = []
    for path, caption in FIGURES:
        image = build.scaled_image(str(path), col_w, max_height=17.2 * cm)
        cells.append([
            image,
            Spacer(1, 0.15 * cm),
            Paragraph(caption, styles["Caption"]),
        ])
    table = Table(
        [[cells[0], "", cells[1]]],
        colWidths=[col_w, gap, col_w],
    )
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def make_story(content, styles, diagram, width, labels):
    sections = content["sections"]
    story = [
        Spacer(1, 4.2 * cm),
        Paragraph(content["title"], styles["CoverTitle"]),
        Paragraph(content["subtitle"], styles["CoverSubtitle"]),
        Spacer(1, 0.4 * cm),
        HRFlowable(
            width="40%", thickness=2, color=build.ACCENT,
            hAlign="CENTER", spaceBefore=8, spaceAfter=8,
        ),
        Paragraph(f"Author<br/>{content['author']}", styles["AuthorLine"]),
        PageBreak(),
        Paragraph("Index", styles["SectionHeading"]),
        HRFlowable(
            width="100%", thickness=0.6, color=colors.HexColor("#E2E8F0"),
            spaceBefore=0, spaceAfter=10,
        ),
        Paragraph(content["index_intro"], styles["IndexIntro"]),
    ]
    for section in sections:
        heading = section["heading"]
        page = labels.get(heading, "")
        title = Paragraph(heading, styles["IndexTitle"])
        number = Paragraph(page, styles["IndexPage"])
        row = Table([[title, number]], colWidths=[width - 2.2 * cm, 2.2 * cm])
        row.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#E2E8F0")),
        ]))
        story.append(row)
        story.append(Paragraph(section["index"], styles["IndexBlurb"]))

    for section in sections:
        heading = section["heading"]
        story.append(PageBreak())
        story.append(Paragraph(heading, styles["SectionHeading"]))
        story.append(HRFlowable(
            width="100%", thickness=0.6, color=colors.HexColor("#E2E8F0"),
            spaceBefore=0, spaceAfter=10,
        ))
        story.extend(build.body_to_flowables(section["body"], styles, diagram, width))
        if heading.strip().lower() == build.DIAGRAM_SECTION:
            lead = Paragraph(
                "The supervisor agent delegates automation to the browser agent "
                "and to the file and desktop agents. The live link returns you to the screen.",
                styles["BodyTextX"],
            )
            figure = KeepTogether([
                lead,
                build.scaled_image(str(diagram), width, max_height=10.5 * cm),
                Paragraph("How the agents automate this computer", styles["Caption"]),
            ])
            story.append(figure)
            story.append(PageBreak())
            story.append(Paragraph(
                "You meet the agents in two places. Telegram is where you ask. "
                "The live link is where you see the computer and finish a step yourself.",
                styles["BodyTextX"],
            ))
            story.append(screenshot_pair(width, styles))
    return story


def draw_footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#E2E8F0"))
    canvas.setLineWidth(0.4)
    canvas.line(2 * cm, 1.55 * cm, A4[0] - 2 * cm, 1.55 * cm)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(build.MUTED)
    canvas.drawString(2 * cm, 1.05 * cm, FOOTER)
    canvas.drawRightString(A4[0] - 2 * cm, 1.05 * cm, f"Page {doc.page}")
    canvas.restoreState()


def build_once(path, content, styles, diagram, labels, collected):
    doc = OverviewDoc(
        path,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=1.8 * cm,
        bottomMargin=2 * cm,
        title=content["title"],
        author=content["author"],
        subject="Agentic automation for your own computer",
        section_pages=collected,
    )
    story = make_story(content, styles, diagram, doc.width, labels)
    doc.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return doc.page


def main() -> None:
    content = json.loads((HERE / "content.json").read_text())
    content["author"] = AUTHOR
    content["footer"] = FOOTER
    diagram = HERE / "architecture.png"
    styles = extra_styles(build.build_styles())
    order = [section["heading"] for section in content["sections"]]
    output = str(HERE / "product-overview.pdf")
    shown = {}
    for _ in range(4):
        collected = {}
        total = build_once(output, content, styles, diagram, shown, collected)
        updated = format_labels(collected, total, order)
        if updated == shown and all(updated.values()):
            break
        shown = updated
    print(f"Wrote {output}")
    for heading in order:
        print(f"  {shown.get(heading, '?'):>6}  {heading}")


if __name__ == "__main__":
    sys.exit(main())
