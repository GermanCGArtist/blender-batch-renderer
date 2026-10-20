"""Builds docs/Blender_Batch_Renderer_Documentation.pdf  (python3 docs/make_docs.py)"""
import os
import time

from reportlab.graphics.shapes import Drawing, Rect, String, Line, Polygon
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer, Table, TableStyle,
                                PageBreak, Image, KeepTogether, NextPageTemplate, Preformatted, Flowable)
from reportlab.platypus.tableofcontents import TableOfContents

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "Blender_Batch_Renderer_Documentation.pdf")
ICON = os.path.join(ROOT, "bbr", "assets", "icon.png")
VERSION = "0.6.0"

F = "/usr/share/fonts/truetype/dejavu/"
G = "/usr/share/fonts/truetype/google-fonts/"
pdfmetrics.registerFont(TTFont("Body", F + "DejaVuSans.ttf"))
pdfmetrics.registerFont(TTFont("Body-Bold", F + "DejaVuSans-Bold.ttf"))
pdfmetrics.registerFont(TTFont("Body-Italic", F + "DejaVuSans-Oblique.ttf"))
pdfmetrics.registerFont(TTFont("Body-BoldItalic", F + "DejaVuSans-BoldOblique.ttf"))
pdfmetrics.registerFont(TTFont("Mono", F + "DejaVuSansMono.ttf"))
pdfmetrics.registerFont(TTFont("Head", G + "Poppins-SemiBold.ttf" if os.path.exists(G + "Poppins-SemiBold.ttf")
                               else G + "Poppins-Bold.ttf"))
pdfmetrics.registerFont(TTFont("Head-Light", G + "Poppins-Light.ttf"))
from reportlab.pdfbase.pdfmetrics import registerFontFamily  # noqa: E402
registerFontFamily("Body", normal="Body", bold="Body-Bold", italic="Body-Italic", boldItalic="Body-BoldItalic")

NAVY = colors.HexColor("#1c2340")
NAVY2 = colors.HexColor("#2b3765")
ORANGE = colors.HexColor("#f07a4a")
PINK = colors.HexColor("#e9466f")
INK = colors.HexColor("#1f2430")
MUTED = colors.HexColor("#5b6275")
LINE = colors.HexColor("#d9dce5")
TINT = colors.HexColor("#f4f5f9")
NOTE = colors.HexColor("#fff4ec")

ss = {}
ss["body"] = ParagraphStyle("body", fontName="Body", fontSize=9.6, leading=14.2, textColor=INK, spaceAfter=6)
ss["small"] = ParagraphStyle("small", parent=ss["body"], fontSize=8.4, leading=11.6, textColor=MUTED, spaceAfter=0)
ss["cell"] = ParagraphStyle("cell", parent=ss["body"], fontSize=8.6, leading=11.8, spaceAfter=0)
ss["cellb"] = ParagraphStyle("cellb", parent=ss["cell"], fontName="Body-Bold")
ss["cellh"] = ParagraphStyle("cellh", parent=ss["cell"], fontName="Body-Bold", textColor=colors.white)
ss["h1"] = ParagraphStyle("h1", fontName="Head", fontSize=19, leading=24, textColor=NAVY, spaceBefore=10,
                          spaceAfter=10, keepWithNext=1)
ss["h2"] = ParagraphStyle("h2", fontName="Head", fontSize=12.5, leading=17, textColor=NAVY, spaceBefore=12,
                          spaceAfter=5, keepWithNext=1)
ss["h3"] = ParagraphStyle("h3", fontName="Body-Bold", fontSize=9.8, leading=14, textColor=INK, spaceBefore=6,
                          spaceAfter=3, keepWithNext=1)
ss["bullet"] = ParagraphStyle("bullet", parent=ss["body"], leftIndent=13, bulletIndent=2, spaceAfter=3)
ss["code"] = ParagraphStyle("code", fontName="Mono", fontSize=8.1, leading=11.2, textColor=INK)
ss["note"] = ParagraphStyle("note", parent=ss["body"], fontSize=9, leading=13.2, spaceAfter=0)
ss["toc1"] = ParagraphStyle("toc1", fontName="Body", fontSize=10.5, leading=19, textColor=INK, leftIndent=0)
ss["toc2"] = ParagraphStyle("toc2", fontName="Body", fontSize=9, leading=14, textColor=MUTED, leftIndent=16)


# ---------------------------------------------------------------- helpers
def P(text, style="body"):
    return Paragraph(text, ss[style])


def H1(text):
    p = Paragraph(text, ss["h1"])
    p._toc = (0, text)
    return p


def H2(text):
    p = Paragraph(text, ss["h2"])
    p._toc = (1, text)
    return p


def H3(text):
    return Paragraph(text, ss["h3"])


def bullets(items):
    return [Paragraph(t, ss["bullet"], bulletText="•") for t in items]


def code(text):
    t = Table([[Preformatted(text.strip("\n"), ss["code"])]], colWidths=[170 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), TINT), ("BOX", (0, 0), (-1, -1), 0.5, LINE),
                           ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                           ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return t


