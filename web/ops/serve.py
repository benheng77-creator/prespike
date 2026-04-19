"""
Thin wrapper around http.server that sends no-cache headers on every response,
so dashboard changes land without users having to hard-refresh.
"""

from __future__ import annotations

import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class NoCacheHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:
        # Quieter than the default — only log errors
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(format, *args)


def main() -> int:
    host = "127.0.0.1"
    port = 5173
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    with ThreadingHTTPServer((host, port), NoCacheHandler) as srv:
        print(f"[dash] serving http://{host}:{port}/  (no-cache)", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
