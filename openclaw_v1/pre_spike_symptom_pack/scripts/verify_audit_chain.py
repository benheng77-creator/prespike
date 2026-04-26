#!/usr/bin/env python3
"""Verify audit log SHA-256 chain integrity."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# When invoked as a top-level script (`python scripts/verify_audit_chain.py`),
# the pack is not on sys.path. Insert the pack root so we can import the
# pack's services as a package.
_PACK_ROOT = Path(__file__).resolve().parent.parent
if str(_PACK_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_PACK_ROOT.parent))

from pre_spike_symptom_pack.services.audit_log import AuditLog  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-dir", required=True)
    args = ap.parse_args()
    ok, errors = AuditLog.verify_chain(args.log_dir)
    if ok:
        print("audit chain OK")
        sys.exit(0)
    print("audit chain BROKEN:", file=sys.stderr)
    for e in errors:
        print(f"  - {e}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
