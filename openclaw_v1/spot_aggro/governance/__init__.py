"""
spot_aggro governance layer — permanent non-trading AI roles.

Two always-on layers live here:
  - wri/       Win-Rate Root-Cause Investigator (diagnostic)
  - governor/  Forensic Governor (report approval)

SPOT AGGRO ONLY. These modules do not place trades, do not size trades,
do not cancel trades, do not replace or weaken any execution gate, and
never consult account equity or working capital. They are read-only over
the trade log and forensic reports.
"""
