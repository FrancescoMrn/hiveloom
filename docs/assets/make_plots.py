"""Regenerate the README charts from evals/article-extractor/RESULTS.md (0.3.1).

The numbers below are transcribed from that file, not recomputed from the eval
logs: re-running the sweep means updating both. Run `python3 make_plots.py`
from anywhere; output lands next to this script.
"""
# ruff: noqa: E402 - matplotlib.use() must run before pyplot is imported.
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np

OUT = Path(__file__).resolve().parent
OUT.mkdir(parents=True, exist_ok=True)

# validated palette (dataviz reference, light mode)
BLUE = "#2a78d6"      # harness
ORANGE = "#eb6834"    # raw
AQUA = "#1baf7a"      # third entity (sonnet incumbent)
BLUE_LIGHT = "#86b6ef"
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"

plt.rcParams.update({
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans"],
    "text.color": INK,
    "axes.edgecolor": BASELINE,
    "axes.labelcolor": INK2,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "axes.grid": False,
    "svg.fonttype": "none",
})


def style_ax(ax, ygrid=True, xgrid=False):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(BASELINE)
        ax.spines[side].set_linewidth(0.8)
    if ygrid:
        ax.yaxis.grid(True, color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
    if xgrid:
        ax.xaxis.grid(True, color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
    ax.tick_params(length=0, labelsize=9)


# ---------------------------------------------------------------- chart 1
# Task success, raw vs harness, per model (+ Sonnet raw reference)
models = ["Claude Haiku 4.5", "Qwen3 4B\n(local)", "Qwen3.6 35B\n(local)", "Gemma4 12B\n(local)"]
raw = [3.1, 58.3, 75.0, 91.7]
harness = [64.6, 68.8, 84.4, 89.6]
sig = ["+61.5 pts\np < 0.0001", "+10.4 (n.s.)", "+9.4 (n.s.)", "−2.1 (n.s.)"]

fig, ax = plt.subplots(figsize=(8.6, 4.6), dpi=200)
x = np.arange(len(models))
w = 0.32
b1 = ax.bar(x - w / 2, raw, w * 0.94, color=ORANGE, label="raw model", zorder=3)
b2 = ax.bar(x + w / 2, harness, w * 0.94, color=BLUE,
            label="same model + hiveloom harness", zorder=3)
ax.axhline(100, color=MUTED, linewidth=1.2, linestyle=(0, (4, 3)), zorder=2)
ax.text(1.5, 103.0, "Claude Sonnet 5, raw (incumbent): 100%",
        ha="center", va="bottom", fontsize=8.5, color=INK2)

for bars in (b1, b2):
    for rect in bars:
        ax.text(rect.get_x() + rect.get_width() / 2, rect.get_height() + 1.5,
                f"{rect.get_height():.0f}%", ha="center", va="bottom",
                fontsize=8.5, color=INK2)
for i, s in enumerate(sig):
    top = max(raw[i], harness[i])
    ax.text(x[i], min(top + 9.5, 109.0), s, ha="center", va="bottom", fontsize=7.6,
            color=INK if i == 0 else MUTED, linespacing=1.25)

ax.set_ylim(0, 121)
ax.set_yticks([0, 25, 50, 75, 100])
ax.set_yticklabels(["0%", "25%", "50%", "75%", "100%"])
ax.set_xticks(x)
ax.set_xticklabels(models, fontsize=9, color=INK2)
style_ax(ax)
ax.set_title("Task success: same model, same prompt, same tool — with and without the harness",
             fontsize=11, color=INK, loc="left", pad=14)
ax.text(0, 1.015,
        "article-extractor benchmark · 32 live URLs × 3 epochs (96 runs/arm) · hiveloom 0.3.1",
        transform=ax.transAxes, fontsize=8, color=MUTED)
ax.legend(loc="upper left", bbox_to_anchor=(0.0, 0.98), frameon=False, fontsize=8.5,
          handlelength=1.2, handleheight=1.0)
fig.tight_layout()
fig.savefig(OUT / "01-task-success.png", bbox_inches="tight")
plt.close(fig)

# ---------------------------------------------------------------- chart 2
# Hallucination rate, raw vs harness
hall_raw = [96, 19, 16, 1]
hall_har = [11, 16, 0, 0]

fig, ax = plt.subplots(figsize=(8.6, 4.2), dpi=200)
b1 = ax.bar(x - w / 2, hall_raw, w * 0.94, color=ORANGE, label="raw model", zorder=3)
b2 = ax.bar(x + w / 2, hall_har, w * 0.94, color=BLUE,
            label="same model + hiveloom harness", zorder=3)
for bars in (b1, b2):
    for rect in bars:
        v = rect.get_height()
        ax.text(rect.get_x() + rect.get_width() / 2, v + 1.5,
                f"{v:.0f}%", ha="center", va="bottom", fontsize=8.5,
                color=INK if v == 0 else INK2,
                fontweight="bold" if v == 0 else "normal")
ax.set_ylim(0, 108)
ax.set_yticks([0, 25, 50, 75, 100])
ax.set_yticklabels(["0%", "25%", "50%", "75%", "100%"])
ax.set_xticks(x)
ax.set_xticklabels(models, fontsize=9, color=INK2)
style_ax(ax)
ax.set_title("Hallucinated output (title/headings not on the live page) — lower is better",
             fontsize=11, color=INK, loc="left", pad=14)
ax.text(0, 1.02, "checked by re-fetching each page and verifying the output verbatim · 96 runs/arm",
        transform=ax.transAxes, fontsize=8, color=MUTED)
ax.legend(loc="upper right", frameon=False, fontsize=8.5, handlelength=1.2)
fig.tight_layout()
fig.savefig(OUT / "02-hallucination.png", bbox_inches="tight")
plt.close(fig)

# ---------------------------------------------------------------- chart 3
# Cost per successful result
labels = [
    "Claude Haiku 4.5, raw",
    "Claude Haiku 4.5 + hiveloom",
    "Claude Sonnet 5, raw (incumbent)",
]
costs = [0.2212, 0.0186, 0.0073]
colors = [ORANGE, BLUE, AQUA]

fig, ax = plt.subplots(figsize=(8.6, 2.9), dpi=200)
y = np.arange(len(labels))[::-1]
bars = ax.barh(y, costs, height=0.52, color=colors, zorder=3)
for yi, c in zip(y, costs, strict=True):
    ax.text(c + 0.004, yi, f"${c:.4f}", va="center", ha="left", fontsize=9.5, color=INK)
ax.text(costs[1] + 0.032, y[1], "12× cheaper than raw Haiku", va="center", ha="left",
        fontsize=8.5, color=INK2, style="italic")
ax.set_yticks(y)
ax.set_yticklabels(labels, fontsize=9.5, color=INK)
ax.set_xlim(0, 0.26)
ax.set_xticks([0, 0.05, 0.10, 0.15, 0.20, 0.25])
ax.set_xticklabels(["$0", "$0.05", "$0.10", "$0.15", "$0.20", "$0.25"])
style_ax(ax, ygrid=False, xgrid=True)
ax.set_title("Cost per successful extraction (USD)", fontsize=11, color=INK, loc="left", pad=12)
fig.tight_layout()
fig.savefig(OUT / "03-cost-per-success.png", bbox_inches="tight")
plt.close(fig)

# ---------------------------------------------------------------- chart 4
# Prompt caching: cold vs warm run cost (same entity, two conditions → one hue, two steps)
fig, ax = plt.subplots(figsize=(6.2, 2.4), dpi=200)
vals = [0.0100, 0.0019]
names = ["First run (cache write)", "Every warm run (cache read)"]
y = np.arange(2)[::-1]
ax.barh(y, vals, height=0.5, color=[BLUE_LIGHT, BLUE], zorder=3)
ax.text(vals[0] + 0.0002, y[0], "$0.0100", va="center", fontsize=9.5, color=INK)
ax.text(vals[1] + 0.0002, y[1], "$0.0019   −81%", va="center", fontsize=9.5,
        color=INK, fontweight="bold")
ax.set_yticks(y)
ax.set_yticklabels(names, fontsize=9.5, color=INK)
ax.set_xlim(0, 0.0125)
ax.set_xticks([0, 0.005, 0.010])
ax.set_xticklabels(["$0", "$0.005", "$0.010"])
style_ax(ax, ygrid=False, xgrid=True)
ax.set_title("Prompt caching on a 7k-token harness prompt (Haiku 4.5, live measurement)",
             fontsize=10.5, color=INK, loc="left", pad=12)
fig.tight_layout()
fig.savefig(OUT / "04-prompt-caching.png", bbox_inches="tight")
plt.close(fig)

print("wrote:", *[p.name for p in sorted(OUT.glob('*.png'))])
