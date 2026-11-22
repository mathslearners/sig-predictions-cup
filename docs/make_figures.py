"""Rebuild the README figures from docs/data/daily.jsonl (the daily report's 21:45Z snapshots).

    python docs/make_figures.py
"""
import json
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
OUT = HERE / "img"
SURFACE, INK, INK2, GRID = "#F7FAFF", "#0B1F4D", "#475A85", "#D6E4FB"
S1, S2, S3, S4 = "#1E3A8A", "#2563EB", "#60A5FA", "#94A3B8"   # categorical slots 1-4, fixed order

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "text.color": INK, "font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.axisbelow": True,
})

rows = [json.loads(l) for l in (HERE / "data" / "daily.jsonl").read_text().splitlines() if l.strip()]
t = [datetime.fromisoformat(r["at"]) for r in rows]
k = lambda v: v / 1000


def fmt_k(ax):
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}k"))


# 1. account value at fair + free cash (two panels, one shared time axis, no dual y-axis)
fig, (a1, a2) = plt.subplots(1, 2, figsize=(13, 4.2), gridspec_kw={"width_ratios": [2, 1]})
a1.plot(t, [k(r["value"]) for r in rows], color=S2, lw=2, marker="o", ms=4)
a1.axhline(100, color=INK2, lw=1, ls=":")
a1.text(t[-1], 103, "start 100k", ha="right", color=INK2, fontsize=9)
a1.set_title("Account value at Kalshi/Polymarket fair (daily 21:45Z)", loc="left", fontsize=11)
for when, label, y in (("2026-10-06", "+74k in the first 4 days", 166), ("2026-11-04", "election night", 221)):
    x = datetime.fromisoformat(when + "T21:45:00+00:00")
    a1.annotate(label, (x, y - 8), xytext=(0, 6), textcoords="offset points", ha="center", color=INK2, fontsize=9)
fmt_k(a1); a1.set_ylim(90, 230)
a2.bar(t, [k(r["cash"]) for r in rows], width=0.7, color=S3)
a2.set_title("Free cash at the same snapshot", loc="left", fontsize=11)
fmt_k(a2)
for a in (a1, a2):
    a.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO, interval=2))
    a.xaxis.set_major_formatter(mdates.DateFormatter('%d %b'))
fig.tight_layout(); fig.savefig(OUT / "account_value.png", dpi=150); plt.close(fig)

# 2. daily edge at fill by component: opening fills above zero, exits below
comp = [("tight", "ladder / dips", S1), ("catcher", "sweep catchers", S2), ("snipe", "sniper", S3),
        ("exit", "exits (given back)", S4)]
fig, ax = plt.subplots(figsize=(9, 4.2))
base_pos = [0.0] * len(rows)
for key, label, col in comp:
    vals = [k(r["components"].get(key, {}).get("edge_at_fill", 0)) for r in rows]
    if key == "exit":
        ax.bar(t, vals, width=0.7, color=col, label=label, edgecolor=SURFACE, linewidth=1)
    else:
        ax.bar(t, vals, width=0.7, bottom=base_pos, color=col, label=label, edgecolor=SURFACE, linewidth=1)
        base_pos = [b + v for b, v in zip(base_pos, vals)]
ax.axhline(0, color=INK2, lw=1)
ax.set_title("Daily edge vs fair at fill time, by order type", loc="left", fontsize=11)
fmt_k(ax)
ax.legend(frameon=False, ncol=4, loc="upper right", fontsize=9)
ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO))
ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
fig.tight_layout(); fig.savefig(OUT / "edge_by_component.png", dpi=150); plt.close(fig)

# 3. final standings (post-mortem table, 7 Nov)
board = [("#1 nimble-beacon", 1519.8), ("#2 steady-finch", 1456.8), ("#3 steady-ember", 1394.2),
         ("#4 kind-brook", 1189.3), ("#5 bright-badger", 985.8), ("#34 this bot", 211.6)]
fig, ax = plt.subplots(figsize=(9, 3.2))
names = [b[0] for b in board][::-1]; vals = [b[1] for b in board][::-1]
bars = ax.barh(names, vals, color=[S1 if "this bot" in n else S3 for n in names], height=0.6)
for b, v in zip(bars, vals):
    ax.text(v + 15, b.get_y() + b.get_height() / 2, f"{v:,.0f}k", va="center", color=INK, fontsize=9)
ax.axvline(100, color=INK2, lw=1, ls=":")
ax.set_title("Final settled balance (start 100k)", loc="left", fontsize=11)
ax.grid(axis="y", visible=False); ax.set_xlim(0, 1700)
ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}k"))
fig.tight_layout(); fig.savefig(OUT / "final_standings.png", dpi=150); plt.close(fig)
print("wrote", *sorted(p.name for p in OUT.glob("*.png")))
