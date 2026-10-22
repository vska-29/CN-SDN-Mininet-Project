#!/usr/bin/env python3
"""
Compare the three controller modes (off = no QoS baseline, static = fixed-rule baseline,
dynamic = proposed) across the three phases (normal, congested, abuse).
Prints a table, writes results/summary.csv and results/comparison.png.
"""
import csv
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
MODES = ["off", "static", "dynamic"]
PHASES = ["normal", "congested", "abuse"]
COLORS = {"off": "#9e9e9e", "static": "#f0ad4e", "dynamic": "#2e9d5b"}


def load():
    data = {}
    for m in MODES:
        for p in PHASES:
            path = os.path.join(RES, f"{m}_{p}.json")
            if os.path.exists(path):
                with open(path) as f:
                    data[(m, p)] = json.load(f)
    return data


def fmt(v):
    return "n/a" if v is None else f"{v:.2f}"


def main():
    data = load()
    if not data:
        print("No results found in", RES, "- run ./run_all.sh first")
        return

    cols = ["mode", "phase", "lat_mean_ms", "lat_p95_ms", "lat_max_ms", "jitter_ms",
            "loss_pct", "down_lat_ms", "down_loss_pct"]
    rows = []
    for (m, p), r in data.items():
        u, d = r["uplink"], r["downlink"]
        rows.append([m, p, u["lat_mean_ms"], u["lat_p95_ms"], u["lat_max_ms"],
                     u["jitter_mean_ms"], u["loss_pct"], d["lat_mean_ms"], d["loss_pct"]])

    widths = [8, 11] + [13] * 7
    print("".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("-" * sum(widths))
    for row in rows:
        print("".join((str(v) if i < 2 else fmt(v)).ljust(w) for i, (v, w) in enumerate(zip(row, widths))))
    print("\nlatency/jitter in ms. Up = player->server updates, Down = server->player snapshots.")

    with open(os.path.join(RES, "summary.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(cols)
        wr.writerows([[v if i < 2 or v is None else round(v, 3) for i, v in enumerate(r)] for r in rows])
    print("Wrote", os.path.join(RES, "summary.csv"))

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; skipping chart (pip3 install matplotlib)")
        return

    metrics = [("lat_mean_ms", "Mean latency (ms)"), ("lat_p95_ms", "95th percentile latency (ms)"),
               ("jitter_mean_ms", "Jitter (ms)"), ("loss_pct", "Packet loss (%)")]
    fig, axes = plt.subplots(1, 4, figsize=(19, 4.2))
    width = 0.26
    for ax, (key, title) in zip(axes, metrics):
        for i, m in enumerate(MODES):
            vals = [(data[(m, p)]["uplink"][key] or 0) if (m, p) in data else 0 for p in PHASES]
            ax.bar([x + (i - 1) * width for x in range(len(PHASES))], vals, width,
                   label=m, color=COLORS[m])
        ax.set_xticks(range(len(PHASES)))
        ax.set_xticklabels(PHASES)
        ax.set_title(title, fontsize=10)
    axes[0].legend(title="SDN mode")
    fig.suptitle("Game traffic (player -> server): no QoS vs static priority vs dynamic SDN")
    fig.tight_layout()
    out = os.path.join(RES, "comparison.png")
    fig.savefig(out, dpi=150)
    print("Wrote", out)


if __name__ == "__main__":
    main()