def note(text, title="Note", color=ORANGE, bg=NOTE):
    t = Table([[Paragraph(f"<b>{title}.</b> {text}", ss["note"])]], colWidths=[170 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("LINEBEFORE", (0, 0), (0, -1), 3, color),
                           ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                           ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    return KeepTogether([Spacer(1, 3), t, Spacer(1, 8)])


def table(rows, widths, header=True, bold_first=True):
    data = []
    for i, r in enumerate(rows):
        if header and i == 0:
            data.append([Paragraph(c, ss["cellh"]) for c in r])
        else:
            data.append([Paragraph(c, ss["cellb" if (j == 0 and bold_first) else "cell"]) for j, c in enumerate(r)])
    t = Table(data, colWidths=[w * mm for w in widths], repeatRows=1 if header else 0)
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
          ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
          ("TOPPADDING", (0, 0), (-1, -1), 4.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5)]
    if header:
        st += [("BACKGROUND", (0, 0), (-1, 0), NAVY)]
        for i in range(1, len(data)):
            if i % 2 == 0:
                st.append(("BACKGROUND", (0, i), (-1, i), TINT))
    t.setStyle(TableStyle(st))
    t.spaceAfter = 9
    return t


# ---------------------------------------------------------------- diagrams
def box(d, x, y, w, h, title, sub="", fill=TINT, stroke=NAVY2, tcolor=NAVY, size=8.6):
    d.add(Rect(x, y, w, h, rx=5, ry=5, fillColor=fill, strokeColor=stroke, strokeWidth=0.9))
    d.add(String(x + w / 2, y + h - 13, title, fontName="Body-Bold", fontSize=size, fillColor=tcolor,
                 textAnchor="middle"))
    for i, line in enumerate(sub.split("\n") if sub else []):
        d.add(String(x + w / 2, y + h - 25 - i * 10, line, fontName="Body", fontSize=7.2, fillColor=MUTED,
                     textAnchor="middle"))


def arrow(d, x1, y1, x2, y2, label="", color=MUTED, lx=0, ly=4, anchor="middle"):
    d.add(Line(x1, y1, x2, y2, strokeColor=color, strokeWidth=1))
    import math
    a = math.atan2(y2 - y1, x2 - x1)
    s = 5
    d.add(Polygon([x2, y2, x2 - s * math.cos(a - 0.4), y2 - s * math.sin(a - 0.4),
                   x2 - s * math.cos(a + 0.4), y2 - s * math.sin(a + 0.4)], fillColor=color, strokeColor=color))
    if label:
        d.add(String((x1 + x2) / 2 + lx, (y1 + y2) / 2 + ly, label, fontName="Body", fontSize=6.8,
                     fillColor=MUTED, textAnchor=anchor))


def window_diagram():
    W, H = 482, 300
    d = Drawing(W, H)
    d.add(Rect(0, 0, W, H, rx=8, ry=8, fillColor=colors.white, strokeColor=NAVY2, strokeWidth=1.2))
    d.add(Rect(0, H - 22, W, 22, rx=8, ry=8, fillColor=NAVY, strokeColor=NAVY))
    d.add(Rect(0, H - 22, W, 10, fillColor=NAVY, strokeColor=NAVY))
    for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        d.add(Rect(10 + i * 13, H - 15, 8, 8, rx=4, ry=4, fillColor=colors.HexColor(c), strokeColor=None))
    d.add(String(W / 2, H - 15, "Blender Batch Renderer", fontName="Body", fontSize=7.5, fillColor=colors.white,
                 textAnchor="middle"))
    # toolbar
    tb = ["Add .blend…", "New folder…", "Remove", "▲", "▼", "▶ Start", "Stop after frame", "■ Stop now",
          "Cancel job", "Job ▾", "Settings…"]
    x = 8
    for t in tb:
        w = 6 + len(t) * 4.2
        d.add(Rect(x, H - 40, w, 13, rx=3, ry=3, fillColor=TINT, strokeColor=LINE))
        d.add(String(x + w / 2, H - 36, t, fontName="Body", fontSize=6, fillColor=INK, textAnchor="middle"))
        x += w + 3
    # batch bar
    d.add(String(8, H - 55, "● rendering · 2/5 jobs done · remaining ≈ 3h 12m (done ≈ 02:40)", fontName="Body",
                 fontSize=6.4, fillColor=MUTED))
    d.add(Rect(W - 128, H - 58, 120, 8, fillColor=TINT, strokeColor=LINE))
    d.add(Rect(W - 128, H - 58, 70, 8, fillColor=ORANGE, strokeColor=None))
    # queue tree
    qx, qy, qw, qh = 8, 104, 190, 128
    d.add(Rect(qx, qy, qw, qh, fillColor=colors.white, strokeColor=LINE))
    rows = [("☑ shot_010", "rendering", 0.6, False), ("☑ Project X (3)", "1/3 done", 0.4, True),
            ("   ☑ shot_020", "queued", 0, False), ("   ☐ shot_030", "skipped", 0, False),
            ("☑ Lookdev (1)", "", 0, True), ("   ☑ turntable", "on hold", 0, False)]
    for i, (n, st, pr, b) in enumerate(rows):
        yy = qy + qh - 16 - i * 17
        d.add(String(qx + 5, yy, n, fontName="Body-Bold" if b else "Body", fontSize=6.6, fillColor=INK))
        d.add(String(qx + 92, yy, st, fontName="Body", fontSize=6.2, fillColor=MUTED))
        d.add(Rect(qx + 140, yy - 1, 44, 6, fillColor=TINT, strokeColor=LINE, strokeWidth=0.3))
        if pr:
            d.add(Rect(qx + 140, yy - 1, 44 * pr, 6, fillColor=ORANGE, strokeColor=None))
    # right tabs
    rx_, ry_, rw, rh = 204, 104, 270, 128
    d.add(Rect(rx_, ry_, rw, rh, fillColor=colors.white, strokeColor=LINE))
    for i, t in enumerate(("Live", "Job settings", "Issues")):
        d.add(Rect(rx_ + 4 + i * 62, ry_ + rh - 14, 58, 11, fillColor=TINT if i else NAVY2, strokeColor=LINE))
        d.add(String(rx_ + 33 + i * 62, ry_ + rh - 11, t, fontName="Body", fontSize=5.8,
                     fillColor=colors.white if i == 0 else INK, textAnchor="middle"))
    for i, t in enumerate(("Current frame", "Job", "Last finished frame")):
        bx = rx_ + 4 + i * 89
        d.add(Rect(bx, ry_ + 52, 85, 56, fillColor=TINT, strokeColor=LINE))
        d.add(String(bx + 4, ry_ + 100, t, fontName="Body-Bold", fontSize=5.8, fillColor=INK))
    d.add(Rect(rx_ + 4, ry_ + 4, 129, 44, fillColor=TINT, strokeColor=LINE))
    d.add(Rect(rx_ + 137, ry_ + 4, 129, 44, fillColor=TINT, strokeColor=LINE))
    d.add(String(rx_ + 8, ry_ + 40, "Render time per frame", fontName="Body-Bold", fontSize=5.6, fillColor=INK))
    d.add(String(rx_ + 141, ry_ + 40, "Memory per frame", fontName="Body-Bold", fontSize=5.6, fillColor=INK))
    import math
    for k in range(2):
        pts = []
        for j in range(12):
            pts += [rx_ + 10 + k * 133 + j * 10, ry_ + 14 + 12 * (0.5 + 0.4 * math.sin(j * 0.8 + k))]
        for j in range(0, len(pts) - 2, 2):
            d.add(Line(pts[j], pts[j + 1], pts[j + 2], pts[j + 3], strokeColor=colors.HexColor("#3d8bfd"),
                       strokeWidth=1))
    # console
    d.add(Rect(8, 8, W - 16, 90, fillColor=colors.white, strokeColor=LINE))
    d.add(Rect(12, 84, 70, 11, fillColor=NAVY2, strokeColor=LINE))
    d.add(String(47, 87, "Console", fontName="Body", fontSize=5.8, fillColor=colors.white,
                 textAnchor="middle"))
    d.add(Rect(84, 84, 50, 11, fillColor=TINT, strokeColor=LINE))
    d.add(String(109, 87, "Messages", fontName="Body", fontSize=5.8, fillColor=INK, textAnchor="middle"))
    lines = ["BBR_FRAME_START {\"frame\": 41}", "BBR_STATS {\"s\": \"Frame:41 | Time:00:03.10 | Remaining:00:03.6 | Sample 7/16\"}",
             "BBR_FRAME_DONE {\"frame\": 41, \"render_s\": 6.71, ...}", "BBR_FRAME_START {\"frame\": 42}"]
    for i, l in enumerate(lines):
        d.add(String(14, 72 - i * 11, l, fontName="Mono", fontSize=5.8, fillColor=MUTED))
    # callouts
    labels = [(1, H - 33, "A"), (W - 140, H - 54, "B"), (qx + 2, qy + qh + 4, "C"), (rx_ + 2, ry_ + rh + 4, "D"),
              (4, 100, "E")]
    for x, y, t in labels:
        d.add(Rect(x - 6, y - 4, 13, 13, rx=6.5, ry=6.5, fillColor=ORANGE, strokeColor=colors.white))
        d.add(String(x + 0.5, y, t, fontName="Body-Bold", fontSize=7, fillColor=colors.white, textAnchor="middle"))
    return d


def architecture_diagram():
    W, H = 482, 236
    d = Drawing(W, H)
    xs, ws = [6, 182, 358], [128, 128, 118]
    box(d, xs[0], 150, ws[0], 70, "Window (Qt)", "queue tree, live view,\ncharts, console,\nsettings dialogs")
    box(d, xs[1], 150, ws[1], 70, "Runner (thread)", "picks the next job,\nstarts / stops Blender,\nretries, safety checks")
    box(d, xs[2], 150, ws[2], 70, "Blender (background)", "runs bbr_render.py,\nrenders frame by frame,\nsaves via temp + rename",
        fill=NOTE, stroke=ORANGE)
    for i, (top, bottom) in enumerate((("commands", "events"), ("starts / kills", "BBR_ lines"))):
        x1 = xs[i] + ws[i]
        x2 = xs[i + 1]
        cx = (x1 + x2) / 2
        arrow(d, x1 + 2, 192, x2 - 2, 192)
        arrow(d, x2 - 2, 178, x1 + 2, 178)
        d.add(String(cx, 198, top, fontName="Body", fontSize=6.2, fillColor=MUTED, textAnchor="middle"))
        d.add(String(cx, 167, bottom, fontName="Body", fontSize=6.2, fillColor=MUTED, textAnchor="middle"))
    box(d, xs[0], 20, ws[0], 92, "App data folder", "settings.json\n(Blender installs, defaults)\nqueue.json\n(folders + job order)\napp.lock")
    box(d, xs[1], 20, ws[1], 92, "Job folder", "job.json (full job state)\ndata/frames.csv\ndata/attempts.csv\ndata/report.xlsx\nlogs/, logs/crashes/")
    box(d, xs[2], 20, ws[2], 92, "Job folder: output", "frames/<name>_0001.png …\nframes/<node name>/\npreview/<name>.mp4\n(preview made by a\nsecond Blender run)")
    arrow(d, xs[0] + 64, 150, xs[0] + 64, 112, "reads / writes", lx=4, anchor="start")
    arrow(d, xs[1] + 64, 150, xs[1] + 64, 112, "saves after every frame", lx=4, anchor="start")
    arrow(d, xs[2] + 59, 150, xs[2] + 59, 112, "writes frames", lx=4, anchor="start")
    return d


def network_diagram():
    W, H = 482, 214
    d = Drawing(W, H)
    box(d, 160, 112, 162, 88, "Coordinator", "the queue, the job folders,\nframes end up here\nhands out frames (leases)\nmay render too",
        fill=NOTE, stroke=ORANGE)
    box(d, 6, 8, 140, 74, "Worker: PC (LAN)", "Windows, NVIDIA GPU\nBlender 5.2 + add-ons\ncache of job copies")
    box(d, 336, 8, 140, 74, "Worker: MacBook (Wi-Fi)", "macOS, Apple GPU\nBlender 5.2 + add-ons\ncache of job copies")
    for x1, x2, side in ((76, 190, "start"), (406, 292, "end")):
        arrow(d, x1 + (8 if side == "start" else -8), 82, x2, 112)
        arrow(d, x2 + (14 if side == "start" else -14), 112, x1 + (22 if side == "start" else -22), 82)
    d.add(String(96, 108, "hello, lease, heartbeat,", fontName="Body", fontSize=6.6, fillColor=MUTED, textAnchor="middle"))
    d.add(String(96, 99, "finished frames ↑", fontName="Body", fontSize=6.6, fillColor=MUTED, textAnchor="middle"))
    d.add(String(386, 108, "job copy (zip, once) ↓", fontName="Body", fontSize=6.6, fillColor=MUTED, textAnchor="middle"))
    d.add(String(386, 99, "next frame, stop signals ↓", fontName="Body", fontSize=6.6, fillColor=MUTED, textAnchor="middle"))
    d.add(String(241, 50, "home network, HTTP on port 8642", fontName="Body-Bold", fontSize=7.2, fillColor=NAVY,
                 textAnchor="middle"))
    d.add(String(241, 39, "discovery: UDP 8643 · paired once with a 6-digit code", fontName="Body", fontSize=6.8,
                 fillColor=MUTED, textAnchor="middle"))
    return d


def lifecycle_diagram():
    W, H = 482, 150
    d = Drawing(W, H)
    fills = {"done": "#e7f6ec", "done (with failed frames)": "#fff6dd", "failed": "#fde8ea", "cancelled": "#eeeeee",
             "on hold": "#f5ecfb", "rendering": "#e7f0ff", "stopped": "#fff4ec", "queued": "#f4f5f9"}

    def node(name, x, y, w=96):
        d.add(Rect(x, y, w, 20, rx=5, ry=5, fillColor=colors.HexColor(fills[name]), strokeColor=NAVY2,
                   strokeWidth=0.8))
        d.add(String(x + w / 2, y + 7, name, fontName="Body-Bold", fontSize=6.8, fillColor=INK, textAnchor="middle"))
        return x, y, w

    q = node("queued", 10, 70, 70)
    r = node("rendering", 130, 70, 80)
    st = node("stopped", 130, 8, 80)
    ends = ["done", "done (with failed frames)", "failed", "cancelled", "on hold"]
    ex = 250
    arrow(d, q[0] + q[2], 80, r[0], 80, "Start", ly=4)
    arrow(d, 162, 70, 162, 28)
    arrow(d, 178, 28, 178, 70)
    d.add(String(120, 47, "Stop / Quit / disk full", fontName="Body", fontSize=6.2, fillColor=MUTED,
                 textAnchor="end"))
    d.add(String(186, 47, "Start", fontName="Body", fontSize=6.2, fillColor=MUTED))
    reasons = ["all frames finished", "some frames gave up", "cannot be rendered", "Cancel job",
               "file changed / missing"]
    for i, (e, why) in enumerate(zip(ends, reasons)):
        y = 128 - i * 29
        node(e, ex, y, 118)
        arrow(d, r[0] + r[2], 80, ex, y + 10)
        d.add(String(ex + 124, y + 7, why, fontName="Body", fontSize=6.2, fillColor=MUTED))
    d.add(String(10, 130, "Retry failed frames / Continue with", fontName="Body", fontSize=6.2, fillColor=MUTED))
    d.add(String(10, 121, "changed file / Start job over → queued", fontName="Body", fontSize=6.2,
                 fillColor=MUTED))
    return d


# ---------------------------------------------------------------- doc template
class Doc(BaseDocTemplate):
    def __init__(self, fn):
        super().__init__(fn, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=22 * mm,
                         bottomMargin=20 * mm, title="Blender Batch Renderer – Documentation",
                         author="Blender Batch Renderer", subject="User guide and technical reference", creator="make_docs.py")
        fr = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id="f")
        self.addPageTemplates([PageTemplate("cover", [fr], onPage=self.cover),
                               PageTemplate("main", [fr], onPage=self.decor)])

    def afterFlowable(self, f):
        t = getattr(f, "_toc", None)
        if t:
            level, text = t
            key = f"h{id(f)}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=level, closed=False)
            self.notify("TOCEntry", (level, text, self.page, key))

    def cover(self, c, doc):
        w, h = A4
        c.saveState()
        c.setFillColor(NAVY)
        c.rect(0, 0, w, h, stroke=0, fill=1)
        c.setFillColor(NAVY2)
        c.rect(0, 0, w, h * 0.36, stroke=0, fill=1)
        c.drawImage(ICON, w / 2 - 55 * mm / 2, h - 118 * mm, 55 * mm, 55 * mm, mask="auto")
        c.setFillColor(colors.white)
        c.setFont("Head", 30)
        c.drawCentredString(w / 2, h - 140 * mm, "Blender Batch Renderer")
        c.setFont("Head-Light", 14)
        c.setFillColor(colors.HexColor("#c9cfe6"))
        c.drawCentredString(w / 2, h - 151 * mm, "User guide & technical reference")
        c.setFillColor(ORANGE)
        c.rect(w / 2 - 15 * mm, h - 160 * mm, 30 * mm, 1.2 * mm, stroke=0, fill=1)
        c.setFont("Body", 9.5)
        c.setFillColor(colors.HexColor("#c9cfe6"))
        c.drawCentredString(w / 2, 60 * mm, f"Version {VERSION}  ·  {time.strftime('%B %Y')}")
        c.drawCentredString(w / 2, 53 * mm, "Queue, render, stop and resume Blender jobs – overnight, unattended.")
        c.restoreState()

    def decor(self, c, doc):
        w, h = A4
        c.saveState()
        c.setStrokeColor(LINE)
        c.setLineWidth(0.5)
        c.line(20 * mm, h - 14 * mm, w - 20 * mm, h - 14 * mm)
        c.setFont("Body", 7.5)
        c.setFillColor(MUTED)
        c.drawString(20 * mm, h - 11.5 * mm, "Blender Batch Renderer")
        c.drawRightString(w - 20 * mm, h - 11.5 * mm, f"Documentation · v{VERSION}")
        c.drawCentredString(w / 2, 11 * mm, str(doc.page))
        c.setFillColor(ORANGE)
        c.rect(20 * mm, h - 14.3 * mm, 12 * mm, 0.9 * mm, stroke=0, fill=1)
        c.restoreState()


