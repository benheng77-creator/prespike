"""
spot_forensic.v1 PDF renderer.

Matches the human-facing template in the principal spec §7.
One PDF per report_id, persisted under forensic_v2/reports/ and indexed
back on spot_forensic_runs.pdf_path.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
)


REPORT_DIR = Path(__file__).resolve().parent / "reports"
REPORT_DIR.mkdir(exist_ok=True)


_CONF_COLORS = {
    "PROVEN":       colors.HexColor("#16a34a"),   # green
    "LIKELY":       colors.HexColor("#eab308"),   # amber
    "WEAK":         colors.HexColor("#f97316"),   # orange
    "UNVERIFIABLE": colors.HexColor("#6b7280"),   # grey
}

_VERDICT_COLORS = {
    "NEEDS_HALT":        colors.HexColor("#dc2626"),
    "NEEDS_TUNING":      colors.HexColor("#eab308"),
    "ACCEPTABLE":        colors.HexColor("#16a34a"),
    "INSUFFICIENT_DATA": colors.HexColor("#6b7280"),
}


def render_pdf(report_row: dict) -> str:
    """Render a stored spot_forensic_runs row to a PDF and return the path.

    `report_row` is the dict returned by forensic_v2.persistence.get_run —
    it has the top-level columns plus `payload` (the parsed payload_json).
    """
    report_id = report_row.get("report_id")
    if not report_id:
        raise ValueError("report_row missing report_id")

    filepath = REPORT_DIR / f"forensic_v2_{report_id}.pdf"

    payload = report_row.get("payload") or {}
    adj = payload.get("adjudication") or {}
    meta = payload.get("specialist_meta") or {}
    outputs = payload.get("specialist_outputs") or {}

    doc = SimpleDocTemplate(
        str(filepath), pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
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
    styles.add(ParagraphStyle(name="Mono", parent=styles["BodyText"],
                              fontName="Courier", fontSize=8, leading=10))

    story: list = []

    # ------- Header -------------------------------------------------------
    story.append(Paragraph("SPOT AGGRO — Forensic Truth Report (v2)",
                           styles["Title2"]))
    generated_ts = report_row.get("generated_ts_ms") or int(time.time() * 1000)
    ws = report_row.get("window_start_ts_ms") or 0
    we = report_row.get("window_end_ts_ms") or 0
    window_h = (we - ws) / 3_600_000 if (we and ws) else 0
    gen_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(generated_ts / 1000))
    win_str = (
        f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(ws/1000))} → "
        f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(we/1000))}  "
        f"({window_h:.1f}h)"
    )
    story.append(Paragraph(
        f"ID: <font face='Courier'>{report_id}</font><br/>"
        f"Window: {win_str}<br/>"
        f"Generated: {gen_str}",
        styles["Small"],
    ))
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#30363d")))
    story.append(Spacer(1, 6))

    # ------- Quorum banner ------------------------------------------------
    q_ret = int(report_row.get("quorum_returned") or 0)
    q_req = int(report_row.get("quorum_required") or 4)
    degraded = bool(report_row.get("degraded_confidence"))
    banner_color = (
        colors.HexColor("#dc2626") if q_ret == 0
        else colors.HexColor("#eab308") if degraded
        else colors.HexColor("#16a34a")
    )
    banner_text = (
        f"QUORUM: {q_ret} / {q_req} specialists returned  —  "
        f"{'DEGRADED CONFIDENCE' if degraded else 'FULL CONFIDENCE'}"
    )
    story.append(_banner_table(banner_text, banner_color))

    # ------- Overall verdict ----------------------------------------------
    verdict = adj.get("overall_verdict") or report_row.get("overall_verdict") or "INSUFFICIENT_DATA"
    verdict_color = _VERDICT_COLORS.get(verdict, colors.HexColor("#6b7280"))
    story.append(Spacer(1, 4))
    story.append(_banner_table(f"OVERALL VERDICT: {verdict}", verdict_color))
    if adj.get("verdict_justification"):
        story.append(Paragraph(adj["verdict_justification"], styles["Body2"]))
    story.append(Spacer(1, 6))

    # ------- Headline metrics --------------------------------------------
    story.append(Paragraph("Headline Metrics (window)", styles["SectionHead"]))
    hm = adj.get("headline_metrics") or {}
    # Prefer the DB row (authoritative, numeric) over LLM echo.
    metrics_rows = [
        ["n_trades_closed", _fmt(report_row.get("n_trades_in_window"))],
        ["expectancy_usd", _fmt_money(report_row.get("expectancy_usd"))],
        ["win_rate", _fmt_pct(report_row.get("win_rate"))],
        ["profit_factor", _fmt(report_row.get("profit_factor"), prec=3)],
        ["friction_ratio", _fmt(report_row.get("friction_ratio"), prec=4)],
    ]
    phy = outputs.get("physics") or {}
    phy_overall = (phy.get("computed_metrics") or {}).get("overall") or phy.get("metrics") or {}
    if phy_overall.get("avg_win_usd") is not None:
        metrics_rows.append(["avg_win_usd", _fmt_money(phy_overall.get("avg_win_usd"))])
    if phy_overall.get("avg_loss_usd") is not None:
        metrics_rows.append(["avg_loss_usd", _fmt_money(phy_overall.get("avg_loss_usd"))])
    if phy_overall.get("ticket_floor_violations") is not None:
        metrics_rows.append(["ticket_floor_violations",
                             _fmt(phy_overall.get("ticket_floor_violations"))])
    story.append(_kv_table(metrics_rows))
    story.append(Spacer(1, 8))

    # ------- Ranked root causes ------------------------------------------
    story.append(Paragraph("Ranked Root Causes", styles["SectionHead"]))
    rrcs = adj.get("ranked_root_causes") or []
    if not rrcs:
        story.append(Paragraph("None. (insufficient evidence or all checks clean.)",
                               styles["Body2"]))
    for rc in rrcs[:7]:
        rank = rc.get("rank", "?")
        conf = (rc.get("confidence") or "UNVERIFIABLE").upper()
        cause = rc.get("root_cause") or "(no description)"
        evid = ", ".join(rc.get("evidence_ids") or []) or "(no evidence_ids)"
        fix = rc.get("fix_recommendation") or {}
        conf_col = _CONF_COLORS.get(conf, colors.HexColor("#6b7280"))
        story.append(_conf_header(f"#{rank}  {conf}", conf_col))
        story.append(Paragraph(_esc(cause), styles["Body2"]))
        story.append(Paragraph(f"<i>evidence: {_esc(evid)}</i>", styles["Small"]))
        if fix.get("lever"):
            fix_text = (
                f"<b>FIX</b>: <font face='Courier'>{_esc(fix.get('lever'))}</font>"
            )
            if fix.get("config_key"):
                fix_text += f" · key=<font face='Courier'>{_esc(fix['config_key'])}</font>"
            if fix.get("current_value") is not None or fix.get("proposed_value") is not None:
                fix_text += (
                    f" · {_esc(fix.get('current_value'))} → {_esc(fix.get('proposed_value'))}"
                )
            story.append(Paragraph(fix_text, styles["Body2"]))
            if fix.get("expected_effect"):
                story.append(Paragraph(
                    f"<i>effect:</i> {_esc(fix['expected_effect'])}",
                    styles["Small"],
                ))
        story.append(Spacer(1, 4))

    # ------- Anti-patterns ------------------------------------------------
    aps = adj.get("anti_patterns") or []
    if aps:
        story.append(Paragraph("Anti-Patterns", styles["SectionHead"]))
        ap_rows = [["Pattern", "Trade Count", "Trade IDs (first 5)"]]
        for p in aps:
            ids = p.get("trade_ids") or []
            ids_str = ", ".join(str(x) for x in ids[:5]) + (
                f"  (+{len(ids)-5} more)" if len(ids) > 5 else ""
            )
            ap_rows.append([p.get("pattern", "?"), str(p.get("trade_count", 0)), ids_str])
        story.append(_data_table(ap_rows, col_widths=[55 * mm, 25 * mm, 90 * mm]))
        story.append(Spacer(1, 6))

    # ------- L3 highlights: SPI collapses + transitions ------------------
    reg = outputs.get("regime") or {}
    reg_metrics = reg.get("computed_metrics") or {}
    spi_clusters = reg_metrics.get("spi_collapse_clusters") or []
    transition_failures = reg_metrics.get("transition_failures") or []
    if spi_clusters or transition_failures:
        story.append(Paragraph("Regime Pressure (L3)", styles["SectionHead"]))
        if spi_clusters:
            rows = [["Cluster", "Trades", "Reasons"]]
            for c in spi_clusters[:6]:
                rows.append([
                    c.get("cluster_id", "?"),
                    str(c.get("trade_count", 0)),
                    ", ".join(f"{k}:{v}" for k, v in (c.get("exit_reasons") or {}).items()),
                ])
            story.append(Paragraph("<b>SPI-collapse clusters</b>", styles["Body2"]))
            story.append(_data_table(rows, col_widths=[25 * mm, 20 * mm, 125 * mm]))
            story.append(Spacer(1, 4))
        if transition_failures:
            story.append(Paragraph(
                f"<b>Regime-transition failures:</b> {len(transition_failures)} trades lost money "
                f"after regime flipped between entry and exit.",
                styles["Body2"],
            ))
        story.append(Spacer(1, 6))

    # ------- Evidence gaps ------------------------------------------------
    gaps = adj.get("evidence_gaps") or []
    if gaps:
        story.append(Paragraph("Evidence Gaps", styles["SectionHead"]))
        for g in gaps:
            story.append(Paragraph(f"• {_esc(g)}", styles["Body2"]))
        story.append(Spacer(1, 6))

    # ------- Cost + latency ----------------------------------------------
    story.append(Paragraph("Cost + Latency", styles["SectionHead"]))
    cost_rows = [
        ["total cost",    _fmt_money(report_row.get("cost_usd_total"), prec=6)],
        ["total latency", f"{(report_row.get('latency_ms_total') or 0) / 1000:.1f} s"],
    ]
    story.append(_kv_table(cost_rows))
    if meta:
        per_role = [["Role", "OK", "Cost", "Latency", "Retries", "Error"]]
        for role_key in ("L1", "L2", "L3", "L4", "L5"):
            m = meta.get(role_key)
            if not m:
                continue
            per_role.append([
                role_key,
                "✓" if m.get("ok") else "✗",
                _fmt_money(m.get("cost_usd"), prec=6),
                f"{(m.get('latency_ms') or 0) / 1000:.1f}s",
                str(m.get("retries", 0)),
                (m.get("error") or "-")[:40],
            ])
        story.append(Spacer(1, 4))
        story.append(_data_table(
            per_role,
            col_widths=[15 * mm, 12 * mm, 22 * mm, 20 * mm, 18 * mm, 83 * mm],
        ))

    # ------- Footer -------------------------------------------------------
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#30363d")))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "Reproducibility: spot_forensic.v1 schema lock.  "
        f"Raw JSON: /spot_aggro/forensic_v2/{report_id}",
        styles["Small"],
    ))

    doc.build(story)
    return str(filepath)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _banner_table(text: str, color) -> Table:
    t = Table([[text]], colWidths=[180 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), color),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _conf_header(text: str, color) -> Table:
    t = Table([[text]], colWidths=[45 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), color),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return t


def _kv_table(rows: list[list]) -> Table:
    data = [[k, str(v) if v is not None else "-"] for k, v in rows]
    t = Table(data, colWidths=[50 * mm, 80 * mm])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Courier"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d0d7de")),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return t


def _data_table(rows: list[list], col_widths: list = None) -> Table:
    t = Table(rows, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef0f3")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d0d7de")),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t


def _fmt(v: Any, prec: int = 0) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{prec}f}" if prec else f"{v:g}"
    return str(v)


def _fmt_money(v: Any, prec: int = 2) -> str:
    if v is None:
        return "-"
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(fv) < 0.0001 and prec == 2:
        prec = 6
    sign = "-" if fv < 0 else ""
    return f"{sign}${abs(fv):.{prec}f}"


def _fmt_pct(v: Any) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(v)


def _esc(s: Any) -> str:
    if s is None:
        return "-"
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
