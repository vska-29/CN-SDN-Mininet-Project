#!/usr/bin/env python3
"""
Local tests (no Mininet, no root needed). Run from the project folder:
    python3 tests/test_local.py -v

Covers: congestion detector, controller event API validation, player registration,
sequence numbers / loss / stale-packet handling, unregistered-sender filtering,
and the application -> controller notification path (register / leave events).
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import warnings
warnings.simplefilter("ignore", ResourceWarning)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from controller_logic import CongestionDetector, EventServer  # noqa: E402

GAME_PORT, API_PORT = 19999, 18080


class CongestionDetectorTests(unittest.TestCase):
    def test_detects_and_recovers_with_hysteresis(self):
        d = CongestionDetector(10_000_000, high=0.8, low=0.5, up_polls=2, down_polls=3)
        tx, t, changes = 0, 0.0, []
        def poll(bps):
            nonlocal tx, t
            tx += int(bps / 8); t += 1.0
            changes.append(d.update(tx, t))
        poll(0)                      # first sample only primes the detector
        poll(1_000_000)              # 10%  -> fine
        poll(9_500_000)              # 95%  -> 1st high poll, not yet congested
        self.assertFalse(d.congested)
        poll(9_800_000)              # 98%  -> 2nd high poll -> congested
        self.assertTrue(d.congested)
        poll(6_000_000)              # 60% (throttled flood): between low/high -> stays congested
        self.assertTrue(d.congested)
        poll(100_000); poll(100_000)
        self.assertTrue(d.congested)  # needs 3 low polls
        poll(100_000)
        self.assertFalse(d.congested)
        self.assertEqual(sum(1 for c in changes if c[2]), 2)  # exactly one up, one down

    def test_counter_reset_is_ignored(self):
        d = CongestionDetector(10_000_000)
        d.update(1_000_000, 0.0)
        util, cong, changed = d.update(10, 1.0)   # counter went backwards
        self.assertFalse(changed)


class EventApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seen = []
        cls.srv = EventServer("127.0.0.1", API_PORT + 1,
                              lambda e, ip, n: cls.seen.append((e, ip, n)) or {"players": 1},
                              lambda: {"mode": "test"})
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{API_PORT + 1}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def post(self, obj):
        req = urllib.request.Request(self.url + "/event", data=json.dumps(obj).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_valid_event_accepted(self):
        self.assertEqual(self.post({"event": "register", "ip": "10.0.0.1", "name": "p1"}), 200)
        self.assertIn(("register", "10.0.0.1", "p1"), self.seen)

    def test_invalid_events_rejected(self):
        self.assertEqual(self.post({"event": "register", "ip": "not-an-ip"}), 400)
        self.assertEqual(self.post({"event": "explode", "ip": "10.0.0.1"}), 400)
        self.assertEqual(self.post({"ip": "10.0.0.1"}), 400)

    def test_status_endpoint(self):
        with urllib.request.urlopen(self.url + "/status", timeout=2) as r:
            self.assertEqual(json.loads(r.read())["mode"], "test")


def run_server(duration, out, controller=None, idle=1.5):
    cmd = [sys.executable, os.path.join(ROOT, "game_server.py"), "--port", str(GAME_PORT),
           "--duration", str(duration), "--idle-timeout", str(idle), "--out", out]
    if controller:
        cmd += ["--controller", controller]
    return subprocess.Popen(cmd)


class GameProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, "server.json")

    def test_sequence_numbers_loss_and_stale_packets(self):
        srv = run_server(4, self.out)
        time.sleep(0.6)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.sendto(json.dumps({"t": "REG", "name": "x"}).encode(), ("127.0.0.1", GAME_PORT))
        self.assertEqual(json.loads(s.recvfrom(1024)[0])["t"], "ACK")
        for seq in (0, 1, 3, 2, 4):       # 2 arrives late (after 3) -> counted lost, then stale
            st = {"t": "STATE", "seq": seq, "ts": time.time(), "x": 1, "y": 2}
            s.sendto(json.dumps(st).encode(), ("127.0.0.1", GAME_PORT))
            time.sleep(0.02)
        srv.wait()
        p = list(json.load(open(self.out))["players"].values())[0]
        self.assertEqual(p["expected"], 5)
        self.assertEqual(p["received"], 4)
        self.assertEqual(p["lost"], 1)
        self.assertEqual(p["stale_dropped"], 1)

    def test_duplicate_registration_is_idempotent_and_strangers_ignored(self):
        srv = run_server(3, self.out)
        time.sleep(0.6)
        a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); a.settimeout(1)
        for _ in range(3):                # client retries must not create 3 players
            a.sendto(json.dumps({"t": "REG", "name": "a"}).encode(), ("127.0.0.1", GAME_PORT))
            a.recvfrom(1024)
        b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # never registers
        b.sendto(json.dumps({"t": "STATE", "seq": 0, "ts": time.time()}).encode(), ("127.0.0.1", GAME_PORT))
        b.sendto(b"\x00\x01garbage", ("127.0.0.1", GAME_PORT))
        srv.wait()
        stats = json.load(open(self.out))
        self.assertEqual(len(stats["players"]), 1)
        self.assertGreaterEqual(stats["ignored_datagrams"], 2)

    def test_end_to_end_with_controller_notifications(self):
        seen = []
        api = EventServer("127.0.0.1", API_PORT,
                          lambda e, ip, n: seen.append((e, ip, n)) or {}, lambda: {})
        threading.Thread(target=api.serve_forever, daemon=True).start()
        srv = run_server(8, self.out, controller=f"http://127.0.0.1:{API_PORT}")
        time.sleep(0.6)
        clients = [subprocess.Popen([sys.executable, os.path.join(ROOT, "game_client.py"),
                                     "--server", "127.0.0.1", "--port", str(GAME_PORT),
                                     "--name", f"p{i}", "--duration", "2",
                                     "--out", os.path.join(self.tmp, f"c{i}.json")])
                   for i in (1, 2)]
        for c in clients:
            self.assertEqual(c.wait(), 0)
        srv.wait()
        api.shutdown()
        events = [e for e, _, _ in seen]
        self.assertEqual(events.count("register"), 2)   # both players announced to the controller
        self.assertEqual(events.count("leave"), 2)      # and both reported as left afterwards
        stats = json.load(open(self.out))
        self.assertEqual(len(stats["players"]), 2)
        for p in stats["players"].values():
            self.assertEqual(p["lost"], 0)
        c1 = json.load(open(os.path.join(self.tmp, "c1.json")))
        self.assertGreater(c1["snaps_received"], 10)    # snapshots flow back to players


if __name__ == "__main__":
    unittest.main()
