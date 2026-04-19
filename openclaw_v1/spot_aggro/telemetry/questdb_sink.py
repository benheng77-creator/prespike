"""
QuestDB ILP sink — non-blocking, bounded, drop-and-count.

Contract (non-negotiable):
    - emit(table, tags, fields, ts_ns) NEVER blocks the caller.
    - One daemon thread drains a bounded queue over one persistent TCP
      socket. On socket error: reconnect with exponential backoff up to
      30s, keep draining.
    - Queue full → drop oldest, increment `dropped`. No backpressure
      into the engine EVER.
    - If QuestDB is unreachable forever, the engine keeps trading
      unaffected; emits silently drop.
"""
from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from typing import Any, Mapping, Optional

log = logging.getLogger("spot_aggro.telemetry.questdb")


class QuestDBSink:
    def __init__(
        self,
        host: str,
        port: int,
        capacity: int,
        *,
        connect_timeout_s: float = 2.0,
        send_timeout_s: float = 1.5,
    ) -> None:
        self._host = host
        self._port = port
        self._connect_timeout_s = float(connect_timeout_s)
        self._send_timeout_s = float(send_timeout_s)
        self._q: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=int(capacity))
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._run, name="spot-qdb-sink", daemon=True
        )
        self._stats = {"sent": 0, "dropped": 0, "reconnects": 0, "errors": 0,
                       "queued": 0}
        self._stats_lock = threading.Lock()
        self._started = False

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._worker.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        if not self._started:
            return
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        self._worker.join(timeout=float(timeout_s))
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        self._sock = None

    # --- public emit -------------------------------------------------------

    def emit(
        self,
        table: str,
        tags: Mapping[str, Any],
        fields: Mapping[str, Any],
        ts_ns: int,
    ) -> None:
        try:
            line = self._build_line(table, tags, fields, int(ts_ns))
        except Exception as exc:
            # Malformed row — count as dropped, never raise to caller.
            with self._stats_lock:
                self._stats["dropped"] += 1
            log.warning("emit build_line failed: %s", exc)
            return
        try:
            self._q.put_nowait(line)
            with self._stats_lock:
                self._stats["queued"] += 1
        except queue.Full:
            # Drop oldest, enqueue new. Counts both an oldest drop and a new add.
            try:
                self._q.get_nowait()
                with self._stats_lock:
                    self._stats["dropped"] += 1
                self._q.put_nowait(line)
                with self._stats_lock:
                    self._stats["queued"] += 1
            except queue.Empty:
                with self._stats_lock:
                    self._stats["dropped"] += 1

    def get_stats(self) -> dict:
        with self._stats_lock:
            s = dict(self._stats)
        s["qsize"] = self._q.qsize()
        s["capacity"] = self._q.maxsize
        s["connected"] = self._sock is not None
        return s

    # --- line builder ------------------------------------------------------

    @staticmethod
    def _esc_tag(v: str) -> str:
        return (
            str(v)
            .replace("\\", "\\\\")
            .replace(",", "\\,")
            .replace(" ", "\\ ")
            .replace("=", "\\=")
            .replace("\n", "")
        )

    @staticmethod
    def _esc_str(v: str) -> str:
        return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'

    @classmethod
    def _build_line(
        cls,
        table: str,
        tags: Mapping[str, Any],
        fields: Mapping[str, Any],
        ts_ns: int,
    ) -> bytes:
        parts = [table]
        for k, v in tags.items():
            if v is None or v == "":
                continue
            parts.append(f"{k}={cls._esc_tag(v)}")
        tag_part = ",".join(parts)

        f_parts = []
        for k, v in fields.items():
            if v is None:
                continue
            if isinstance(v, bool):
                f_parts.append(f"{k}={'t' if v else 'f'}")
            elif isinstance(v, int) and not isinstance(v, bool):
                f_parts.append(f"{k}={v}i")
            elif isinstance(v, float):
                f_parts.append(f"{k}={v}")
            else:
                f_parts.append(f"{k}={cls._esc_str(v)}")
        if not f_parts:
            # ILP requires at least one field; emit a meta field.
            f_parts.append("_ok=t")
        field_part = ",".join(f_parts)

        return f"{tag_part} {field_part} {ts_ns}\n".encode("utf-8")

    # --- worker + socket ---------------------------------------------------

    def _connect(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            try:
                s = socket.create_connection(
                    (self._host, self._port), timeout=self._connect_timeout_s
                )
                s.settimeout(self._send_timeout_s)
                self._sock = s
                with self._stats_lock:
                    self._stats["reconnects"] += 1
                log.info("questdb connected %s:%d", self._host, self._port)
                return
            except OSError as exc:
                log.warning("questdb connect failed: %s (retry in %.1fs)",
                            exc, backoff)
                self._stop.wait(backoff)
                backoff = min(30.0, backoff * 2)

    def _run(self) -> None:
        self._connect()
        while not self._stop.is_set():
            try:
                line = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                break
            if self._sock is None:
                self._connect()
                if self._sock is None:
                    # stop was signalled mid-connect
                    break
            try:
                self._sock.sendall(line)
                with self._stats_lock:
                    self._stats["sent"] += 1
            except OSError as exc:
                log.warning("questdb send failed: %s", exc)
                with self._stats_lock:
                    self._stats["errors"] += 1
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
                # Best-effort requeue; drop if queue already full.
                try:
                    self._q.put_nowait(line)
                except queue.Full:
                    with self._stats_lock:
                        self._stats["dropped"] += 1
