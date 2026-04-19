"""PDF report renderer — spot_aggro forensic reports.

Uses reportlab to generate professional PDF reports.
Each report is a self-contained document with:
- Executive summary
- Trade-by-trade analysis
- Issue breakdown
- Recommended fixes
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, HRFlowable,
)

from .models import ForensicReport


REPORT_DIR = Path(__file__).resolve().parent / "reports"
REPORT_DIR.mkdir(exist_ok=True)


def render_pdf(report: ForensicReport) -> str:
    """Render a ForensicReport to PDF. Returns the file path."""
    filename = f"forensic_{report.report_id}.pdf"
    filepath = REPORT_DIR / filename

    doc = SimpleDocTemplate(
        str(filepath), pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=15*mm, bottomMargin=15*mm,
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Title2", parent=styles["Title"],
                              fontSize=16, spaceAfter=6))
    styles.add(ParagraphStyle(name="SectionHead", parent=styles["Heading2"],
                              fontSize=12, textColor=colors.HexColor("#1877f2"),
                              spaceAfter=4, spaceBefore=10))
    styles.add(ParagraphStyle(name="Body2", parent=styles["BodyText"],
                              fontSize=9, leading=12))
    styles.add(ParagraphStyle(name="Small", parent=styles["BodyText"],
                              fontSize=8, leading=10, textColor=colors.grey))

    elements = []

    # ── Header ──
    elements.append(Paragraph("SPOT AGGRO — Forensic Accuracy Report", styles["Title2"]))
    period_start = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(report.period_start))
    period_end = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(report.period_end))
    generated = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(report.generated_at))
    elements.append(Paragraph(
        f"Period: {period_start} — {period_end}<br/>"
        f"Generated: {generated} | Report ID: {report.report_id}",
        styles["Small"],
    ))
    elements.append(Spacer(1, 8))
    elements.append(HRFlowable(width="100%", color=colors.HexColor("#30363d")))
    elements.append(Spacer(1, 8))

    # ── Executive Summary ──
    elements.append(Paragraph("Executive Summary", styles["SectionHead"]))
    elements.append(Paragraph(report.executive_summary or "No summary available.", styles["Body2"]))
    elements.append(Spacer(1, 6))

    # ── Stats Table ──
    elements.append(Paragraph("Performance Summary", styles["SectionHead"]))
    stats_data = [
        ["Metric", "Value"],
        ["Total Trades", str(report.total_trades)],
        ["Greens", str(report.greens)],
        ["Reds", str(report.reds)],
        ["Flat", str(report.flats)],
        ["Total PnL", f"${report.total_pnl:+.4f}"],
        ["Win Rate", f"{report.win_rate:.1%}"],
        ["Avg Win", f"${report.avg_win:+.4f}"],
        ["Avg Loss", f"${report.avg_loss:+.4f}"],
        ["Expectancy", f"${report.expectancy:+.4f}"],
    ]
    t = Table(stats_data, colWidths=[120, 120])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#161b22")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#30363d")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f8fa")]),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 8))

    # ── Forensic Counts ──
    elements.append(Paragraph("Forensic Findings", styles["SectionHead"]))
    forensic_data = [
        ["Finding", "Count"],
        ["False Positives", str(report.false_positives)],
        ["False Negatives (Missed)", str(report.false_negatives)],
        ["Ranking Mistakes", str(report.ranking_mistakes)],
        ["Threshold Mistakes", str(report.threshold_mistakes)],
        ["Poor Entries", str(report.poor_entries)],
        ["Poor Exits", str(report.poor_exits)],
    ]
    t2 = Table(forensic_data, colWidths=[160, 80])
    t2.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#161b22")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#30363d")),
    ]))
    elements.append(t2)
    elements.append(Spacer(1, 8))

    # ── Top Issues ──
    if report.top_issues:
        elements.append(Paragraph("Top Issues", styles["SectionHead"]))
        for i, issue in enumerate(report.top_issues[:5], 1):
            elements.append(Paragraph(f"{i}. {issue}", styles["Body2"]))
        elements.append(Spacer(1, 6))

    # ── Recommended Fixes ──
    if report.recommended_fixes:
        elements.append(Paragraph("Recommended Fixes", styles["SectionHead"]))
        for i, fix in enumerate(report.recommended_fixes[:5], 1):
            elements.append(Paragraph(f"{i}. {fix}", styles["Body2"]))
        elements.append(Spacer(1, 6))

    # ── Agent Analysis ──
    for label, text in [
        ("Structure Analysis", report.structure_analysis),
        ("Quant Analysis", report.quant_analysis),
        ("Liquidity Analysis", report.liquidity_analysis),
        ("Regime Analysis", report.regime_analysis),
    ]:
        if text:
            elements.append(Paragraph(label, styles["SectionHead"]))
            elements.append(Paragraph(text[:600], styles["Body2"]))
            elements.append(Spacer(1, 4))

    # ── Trade-by-Trade ──
    if report.trade_reviews:
        elements.append(PageBreak())
        elements.append(Paragraph("Trade-by-Trade Review", styles["SectionHead"]))
        for tr in report.trade_reviews[:30]:
            color = "#10b981" if tr.verdict == "GREEN" else "#ef4444" if tr.verdict == "RED" else "#8b949e"
            elements.append(Paragraph(
                f'<font color="{color}"><b>{tr.verdict}</b></font> '
                f'{tr.symbol} [{tr.tier}] ${tr.notional_usd:.2f} '
                f'ret={tr.ret_pct:+.2%} PnL=${tr.pnl_usd:+.4f} '
                f'| quality={tr.quality} | exit={tr.exit_reason}',
                styles["Body2"],
            ))
            if tr.root_cause:
                elements.append(Paragraph(
                    f'&nbsp;&nbsp;&nbsp;Root cause: {tr.root_cause[:120]}',
                    styles["Small"],
                ))
            if tr.fix:
                elements.append(Paragraph(
                    f'&nbsp;&nbsp;&nbsp;Fix: {tr.fix[:120]}',
                    styles["Small"],
                ))
            elements.append(Spacer(1, 3))

    # ── Footer ──
    elements.append(Spacer(1, 20))
    elements.append(HRFlowable(width="100%", color=colors.HexColor("#30363d")))
    elements.append(Paragraph(
        f"SPOT AGGRO Forensic Report | LLM cost: ${report.cost_usd:.4f} | "
        f"Agents OK: {report.agents_ok}/5",
        styles["Small"],
    ))

    doc.build(elements)
    return str(filepath)