class Fig(Flowable):
    def __init__(self, drawing, caption):
        super().__init__()
        self.d, self.caption = drawing, caption

    def wrap(self, aw, ah):
        self.cap = Paragraph(self.caption, ParagraphStyle("cap", parent=ss["small"], alignment=TA_CENTER))
        cw, chh = self.cap.wrap(aw, ah)
        self.ch = chh
        return aw, self.d.height + chh + 22

    def draw(self):
        from reportlab.graphics import renderPDF
        aw = self._frame._aW if hasattr(self, "_frame") else 170 * mm
        x = (170 * mm - self.d.width) / 2
        renderPDF.draw(self.d, self.canv, x, self.ch + 18)
        self.cap.drawOn(self.canv, 0, 10)


# ---------------------------------------------------------------- content
def build():
    s = []
    s.append(NextPageTemplate("main"))
    s.append(PageBreak())

    toc = TableOfContents()
    toc.levelStyles = [ss["toc1"], ss["toc2"]]
    toc.dotsMinLevel = 0
    s += [Paragraph("Contents", ss["h1"]), Spacer(1, 4), toc, PageBreak()]

    # 1 ----------------------------------------------------------------
    s.append(H1("1  Overview"))
    s.append(P("Blender Batch Renderer is a desktop app that renders a queue of <b>.blend</b> files one after "
               "another – typically overnight, with nobody watching. Its central promise: <b>you can stop, cancel or "
               "quit at any moment, and the batch later continues from the last finished frame</b>. Nothing that "
               "was finished is rendered twice, and nothing half-written is ever mistaken for a finished frame."))
    s.append(P("The app runs Blender in the background (no Blender window), one job at a time, and watches it: "
               "it shows live progress, estimates when the batch will finish and how much disk space it will "
               "use, retries frames when Blender crashes, records statistics for every frame, and turns each "
               "finished job into a quick MP4 preview."))
    s.append(H2("1.1  Key features"))
    s += bullets([
        "<b>Resume anywhere</b> – progress is saved after every frame; stop, quit or crash and press Start again.",
        "<b>Several Blender versions in one batch</b> – each file is matched to the install it was saved with.",
        "<b>Folders</b> to organise the queue, with drag &amp; drop and per-folder on/off switches.",
        "<b>Retries</b> per frame with an optional waiting time, a frame timeout and optional regular Blender restarts.",
        "<b>Live view</b>: current frame and samples, Blender's own remaining-time estimate, ETA for job and batch, "
        "projected disk usage, memory, thumbnail of the last frame, charts and Blender's console.",
        "<b>Data per job</b>: CSV files written live, an Excel report with charts, full logs and crash reports.",
        "<b>MP4 preview</b> made automatically when a job finishes (or on demand).",
        "<b>Safety checks</b>: disk space, .blend changed or missing mid-job, moved render folders, single instance, "
        "keeping the computer awake.",
        "<b>Optional preflight check</b> for missing textures, missing linked files, unbaked simulations and more.",
        "<b>Queue files</b> – save a queue as a <font face='Mono'>.bbrqueue</font> file and switch between setups; "
        "double-click a queue file to open it.",
        "<b>Status page in any browser</b> (also on your phone) and a <b>web API</b> for other tools.",
        "<b>Blender add-on</b> “Send to Batch Renderer” – add the open file to the queue from inside Blender.",
        "<b>Rendering on several computers</b> – frames of an animation are shared with worker computers on the "
        "home network (Windows, macOS, Linux; any GPU), with statistics per machine and combined.",
        "<b>Keyboard shortcuts</b> for everything you do often.",
        "Runs on macOS (tested), Windows and Linux; also usable from the command line.",
    ])
    s.append(H2("1.2  Guiding principles"))
    s += bullets([
        "<b>The .blend file decides.</b> Render settings are taken from the file as saved. The app only changes "
        "what it must (output path; video output becomes an image sequence) and the few parameters you "
        "explicitly override (frame range, samples, resolution %).",
        "<b>The frame is the unit of work.</b> A frame is either finished and safely on disk, or it will be rendered "
        "(again). Rendering inside a frame cannot be resumed – this is a Blender limitation.",
        "<b>Unattended first.</b> Problems never stop the night unless they must: failing frames are skipped after "
        "their attempts, broken or changed files are set aside, and the batch carries on with the next job.",
    ])

    # 2 ----------------------------------------------------------------
    s.append(PageBreak())
    s.append(H1("2  Installation &amp; starting"))
    s.append(H2("2.1  Requirements"))
    s += bullets([
        "Python 3.9 or newer (on the development Mac: Python 3.13 from python.org).",
        "One or more Blender installs (tested with Blender 5.2; written for 2.9x – 5.x).",
        "About 0.5 GB for the Python environment while building; the finished app is roughly 80–120 MB.",
    ])
    s.append(H2("2.2  One-time setup"))
    s.append(table([
        ["System", "Run", "What it does"],
        ["macOS", "double-click <font face='Mono'>setup.command</font>",
         "Creates a fresh private Python environment in <font face='Mono'>.venv</font> and installs the packages. "
         "If macOS refuses to open it: right-click → Open."],
        ["Windows", "<font face='Mono'>setup.bat</font>", "Same, using the Python launcher."],
        ["Linux", "<font face='Mono'>./setup.sh</font>", "Same."],
    ], [24, 50, 96]))
    s.append(P("Installed packages: <b>PySide6-Essentials</b> (the window), <b>openpyxl</b> (Excel report), "
               "<b>psutil</b> (memory readings) and <b>zstandard</b> (reading the version of compressed .blend files)."))
    s.append(H2("2.3  Starting the app"))
    s.append(H3("Option A – a real macOS app (recommended)"))
    s.append(P("Double-click <font face='Mono'>build_app.command</font>. It builds <b>Blender Batch Renderer.app</b> "
               "next to the script, with its own icon and name in the Dock; drag it to Applications if you like. "
               "You only need to build again when an update asks for it (2.4)."))
    s.append(P("<b>Windows:</b> run <font face='Mono'>build_windows.bat</font> once. It builds the folder "
               "<b>Blender Batch Renderer</b> with <font face='Mono'>Blender Batch Renderer.exe</font> inside, "
               "with the same self-test."))
    s.append(P("The build leaves out the parts of Qt the app never uses (QML/Quick, PDF, networking, OpenGL, SVG, "
               "translations – see <font face='Mono'>bbr.spec</font>) and then starts the new app once in the "
               "background as a self-test: it draws the charts, loads a thumbnail and writes a test report. If "
               "that test fails, the script automatically rebuilds with all of Qt included and says so."))
    s.append(H3("Option B – run from the source folder"))
    s.append(P("Double-click <font face='Mono'>run.command</font> (Windows <font face='Mono'>run.bat</font>, Linux "
               "<font face='Mono'>./run.sh</font>). A Terminal window opens alongside the app; keep it open."))
    s.append(note("Once the app is built, the <font face='Mono'>.venv</font> folder is only needed for "
                  "Option B and for rebuilding. You can delete it to save space; <font face='Mono'>setup.command</font> "
                  "recreates it.", "Disk space"))
    s.append(H2("2.4  Updates without rebuilding"))
    s.append(P("The built app consists of a <b>shell</b> – Python, Qt and the libraries, about 95 % of its size – and "
               "the <b>app code</b>. <i>Help → Check for updates…</i> asks GitHub for a newer version and installs "
               "only the app code (1–2 MB) into the app data folder (<font face='Mono'>app/</font>, see 10.3), next "
               "to the current version. The shell stays as it is, so the app is not rebuilt. Nothing is checked or "
               "installed unless you ask, and nothing while something renders."))
    s.append(table([
        ["Channel", "Offers"],
        ["Stable", "Versions that were tried and approved (the <font face='Mono'>release</font> branch)."],
        ["Alpha", "Every change as soon as it is made (the <font face='Mono'>main</font> branch) – experimental, "
                  "marked “alpha”."],
    ], [30, 140]))
    s += bullets([
        "Every file of an update is checked against the checksums GitHub keeps for that version before it is "
        "installed; a damaged download is refused.",
        "<i>Use previous version</i> (same window) switches back. A version that does not start is replaced by the "
        "previous one automatically, with a message.",
        "A version that needs a newer shell (new Python, Qt or library) is not installed; the window says to build "
        "the app again (<font face='Mono'>build_app.command</font> / <font face='Mono'>build_windows.bat</font>).",
        "Coordinator and workers must run the same version – update both computers.",
        "When running from source (<font face='Mono'>run.command</font>), updates are not used; use "
        "<font face='Mono'>git pull</font> instead.",
    ])
    s.append(H2("2.5  First launch"))
    s.append(P("On first launch the app searches for Blender installs (macOS: <font face='Mono'>/Applications</font> "
               "and <font face='Mono'>~/Applications</font>; Windows: Program Files and Steam; Linux: PATH, "
               "<font face='Mono'>/opt</font>, home folder) and asks each one for its version. Add or remove installs "
               "any time under <b>Settings…</b>. Only one copy of the app can run at a time; a second copy refuses "
               "to start instead of rendering the same queue twice."))

    # 3 ----------------------------------------------------------------
    s.append(PageBreak())
    s.append(H1("3  The window"))
    s.append(Fig(window_diagram(), "Figure 1 – Layout of the main window (schematic)."))
    s.append(table([
        ["", "Area", "Contents"],
        ["A", "Toolbar &amp; menus", "Add files, folders, ordering, Start / Stop / Cancel, the <b>Job ▾</b> menu and "
              "Settings. The menu bar has File (queue files, adding, folders, settings), Queue, Job and Help (status "
              "page, keyboard shortcuts, documentation); most commands are also in right-click menus."],
        ["B", "Batch summary", "State (idle / rendering / stopping), jobs done, remaining time for the whole batch "
              "with the expected finishing time, whether the computer is being kept awake, and a batch progress bar."],
        ["C", "Queue", "Jobs and folders in render order, each with status, progress, frames, Blender version and "
              "ETA. Checkboxes switch jobs or whole folders on/off. Drag to reorder or move between folders; drop "
              ".blend files to add them."],
        ["D", "Detail tabs", "<b>Live</b>, <b>Job settings</b> and <b>Issues</b> for the selected job."],
        ["E", "Console &amp; messages", "Blender's console output (follows the rendering job, or shows the selected "
              "one) and a log of the app's own messages, warnings and errors."],
    ], [8, 34, 128], bold_first=True))
    s.append(P("<b>Tip:</b> hover the mouse over any setting, button, column header or label to see what it does."))
    s.append(H2("3.1  Live tab"))
    s += bullets([
        "<b>Current frame</b> – frame number, sample progress (e.g. <i>sample 64/128</i>), elapsed time, "
        "<b>Blender's own remaining-time estimate</b> for the frame, and memory (now / Blender's peak).",
        "<b>Job</b> – frames done / failed / to go, average, fastest and slowest frame, time remaining, "
        "finishing time, disk used and projected total, total render time, number of Blender runs, the Blender "
        "install used and why, engine, device, samples and resolution.",
        "<b>Last finished frame</b> – a thumbnail (PNG, JPG, TIFF, WebP; not EXR).",
        "<b>Charts</b> – render time per frame, and memory per frame (Blender's reported peak and the measured "
        "peak of the Blender process).",
    ])
    s.append(H2("3.2  Job settings tab"))
    s.append(P("Shows and edits the selected job's settings (see chapter 9). Changes apply the next time the job "
               "renders; finished frames are kept. Settings cannot be changed while the job is rendering."))
    s.append(H2("3.3  Issues tab"))
    s.append(P("Everything that went wrong or needs attention for this job: notes from Blender (e.g. video output "
               "switched to PNG), the overrides applied, preflight results, every frame that failed or is waiting "
               "for a retry with its last error, and a list of all Blender runs with start time, duration, frames "
               "rendered and result."))
    s.append(H2("3.4  Machines tab and mode switch"))
    s.append(P("The <b>Machines</b> tab lists the computers that render with this one, with their state and "
               "statistics, and switches sharing on or off (chapter 15). The <b>Mode</b> box at the top right of the "
               "window turns this computer into a <b>worker</b>: the queue is hidden and the window shows the worker "
               "view instead (15.1). Both are locked while something is rendering."))

    # 4 ----------------------------------------------------------------
    s.append(H1("4  Working with the queue"))
    s.append(H2("4.1  Adding files"))
    s.append(P("Use <b>Add .blend…</b> (⌘I), or drop .blend files onto the queue – dropped onto a folder, they go "
               "into that folder. A dialog asks for:"))
    s += bullets([
        "<b>Queue folder</b> – where the jobs appear in the queue (or top level).",
        "<b>Render folder</b> – by default a folder called <i>render</i> <b>next to each .blend file</b> "
        "(<font face='Mono'>shots/shot_010.blend → shots/render/shot_010/</font>), so the output is always easy to find "
        "from the file. Alternatively a <b>custom folder</b>; each file then gets its own sub-folder inside it. A queue "
        "folder with its own render folder pre-selects that one.",
        "<b>Blender</b> – <i>Auto</i> or a specific install, and the other job settings (chapter 9). They apply to "
        "all files added in this step.",
    ])
    s.append(note("If you add a file that was rendered into the same render folder before (and it is not in the "
                  "queue), the app re-attaches to the existing job folder and keeps all finished frames. A different "
                  "file with the same name gets a numbered folder (<i>name_2</i>) instead.", "Re-adding"))
    s.append(H2("4.2  Folders"))
    s.append(P("Folders organise the queue, e.g. one per project. Create one with <b>New folder…</b>; give it a "
               "name and optionally a default render folder, which is pre-filled whenever you add files to it. "
               "Double-click a folder (or <i>File → Folder settings…</i>) to edit it. Deleting a folder moves its "
               "jobs to the top level – no job is removed."))
    s.append(P("Move jobs by dragging them, or right-click → <i>Move to folder</i>. <b>▲ / ▼</b> move the selected job "
               "within its folder, or the selected folder among folders."))
    s.append(H2("4.3  Render order and skipping"))
    s.append(P("The queue renders exactly as shown, top to bottom: jobs at the top level first, then each folder's "
               "jobs in order. One job renders at a time. Untick a job or a whole folder to skip it without "
               "removing it; skipped jobs show <i>(skipped)</i> and keep their progress."))
    s.append(H2("4.4  Removing jobs"))
    s.append(P("<b>Remove</b> takes the job out of the queue only. Its folder with frames, data and logs stays on "
               "disk. Put it back later by adding the same file to the same render folder, or with "
               "<i>File → Add existing job folder…</i>."))

    s.append(H2("4.5  Queue files"))
    s.append(P("A queue can be saved as a file (<font face='Mono'>.bbrqueue</font>) and opened again later, so you can keep "
               "several setups – e.g. one per project – and switch between them. Commands in the <b>File</b> menu: "
               "<b>New Queue</b> (⌘N), <b>Open Queue…</b> (⌘O), <b>Open Recent</b>, <b>Save Queue</b> (⌘S) and "
               "<b>Save Queue As…</b> (⇧⌘S). Double-clicking a queue file in Finder opens it in the app; if the app is "
               "already running, the running app opens it."))
    s += bullets([
        "A queue file stores the <b>folders, the jobs in their order and their on/off switches</b> – not the progress. "
        "Progress always lives in each job's own folder, so a job shows the same progress in every queue that contains it.",
        "<b>Saving is manual.</b> Unsaved changes are shown by a dot in the window's close button (an asterisk in the "
        "title on other systems). Before a queue with unsaved changes is replaced (New / Open), the app asks whether to "
        "save it. The current queue is still kept when you quit, so nothing is lost if you forget to save.",
        "Paths are stored both absolute and <b>relative to the queue file</b>: if you move a project folder that contains "
        "the queue file and its render folders, the queue still finds its jobs.",
        "A queue cannot be opened or replaced while a render is running – stop the batch first.",
    ])
    s.append(H2("4.6  Keyboard shortcuts"))
    s.append(P("Also listed in the app under <i>Help → Keyboard shortcuts</i> (⌘/). On Windows and Linux, ⌘ is Ctrl."))
    s.append(table([
        ["Keys", "Action"],
        ["⌫ / Delete", "Remove the selected job or folder (asks first; files stay on disk)"],
        ["⌘⌫", "Remove without asking"],
        ["↩", "Open the job's render folder (on a folder: folder settings)"],
        ["Space", "Play the preview video"],
        ["⌘↑ / ⌘↓", "Move the selected job or folder up / down"],
        ["⌘R · ⇧⌘. · ⌘.", "Start · stop after this frame · stop now"],
        ["⌘N · ⌘O · ⌘S · ⇧⌘S", "New · open · save · save queue as"],
        ["⌘I · ⇧⌘N", "Add .blend files · new folder"],
        ["⌘P · ⌘, · ⌘/", "Create preview · settings · list of shortcuts"],
    ], [44, 126]))
    s.append(P("The keys that act on the selected row only work while the queue list has the keyboard focus, so typing in "
               "a text field can never remove a job."))

    # 5 ----------------------------------------------------------------
    s.append(H1("5  Rendering, stopping &amp; resuming"))
    s.append(table([
        ["Command", "What happens"],
        ["▶ Start (⌘R)", "Renders the queue from the first job that still has work. Interrupted jobs continue "
                        "with their first missing frame."],
        ["Stop after frame", "The current frame finishes and is saved, then the batch stops."],
        ["■ Stop now (⌘.)", "Blender is ended immediately. The interrupted frame does not count as a failure and is "
                           "rendered again next time."],
        ["Cancel job", "Ends the current job, marks it <i>cancelled</i> and continues with the next job. Finished "
                       "frames are kept."],
        ["Quit (close window)", "If a render is running, asks: <i>Stop now and quit</i>, <i>Finish this frame, then "
                                "quit</i>, or <i>Keep rendering</i>. Either way, the next Start continues where it stopped."],
        ["Retry failed frames", "Queues a job again and renders only the frames that gave up (also for failed, "
                                "cancelled and on-hold jobs)."],
        ["Continue with changed file", "For a job on hold because its .blend changed: keep finished frames, render "
                                       "the rest from the changed file."],
        ["Start job over…", "Forgets the job's progress and renders every frame again (existing files are overwritten)."],
        ["Create preview (⌘P)", "Builds the MP4 preview from the frames finished so far."],
    ], [42, 128]))
    s.append(H2("5.1  How resuming works"))
    s.append(P("After every finished frame the app writes the job's complete state to <font face='Mono'>job.json</font> "
               "in the job folder (written to a temporary file and renamed, so it is never half-written). When a job "
               "starts, the app checks that every frame recorded as finished still exists on disk and is not empty; "
               "missing frames are simply rendered again. Blender is then told which frames to skip."))
    s.append(P("Inside Blender, each frame is saved under a temporary name (<font face='Mono'>.tmp_…</font>) and "
               "renamed only when it is complete. If Blender is killed while saving, only the temporary file is "
               "left, and it is cleaned up before the next run."))
    s.append(note("If the app itself crashes or the computer loses power, nothing special is needed: on the next "
                  "launch a job that was <i>rendering</i> is shown as <i>stopped (interrupted)</i>. With "
                  "<i>Resume an interrupted batch automatically</i> enabled in Settings, the batch restarts by itself.",
                  "Crashes and power loss"))
    s.append(H2("5.2  Job states"))
    s.append(Fig(lifecycle_diagram(), "Figure 2 – Job states."))
    s.append(table([
        ["State", "Meaning", "Rendered by Start?"],
        ["queued", "Waiting for its turn.", "yes"],
        ["rendering", "Blender is working on it (or loading, checking, waiting to retry, making the preview).", "–"],
        ["stopped", "Stopped by you, by quitting, by a full disk, or interrupted by a crash.", "yes – continues"],
        ["done", "All frames finished.", "no"],
        ["done (with failed frames)", "Finished, but some frames gave up after all attempts.", "no – use Retry"],
        ["failed", "The file could not be rendered at all (e.g. no camera, cannot load).", "no – use Retry"],
        ["cancelled", "Cancelled by you.", "no – use Retry"],
        ["on hold", "Needs a decision: the .blend changed since frames were rendered, or it is missing.",
         "no – see 7.2"],
    ], [38, 100, 32]))

    # 6 ----------------------------------------------------------------
    s.append(H1("6  Failures, retries &amp; restarts"))
    s.append(H2("6.1  Frame failures"))
    s.append(P("If Blender crashes or reports an error while rendering a frame, that frame's attempt counter goes up, "
               "the console log is copied to <font face='Mono'>logs/crashes/</font> together with Blender's own crash "
               "report, and Blender is started again after <b>Retry delay</b>. After <b>Attempts</b> "
               "failed attempts the frame is marked <i>failed</i> and the job continues with the next frame. "
               "Failed frames are listed in the Issues tab and in the reports."))
    s.append(H2("6.2  Files that will not render"))
    s.append(P("If Blender fails before rendering any frame (e.g. the file cannot be read), this counts as a failed "
               "start; after the same number of attempts the job is marked <i>failed</i> and the batch moves on. "
               "Some problems are recognised immediately and fail the job without retries – for example a scene "
               "without an active camera."))
    s.append(H2("6.3  Frame timeout"))
    s.append(P("With a <b>Frame timeout</b> set, a frame that takes longer is ended and counted as a failed attempt – "
               "useful against renders that hang. Leave it at <i>no limit</i> unless you know your frame times."))
    s.append(H2("6.4  Restart Blender"))
    s.append(P("Normally one Blender process renders all remaining frames of a job, which saves loading time. Over a "
               "long sequence Blender's memory use can grow until it crashes. With <b>Restart Blender</b> set to N "
               "frames, a fresh Blender is started after every N frames. These planned restarts are never "
               "counted as failures; the cost is one file load per restart."))

    # 7 ----------------------------------------------------------------
    s.append(H1("7  Safety checks"))
    s.append(H2("7.1  Disk space"))
    s.append(P("Before each Blender run and after every frame, the app checks the free space on the render drive. If "
               "it is below the limit in Settings (default 5 GB) – or below three average frames, whichever is "
               "more – the batch stops right after the current frame. If Blender reports <i>No space left on "
               "device</i> while saving, the same happens. No frame is marked as failed; free some space and press "
               "Start."))
    s.append(H2("7.2  Changed or missing .blend files"))
    s.append(P("The app remembers the modification time and size of each .blend when its frames are rendered. If the "
               "file was saved again after some frames were finished, continuing could mix two versions of the scene "
               "in one sequence. The job is therefore put <b>on hold</b> and the batch continues with other jobs. "
               "Decide in the <b>Job ▾</b> menu: <i>Continue with changed file</i> (keep finished frames) or "
               "<i>Start job over</i>. A .blend that no longer exists also puts its job on hold instead of failing it."))
    s.append(H2("7.3  Moved render folders"))
    s.append(P("Frames are recorded relative to their job folder, so moving or renaming the render folder – or the "
               "drive getting a different name – does not lose any frames. A moved job folder is reported as missing "
               "when the app starts; put it back with <i>File → Add existing job folder…</i>."))
    s.append(H2("7.4  One copy at a time"))
    s.append(P("An operating-system file lock ensures that only one copy of the app (window or command line) works "
               "on the queue. The lock is released automatically if the app crashes, so it never needs cleaning up."))
    s.append(H2("7.5  Keeping the computer awake"))
    s.append(P("While a batch runs, the app prevents the computer from sleeping (macOS: <font face='Mono'>caffeinate"
               "</font>; Windows: system execution state; Linux: <font face='Mono'>systemd-inhibit</font>)."))
    s.append(note("On macOS this only works while the Mac is plugged in, and a MacBook still goes to sleep when its "
                  "lid is closed without an external display.", "Limits"))

    # 8 ----------------------------------------------------------------
    s.append(H1("8  Blender versions"))
    s.append(P("Every .blend file records the Blender version it was saved with. The app reads it directly from the "
               "file header – also for compressed files – without starting Blender, and picks an install:"))
    s += bullets([
        "an install with the <b>same major.minor version</b> (the newest patch release if there are several);",
        "otherwise the <b>next newer</b> version (the lowest version above the file's, again its newest patch);",
        "otherwise – the file is newer than every install – the newest install, with a warning in the job.",
    ])
    s.append(P("The choice and the reason are shown in the queue, the Live tab and the report. In the job settings "
               "you can force a specific install instead of <i>Auto</i>. Several versions can be mixed freely in one "
               "batch, since every job starts its own Blender."))
    s.append(note("Blender is started with your normal user preferences, so add-ons and GPU settings are the same "
                  "as in your interactive Blender. Drivers that use Python expressions only work if <i>Auto Run Python "
                  "Scripts</i> is enabled in Blender's preferences – the same as for any command-line render.",
                  "Preferences &amp; add-ons"))

    # 9 ----------------------------------------------------------------
    s.append(H1("9  Settings reference"))
    s.append(H2("9.1  Job settings"))
    s.append(P("Set when adding files, edited later in the <b>Job settings</b> tab. Defaults for new jobs are set in "
               "<b>Settings…</b>."))
    s.append(table([
        ["Setting", "Default", "Description"],
        ["Render folder", "next to the .blend", "A <i>render</i> folder beside each .blend, or a custom parent folder."],
        ["Blender", "Auto", "Which install renders the job (chapter 8)."],
        ["Start frame / End frame", "from file", "Override the frame range. Unticked = use the file's range."],
        ["Samples", "from file", "Override render samples (Cycles samples or EEVEE render samples)."],
        ["Resolution", "from file", "Override the resolution percentage."],
        ["Attempts", "3", "How often a frame (or loading the file) is tried before giving up."],
        ["Retry delay", "30 s", "Pause before Blender is started again after a failure."],
        ["Frame timeout", "no limit", "End a frame that takes longer than this; counts as a failed attempt."],
        ["Restart Blender", "never", "Fresh Blender process every N frames (6.4)."],
        ["MP4 preview", "on", "Build the preview video when the job finishes."],
        ["Check file first", "off", "Run the preflight check (chapter 12) before the first render of the job."],
        ["Ignore add-ons", "off", "Workers render the job even without the add-ons "
                                                                 "it seems to use (15.2)."],
        ["File transfer", "Smart", "Smart: files once, frame files when needed; Full copy: "
                                                 "everything before the first frame (15.3)."],
        ["Test frame", "on", "Test frame before a computer joins (15.4)."],
        ["Devices", "as saved in the file", "For each computer: GPU, CPU, or Off (never renders this "
                                                        "job). Cycles only (15.2)."],
    ], [44, 30, 96]))
    s.append(H2("9.2  App settings"))
    s.append(table([
        ["Setting", "Default", "Description"],
        ["Blender installs", "found automatically", "List of installs with their versions; Add…, Remove, Scan."],
        ["Keep awake while rendering", "on", "Prevent sleep while a batch runs."],
        ["Resume on start", "off", "Restart an interrupted batch when the app launches."],
        ["Console lines", "5000", "How much Blender output the console keeps in memory (the log files "
                                              "always contain everything)."],
        ["Min. free space", "5 GB", "Disk-space limit for 7.1."],
        ["Status page and API", "on", "Chapter 13."],
        ["Port", "8642", "Network port of the status page and API."],
        ["Home network access", "off", "Makes the status page reachable from phones and other "
                                                           "computers at home; they need the key."],
        ["Access key", "random", "Part of the link for other devices; <i>New key</i> locks out old links."],
        ["Mode (top right)", "Coordinator", "Coordinator keeps the queue; Worker renders for a coordinator (15)."],
        ["Share jobs with workers", "off", "Machines tab: other computers may render frames of the queue."],
        ["This computer renders too", "on", "Machines tab: off = the coordinator only hands out frames."],
        ["Pairing code", "random", "Machines tab: 6 digits a new worker enters once; <i>New code</i> for new workers."],
        ["Worker: keep downloaded jobs up to", "50 GB", "Worker window: size of the cache of job copies (15.8)."],
        ["Worker: connect automatically when the app starts", "off", "Otherwise press <i>Connect</i> in the "
                                                                     "worker window (15.1)."],
        ["Worker: folder for downloaded files", "app data folder", "Worker window: <i>Folder…</i> (15.8)."],
    ], [52, 34, 84]))
    s.append(H2("9.3  What the app changes in your file"))
    s.append(P("Only in the background Blender session – <b>the .blend file itself is never saved or modified</b>:"))
    s += bullets([
        "the output path (frames go to the job's <font face='Mono'>frames/</font> folder);",
        "video output (FFmpeg etc.) becomes a PNG sequence, keeping 8- or 16-bit colour depth;",
        "compositor <i>File Output</i> nodes write into <font face='Mono'>frames/&lt;node name&gt;/</font>;",
        "the overrides you set (frame range, samples, resolution %).",
    ])

    # 10 ---------------------------------------------------------------
    s.append(H1("10  Output folders &amp; data files"))
    s.append(code("""
<render folder>/<file name>/
  frames/            <name>_0001.png …      finished frames only
  frames/<node>/     output of compositor File Output nodes
  preview/           <name>.mp4             preview video
  data/              frames.csv             one row per frame event, written live
                     attempts.csv           one row per Blender run
                     report.xlsx            summary, frames, attempts + charts
                     preflight.json         result of the optional file check
  logs/              attempt_001.log …      complete Blender console of every run
                     preflight.log, preview.log
  logs/crashes/      logs + Blender crash reports of failed runs
  logs/tmp/          Blender's temporary folder for this job
  job.json           the job's full state – this is what makes resuming work
"""))
    s.append(Spacer(1, 8))
    s.append(H2("10.1  frames.csv"))
    s.append(table([
        ["Column", "Meaning"],
        ["frame, status", "Frame number; <i>done</i>, <i>pending</i> (will be retried) or <i>failed</i>."],
        ["attempt_no, attempts_for_frame", "Number of the Blender run; how many times this frame was tried."],
        ["finished_at", "Date and time."],
        ["render_s, save_s", "Seconds for rendering and for saving the frame."],
        ["blender_peak_mb", "Highest memory value Blender reported during the frame."],
        ["process_peak_mb", "Highest measured memory of the Blender process during the frame."],
        ["samples, size_bytes, file", "Samples, file size and path of the frame."],
        ["error", "Last error for frames that failed."],
    ], [56, 114]))
    s.append(H2("10.2  report.xlsx"))
    s.append(P("Rewritten whenever a job finishes, stops or is cancelled. <b>Summary</b>: file, versions, engine, "
               "device, resolution, samples, frame range, fps, output format, totals (frames done/failed, Blender "
               "runs, total render time, wall-clock time, average/fastest/slowest frame, memory peaks, disk used, "
               "preview path) with charts of render time, memory and file size per frame. <b>Frames</b>: one row per "
               "frame. <b>Attempts</b>: one row per Blender run with duration, frames rendered, exit code, result and "
               "log file. If the report is open in Excel and locked, a timestamped copy is written instead."))
    s.append(H2("10.3  Where the app keeps its own data"))
    s.append(P("macOS: <font face='Mono'>~/Library/Application Support/BlenderBatchRenderer/</font>; Windows: "
               "<font face='Mono'>%APPDATA%\\BlenderBatchRenderer</font>; Linux: "
               "<font face='Mono'>~/.local/share/BlenderBatchRenderer</font>. It contains "
               "<font face='Mono'>settings.json</font> (installs and defaults), <font face='Mono'>queue.json</font> "
               "(folders and the order of job folders) and <font face='Mono'>app.lock</font>. On a worker it also "
               "holds <font face='Mono'>worker_cache/</font> (downloaded job copies) and "
               "<font face='Mono'>worker_logs/</font> (15.8). Everything about a job lives in its own job folder."))

    # 11 ---------------------------------------------------------------
    s.append(H1("11  Preview video"))
    s.append(P("When a job finishes (if <i>MP4 preview</i> is on), or when you choose <b>Create preview</b>, "
               "the finished frames are loaded into Blender's video editor in a second, add-on-free Blender run and "
               "encoded as H.264 MP4 at the scene's frame rate and output resolution (rounded to even pixels). The "
               "result is <font face='Mono'>preview/&lt;name&gt;.mp4</font>; double-click a job's folder or "
               "right-click → <i>Play preview</i>."))
    s += bullets([
        "Frames that failed appear as short black gaps, so timing stays correct.",
        "PNG/JPG sequences are shown as they are; EXR sequences use the scene's view transform and look.",
        "The preview is meant for checking, not as a final deliverable – use your editor (e.g. DaVinci Resolve) "
        "with the image sequence for that.",
    ])

    # 12 ---------------------------------------------------------------
    s.append(H1("12  Preflight check"))
    s.append(P("With <i>Check file first</i> enabled, Blender opens the file once before the first render and "
               "reports problems, which appear as warnings and in the Issues tab (and in "
               "<font face='Mono'>data/preflight.json</font>). The job still renders – the check informs, it does not "
               "block. It looks for:"))
    s += bullets([
        "a scene without an active camera;",
        "missing images, linked libraries, sounds, movie clips, fonts, volumes and cache files;",
        "simulation caches that are not baked (cloth, soft body, particles and hair dynamics, dynamic paint, "
        "rigid body world) and fluid domains using a replay cache – these can come out differently when a job "
        "resumes mid-sequence;",
        "Cycles set to GPU without any GPU enabled in Blender's preferences;",
        "notes: video output that will become PNG, File Output nodes that will be redirected.",
    ])

    # 13 ---------------------------------------------------------------
    s.append(H1("13  Status page &amp; API"))
    s.append(P("While the app is open it runs a small web server. In a browser it shows a <b>status page</b>; for other "
               "programs it offers an <b>API</b>. Open the page with <i>Help → Status page</i> or at "
               "<font face='Mono'>http://127.0.0.1:8642/</font>."))
    s.append(H2("13.1  The status page"))
    s += bullets([
        "Batch state, jobs done, remaining time for the batch and the expected finishing time.",
        "The frame being rendered: sample progress, Blender's remaining time, the job's remaining time, memory, and a "
        "thumbnail of the last finished frame.",
        "The queue with its folders: status, notes, progress, frames and remaining time per job; a <i>retry</i> link "
        "for failed, cancelled and on-hold jobs.",
        "Buttons for <b>Start</b>, <b>Stop after frame</b> and <b>Stop now</b>, and Blender's console (fold-out).",
        "It refreshes every two seconds and adapts to phone screens and dark mode.",
    ])
    s.append(H2("13.2  On your phone or another computer"))
    s.append(P("In <i>Settings → Status page &amp; API</i>, tick <b>Home network access</b>. "
               "<i>Copy link</i> copies a link such as <font face='Mono'>http://192.168.1.20:8642/?key=…"
               "</font>; open it on the phone (and add it to the home screen). Devices without the key are refused."))
    s.append(note("Away from home the app is not reachable – on purpose. If you want that, use a VPN such as "
                  "Tailscale (your phone then behaves as if it were at home). Do not open a port on your router.",
                  "Outside your home"))
    s.append(H2("13.3  Security"))
    s += bullets([
        "By default the server only accepts connections from the computer itself.",
        "Other devices (if allowed) must send the key. <i>New key</i> in Settings makes all old links stop working.",
        "Requests that change something must carry a special header, which ordinary web pages cannot send to the app; "
        "requests with an unexpected host name are refused. A random website you visit therefore cannot control the app.",
        "The API only works with files on this computer and never deletes files: removing a job only takes it out of the queue.",
    ])
    s.append(H2("13.4  API reference (version 1)"))
    s.append(P("All answers are JSON. From the same computer, requests that change something need the header "
               "<font face='Mono'>X-BBR-Request: 1</font>; other devices send <font face='Mono'>X-API-Key: &lt;key&gt;</font>."))
    s.append(table([
        ["Request", "Purpose"],
        ["GET /api/v1/status", "Batch state, live frame information, estimates, current queue file."],
        ["GET /api/v1/queue", "Folders and jobs in render order (status, progress, frames, ETA, disk use)."],
        ["GET /api/v1/jobs/&lt;id&gt;", "Job details: settings, scene facts, preflight, problem frames, Blender runs, frame times."],
        ["GET /api/v1/jobs/&lt;id&gt;/thumbnail", "The last finished frame as an image (not for EXR)."],
        ["GET /api/v1/console?since=N", "Blender console lines after line number N (optional &amp;job=&lt;id&gt;)."],
        ["POST /api/v1/jobs", "Add a job. Body: <font face='Mono'>blend</font> (path, required), optional "
                              "<font face='Mono'>folder</font> (queue folder name or id, created if missing), "
                              "<font face='Mono'>render_root</font> (\"\" = next to the .blend), "
                              "<font face='Mono'>frame_start, frame_end, samples, resolution_pct, max_attempts, "
                              "retry_delay_s, frame_timeout_min, restart_every, make_preview, preflight, blender, start</font>."],
        ["POST /api/v1/batch/start", "Start the batch."],
        ["POST /api/v1/batch/stop", "Body <font face='Mono'>{\"mode\": \"after_frame\"}</font> or "
                                    "<font face='Mono'>{\"mode\": \"now\"}</font>."],
        ["POST /api/v1/batch/cancel-job", "Cancel the job being rendered and continue with the next."],
        ["POST /api/v1/jobs/&lt;id&gt;/retry · /reset · /preview", "Retry failed frames · start over · create the preview."],
        ["POST /api/v1/jobs/&lt;id&gt;/enabled", "Body <font face='Mono'>{\"enabled\": false}</font> – switch a job off or on."],
        ["DELETE /api/v1/jobs/&lt;id&gt;", "Remove the job from the queue (files stay on disk)."],
        ["POST /api/v1/queue/open", "Body <font face='Mono'>{\"path\": \"…/x.bbrqueue\"}</font> – open a queue file in the window."],
    ], [62, 108]))
    s.append(P("Errors come back with an HTTP error code and <font face='Mono'>{\"error\": \"…\"}</font>, e.g. 400 for a "
               "missing .blend, 404 for an unknown job, 409 when a job is rendering."))
    s.append(code("""
curl http://127.0.0.1:8642/api/v1/status
curl -X POST -H "X-BBR-Request: 1" \\
     -d '{"blend": "/Users/me/shots/shot_010.blend", "folder": "Project X", "start": true}' \\
     http://127.0.0.1:8642/api/v1/jobs
"""))
    s.append(Spacer(1, 8))

    # 14 ---------------------------------------------------------------
    s.append(H1("14  Blender add-on “Send to Batch Renderer”"))
    s.append(P("The add-on adds the file open in Blender to the queue with one click. It uses the API, so the app must be "
               "running with the status page &amp; API switched on (the default)."))
    s.append(H2("14.1  Installing"))
    s += bullets([
        "In Blender: <i>Edit → Preferences → Add-ons</i>, open the ▾ menu, choose <i>Install from Disk…</i> and pick "
        "<font face='Mono'>blender_addon/bbr_send_to_queue.py</font> from the app's folder. Tick it to enable it.",
        "Install it in each Blender version you use. If you changed the app's port, enter the same port in the add-on's "
        "preferences.",
    ])
    s.append(H2("14.2  Using it"))
    s.append(P("<i>Render → Send to Batch Renderer…</i>, or the <b>Batch Renderer</b> panel under <i>Properties → Output</i>. "
               "A small dialog asks for:"))
    s += bullets([
        "the render folder – next to the .blend (default) or a custom folder;",
        "an optional queue folder (created if it does not exist);",
        "an optional frame range (pre-filled with the scene's range);",
        "whether to save unsaved changes first (on by default – the batch renders the file as it is on disk);",
        "whether to start rendering right away.",
    ])
    s.append(P("<i>Open status page</i> in the same panel opens the status page in the browser. If the app is not running, "
               "the add-on says so instead of failing silently."))

    # 15 ---------------------------------------------------------------
    s.append(PageBreak())
    s.append(H1("15  Rendering on several computers"))
    s.append(P("Several computers can render the frames of the same animation together. One of them – the "
               "<b>coordinator</b> – has the queue as usual. The others run the same app in <b>worker</b> mode: they "
               "ask the coordinator for frames, render them and send them back. Finished frames, reports and the "
               "preview end up in the coordinator's job folders exactly as if it had rendered everything itself. "
               "No shared folder or NAS is needed: the app transfers everything itself."))
    s.append(Fig(network_diagram(), "Figure 3 – A coordinator with two workers."))
    s.append(H2("15.1  Setting it up"))
    s += bullets([
        "<b>On every computer</b>: install the app and the Blender version(s) your files need, plus the add-ons your "
        "scenes use (for example LensSim). Add-ons are not copied between machines – each one needs its own "
        "installation (and licence). Enable them in Blender's preferences as usual.",
        "<b>On the coordinator</b>: open the <b>Machines</b> tab and tick <i>Share jobs with workers</i>. "
        "The tab shows a 6-digit <b>pairing code</b> and the address workers connect to. When the firewall asks, "
        "allow incoming connections for the app.",
        "<b>On each worker</b>: switch the mode (top right of the window) to <b>Worker</b>. Press <i>Find on "
        "network</i> (or type the coordinator's address), enter the pairing code and press <i>Pair</i>. This is "
        "needed only once. Afterwards the worker connects only when you press <b>Connect</b> in its window – or by "
        "itself when the app starts, if you tick <i>Connect on start</i>. "
        "<b>Disconnect</b> stops it (not while it is rendering: pause or stop first).",
        "Press <b>Start</b> on the coordinator as usual. Workers that are online and switched on join in.",
    ])
    s.append(note("The mode switch, sharing and the worker's settings are <b>locked while something is rendering</b>. "
                  "On a worker, use <i>Pause after frame</i> or <i>Stop now</i> instead.", "Locked while rendering"))
    s.append(H2("15.2  What happens"))
    s += bullets([
        "<b>The job's files.</b> Before the first worker renders a job, the coordinator lists every file the job "
        "needs and writes copies of the .blend and of every <b>linked library</b> (also libraries linked by "
        "libraries) whose paths point to those files. Your original files are not changed. The files are sent one "
        "by one, each with a checksum (section 15.3).",
        "<b>Frames, not ranges.</b> Workers ask for one frame at a time and keep Blender open between frames, "
        "so a fast PC simply renders more frames than a laptop. Each frame is <i>leased</i> to one machine; while "
        "a worker sends its heartbeat every few seconds, the lease stays valid.",
        "<b>Delivery.</b> A finished frame is sent to the coordinator right away (in the background – Blender "
        "already renders the next one) and saved there via a temporary name, like local frames. The worker "
        "deletes its copy once the coordinator confirmed it.",
        "<b>Add-ons and versions.</b> The coordinator detects which add-ons a file uses (from its node groups, "
        "custom properties and render handlers) and gives frames only to workers with the same Blender version "
        "(major.minor) and those add-ons enabled. The Machines tab shows why a worker gets no frames. If the add-ons "
        "do not change the image, tick <i>Ignore add-ons</i> in the job settings.",
        "<b>Render engines.</b> Cycles, EEVEE and Workbench are supported. Cycles uses each worker's own device "
        "preferences (OptiX/CUDA on NVIDIA, Metal on Apple Silicon); the device shows in the Machines tab.",
        "<b>Devices.</b> In a job's settings, choose for each computer <i>From file</i>, "
        "<i>GPU</i>, <i>CPU</i> or <i>Off</i>. For example, a scene using an add-on that only works on NVIDIA "
        "GPUs renders on the PC's GPU and on the Mac's CPU (or is switched off there); a scene that needs more "
        "VRAM than the PC has renders on the Mac's GPU and is switched off for the PC. <i>GPU</i> uses the GPU set "
        "in that computer's Blender preferences, or the first one found.",
    ])
    s.append(H2("15.3  Sending the files"))
    s.append(table([
        ["Group", "Examples", "Sent"],
        ["Scene", "the .blend and its linked libraries", "before the first frame; only changed files when you save again"],
        ["Assets", "textures, HDRIs, UDIM tiles, Alembic/USD, single VDBs, movies, fonts",
         "once; kept on the worker by checksum and reused by every job that uses them"],
        ["Frame files", "image sequences, VDB sequences, simulation and fluid caches on disk",
         "<b>Smart</b>: only the files a frame needs, just before it renders (the next frame's files load while "
         "the current one renders), deleted afterwards. <b>Full copy</b>: all before the first frame."],
    ], [26, 64, 80]))
    s += bullets([
        "<b>File transfer</b> (job setting): <i>Smart</i> (default) or <i>Full copy</i>. Only frame "
        "files the job's frame range actually uses are sent at all. For caches, the neighbouring frames are "
        "included, because motion blur reads them.",
        "<b>Automatic fallback.</b> After opening the job, the worker's Blender checks every file the scene refers "
        "to. If something is missing, or Blender reports that it cannot read a file while rendering a frame, the "
        "worker switches that job to Full copy and renders the frame again. Such a frame is never delivered or "
        "counted as failed, and the coordinator shows a message.",
        "<b>Checksums.</b> Every file is checked after transfer, in both directions. A damaged download is fetched "
        "again (three times at most); a damaged frame is sent again. The coordinator remembers checksums and reads a "
        "file again only when its size or date changes. A file that changes during the batch makes the coordinator "
        "renew the job's file list.",
        "<b>Compression</b> only where it helps: a file that does not shrink by at least 10 % (EXR, PNG, VDB, "
        "Alembic …) is sent as it is.",
        "<b>Version check.</b> Coordinator and workers must run the same version of the app; another version is "
        "refused with a clear message.",
        "<b>Unbaked simulations</b> can come out differently on another computer – such jobs stay on the "
        "coordinator until they are baked.",
    ])
    s.append(H2("15.4  Consistency check and the last frames"))
    s.append(P("With <i>Test frame</i> (job setting, on by default), a computer "
               "that joins a job first renders a frame that is already finished. The coordinator compares the two "
               "images in Blender, block by block, so render noise – which differs between CPU, GPU and GPU makers – "
               "averages out, while a missing add-on effect, other colours or missing textures stand out. A computer "
               "whose frame differs is left out of that job; its test frame is kept in "
               "<font face='Mono'>data/consistency/</font>. Untick the setting to use it anyway. The check runs again "
               "when the job's files or the computer's device setting change."))
    s.append(P("<b>Is it worth it?</b> A computer gets no more frames of a job when the others would finish all "
               "remaining frames before it finishes one (its render time plus moving that frame's files). So a slow "
               "laptop does not hold up the end of a job."))
    s.append(H2("15.5  Stopping, failures and lost machines"))
    s.append(table([
        ["Situation", "What happens"],
        ["Stop after frame", "Every machine finishes and delivers the frame it is working on, then the batch stops."],
        ["Stop now / Cancel job", "Every machine drops its frame at once; nothing is counted as failed."],
        ["Pausing one computer", "<b>Pause after frame</b> (it delivers its frame, then takes no more), <b>Stop now</b> "
                                 "(it drops its frame, which goes to another computer; nothing counts as failed) "
                                 "and <b>Resume</b> – in the worker window, in the Machines tab (select or "
                                 "right-click the computer) or on the status page. The others carry on with the job."],
        ["A worker crashes on a frame", "The frame counts one failed attempt (with the machine's name) and goes "
                                        "preferably to another machine. A worker that keeps crashing on a job or "
                                        "cannot open it is taken off that job for the rest of the batch."],
        ["A worker disappears (sleep, Wi-Fi drop, quit)", "After 45 seconds without a heartbeat its frame goes back "
                                                          "into the pool. A frame it finishes later is still "
                                                          "accepted if nobody delivered it meanwhile."],
        ["The coordinator quits", "Workers finish their frame and keep it until the coordinator is back, then deliver it."],
        ["The .blend or a library is saved again", "A new file list is made; workers fetch only the changed files. "
                                                   "The usual on-hold rule (7.2) applies."],
    ], [52, 118]))
    s.append(H2("15.6  Statistics"))
    s += bullets([
        "<b>Machines tab</b> (coordinator): per machine its state, what it is rendering (frame, samples, Blender's "
        "remaining time), device, Blender versions and add-ons, frames this batch, average frame time, failed "
        "attempts, data with the measured transfer speed, and all-time totals; above the table the combined figures and the speed of all machines "
        "together (frames per hour). Untick a machine to leave it out (when no batch is running).",
        "<b>Live tab</b>: frames per machine for the selected job, which frames are being rendered where, and the "
        "size of the job copy. Time remaining uses the combined speed of all machines that can render the job.",
        "<b>Reports</b>: <font face='Mono'>frames.csv</font> has a <i>machine</i> column; <font face='Mono'>"
        "report.xlsx</font> gets a <b>Machines</b> sheet (frames, share, render time, average / fastest / slowest "
        "frame, failed attempts, disk) with charts.",
        "<b>Worker window</b>: this session and all time, per job; the frame being rendered with samples, "
        "remaining time, memory and device; downloads; the Blender console.",
        "<b>Status page</b>: a Machines section with the same per-machine and combined figures.",
    ])
    s.append(H2("15.7  Security"))
    s.append(P("Sharing makes the app reachable on the home network. Every worker gets <b>its own key</b> when it "
               "pairs; the coordinator stores only a fingerprint of it. A key works only together with that "
               "worker's machine id. Each pairing code works <b>once</b> and changes after a successful pairing; it "
               "only works while sharing is on, and repeated wrong codes are blocked. <i>Unpair and remove</i> "
               "(right-click a machine in the Machines tab) revokes that worker's key at once – it has to pair "
               "again with the current code to come back. Without a valid key, nothing can be downloaded or "
               "uploaded. The status page still needs its own key for "
               "other devices. Use the app only on networks you trust; for working away from home use a VPN such "
               "as Tailscale."))
    s.append(H2("15.8  Where the files are"))
    s.append(table([
        ["What", "Where"],
        ["Job files on the coordinator", "<font face='Mono'>&lt;job folder&gt;/package/v_&lt;version&gt;/</font> – "
                                         "the .blend copies and the file list; renewed when the scene changes."],
        ["Files on a worker", "<font face='Mono'>worker_cache/</font> in the app data folder (10.3), or the folder "
                              "chosen with <i>Folder…</i>: <font face='Mono'>store/</font> (files by checksum), "
                              "<font face='Mono'>jobs/</font> (each job's layout, linked to the store). The longest "
                              "unused files are removed when the size limit is reached; <i>Clear</i> empties it. "
                              "On Windows, choose a short folder (e.g. D:\\bbr) if file names are long."],
        ["Frames on their way", "<font face='Mono'>worker_cache/work/&lt;job&gt;/</font> – deleted once the coordinator "
                                "has confirmed each frame."],
        ["Worker crash logs", "<font face='Mono'>worker_logs/</font> in the app data folder."],
        ["Known workers", "<font face='Mono'>settings.json</font> on the coordinator – listed (offline) after a "
                          "restart until they connect again."],
    ], [46, 124]))

    # 16 ---------------------------------------------------------------
    s.append(PageBreak())
    s.append(H1("16  How it works"))
    s.append(Fig(architecture_diagram(), "Figure 4 – Components and data flow."))
    s.append(P("The app has three layers. The <b>window</b> (Qt) shows the queue and live data and sends commands. "
               "The <b>runner</b> runs in a background thread: it picks the next runnable job, performs the safety "
               "checks, starts Blender as a separate process, reads its output line by line, samples its memory, "
               "handles stops, timeouts and retries, and saves state. <b>Blender</b> runs headless with a small "
               "script (<font face='Mono'>bbr_render.py</font>) that applies the overrides, renders frame by frame "
               "and reports back."))
    s.append(H2("16.1  Communication with Blender"))
    s.append(P("The render script prints one line per event, starting with <font face='Mono'>BBR_</font> followed by "
               "JSON. Everything else Blender prints is kept as console output."))
    s.append(table([
        ["Line", "Meaning"],
        ["BBR_JOB_INFO", "Scene facts after overrides: engine, device, frame range, fps, resolution, samples, output "
                         "format, colour management, File Output nodes, notes."],
        ["BBR_PLAN", "Total frames and frames still to render."],
        ["BBR_FRAME_START", "A frame begins."],
        ["BBR_STATS", "Blender's render statistics (samples, time, remaining time, memory), at most four times per "
                      "second. Needed because Blender 5 no longer prints progress lines in background mode."],
        ["BBR_FRAME_DONE", "A frame is safely on disk: file, size, render and save time, peak memory, samples."],
        ["BBR_FRAME_ERROR / BBR_FATAL", "A frame failed / the job cannot be rendered at all."],
        ["BBR_CHUNK_DONE / BBR_ALL_DONE", "Planned restart point reached / every frame finished."],
        ["BBR_WARN", "A note for the user (e.g. output switched to PNG)."],
        ["BBR_NEED_FRAME", "Blender asks which frame to render next; the app answers on Blender's input with "
                           "<font face='Mono'>FRAME n</font> or <font face='Mono'>DONE</font>. This is how frames are "
                           "shared between machines while Blender stays open."],
        ["BBR_PACKAGE / BBR_ADDONS", "The job's file list / list of enabled add-ons."],
        ["BBR_INPUT_MISSING", "A worker's Blender misses files the scene needs (triggers Full copy)."],
        ["BBR_COMPARE", "Result of comparing two renders (consistency check)."],
    ], [48, 122]))
    s.append(P("Blender versions up to 4.x also print their own status lines (<font face='Mono'>Fra:… | Time:… | "
               "Remaining:… | Sample …</font>); these are read as well."))
    s.append(H2("16.2  Stopping cleanly"))
    s.append(P("Blender runs in its own process group. <i>Stop now</i> sends it a terminate signal and, if it has not "
               "ended after five seconds, kills it. <i>Stop after frame</i> waits for the next <font face='Mono'>"
               "BBR_FRAME_DONE</font> and then ends Blender while it is between frames. A frame interrupted by the "
               "user is never counted as a failed attempt."))
    s.append(H2("16.3  Estimates"))
    s += bullets([
        "<b>Job time remaining</b> = Blender's remaining time for the current frame + (frames left − 1) × the "
        "average of the last ten frames.",
        "<b>Batch time remaining</b> = the sum over all jobs that will still render. Jobs that have never started "
        "have no frame times yet and are counted separately (shown as “no estimate”). Loading time per Blender "
        "start is not included.",
        "<b>Projected disk use</b> = average frame size × number of frames.",
    ])
    s.append(H2("16.4  Source code layout"))
    s.append(table([
        ["File", "Purpose"],
        ["bbr/models.py", "Job, job settings and app settings; frame bookkeeping and estimates."],
        ["bbr/jobqueue.py", "Queue order, folders, adding / re-attaching jobs."],
        ["bbr/runner.py", "The batch engine: job loop, Blender runs, retries, safety checks, preview."],
        ["bbr/process.py", "Starting, reading, memory sampling and killing a Blender process."],
        ["bbr/blender_scripts/", "Scripts that run inside Blender: render, preflight, preview, shared helpers."],
        ["bbr/parse.py", "Reading BBR_ lines and Blender's statistics; formatting of times and sizes."],
        ["bbr/installs.py, blendfile.py", "Finding Blender installs; reading a .blend's version."],
        ["bbr/reports.py, preview.py", "CSV and Excel output; building the MP4 preview."],
        ["bbr/storage.py, keepawake.py", "Safe file writing, app data folder, instance lock, free space; sleep prevention."],
        ["bbr/gui/", "The window: main window, queue tree, dialogs, charts, app start-up."],
        ["bbr/cli.py", "Command-line interface."],
        ["bbr/api.py, bbr/control.py", "Web server, status page, API routes and security; what the API can read and do."],
        ["bbr/assets/status.html", "The status page (plain HTML and JavaScript)."],
        ["bbr/gui/api_bridge.py", "Runs API requests on the window's thread."],
        ["bbr/scheduler.py", "Leases: which machine renders which frame; machines and their statistics."],
        ["bbr/workerapi.py, bbr/worker.py", "Coordinator endpoints for workers and discovery; the worker client."],
        ["bbr/package.py", "The job's file list and frame mapping (with blender_scripts/bbr_package.py)."],
        ["bbr/hashing.py", "Checksums, remembered until a file changes."],
        ["blender_scripts/bbr_compare.py", "Compares two renders for the consistency check."],
        ["bbr/gui/machines_view.py, worker_view.py", "Machines tab; the window in worker mode."],
        ["blender_addon/", "The “Send to Batch Renderer” add-on for Blender."],
        ["bbr.spec, build_app.command", "Building the trimmed macOS app."],
        ["tests/", "Engine, API, queue-file and add-on tests with a simulated Blender."],
        ["tools/make_icon.py, docs/make_docs.py", "Draws the app icon; builds this document."],
    ], [62, 108]))

    # 17 ---------------------------------------------------------------
    s.append(H1("17  Command line"))
    s.append(P("Everything except the window is also available in a terminal, from the app's source folder:"))
    s.append(code("""
.venv/bin/python -m bbr scan                       # find Blender installs and save them
.venv/bin/python -m bbr scan "/Applications/Blender 4.app"   # … plus extra installs
.venv/bin/python -m bbr add shot.blend --root /path/to/render --folder "Project X" \\
        --start 1 --end 100 --samples 64 --pct 50 --attempts 3 --delay 30 --timeout 20 \\
        --blender auto --no-preview --preflight
.venv/bin/python -m bbr list                       # queue with status and progress
.venv/bin/python -m bbr run                        # render the queue; Ctrl+C = stop now
.venv/bin/python -m bbr worker --code 123456       # be a worker (finds the coordinator, pairs once)
.venv/bin/python -m bbr worker --server 192.168.1.20 --name "Render PC"
"""))
    s.append(Spacer(1, 6))
    s.append(P("The command line and the window share the same queue, settings and lock – only one of them can "
               "render at a time. <font face='Mono'>run</font> also starts the status page and API."))

    # 18 ---------------------------------------------------------------
    s.append(H1("18  Troubleshooting &amp; limitations"))
    s.append(table([
        ["Symptom", "What to do"],
        ["A job is <i>on hold</i>", "Its .blend changed or is missing – see the status note and section 7.2."],
        ["The batch stopped with “disk almost full”", "Free space on the render drive (or lower the limit in "
                                                               "Settings) and press Start."],
        ["Frames keep failing", "Open <i>Issues</i> for the error, and the log in "
                                "<font face='Mono'>logs/crashes/</font>. Memory problems: try <i>Restart Blender</i> "
                                "or lower samples / resolution."],
        ["Wrong Blender version used", "Check the reason in the Live tab; add the missing install under Settings or "
                                       "choose one in the job settings."],
        ["“Job folder missing” on start", "The render folder was moved or renamed: <i>File → Add existing job "
                                                  "folder…</i>."],
        ["No sample progress in the Live tab", "Look for <font face='Mono'>BBR_STATS</font> lines in the console; "
                                               "their format can change between Blender versions."],
        ["“Already running” message", "Another copy of the app (or the command line) is open."],
        ["Mac went to sleep", "Keep it plugged in; a closed MacBook lid without an external display still sleeps."],
        ["Status page does not open", "Check <i>Settings → Status page &amp; API</i>; the Messages tab says if the port is "
                                      "taken – choose another port (and enter it in the Blender add-on too)."],
        ["Phone cannot reach the page", "Tick <i>Home network access</i>, use the copied link (it contains the key), make "
                                        "sure the phone is on the same Wi-Fi, and allow the app in the Mac's firewall."],
        ["Add-on: \u201cnot running\u201d", "Start the app; check that the port in the add-on's preferences matches the app."],
        ["A worker gets no frames", "The Machines tab says why: other Blender version, missing add-on, switched off, "
                                    "or the job copy could not be made (Messages tab)."],
        ["Worker cannot find the coordinator", "Both on the same network? Sharing on? Type the address shown in the "
                                               "Machines tab; allow the app in the firewall (Windows asks on first use)."],
        ["Frames from a worker look different", "Compare Blender version, add-on versions and colour management; "
                                                "render a test frame on each machine."],
    ], [56, 114]))
    s.append(H2("Known limitations"))
    s += bullets([
        "A frame interrupted mid-render starts again from zero (Blender cannot resume inside a frame).",
        "Compositor <i>File Output</i> files are written directly by Blender, not via temporary names, and are not "
        "checked on resume.",
        "Stereo / multi-view renders: only the main view is saved by the render script (untested).",
        "Multilayer EXR output is supported in principle but has not been tested yet.",
        "Thumbnails are not available for EXR frames.",
        "Unbaked simulations may differ after a resume – bake them first.",
        "Each Blender start loads your add-ons; slow add-ons add time to every start (and every planned restart).",
        "Only the active scene of a file is rendered; put view layers you want separately into separate files.",
        "Several computers: unbaked simulations are not shared; geometry-nodes bakes and image sequences used "
        "in unusual places (e.g. a geometry-nodes Image Texture) are sent completely instead of frame by frame. "
        "Third-party render engines are not supported for sharing yet. Add-ons are detected by heuristics; a scene that uses an add-on only through "
        "a driver or script may not be recognised.",
    ])
    s.append(H2("Testing"))
    s.append(P("<font face='Mono'>python3 tests/test_api.py</font> tests the API and its security, queue files, the default "
               "render folder and the Blender add-on. <font face='Mono'>python3 tests/test_engine.py</font> runs 17 scenarios against a simulated Blender: "
               "crashes and retries, frames that always fail, stop and resume, quitting mid-render, files that do not "
               "load, missing camera, video-to-PNG switching and overrides, cancelling, re-attaching, Blender-5-style "
               "output, planned restarts, full disk, free-space limit, changed and missing .blend files, moved render "
               "folders, folders and the single-instance lock. <font face='Mono'>python3 tests/test_distributed.py</font> "
               "simulates a coordinator with workers: job copies with nested linked libraries and add-on detection, "
               "pairing and access checks, discovery, faster and slower workers, a missing add-on, crashing "
               "workers, a worker that disappears, stop now / stop after frame, and the coordinator rendering "
               "together with a worker. <font face='Mono'>python3 tests/test_updater.py</font> tests updates against a "
               "local stand-in for GitHub. <font face='Mono'>python3 tests/test_transfer.py</font> tests the file "
               "transfer: Smart transfer and reuse across jobs, damaged downloads and uploads, the switch to Full "
               "copy, the version check, the consistency check, the last-frames rule and remembered checksums."))

    return s


def main():
    doc = Doc(OUT)
    story = build()
    doc.multiBuild(story)
    print("written", OUT)


if __name__ == "__main__":
    main()
