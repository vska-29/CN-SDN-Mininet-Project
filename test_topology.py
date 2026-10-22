#!/usr/bin/env python3
r"""
Mininet topology + experiment runner.

Topology (the s1-s2 trunk is the 10 Mbps bottleneck, shaped by OVS HTB queues):

   p1 --\                                    /-- srv  (game server)
   p2 ---- s1 ====== (bottleneck) ====== s2 ---- bg2  (iperf sink)
   p3 --/    |                               \-- root (controller event API, 10.0.0.254)
            bg1  (background-traffic source and "attacker")

Three phases are run for one controller mode per invocation:
  normal     only the game
  congested  game + 15 Mbps UDP flood bg1 -> bg2 across the bottleneck
  abuse      game + 15 Mbps UDP flood bg1 -> srv:9999 (an unregistered host
             sending traffic to the game port to steal priority)

Usage (the controller must already be running with the matching GAME_QOS):
    sudo python3 test_topology.py --mode dynamic
"""
import argparse
import json
import os
import subprocess
import time

from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import Node, OVSSwitch, RemoteController

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
PORT = 9999
BOTTLENECK = 10_000_000  # bits/s
SRV_IP, BG1_IP, BG2_IP, CTRL_IP = "10.0.0.100", "10.0.0.101", "10.0.0.102", "10.0.0.254"
CTRL_URL = f"http://{CTRL_IP}:8080"
NULL = subprocess.DEVNULL
PHASES = ("normal", "congested", "abuse")


def build():
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)
    s1 = net.addSwitch("s1", protocols="OpenFlow13")
    s2 = net.addSwitch("s2", protocols="OpenFlow13")
    for i in (1, 2, 3):
        p = net.addHost(f"p{i}", ip=f"10.0.0.{i}")
        # small delay + 1% random loss on access links to exercise loss handling
        net.addLink(p, s1, delay="2ms", loss=1)
    net.addLink(net.addHost("bg1", ip=BG1_IP), s1)
    net.addLink(net.addHost("srv", ip=SRV_IP), s2, delay="2ms")
    net.addLink(net.addHost("bg2", ip=BG2_IP), s2)
    # trunk with named interfaces so OVS QoS can be attached to them
    net.addLink(s1, s2, intfName1="s1-trunk", intfName2="s2-trunk")
    # Put the controller's event API on the data network: a node in the root
    # namespace (where Ryu runs) plugged into s2, so the game server can reach it.
    root = Node("root", inNamespace=False)
    link = net.addLink(root, s2)
    root.setIP(f"{CTRL_IP}/24", intf=link.intf1)
    return net


def setup_queues(net):
    """HTB on both trunk ports: q1 game (highest prio), q0 default, q2 throttled best-effort."""
    os.system("ovs-vsctl --all destroy qos >/dev/null 2>&1; ovs-vsctl --all destroy queue >/dev/null 2>&1")
    for sw, port in (("s1", "s1-trunk"), ("s2", "s2-trunk")):
        net.get(sw).cmd(
            f"ovs-vsctl set port {port} qos=@q -- "
            f"--id=@q create qos type=linux-htb other-config:max-rate={BOTTLENECK} "
            f"queues:0=@q0 queues:1=@q1 queues:2=@q2 -- "
            f"--id=@q0 create queue other-config:min-rate=1000000 "
            f"other-config:max-rate={BOTTLENECK} other-config:priority=1 -- "
            f"--id=@q1 create queue other-config:min-rate=3000000 "
            f"other-config:max-rate={BOTTLENECK} other-config:priority=0 -- "
            f"--id=@q2 create queue other-config:min-rate=500000 "
            f"other-config:max-rate=6000000 other-config:priority=2"
        )


def controller_status(net):
    """Ask the controller's event API for its status, from inside the network (srv host)."""
    cmd = ("python3 -c \"import urllib.request as u;"
           f"print(u.urlopen('{CTRL_URL}/status',timeout=2).read().decode())\"")
    return net.get("srv").cmd(cmd).strip()


def pct(vals, p):
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p / 100))] if s else None


