# Multiplayer Game Communication Network (UDP + SDN)

**Course:** Computer Networks (UE25CS243A), SDN-Socket Programming Jackfruit Mini Project, Topic 17

Players exchange real-time game state over **UDP sockets** inside a **Mininet** network controlled by a
**Ryu (OpenFlow 1.3)** SDN controller. The controller reacts to what the game is doing and to the state of
the network, so game traffic stays fast even when the network is congested or abused.

---

## 1. Architecture

```
   p1 --\                                    /-- srv   (UDP game server)
   p2 ---- s1 ======= 10 Mbps trunk ======= s2 ---- bg2   (iperf sink)
   p3 --/    |                                \-- root  (controller event API  10.0.0.254:8080)
            bg1  (background traffic / "attacker")

   Ryu controller (OpenFlow 1.3, 127.0.0.1:6653) controls s1 and s2
```

**Communication flows**

| Flow | Protocol | Purpose |
|---|---|---|
| player <-> server | UDP port 9999 | `REG`/`ACK` registration, `STATE` updates, `SNAP` world snapshots |
| server -> controller | HTTP `POST /event` | application events: player **registered** / **left** |
| controller <-> switches | OpenFlow 1.3 | flow rules, port statistics |
| switches' trunk ports | OVS HTB queues | q1 = game (highest priority), q0 = default, q2 = throttled |

**Switch pipeline (two tables)**

- `table 0` classifier: game traffic is sent to queue 1; (when congested) other IP traffic to queue 2; then `goto table 1`
- `table 1` L2 learning switch; table misses go to the controller

### Three controller modes (this is the comparison the rubric asks for)

| Mode | What the controller does | Role |
|---|---|---|
| `off` | plain forwarding, no prioritization | baseline 1 |
| `static` | fixed rule: **any** UDP packet to/from port 9999 gets priority, always | baseline 2 (static approach) |
| `dynamic` | **(a)** installs/removes priority flows per player when the game server reports `register`/`leave`; **(b)** polls the trunk counters, detects congestion, and throttles non-game traffic to a rate-limited low-priority queue until the congestion clears | **proposed approach** |

### Why `dynamic` is better than `static` (the "abuse" scenario)

In `static` mode anyone who sends UDP to port 9999 gets priority, so an unregistered host can flood the
game port and steal the priority queue from the real players. In `dynamic` mode only **registered**
players' IP addresses get priority flows, and the flood is throttled when congestion is detected.

---

## 2. Files

| File | Purpose |
|---|---|
| `game_server.py` | UDP server: registration, sequence numbers, loss/stale detection, latency and jitter measurement, snapshots, idle detection, **notifies the controller** |
| `game_client.py` | UDP client: registration with retries, state updates, snapshot receiving, stale-snapshot dropping |
| `sdn_controller.py` | Ryu controller with `off` / `static` / `dynamic` modes |
| `controller_logic.py` | Ryu-independent logic: HTTP event API and congestion detector (unit-tested) |
| `test_topology.py` | Mininet topology, OVS queues, and the experiment (3 phases) |
| `run_all.sh` | Runs all 3 modes and the analysis |
| `analyze.py` | Results table, `results/summary.csv`, `results/comparison.png` |
| `tests/test_local.py` | 8 automated tests (no Mininet needed) |
| `docs/TEST_CASES.md` | Test-case table (expected vs. actual) |

---

## 3. Setup (Ubuntu 20.04 / 22.04 VM)

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch iperf python3-pip
pip3 install -r requirements.txt        # ryu, eventlet==0.30.2, matplotlib
export PATH=$PATH:~/.local/bin          # if ryu-manager is not found
```

Check: `sudo mn --test pingall` should end with `0% dropped`, and `ryu-manager --version` should print a version.

## 4. Run

**Quick check without Mininet** (works on any computer with Python 3):

```bash
python3 tests/test_local.py -v
```

**Full experiment** (about 6-7 minutes; runs `off`, `static`, `dynamic`, each with 3 phases):

```bash
chmod +x run_all.sh
./run_all.sh
```

Outputs go to `results/`: per-run JSON, `summary.csv`, `comparison.png`, the controller logs
`ryu_<mode>.log`, and `flows_<mode>_<phase>.txt` (the switch's flow table captured during each run).

**Manual run** (two terminals, e.g. to demo one mode):

```bash
# terminal 1
GAME_QOS=dynamic ryu-manager --ofp-tcp-listen-port 6653 sdn_controller.py
# terminal 2
sudo mn -c
sudo python3 test_topology.py --mode dynamic
```

**Live demo commands** (while the experiment is running):

```bash
curl http://10.0.0.254:8080/status       # controller's view: players, congestion state
sudo ovs-ofctl -O OpenFlow13 dump-flows s1 table=0   # priority flows appear/disappear
```

---

## 5. Experiments

| Phase | What happens |
|---|---|
| `normal` | 3 players, 30 updates/s each, no background traffic |
| `congested` | plus a 15 Mbps UDP flood `bg1 -> bg2` across the 10 Mbps trunk |
| `abuse` | plus a 15 Mbps UDP flood `bg1 -> srv:9999` from an **unregistered** host |

Every phase also has 1% random packet loss and 2 ms delay on the player links (netem), so the
loss-handling code is exercised even without congestion.

**Metrics:** one-way latency (mean, p95, max), jitter (mean absolute difference of consecutive
latencies, plus RFC 3550 smoothed jitter), packet loss from sequence-number gaps (both directions).

**What to expect** (check against your own results, which are the real evidence):

- `off`: latency, jitter and loss rise sharply under `congested` and `abuse`.
- `static`: protected under `congested`, but degrades under `abuse` because the attacker's packets also get priority.
- `dynamic`: stays close to the `normal` baseline in all three phases.

---

## 6. Requirement mapping

**Topic requirements**

| Requirement | Where |
|---|---|
| Implement UDP game-state updates | `STATE` (player -> server) and `SNAP` (server -> players) |
| Register multiple players | `REG`/`ACK` handshake, 3 players, server assigns ids, idempotent registration |
| Use sequence numbers | per-player `seq` on `STATE`, global `seq` on `SNAP` |
| Handle packet loss | `REG` retries; gaps counted as loss; stale/out-of-order packets dropped; no retransmission of game state (a late update is useless) |
| Implement traffic prioritization | OpenFlow flow rules + `set_queue` into an OVS HTB priority queue |
| Measure latency / jitter | per-packet one-way latency; mean-delta and RFC 3550 jitter |
| Test under congestion | `congested` and `abuse` phases |

**Rubric**

| Rubric item | Where it is covered |
|---|---|
| Problem understanding and architecture | Section 1 |
| TCP/UDP socket implementation | `game_server.py`, `game_client.py` |
| Mininet + SDN implementation | `test_topology.py`, `sdn_controller.py` |
| Socket-SDN integration | game server -> controller events; flows keyed on registered player IPs |
| Dynamic SDN functionality | per-player flows on register/leave; congestion monitoring and throttling |
| Challenging scenario / robustness | congestion, random loss, unauthorized traffic on the game port |
| Performance evaluation and comparison | 3 modes x 3 phases, `analyze.py` |
| Documentation, demo, viva | this README, `docs/TEST_CASES.md`, reproducible `run_all.sh` |

---

