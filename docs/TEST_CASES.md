# Test Cases

**A. Automated tests** (`python3 tests/test_local.py -v`, no Mininet needed). Status shows the result when these
were last run during development; re-run them on your machine before the demo.

| ID | Test | Expected | Status |
|---|---|---|---|
| A1 | Congestion detector with rising/falling load | congested after 2 high polls; stays congested at 60% (throttled flood); recovers after 3 low polls; exactly one up and one down change | PASS |
| A2 | Detector sees a counter reset | ignored, no false change | PASS |
| A3 | Event API accepts a valid `register` event | HTTP 200, callback receives (event, ip, name) | PASS |
| A4 | Event API rejects bad IP / unknown event / missing field | HTTP 400 | PASS |
| A5 | `GET /status` | returns the controller status JSON | PASS |
| A6 | Sequence numbers: send 0,1,3,2,4 | expected 5, received 4 in order, 1 lost, 1 stale dropped | PASS |
| A7 | Duplicate `REG` (client retries) and an unregistered sender | only 1 player created; stranger and garbage datagrams ignored | PASS |
| A8 | End to end: 2 clients with a mock controller | both players registered, both reported `register` then `leave` to the controller, 0 loss on loopback, snapshots received | PASS |

**B. Mininet / SDN tests** (`./run_all.sh`). Fill in the "Actual" column from your own results
(`results/summary.csv`, `results/ryu_dynamic.log`, `results/flows_*.txt`).

| ID | Scenario | Expected | Actual |
|---|---|---|---|
| B1 | `sudo mn --test pingall` | 0% dropped | |
| B2 | Controller status from `srv`: `curl http://10.0.0.254:8080/status` | JSON with mode, switches `[1, 2]`, trunk ports | |
| B3 | 3 players register in `dynamic` mode | `ryu_dynamic.log` shows "player ... registered -> priority flows installed" 3 times; `flows_dynamic_normal.txt` shows `set_queue:1` flows for each player IP | |
| B4 | Players finish and go idle | log shows "player ... left -> priority flows removed" | |
| B5 | `normal` phase, all modes | low latency and jitter; about 1% loss from the lossy links | |
| B6 | `congested` phase, `off` | latency, jitter and loss clearly higher than B5 | |
| B7 | `congested` phase, `static` and `dynamic` | latency and jitter close to B5 | |
| B8 | `congested` phase, `dynamic` | log shows "MONITOR ... CONGESTED" then "ENABLING throttling"; later "recovered" / "DISABLING"; `flows_dynamic_congested.txt` shows a `set_queue:2` rule | |
| B9 | `abuse` phase, `off` and `static` | game traffic degraded (in `static`, the attacker's packets are also in the priority queue) | |
| B10 | `abuse` phase, `dynamic` | game traffic close to B5; attacker has no priority flow | |
| B11 | Random loss on player links | loss near 1%; game continues; registration succeeds via retries | |
| B12 | Server ignores the flood | `ignored_datagrams` > 0 in the `abuse` JSON; server keeps serving players | |