def summarize(mode, phase, srv_file, client_files):
    with open(srv_file) as f:
        srv = json.load(f)
    lats, expected, received, jit, jit_rfc = [], 0, 0, [], []
    for p in srv["players"].values():
        lats += p["lat_ms"]
        expected += p["expected"]
        received += p["received"]
        jit.append(p["jitter_mean_ms"])
        jit_rfc.append(p["jitter_rfc_ms"])
    cl_lat, cl_recv, cl_lost, sent = [], 0, 0, 0
    for cf in client_files:
        if os.path.exists(cf):
            with open(cf) as f:
                c = json.load(f)
            cl_lat += c["snap_lat_ms"]
            cl_recv += c["snaps_received"]
            cl_lost += c["snaps_lost"]
            sent += c["sent"]
    events = srv.get("events", [])
    return {
        "mode": mode, "phase": phase,
        "players_registered": len(srv["players"]),
        "ignored_datagrams": srv.get("ignored_datagrams", 0),
        "controller_events": {
            "register_ok": sum(1 for e in events if e["event"] == "register" and e["ok"]),
            "leave_ok": sum(1 for e in events if e["event"] == "leave" and e["ok"]),
            "failed": sum(1 for e in events if not e["ok"]),
        },
        "uplink": {
            "updates_sent": sent, "received": received,
            "loss_pct": 100.0 * (1 - received / expected) if expected else None,
            "lat_mean_ms": sum(lats) / len(lats) if lats else None,
            "lat_p95_ms": pct(lats, 95), "lat_max_ms": max(lats) if lats else None,
            "jitter_mean_ms": sum(jit) / len(jit) if jit else None,
            "jitter_rfc_ms": sum(jit_rfc) / len(jit_rfc) if jit_rfc else None,
        },
        "downlink": {
            "snaps_received": cl_recv,
            "loss_pct": 100.0 * cl_lost / (cl_recv + cl_lost) if (cl_recv + cl_lost) else None,
            "lat_mean_ms": sum(cl_lat) / len(cl_lat) if cl_lat else None,
            "lat_p95_ms": pct(cl_lat, 95),
        },
    }


def run_phase(net, mode, phase, duration):
    srv, bg1, bg2, s1 = net.get("srv"), net.get("bg1"), net.get("bg2"), net.get("s1")
    srv_file = os.path.join(RES, "tmp_server.json")
    client_files = [os.path.join(RES, f"tmp_client_p{i}.json") for i in (1, 2, 3)]
    for f in [srv_file] + client_files:
        if os.path.exists(f):
            os.remove(f)

    warmup = 0 if phase == "normal" else 5   # let the flood fill the queue / be detected
    server = srv.popen(
        ["python3", os.path.join(HERE, "game_server.py"), "--port", str(PORT),
         "--duration", str(warmup + duration + 8), "--idle-timeout", "3",
         "--controller", CTRL_URL, "--out", srv_file], stdout=NULL, stderr=NULL)
    time.sleep(1)

    background, iperf_sink = None, None
    if phase == "congested":
        iperf_sink = bg2.popen(["iperf", "-u", "-s"], stdout=NULL, stderr=NULL)
        time.sleep(0.5)
        background = bg1.popen(["iperf", "-u", "-c", BG2_IP, "-b", "15M",
                                "-t", str(warmup + duration + 6)], stdout=NULL, stderr=NULL)
    elif phase == "abuse":
        background = bg1.popen(["iperf", "-u", "-c", SRV_IP, "-p", str(PORT), "-b", "15M",
                                "-t", str(warmup + duration + 6)], stdout=NULL, stderr=NULL)
    time.sleep(warmup)

    clients = []
    for i, cf in zip((1, 2, 3), client_files):
        clients.append(net.get(f"p{i}").popen(
            ["python3", os.path.join(HERE, "game_client.py"), "--server", SRV_IP,
             "--port", str(PORT), "--name", f"p{i}", "--rate", "30",
             "--duration", str(duration), "--out", cf], stdout=NULL, stderr=NULL))

    # evidence for the report: what the classifier table looks like mid-experiment
    time.sleep(min(6, duration / 2))
    flows = s1.cmd("ovs-ofctl -O OpenFlow13 dump-flows s1 table=0")
    with open(os.path.join(RES, f"flows_{mode}_{phase}.txt"), "w") as f:
        f.write(flows)

    for c in clients:
        c.wait()
    server.wait()
    for proc in (background, iperf_sink):
        if proc:
            proc.terminate()
    time.sleep(5 if phase != "normal" else 1)   # let congestion state clear between phases
    return summarize(mode, phase, srv_file, client_files)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["off", "static", "dynamic"], required=True,
                    help="must match GAME_QOS used when starting the controller")
    ap.add_argument("--duration", type=int, default=15, help="measured seconds per phase")
    args = ap.parse_args()
    os.makedirs(RES, exist_ok=True)

    setLogLevel("info")
    net = build()
    net.start()
    setup_queues(net)
    time.sleep(3)
    net.pingAll(timeout="1")  # let the controller learn MAC addresses
    info("*** controller status: " + controller_status(net) + "\n")

    for phase in PHASES:
        info(f"*** mode={args.mode} phase={phase}\n")
        result = run_phase(net, args.mode, phase, args.duration)
        with open(os.path.join(RES, f"{args.mode}_{phase}.json"), "w") as f:
            json.dump(result, f, indent=2)
        info(json.dumps(result, indent=2) + "\n")

    info("*** controller status: " + controller_status(net) + "\n")
    net.stop()


if __name__ == "__main__":
    main()
