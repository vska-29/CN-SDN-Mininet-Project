#!/usr/bin/env python3
"""
UDP game client.

- Registers with the server (retransmits REG on timeout, because UDP can lose packets).
- Sends STATE updates at a fixed rate with increasing sequence numbers.
- Receives world SNAP packets; uses their sequence numbers to count lost snapshots
  and to discard stale / out-of-order ones (a late snapshot is useless in a game).
- Writes its own statistics (snapshot latency, loss) to JSON when done.
"""
import argparse
import json
import random
import socket
import sys
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--name", default="player")
    ap.add_argument("--rate", type=float, default=30, help="updates per second")
    ap.add_argument("--duration", type=float, default=20)
    ap.add_argument("--out", default="client_stats.json")
    args = ap.parse_args()

    server = (args.server, args.port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # ---- registration with retransmission ----
    sock.settimeout(0.5)
    pid = None
    attempts = 0
    for attempts in range(1, 21):
        sock.sendto(json.dumps({"t": "REG", "name": args.name}).encode(), server)
        try:
            data, _ = sock.recvfrom(2048)
            msg = json.loads(data.decode())
            if msg.get("t") == "ACK":
                pid = msg["id"]
                break
        except (socket.timeout, ValueError):
            continue
    if pid is None:
        print(f"{args.name}: registration failed", file=sys.stderr)
        sys.exit(1)

    # ---- main loop: send updates, receive snapshots ----
    interval = 1.0 / args.rate
    start = time.time()
    next_send = start
    seq = 0
    x, y = random.uniform(0, 100), random.uniform(0, 100)
    last_snap = None
    snaps = snap_lost = snap_stale = 0
    snap_lat = []

    while time.time() - start < args.duration:
        now = time.time()
        if now >= next_send:
            x += random.uniform(-1, 1)
            y += random.uniform(-1, 1)
            state = {"t": "STATE", "id": pid, "seq": seq, "ts": time.time(), "x": x, "y": y}
            sock.sendto(json.dumps(state).encode(), server)
            seq += 1
            next_send += interval

        sock.settimeout(max(0.0005, min(interval, next_send - time.time())))
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            continue
        rx = time.time()
        try:
            msg = json.loads(data.decode())
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(msg, dict) or msg.get("t") != "SNAP":
            continue
        s = msg["seq"]
        if last_snap is None or s > last_snap:
            if last_snap is not None:
                snap_lost += s - last_snap - 1
            last_snap = s
            snaps += 1
            snap_lat.append((rx - msg["ts"]) * 1000.0)
        else:
            snap_stale += 1  # old snapshot: ignore

    with open(args.out, "w") as f:
        json.dump({
            "name": args.name, "id": pid, "sent": seq, "register_attempts": attempts,
            "snaps_received": snaps, "snaps_lost": snap_lost,
            "snaps_stale": snap_stale, "snap_lat_ms": snap_lat,
        }, f)


if __name__ == "__main__":
    main()
