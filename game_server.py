#!/usr/bin/env python3
"""
UDP game server.

Application protocol (JSON over UDP):
  REG   player -> server   register (retried by the client until ACKed)
  ACK   server -> player   registration accepted, carries the player id
  STATE player -> server   position update with per-player sequence number + timestamp
  SNAP  server -> players  world snapshot with a global sequence number + timestamp

What the server does
  * registers multiple players (idempotent, so client retries are safe)
  * uses sequence numbers to count lost updates and to drop stale/out-of-order ones
  * measures one-way latency and jitter (mean |delta| and RFC 3550 smoothed)
  * SDN integration: if --controller is given, it tells the SDN controller when a
    player registers or goes idle/leaves, so the controller can install or remove
    that player's priority flows (application events -> network behaviour).
  * ignores datagrams from unregistered senders and malformed packets
  * writes all measurements to a JSON file when the run ends

One-way latency uses time.time() on both ends; this is valid in Mininet because all
hosts share the same machine clock.
"""
import argparse
import json
import socket
import threading
import time
import urllib.request


def notify_controller(url, event, ip, name, log, timeout=0.5, attempts=5):
    """POST an application event to the SDN controller, retrying a few times. Never raises."""
    body = json.dumps({"event": event, "ip": ip, "name": name}).encode()
    ok = False
    tries = 0
    for tries in range(1, attempts + 1):
        req = urllib.request.Request(url.rstrip("/") + "/event", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ok = resp.status == 200
        except Exception:
            ok = False
        if ok:
            break
        time.sleep(0.2)
    log.append({"t": round(time.time(), 3), "event": event, "ip": ip, "name": name,
                "ok": ok, "attempts": tries})
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--duration", type=float, default=30, help="seconds to run")
    ap.add_argument("--tick", type=float, default=20, help="snapshot rate (Hz)")
    ap.add_argument("--idle-timeout", type=float, default=3.0,
                    help="seconds without updates before a player counts as left")
    ap.add_argument("--controller", default=None,
                    help="SDN controller event API, e.g. http://10.0.0.254:8080")
    ap.add_argument("--out", default="server_stats.json")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(0.005)

    players = {}     # addr -> state dict
    world = {}       # player id -> [x, y]
    events = []      # log of notifications sent to the controller
    next_id = 1
    snap_seq = 0
    ignored = 0      # malformed / unregistered datagrams
    start = time.time()
    next_snap = start
    next_idle_check = start + 0.5
    snap_interval = 1.0 / args.tick

    def notify(event, p, wait=0.0):
        """Tell the controller about an event; optionally wait a bit for it to finish."""
        if not args.controller:
            return
        t = threading.Thread(target=notify_controller,
                             args=(args.controller, event, p["ip"], p["name"], events), daemon=True)
        t.start()
        if wait:
            t.join(wait)

    while time.time() - start < args.duration:
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            data = None

        if data:
            now = time.time()
            try:
                msg = json.loads(data.decode())
            except (ValueError, UnicodeDecodeError):
                msg = None
            if not isinstance(msg, dict):
                ignored += 1
                msg = {}
            kind = msg.get("t")

            if kind == "REG":
                if addr not in players:
                    players[addr] = {
                        "id": next_id, "ip": addr[0], "name": str(msg.get("name", f"p{next_id}"))[:32],
                        "active": True, "last_seen": now,
                        "first_seq": None, "last_seq": None,
                        "received": 0, "gaps": 0, "stale": 0,
                        "lat": [], "prev_lat": None,
                        "jitter_rfc": 0.0, "jitter_sum": 0.0, "jitter_n": 0,
                    }
                    world[next_id] = [0.0, 0.0]
                    next_id += 1
                    # Tell the controller BEFORE acking, so the player's priority
                    # flows are in place by the time it starts sending updates.
                    notify("register", players[addr], wait=0.3)
                p = players[addr]
                p["last_seen"] = now
                if not p["active"]:
                    p["active"] = True
                    notify("register", p, wait=0.3)
                sock.sendto(json.dumps({"t": "ACK", "id": p["id"]}).encode(), addr)

            elif kind == "STATE" and addr in players and "seq" in msg:
                p = players[addr]
                p["last_seen"] = now
                if not p["active"]:
                    p["active"] = True
                    notify("register", p)
                seq = msg["seq"]
                lat = (now - msg["ts"]) * 1000.0  # ms

                if p["last_seq"] is None:
                    p["first_seq"] = p["last_seq"] = seq
                    in_order = True
                elif seq > p["last_seq"]:
                    p["gaps"] += seq - p["last_seq"] - 1  # lost in between
                    p["last_seq"] = seq
                    in_order = True
                else:
                    p["stale"] += 1  # old/duplicate update: ignore it
                    in_order = False

                if in_order:
                    p["received"] += 1
                    world[p["id"]] = [msg.get("x", 0.0), msg.get("y", 0.0)]
                    p["lat"].append(lat)
                    if p["prev_lat"] is not None:
                        d = abs(lat - p["prev_lat"])
                        p["jitter_rfc"] += (d - p["jitter_rfc"]) / 16.0  # RFC 3550
                        p["jitter_sum"] += d
                        p["jitter_n"] += 1
                    p["prev_lat"] = lat
            else:
                ignored += 1  # unknown type or unregistered sender

        now = time.time()
        # periodic world snapshot to every active player
        if now >= next_snap and players:
            snap = {"t": "SNAP", "seq": snap_seq, "ts": now,
                    "players": {str(k): v for k, v in world.items()}}
            payload = json.dumps(snap).encode()
            for a, p in players.items():
                if p["active"]:
                    sock.sendto(payload, a)
            snap_seq += 1
            next_snap += snap_interval

        # idle detection: a silent player has left -> controller removes its flows
        if now >= next_idle_check:
            next_idle_check = now + 0.5
            for p in players.values():
                if p["active"] and now - p["last_seen"] > args.idle_timeout:
                    p["active"] = False
                    notify("leave", p)

    # clean up: tell the controller about everyone still active
    for p in players.values():
        if p["active"]:
            notify("leave", p, wait=0.5)
    time.sleep(0.2)

    stats = {"snapshots_sent": snap_seq, "ignored_datagrams": ignored,
             "events": list(events), "players": {}}
    for p in players.values():
        expected = (p["last_seq"] - p["first_seq"] + 1) if p["last_seq"] is not None else 0
        stats["players"][p["id"]] = {
            "name": p["name"], "ip": p["ip"],
            "expected": expected, "received": p["received"],
            "lost": p["gaps"], "stale_dropped": p["stale"],
            "lat_ms": p["lat"],
            "jitter_rfc_ms": p["jitter_rfc"],
            "jitter_mean_ms": (p["jitter_sum"] / p["jitter_n"]) if p["jitter_n"] else 0.0,
        }
    with open(args.out, "w") as f:
        json.dump(stats, f)


if __name__ == "__main__":
    main()
