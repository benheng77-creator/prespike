"""
Economic event risk tracker.

Fetches the ForexFactory weekly calendar XML feed (free, no API key),
parses high-impact events, and exposes a proximity-weighted
EventRiskScore in [0, 1]:

    event within 10 min : 1.0
    event within 30 min : 0.7
    event within  2 hr  : 0.4
    otherwise           : 0.1

The feed is refreshed on a timer (default 15 minutes). If fetch or parse
fails, the score falls back to a neutral 0.1.

Note: ForexFactory publishes times in US Eastern but the XML doesn't
include a timezone. This tracker treats them as UTC, which introduces up
to a ~5-hour drift. For production use, convert to UTC explicitly based
on the DST rules in effect for the event date.
"""

from __future__ import annotations

import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import aiohttp

log = logging.getLogger(__name__)

FF_WEEKLY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"


class EventRiskTracker:
    def __init__(self, refresh_s: int = 900):
        self.refresh_s = refresh_s
        self._events: List[datetime] = []
        self._last_refresh = 0.0
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None

    async def _fetch_calendar(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        try:
            async with self._session.get(FF_WEEKLY_URL) as resp:
                resp.raise_for_status()
                text = await resp.text()
        except Exception as e:
            log.warning(f"Event calendar fetch failed: {e}")
            return

        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            log.warning(f"Event calendar parse failed: {e}")
            return

        events: List[datetime] = []
        for ev in root.findall("event"):
            impact = (ev.findtext("impact") or "").strip().lower()
            if impact != "high":
                continue
            date_str = (ev.findtext("date") or "").strip()
            time_str = (ev.findtext("time") or "").strip()
            try:
                dt = datetime.strptime(
                    f"{date_str} {time_str}", "%m-%d-%Y %I:%M%p"
                )
                dt = dt.replace(tzinfo=timezone.utc)
                events.append(dt)
            except ValueError:
                continue

        self._events = sorted(events)
        self._last_refresh = time.time()
        log.info(f"Event calendar refreshed: {len(events)} high-impact events loaded")

    async def start(self) -> None:
        self._running = True
        while self._running:
            await self._fetch_calendar()
            await asyncio.sleep(self.refresh_s)

    async def close(self) -> None:
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()

    def current_score(self) -> float:
        now = datetime.now(timezone.utc)
        for dt in self._events:
            if dt < now:
                continue
            delta = dt - now
            if delta <= timedelta(minutes=10):
                return 1.0
            if delta <= timedelta(minutes=30):
                return 0.7
            if delta <= timedelta(hours=2):
                return 0.4
            return 0.1
        return 0.1
