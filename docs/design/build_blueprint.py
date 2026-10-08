#!/usr/bin/env python3
"""Product blueprint PDF from the research mock screens."""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable,
    Image,
    KeepTogether,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

HERE = Path(__file__).resolve().parent
FIG = HERE / "figures"
OUT = HERE / "Control-Machine-blueprint.pdf"

AUTHOR = "Shivank.sharma@wipro.com"
ORG = "AICouncilSwitzerland"
FOOTER = "AI-Council Switzerland"
ACCENT = colors.HexColor("#6D4DFF")
DARK = colors.HexColor("#0F172A")
MUTED = colors.HexColor("#475569")
LINE = colors.HexColor("#E2E8F0")

SECTIONS = [
    "Telegram and the Live Desktop",
    "Product Overview",
    "Target Audience",
    "Agent Factory",
    "Provisioning a Worker",
    "Worker Configuration",
    "Memory and Activity",
    "Skills",
    "Controlled Pause",
    "Assigned Computer",
]


class BlueprintDoc(SimpleDocTemplate):
    def __init__(self, *args, section_pages, **kwargs):
        super().__init__(*args, **kwargs)
        self.section_pages = section_pages

    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name == "SectionHeading":
            self.section_pages.setdefault(flowable.getPlainText(), self.page)


def styles():
    base = getSampleStyleSheet()
    base.add(ParagraphStyle(
        "CoverTitle", parent=base["Title"], fontName="Helvetica-Bold",
        fontSize=28, leading=34, textColor=DARK, alignment=TA_CENTER, spaceAfter=10,
    ))
    base.add(ParagraphStyle(
        "CoverSubtitle", parent=base["Normal"], fontName="Helvetica",
        fontSize=14, leading=20, textColor=MUTED, alignment=TA_CENTER, spaceAfter=6,
    ))
    base.add(ParagraphStyle(
        "AuthorLine", parent=base["Normal"], fontName="Helvetica",
        fontSize=12, leading=16, textColor=DARK, alignment=TA_CENTER, spaceBefore=12,
    ))
    base.add(ParagraphStyle(
        "SectionHeading", parent=base["Heading1"], fontName="Helvetica-Bold",
        fontSize=16, leading=20, textColor=ACCENT, spaceBefore=0, spaceAfter=6,
    ))
    base.add(ParagraphStyle(
        "Body", parent=base["Normal"], fontName="Helvetica",
        fontSize=11, leading=16.5, textColor=DARK, spaceAfter=8,
    ))
    base.add(ParagraphStyle(
        "Lead", parent=base["Normal"], fontName="Helvetica",
        fontSize=11, leading=16.5, textColor=DARK, spaceBefore=2, spaceAfter=6,
    ))
    base.add(ParagraphStyle(
        "Caption", parent=base["Normal"], fontName="Helvetica-Oblique",
        fontSize=9, leading=12, textColor=MUTED, alignment=TA_CENTER,
        spaceBefore=3, spaceAfter=8,
    ))
    base.add(ParagraphStyle(
        "IndexIntro", parent=base["Normal"], fontName="Helvetica",
        fontSize=11, leading=16.5, textColor=DARK, spaceAfter=12,
    ))
    base.add(ParagraphStyle(
        "IndexTitle", parent=base["Normal"], fontName="Helvetica",
        fontSize=12, leading=15, textColor=DARK,
    ))
    base.add(ParagraphStyle(
        "IndexPage", parent=base["Normal"], fontName="Helvetica",
        fontSize=12, leading=15, textColor=ACCENT, alignment=TA_RIGHT,
    ))
    base.add(ParagraphStyle(
        "IndexBlurb", parent=base["Normal"], fontName="Helvetica",
        fontSize=9.5, leading=13, textColor=MUTED, spaceBefore=1, spaceAfter=0,
    ))
    base.add(ParagraphStyle(
        "IndexNum", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=11, leading=14, textColor=ACCENT, alignment=TA_CENTER,
    ))
    base.add(ParagraphStyle(
        "IndexGroup", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=9, leading=12, textColor=ACCENT, spaceBefore=8, spaceAfter=4,
    ))
    base.add(ParagraphStyle(
        "OrgLine", parent=base["Normal"], fontName="Helvetica",
        fontSize=11, leading=14, textColor=MUTED, alignment=TA_CENTER, spaceBefore=2,
    ))
    base.add(ParagraphStyle(
        "StepHead", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=11, leading=15, textColor=DARK, spaceBefore=6, spaceAfter=2,
    ))
    base.add(ParagraphStyle(
        "Cell", parent=base["Normal"], fontName="Helvetica",
        fontSize=10, leading=13, textColor=DARK,
    ))
    base.add(ParagraphStyle(
        "CellHead", parent=base["Normal"], fontName="Helvetica-Bold",
        fontSize=10, leading=13, textColor=DARK,
    ))
    return base


