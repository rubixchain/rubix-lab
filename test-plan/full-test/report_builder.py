#!/usr/bin/env python3
"""
report_builder.py - Builds the catalogue run report (PDF + JSON).

Shape of the PDF, in order:
    1. SUMMARY          - totals, pass rate, duration, and the failing Test
                          IDs up front, so the first thing you see is what
                          broke rather than 80 rows to scan.
    2. RUN CONDITIONS   - which hosts held which role, the tuning flags used,
                          fleet size, when it ran. Without these a report is
                          uninterpretable a month later and cannot be
                          compared against another run - which is the whole
                          point of a regression lab.
    3. WHAT "SKIPPED" MEANS - stated explicitly, because "57 passed" and
                          "57 passed, 23 not attempted" mean very different
                          things and the gap should be visible, not buried.
    4. ALL RESULTS      - Expected and Actual side by side.
    5. FAILURES ONLY    - repeated after the full table, with full notes.
    6. TIMING RESULTS   - the cases whose result IS a measurement (ladders,
                          baselines, limits). These are the numbers later
                          runs get compared against.

The JSON carries the same content in one structured file - conditions and
rows together, with real types - so runs can be diffed or charted without
re-parsing a PDF.
"""

import datetime
import json


# Relative column widths. Narrow columns stay narrow (Test ID, Status,
# Seconds); the text-heavy ones get the remaining space.
MAIN_COLS = [
    ("Test ID", 0.085),
    ("Test Case", 0.235),
    ("Expected", 0.165),
    ("Status", 0.055),
    ("Actual", 0.235),
    ("Note", 0.185),
    ("Sec", 0.040),
]

FAIL_COLS = [
    ("Test ID", 0.085),
    ("Test Case", 0.235),
    ("Expected", 0.185),
    ("Actual", 0.245),
    ("Note", 0.250),
]

TIMING_COLS = [
    ("Test ID", 0.095),
    ("Test Case", 0.315),
    ("Measured Result", 0.520),
    ("Sec", 0.070),
]

SKIP_EXPLANATION = (
    "SKIPPED means the case was <b>not attempted</b> - it is neither a pass nor a "
    "failure, and is never counted as a pass. Each skipped row states its blocker in "
    "the Note column. The usual reasons are: the case needs a node stopped at a precise "
    "moment mid-transfer (NODE-KILL); it needs deliberate database corruption "
    "(DB-SEED, which needs direct Postgres access); the fixture would cost hours to "
    "build (local minting creates one token per unit, so 100,000 RBT is ~25 minutes of "
    "minting per wallet, and the quorum needs the same again to pledge it); or the fleet "
    "is too small (e.g. a case wanting 40 concurrent senders when there are fewer pairs). "
    "Skipped cases are the coverage gap - treat this count as work outstanding."
)


