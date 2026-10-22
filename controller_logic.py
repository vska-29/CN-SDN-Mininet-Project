"""
Controller logic that does not depend on Ryu, so it can be unit-tested anywhere.

1. EventServer: a tiny HTTP API the game server uses to tell the SDN controller
   about application events (player registered / player left).
       POST /event   {"event": "register"|"leave", "ip": "10.0.0.1", "name": "p1"}
       GET  /status  -> JSON with the controller's current view
2. CongestionDetector: turns periodic port byte counters into a
   "link is congested" decision, with hysteresis so it does not flap.
"""
import ipaddress
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

VALID_EVENTS = ("register", "leave")


class CongestionDetector:
    """
    Feed it the cumulative tx_bytes of the bottleneck port once per poll.
    Congested  : utilization >= high for `up_polls` consecutive polls.
    Recovered  : utilization <= low  for `down_polls` consecutive polls.
    """

    def __init__(self, capacity_bps, high=0.8, low=0.5, up_polls=2, down_polls=3):
        self.capacity = float(capacity_bps)
        self.high, self.low = high, low
        self.up_polls, self.down_polls = up_polls, down_polls
        self.prev = None          # (tx_bytes, time)
        self.up = self.down = 0
        self.congested = False
        self.utilization = 0.0

    def update(self, tx_bytes, now):
        """Returns (utilization 0..1+, congested, changed)."""
        if self.prev is None or tx_bytes < self.prev[0] or now <= self.prev[1]:
            self.prev = (tx_bytes, now)   # first sample or counter reset
            return self.utilization, self.congested, False
        dt = now - self.prev[1]
        self.utilization = (tx_bytes - self.prev[0]) * 8.0 / dt / self.capacity
        self.prev = (tx_bytes, now)

        if self.utilization >= self.high:
            self.up, self.down = self.up + 1, 0
        elif self.utilization <= self.low:
            self.up, self.down = 0, self.down + 1
        else:
            self.up = self.down = 0   # in between: keep current state

        changed = False
        if not self.congested and self.up >= self.up_polls:
            self.congested, changed = True, True
        elif self.congested and self.down >= self.down_polls:
            self.congested, changed = False, True
        return self.utilization, self.congested, changed


class EventServer:
    """HTTP server wrapper. on_event(event, ip, name) -> dict; get_status() -> dict."""

    def __init__(self, host, port, on_event, get_status):
        on_event_cb, status_cb = on_event, get_status

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/status":
                    self._send(200, status_cb())
                else:
                    self._send(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                if self.path != "/event":
                    return self._send(404, {"ok": False, "error": "not found"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    msg = json.loads(self.rfile.read(length).decode())
                    event = msg["event"]
                    ip = str(ipaddress.IPv4Address(msg["ip"]))  # validates the address
                    name = str(msg.get("name", ""))[:32]
                    if event not in VALID_EVENTS:
                        raise ValueError("bad event")
                except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                    return self._send(400, {"ok": False, "error": "bad request"})
                result = on_event_cb(event, ip, name)
                self._send(200, {"ok": True, **(result or {})})

            def log_message(self, *args):  # keep the console quiet
                pass

        self.httpd = HTTPServer((host, port), Handler)

    def serve_forever(self):
        self.httpd.serve_forever()

    def shutdown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