def fit_image(path: Path, max_w: float, max_h: float) -> Image:
    from PIL import Image as PILImage

    with PILImage.open(path) as im:
        w, h = im.size
    scale = min(max_w / w, max_h / h)
    image = Image(str(path), width=w * scale, height=h * scale)
    image.hAlign = "CENTER"
    return image


def figure(path: Path, lead: str, caption: str, width: float, max_h: float, s) -> KeepTogether:
    return KeepTogether([
        Paragraph(lead, s["Lead"]),
        fit_image(path, width, max_h),
        Paragraph(caption, s["Caption"]),
    ])


def bullets(items: list[str], s) -> ListFlowable:
    return ListFlowable(
        [ListItem(Paragraph(item, s["Body"]), leftIndent=12) for item in items],
        bulletType="bullet",
        bulletColor=ACCENT,
        bulletFontSize=8,
        leftIndent=16,
        spaceBefore=2,
        spaceAfter=6,
    )


def index_row(number: str, heading: str, blurb: str, page: str, width: float, s):
    num = Paragraph(number, s["IndexNum"])
    title = Paragraph(heading, s["IndexTitle"])
    note = Paragraph(blurb, s["IndexBlurb"])
    text = [title, note]
    pg = Paragraph(page, s["IndexPage"])
    row = Table(
        [[num, text, pg]],
        colWidths=[1.1 * cm, width - 3.1 * cm, 2.0 * cm],
    )
    row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F7F5FF")),
        ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#EDE9FE")),
        ("LEFTPADDING", (0, 0), (0, 0), 4),
        ("RIGHTPADDING", (0, 0), (0, 0), 4),
        ("LEFTPADDING", (1, 0), (1, 0), 10),
        ("RIGHTPADDING", (1, 0), (1, 0), 8),
        ("LEFTPADDING", (2, 0), (2, 0), 4),
        ("RIGHTPADDING", (2, 0), (2, 0), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBEFORE", (0, 0), (0, 0), 3, ACCENT),
        ("ALIGN", (2, 0), (2, 0), "RIGHT"),
    ]))
    return [row, Spacer(1, 0.18 * cm)]


BLURBS = {
    "Telegram and the Live Desktop": "The worker takes the request in Telegram and shows the computer on the phone when a step requires you.",
    "Product Overview": "One computer is given to one agent. The day’s work is delegated to that agent.",
    "Target Audience": "Anyone whose work can be written as instructions: video, slides, code, or any other sequence of steps.",
    "Agent Factory": "The catalog where each worker is listed, opened, and assigned to its computer.",
    "Provisioning a Worker": "Create the record, connect the bot, and the worker is ready for the first message.",
    "Worker Configuration": "The prompt, the path of a job, memory, connections, and the tools it may use.",
    "Memory and Activity": "What the worker retains, and the record of what it said, tried, and decided.",
    "Skills": "Written routines the worker follows, including the steps it must return to you.",
    "Controlled Pause": "A sign-in or a submission waits. You answer in the same chat.",
    "Assigned Computer": "The machine assigned to the worker: browser, clicks, and the outbound connector.",
}

INDEX_GROUPS = [
    ("How the work is done", ["Telegram and the Live Desktop", "Product Overview", "Target Audience"]),
    ("Agent Factory", ["Agent Factory", "Provisioning a Worker", "Worker Configuration"]),
    ("From request to completion", ["Memory and Activity", "Skills", "Controlled Pause", "Assigned Computer"]),
]


