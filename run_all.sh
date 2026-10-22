#!/bin/bash
# Runs the whole experiment: controller modes off, static, dynamic (3 phases each), then analysis.
# Needs: mininet, openvswitch-switch, iperf (v2), ryu, matplotlib. Run from this folder.
cd "$(dirname "$0")"
mkdir -p results

for mode in off static dynamic; do
  echo "=== Controller mode: GAME_QOS=$mode ==="
  sudo mn -c >/dev/null 2>&1
  GAME_QOS=$mode ryu-manager --ofp-tcp-listen-port 6653 sdn_controller.py \
      > results/ryu_$mode.log 2>&1 &
  RYU=$!
  sleep 4
  sudo python3 test_topology.py --mode $mode
  kill $RYU 2>/dev/null
  sleep 2
done

sudo mn -c >/dev/null 2>&1
python3 analyze.py
