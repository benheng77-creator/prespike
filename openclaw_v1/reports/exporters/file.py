"""File exporter: JSON + Markdown are already written by generate_eod.
This exporter just verifies the paths and returns their existence state
so the scheduler can confirm delivery."""

from __future__ import annotations

import json
from pathlib import Path


def export_to_file(report: dict) -> dict:
    json_path = report.get("json_path")
    md_path = report.get("md_path")
    status = {
        "json_ok": bool(json_path and Path(json_path).exists()),
        "md_ok": bool(md_path and Path(md_path).exists()),
        "json_path": json_path,
        "md_path": md_path,
    }
    return status