def story(s, width: float, labels: dict[str, str]):
    flow = [
        Spacer(1, 3.8 * cm),
        Paragraph("Agent Factory", s["CoverTitle"]),
        Paragraph("Product Blueprint", s["CoverSubtitle"]),
        Paragraph("One digital worker for one computer. The request arrives in Telegram.", s["CoverSubtitle"]),
        Spacer(1, 0.2 * cm),
        HRFlowable(width="40%", thickness=2, color=ACCENT, hAlign="CENTER", spaceBefore=8, spaceAfter=8),
        Paragraph(f"Author<br/>{AUTHOR}", s["AuthorLine"]),
        Paragraph(ORG, s["OrgLine"]),
        PageBreak(),
        Paragraph("Index", s["SectionHeading"]),
        HRFlowable(width="100%", thickness=0.6, color=LINE, spaceBefore=0, spaceAfter=8),
        Paragraph(
            "Begin with the conversation and the phone. That is how the worker is used. "
            "The example throughout is Office, filing this week’s timesheet and stopped at the sign-in.",
            s["IndexIntro"],
        ),
    ]
    n = 1
    for group, headings in INDEX_GROUPS:
        flow.append(Paragraph(group.upper(), s["IndexGroup"]))
        for heading in headings:
            flow.extend(index_row(f"{n:02d}", heading, BLURBS[heading], labels.get(heading, ""), width, s))
            n += 1

    flow.append(PageBreak())
    flow.append(Paragraph("Telegram and the Live Desktop", s["SectionHeading"]))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=LINE, spaceBefore=0, spaceAfter=8))
    flow.append(Paragraph(
        "The worker takes its instructions in Telegram. You ask it to file this week’s timesheet. "
        "It replies in the same chat. When the page requires a sign-in, it sends Open desktop. "
        "The phone shows the computer. You complete the sign-in and confirm that you are past "
        "the page. The worker then continues the same job.",
        s["Body"],
    ))
    gap = 0.45 * cm
    col = (width - gap) / 2
    left = [
        fit_image(FIG / "chat.png", col, 12.4 * cm),
        Paragraph("The timesheet conversation. The pause offers Open desktop. The reply resumes the same job.", s["Caption"]),
    ]
    right = [
        fit_image(FIG / "desktop.png", col, 12.4 * cm),
        Paragraph("The assigned computer, shown on the phone. Tap to click. Type below. Keystrokes are not stored.", s["Caption"]),
    ]
    pair = Table([[left, "", right]], colWidths=[col, gap, col])
    pair.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    flow.append(pair)
    flow.append(Paragraph(
        "The display is a sequence of JPEG frames on a signed connection, a few frames each second. "
        "Taps and keystrokes return on that connection and are applied on the computer. The chat "
        "remains the place where the job resumes. The live desktop is the step you take yourself. "
        "The reply in Telegram is the instruction that lets the worker proceed.",
        s["Body"],
    ))

    flow.append(PageBreak())
    flow.append(Paragraph("Product Overview", s["SectionHeading"]))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=LINE, spaceBefore=0, spaceAfter=10))
    flow.append(Paragraph(
        "Give an agent a computer. Then hand it the day.",
        s["Body"],
    ))
    flow.append(Paragraph(
        "Agent Factory gives one computer to one agent. The timesheet, the inbox, the form, the "
        "file — the work that used to wait at the desk — you delegate in Telegram. The agent "
        "takes that work and does it on the computer you gave it. You are no longer the person "
        "at the keyboard. The agent is.",
        s["Body"],
    ))
    flow.append(Paragraph(
        "You decide what is delegated. The agent decides how to carry it out, and it can hand a "
        "step to the browser, to a click on the machine, or later to SAP. When a step must stay "
        "yours — a password, a code, a submission — the agent stops and waits in the same chat.",
        s["Body"],
    ))
    flow.append(Paragraph(
        "Office is that agent at work. It has been given a computer and a timesheet. It is paused "
        "on a MyWipro sign-in, because the password is not its step to take.",
        s["Body"],
    ))
    flow.append(bullets([
        "One worker, one computer, one Telegram chat.",
        "The worker acts in the browser and on the desktop, and reports in the same chat.",
        "The Agent Factory retains the worker, its memory, and its skills while the computer is off.",
        "A sign-in or a submission waits for you. The live desktop shows that computer on the phone.",
        "The worker reads page text and screenshots. Passwords, the machine credential, bot tokens, and keystrokes from the phone are not stored.",
    ], s))
    flow.append(Paragraph(
        "A job records the request, one session on that computer, and the events along the way. "
        "When a step must wait, it records a pause. Your reply in the chat closes the pause, and "
        "the same session continues. The live desktop records nothing.",
        s["Body"],
    ))

    flow.append(PageBreak())
    flow.append(Paragraph("Target Audience", s["SectionHeading"]))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=LINE, spaceBefore=0, spaceAfter=10))
    flow.append(Paragraph(
        "The audience is everyone. If the work can be written as a set of instructions, it can be "
        "delegated to an agent that has been given a computer.",
        s["Body"],
    ))
    flow.append(bullets([
        "Someone who edits video, and can state the cut, the export, and where the finished file should land.",
        "Someone who builds a PowerPoint, and can state the slides, the source material, and when the deck is ready to send.",
        "Someone who writes code, and can state the change, the checks, and the point at which the agent must stop before it writes or runs.",
        "Someone whose day is any other sequence of steps: a form, a folder, a browser, or an application already on that computer.",
        "A team in which each person gives one agent one computer, and sends the instructions in Telegram.",
    ], s))
    flow.append(Paragraph(
        "The profession does not matter. The instructions do. A timesheet, a slide deck, a code "
        "change, and a video export are the same kind of job once they can be described. You send "
        "the description. The agent carries it out on the computer you gave it.",
        s["Body"],
    ))
    flow.append(Paragraph(
        "What stays with you is the step that cannot be written down. A password. A final approval. "
        "A judgment you have not yet made. The agent stops there, shows you the computer, and waits "
        "for the reply in the same chat.",
        s["Body"],
    ))
    flow.append(Paragraph(
        "You do not need a new tool for each kind of work. You need a job you can describe. Telegram "
        "carries the instructions. The computer carries them out.",
        s["Body"],
    ))

    flow.append(PageBreak())
    flow.append(Paragraph("Agent Factory", s["SectionHeading"]))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=LINE, spaceBefore=0, spaceAfter=8))
    flow.append(figure(
        FIG / "factory.png",
        "The Agent Factory lists the workers. Each card is one worker, assigned to its own computer, with its own Telegram chat.",
        "Office is on a timesheet. Sales and Coder are idle. Open a card to configure that worker.",
        width, 8.6 * cm, s,
    ))
    flow.append(Paragraph(
        "Each card shows a name, a one-line role, a status, and a tag. Opening a worker loads its "
        "prompt, its memory, its skills, and the computer it acts on. Creating a worker asks for a "
        "name and a role, adds the card, and opens it. The indicator at the bottom is that "
        "worker’s computer. Online means its connector was seen recently.",
        s["Body"],
    ))

    flow.append(PageBreak())
    flow.append(Paragraph("Provisioning a Worker", s["SectionHeading"]))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=LINE, spaceBefore=0, spaceAfter=10))
    flow.append(Paragraph(
        "A new card starts a worker. The Agent Factory saves the record. You then connect its "
        "Telegram bot and assign it to a computer. After four steps, it can accept a job.",
        s["Body"],
    ))
    steps = [
        ("1. The record",
         "The Agent Factory saves the name, the role, and an empty prompt, and enables Browser and Machine clicks. SAP stays disabled. You can open the worker and edit it. Its bot has no chat yet."),
        ("2. Connect the bot",
         "You create a bot in BotFather and provide the token once. The Agent Factory starts a listener for that token and keeps the token outside the tables. The first /start in that chat marks the bot as connected. That chat is how you address this worker."),
        ("3. Ready on the first message",
         "The first message builds the worker from its prompt, skills, memory, and tools. If any of those change, the next message uses the new instructions. The chat belongs only to this worker."),
        ("4. One worker, one computer",
        "The worker is assigned to one computer. For now that computer is a Mac. In the enterprise case it is a VM, at Daytona, on Azure, or with another provider. The provider names where the VM runs. The connector dials out from that machine either way. If the computer is off, the worker still answers in chat and declines machine work until the connector returns."),
    ]
    for title, body in steps:
        flow.append(Paragraph(title, s["StepHead"]))
        flow.append(Paragraph(body, s["Body"]))
    flow.append(Paragraph(
        "The prompt, the skills, the memory, the tools, and the Telegram chat belong to that "
        "worker. The computer it acts on is the one you assigned. You address only that bot. "
        "Another worker means another chat and another computer, created in the same way.",
        s["Body"],
    ))
    flow.append(Paragraph(
        "The result is a worker you can message from any location. It files, clicks, and replies "
        "on the assigned computer, and it stops when the next step requires you.",
        s["Body"],
    ))

    flow.append(PageBreak())
    flow.append(Paragraph("Worker Configuration", s["SectionHeading"]))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=LINE, spaceBefore=0, spaceAfter=8))
    flow.append(figure(
        FIG / "builder.png",
        "Opening Office shows the worker. Save stores the prompt. Test opens its Telegram chat. Open desktop shows its computer.",
        "Office, paused on this week’s timesheet. Every message returns to the same chat.",
        width, 11.0 * cm, s,
    ))
    flow.append(Paragraph(
        "The status bar names the job in progress. The path is a Telegram message, then the worker, "
        "then memory, the skill in use, and a pause when the worker must ask, then a reply in the "
        "same chat. Below that, memory can be enabled or disabled. Connections show Telegram and "
        "this computer. The tools are Browser, machine clicks, and SAP.",
        s["Body"],
    ))

    flow.append(PageBreak())
    flow.append(Paragraph("Memory and Activity", s["SectionHeading"]))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=LINE, spaceBefore=0, spaceAfter=6))
    flow.append(figure(
        FIG / "memory.png",
        "Memory is what this worker is permitted to retain. You can read each note and delete it. The notes stay with this worker.",
        "Office retains the timesheet practice, the rule to stop before submission, last week’s project, and the fact that its computer dials out.",
        width, 7.4 * cm, s,
    ))
    flow.append(figure(
        FIG / "activity.png",
        "Activity is the record of the open job: what the worker said, what it tried, and each decision.",
        "The timesheet job, newest decision first. The sign-in page is why the job is paused.",
        width, 6.6 * cm, s,
    ))

    flow.append(PageBreak())
    flow.append(Paragraph("Skills", s["SectionHeading"]))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=LINE, spaceBefore=0, spaceAfter=8))
    flow.append(figure(
        FIG / "skills.png",
        "A skill is a routine written for this worker. The routine it is following is marked In use.",
        "The Timesheet skill. Sign-in and submission are returned to you. A new skill has a name, the condition for use, and the instructions.",
        width, 12.2 * cm, s,
    ))
    flow.append(Paragraph(
        "The first message loads these instructions. If a skill changes, the next message follows "
        "the revised routine. Sign-in and submission remain steps the worker returns to you.",
        s["Body"],
    ))

    flow.append(PageBreak())
    flow.append(Paragraph("Controlled Pause", s["SectionHeading"]))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=LINE, spaceBefore=0, spaceAfter=8))
    flow.append(figure(
        FIG / "pause.png",
        "A pause means the worker is waiting. The screen states the next actions and the actions it will not take. You release it by replying in Telegram.",
        "Office is waiting on the MyWipro sign-in. Open the chat is the reply.",
        width, 9.2 * cm, s,
    ))
    flow.append(Paragraph(
        "You sign in on the live desktop, then confirm in the same chat that you are past the page. "
        "That reply allows the worker to continue. If the computer sleeps, the pause remains until "
        "its connector dials out again. The worker will not type the password, start a second job, "
        "or continue on the machine while the computer is off.",
        s["Body"],
    ))

    flow.append(PageBreak())
    flow.append(Paragraph("Assigned Computer", s["SectionHeading"]))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=LINE, spaceBefore=0, spaceAfter=8))
    flow.append(figure(
        FIG / "machine.png",
        "This is the computer assigned to the worker. The connector has dialed out. The worker is viewing the sign-in page and has not typed.",
        "The browser is in use. Machine clicks are available and unused. SAP follows on the same job if the browser cannot operate the screen.",
        width, 11.2 * cm, s,
    ))
    flow.append(Paragraph(
        "The credential that allows the connector to dial out remains on that computer. The browser "
        "is how the worker reads the page and acts. Clicks are how it operates a native application. "
        "SAP is the same job, used when the browser and the clicks cannot reach the screen. One "
        "worker. One computer. One Telegram chat. For now the computer is a Mac. Enterprise "
        "assigns a VM, at Daytona, on Azure, or with another provider, and the connector "
        "dials out from that VM.",
        s["Body"],
    ))
    return flow


