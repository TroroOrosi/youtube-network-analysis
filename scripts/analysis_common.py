"""Shared helpers for the YouTube audience-network analysis.

Centralises data loading, the audience-profile computations (the "what do my
viewers also watch?" question from the reference video), and a reproducible
Japanese-capable matplotlib configuration with a colorblind-safe palette.

All paths are resolved relative to the repository root so the scripts can be
run from anywhere (``python scripts/<name>.py``).
"""

from __future__ import annotations

from pathlib import Path

import matplotlib
import matplotlib.font_manager as fm
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402  (must follow backend selection)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIGURES_DIR = REPO_ROOT / "figures"

# --- input datasets (produced by the notebook) -----------------------------
EDGES_ANON = REPO_ROOT / "edges_anon.csv"
NETWORK_EDGES = REPO_ROOT / "channel_network_edges_filtered.csv"
COMMUNITIES = REPO_ROOT / "channel_communities.csv"
COMMUNITY_SUMMARY = REPO_ROOT / "channel_community_summary.csv"
PANEL_OVERLAP = REPO_ROOT / "channel_panel_overlap_metrics.csv"
CHANNEL_STATS = REPO_ROOT / "channel_statistics_cache.csv"

# Okabe-Ito colorblind-safe qualitative palette.
PALETTE = [
    "#0072B2", "#E69F00", "#009E73", "#D55E00",
    "#CC79A7", "#56B4E9", "#F0E442", "#999999",
]
ACCENT = "#0072B2"
GRID = "#D9D9D9"


def configure_fonts() -> str:
    """Register Noto Sans CJK JP and apply a clean, Excel-like chart style.

    Returns the resolved font family name so callers can log what was used.
    """
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    ]
    family = "DejaVu Sans"
    for path in candidates:
        if Path(path).exists():
            fm.fontManager.addfont(path)
    # Prefer the JP face if matplotlib now knows about it.
    known = {f.name for f in fm.fontManager.ttflist}
    for name in ("Noto Sans CJK JP", "Noto Sans CJK SC", "Noto Serif CJK JP"):
        if name in known:
            family = name
            break

    plt.rcParams.update({
        "font.family": family,
        "axes.unicode_minus": False,
        "figure.dpi": 130,
        "savefig.dpi": 150,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "axes.edgecolor": "#BFBFBF",
        "axes.grid": True,
        "axes.grid.axis": "y",
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "axes.titlesize": 15,
        "axes.titleweight": "bold",
        "axes.labelsize": 11.5,
        "axes.labelweight": "normal",
        "xtick.labelsize": 10,
        "ytick.labelsize": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "legend.frameon": False,
        "figure.autolayout": False,
    })
    return family


# --- data loading -----------------------------------------------------------
def load_all() -> dict[str, pd.DataFrame]:
    """Load every collected dataset into a dict of DataFrames."""
    return {
        "edges_anon": pd.read_csv(EDGES_ANON, dtype={"channel_id": str}),
        "network_edges": pd.read_csv(NETWORK_EDGES, dtype={
            "channel_id_a": str, "channel_id_b": str}),
        "communities": pd.read_csv(COMMUNITIES, dtype={"channel_id": str}),
        "community_summary": pd.read_csv(COMMUNITY_SUMMARY),
        "panel_overlap": pd.read_csv(PANEL_OVERLAP, dtype={"channel_id": str}),
        "channel_stats": pd.read_csv(CHANNEL_STATS, dtype={"channel_id": str}),
    }


# --- audience-profile analysis (the video's core question) ------------------
def audience_top_channels(edges_anon: pd.DataFrame, top_n: int = 25) -> pd.DataFrame:
    """Channels the panel of viewers most commonly also subscribe to.

    This is the direct analogue of the reference video's headline result:
    "which other channels do my viewers tend to watch?"  We count how many of
    the observed viewers subscribe to each channel and express it as a share of
    the panel.
    """
    n_viewers = edges_anon["viewer"].nunique()
    title = (edges_anon.dropna(subset=["channel_title"])
             .drop_duplicates("channel_id")
             .set_index("channel_id")["channel_title"])
    counts = (edges_anon.groupby("channel_id")["viewer"].nunique()
              .sort_values(ascending=False))
    out = counts.head(top_n).rename("viewers").reset_index()
    out["channel_title"] = out["channel_id"].map(title).fillna("(不明)")
    out["panel_share"] = out["viewers"] / n_viewers
    return out[["channel_id", "channel_title", "viewers", "panel_share"]]


def viewer_breadth(edges_anon: pd.DataFrame) -> pd.Series:
    """Distribution of how many channels each viewer subscribes to."""
    return edges_anon.groupby("viewer")["channel_id"].nunique()


def community_sizes(community_summary: pd.DataFrame, min_channels: int = 5):
    """Communities sorted by channel count, filtered to substantive clusters."""
    df = community_summary[community_summary["n_channels"] >= min_channels].copy()
    return df.sort_values("n_channels", ascending=False)


def top_affinity(panel_overlap: pd.DataFrame, top_n: int = 20,
                 min_commenters: int = 10) -> pd.DataFrame:
    """Highest affinity-lift channels (over-subscribed for their size).

    Lift>1 means the panel subscribes more than the channel's overall size
    would predict -> a signal of audience-specific affinity rather than
    generic popularity.
    """
    df = panel_overlap[panel_overlap["observed_commenters"] >= min_commenters]
    df = df.dropna(subset=["affinity_lift"])
    return df.sort_values("affinity_lift", ascending=False).head(top_n)


def fmt_int(n: int) -> str:
    return f"{n:,}"