def _fmt_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "{}s".format(seconds)
    return "{}m {}s".format(seconds // 60, seconds % 60)


def build_json(path, meta, rows):
    payload = {
        "run": meta,
        "summary": {
            "total": len(rows),
            "passed": sum(1 for r in rows if r["status"] == "PASS"),
            "failed": sum(1 for r in rows if r["status"] == "FAIL"),
            "skipped": sum(1 for r in rows if r["status"] == "SKIP"),
            "failed_ids": [r["test_id"] for r in rows if r["status"] == "FAIL"],
            "skipped_ids": [r["test_id"] for r in rows if r["status"] == "SKIP"],
        },
        "results": rows,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def build_pdf(path, title, meta, rows, timing_ids):
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import landscape, A3
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                         Paragraph, Spacer, PageBreak)
    except ImportError:
        return False

    styles = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=styles["BodyText"], fontSize=7, leading=8.5)
    cell_small = ParagraphStyle("cell_small", parent=cell, fontSize=6.5, leading=8)
    hdr = ParagraphStyle("hdr", parent=styles["BodyText"], fontSize=7.5, leading=9,
                          textColor=colors.white, fontName="Helvetica-Bold")
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=8.5, leading=11)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=12, spaceBefore=10,
                         spaceAfter=6)

    page_w = landscape(A3)[0] - 48
    doc = SimpleDocTemplate(path, pagesize=landscape(A3),
                             leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
    el = [Paragraph(title, styles["Title"])]

    passed = sum(1 for r in rows if r["status"] == "PASS")
    failed = sum(1 for r in rows if r["status"] == "FAIL")
    skipped = sum(1 for r in rows if r["status"] == "SKIP")
    total = len(rows)
    attempted = passed + failed
    rate = (100.0 * passed / attempted) if attempted else 0.0

    # ---- 1. SUMMARY -------------------------------------------------------
    el.append(Paragraph("Summary", h2))
    summary_rows = [
        ["Total cases", str(total)],
        ["Passed", str(passed)],
        ["Failed", str(failed)],
        ["Skipped (not attempted)", str(skipped)],
        ["Pass rate (of attempted)", "{:.1f}%  ({}/{})".format(rate, passed, attempted)],
        ["Run duration", _fmt_duration(meta.get("duration_seconds", 0))],
    ]
    t = Table([[Paragraph(a, body), Paragraph(b, body)] for a, b in summary_rows],
              colWidths=[page_w * 0.22, page_w * 0.18])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eeeeee")),
    ]))
    el.append(t)

    failed_ids = [r["test_id"] for r in rows if r["status"] == "FAIL"]
    el.append(Spacer(1, 8))
    if failed_ids:
        el.append(Paragraph(
            "<b>FAILED:</b> {}".format(", ".join(failed_ids)),
            ParagraphStyle("f", parent=body, textColor=colors.HexColor("#b00000"))))
    else:
        el.append(Paragraph("<b>No failures.</b>",
                            ParagraphStyle("g", parent=body,
                                            textColor=colors.HexColor("#006600"))))

    # ---- 2. RUN CONDITIONS ------------------------------------------------
    el.append(Paragraph("Run conditions", h2))
    el.append(Paragraph(
        "A result is only comparable against another run if the conditions match. "
        "These are the conditions this run actually executed under.", body))
    el.append(Spacer(1, 4))
    cond = [[Paragraph("<b>{}</b>".format(k), body), Paragraph(str(v), body)]
            for k, v in meta.get("conditions", [])]
    t = Table(cond, colWidths=[page_w * 0.20, page_w * 0.70])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eeeeee")),
    ]))
    el.append(t)

    # ---- 3. WHAT SKIPPED MEANS -------------------------------------------
    el.append(Paragraph("What \"skipped\" means", h2))
    el.append(Paragraph(SKIP_EXPLANATION, body))

    # ---- 4. ALL RESULTS ---------------------------------------------------
    el.append(PageBreak())
    el.append(Paragraph("All results ({} cases)".format(total), h2))
    el.append(_results_table(rows, MAIN_COLS, page_w, hdr, cell, cell_small, colors, Table,
                              TableStyle, Paragraph))

    # ---- 5. FAILURES ONLY -------------------------------------------------
    fails = [r for r in rows if r["status"] == "FAIL"]
    el.append(PageBreak())
    el.append(Paragraph("Failures ({})".format(len(fails)), h2))
    if not fails:
        el.append(Paragraph("None - every attempted case matched its expected result.", body))
    else:
        el.append(Paragraph(
            "Repeated from the table above so they can be worked through without "
            "scanning the full run.", body))
        el.append(Spacer(1, 4))
        data = [[Paragraph(h, hdr) for h, _w in FAIL_COLS]]
        for r in fails:
            data.append([
                Paragraph(r["test_id"], cell),
                Paragraph(r.get("case", ""), cell),
                Paragraph(r.get("expected", ""), cell),
                Paragraph(r.get("actual", ""), cell),
                Paragraph(r.get("note", ""), cell_small),
            ])
        t = Table(data, colWidths=[page_w * w for _h, w in FAIL_COLS], repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#b00000")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fdf0f0")]),
        ]))
        el.append(t)

    # ---- 6. TIMING RESULTS ------------------------------------------------
    timing = [r for r in rows if r["test_id"] in timing_ids and r["status"] != "SKIP"]
    el.append(PageBreak())
    el.append(Paragraph("Timing and limit results ({})".format(len(timing)), h2))
    el.append(Paragraph(
        "For these cases the measurement <b>is</b> the result - the catalogue asks them to "
        "record a limit, a duration, or a curve rather than pass/fail. These are the "
        "numbers a later run gets compared against; a limit dropping between releases is "
        "the regression signal, even when the case still reports PASS.", body))
    el.append(Spacer(1, 4))
    if not timing:
        el.append(Paragraph("None ran in this pass.", body))
    else:
        data = [[Paragraph(h, hdr) for h, _w in TIMING_COLS]]
        for r in timing:
            data.append([
                Paragraph(r["test_id"], cell),
                Paragraph(r.get("case", ""), cell),
                Paragraph(r.get("actual", ""), cell),
                Paragraph(str(r.get("seconds", "")), cell),
            ])
        t = Table(data, colWidths=[page_w * w for _h, w in TIMING_COLS], repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4e79")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef4fa")]),
        ]))
        el.append(t)

    doc.build(el)
    return True


def _results_table(rows, cols, page_w, hdr, cell, cell_small, colors, Table, TableStyle,
                    Paragraph):
    # Plain hex strings, NOT colors.HexColor(...).hexval(): that returns
    # '0xb00000', and reportlab's inline <font color> markup needs a leading
    # '#'. Getting it wrong raises "Invalid color value" on the first
    # non-PASS row rather than at build time.
    status_colour = {"PASS": "#006600", "FAIL": "#b00000", "SKIP": "#8a6d00"}
    data = [[Paragraph(h, hdr) for h, _w in cols]]
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#333333")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]
    for i, r in enumerate(rows, start=1):
        st = r["status"]
        data.append([
            Paragraph(r["test_id"], cell),
            Paragraph(r.get("case", ""), cell),
            Paragraph(r.get("expected", ""), cell),
            Paragraph('<font color="{}"><b>{}</b></font>'.format(
                status_colour.get(st, "#000000"), st), cell),
            Paragraph(r.get("actual", ""), cell),
            Paragraph(r.get("note", ""), cell_small),
            Paragraph(str(r.get("seconds", "")), cell),
        ])
        if st == "FAIL":
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fdf0f0")))
        elif st == "SKIP":
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fbf7e8")))
    t = Table(data, colWidths=[page_w * w for _h, w in cols], repeatRows=1)
    t.setStyle(TableStyle(style))
    return t