def draw_footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.4)
    canvas.line(1.8 * cm, 1.45 * cm, A4[0] - 1.8 * cm, 1.45 * cm)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(1.8 * cm, 0.95 * cm, FOOTER)
    canvas.drawRightString(A4[0] - 1.8 * cm, 0.95 * cm, f"Page {doc.page}")
    canvas.restoreState()


def build_once(labels: dict[str, str], collected: dict[str, int]) -> int:
    s = styles()
    doc = BlueprintDoc(
        str(OUT),
        pagesize=A4,
        leftMargin=1.7 * cm,
        rightMargin=1.7 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.8 * cm,
        title="Agent Factory — Product blueprint",
        author=AUTHOR,
        subject="One digital worker assigned to one computer",
        section_pages=collected,
    )
    doc.build(story(s, doc.width, labels), onFirstPage=draw_footer, onLaterPages=draw_footer)
    return doc.page


def main() -> None:
    shown: dict[str, str] = {}
    for _ in range(4):
        collected: dict[str, int] = {}
        build_once(shown, collected)
        updated = {heading: str(collected[heading]) for heading in SECTIONS if heading in collected}
        if updated == shown and len(updated) == len(SECTIONS):
            break
        shown = updated
    print(f"Wrote {OUT}")
    for heading in SECTIONS:
        print(f"  {shown.get(heading, '?'):>4}  {heading}")


if __name__ == "__main__":
    main()
