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


# --- video-method reproduction ---------------------------------------------
# The reference video sampled 1,000 subscribers from a ~100k population, keeping
# only users whose subscription list is public.  Our collected panel is smaller,
# so we expose both the full panel and a seed-fixed sample for reproducibility.
VIDEO_SAMPLE_SIZE = 1000
VIDEO_SEED = 42

# Video's stated figures, used purely as a comparison baseline in the report.
VIDEO_REF = {
    "population": 100_000,
    "sample": 1_000,
    "channels": 99_000,
    "links": 400_000,
}


def subscriber_sample(edges_anon: pd.DataFrame, n: int = VIDEO_SAMPLE_SIZE,
                      seed: int = VIDEO_SEED) -> pd.DataFrame:
    """Reproduce the video's Step 1 sampling of public-subscription viewers.

    Every viewer in ``edges_anon`` already has a public subscription list (that
    is how the edge was observed), matching the video's "public accounts only"
    rule.  If the panel is smaller than ``n`` we return the full panel; the
    caller reports the actual size against the video's 1,000.
    """
    viewers = edges_anon["viewer"].drop_duplicates()
    if len(viewers) <= n:
        return edges_anon.copy()
    rng = np.random.default_rng(seed)
    chosen = set(rng.choice(viewers.to_numpy(), size=n, replace=False))
    return edges_anon[edges_anon["viewer"].isin(chosen)].copy()


# Keyword -> interest category map.  The video sorts the audience's other
# channels into interests like math, science, money, reading, horror, trivia,
# business, culture.  We classify each channel title by Japanese/English
# keywords; unmatched channels fall into "その他/未分類".
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "ビジネス・投資・お金": [
        "ビジネス", "投資", "株", "お金", "マネー", "副業", "起業", "経営", "経済",
        "リベラルアーツ", "両学長", "FX", "資産", "節約", "PIVOT", "NewsPicks",
        "ホリエモン", "マコなり", "money", "business", "stock", "楽待", "不動産",
    ],
    "自己啓発・学び": [
        "自己啓発", "学識", "学び", "名言", "心理", "メンタル", "DaiGo", "成功",
        "習慣", "ライフハック", "モチベーション", "サロン", "セラピー", "中田",
        "NAKATA", "UNIVERSITY", "大学", "勉強", "study",
    ],
    "読書・要約・教養": [
        "本要約", "要約", "読書", "本", "書評", "教養", "リベラル", "哲学", "思想",
        "歴史", "偉人", "book", "literature", "文学",
    ],
    "科学・数学・テクノロジー": [
        "科学", "数学", "物理", "化学", "生物", "宇宙", "ゆっくり科学", "サイエンス",
        "AI", "テック", "プログラ", "パソコン", "TAIKI", "ガジェット", "science",
        "math", "tech", "エンジニア",
    ],
    "雑学・ミステリー・都市伝説": [
        "雑学", "都市伝説", "ミステリー", "ホラー", "怖い", "Naokiman", "TOLAND",
        "オカルト", "陰謀", "謎", "不思議", "パラノイア", "スピリチュアル", "神さま",
        "未解決", "horror", "mystery",
    ],
    "エンタメ・音楽・芸能": [
        "音楽", "MUSIC", "ミュージック", "歌", "ライブ", "THE FIRST TAKE", "芸能",
        "コント", "お笑い", "芸人", "狩野英孝", "手越", "ROLAND", "映画", "アニメ",
        "ドラマ", "アート", "art", "movie", "music", "ゲーム", "game",
    ],
    "ニュース・社会": [
        "ニュース", "報道", "政治", "社会", "時事", "解説", "news", "ANN",
        "ReHacQ", "リハック", "ABEMA", "アベプラ", "テレ東", "BIZ", "PRESIDENT",
        "ReHack",
    ],
    "美容・健康・生活": [
        "美容", "整体", "健康", "ダイエット", "筋トレ", "トレーニング", "腰痛",
        "肩こり", "料理", "レシピ", "リュウジ", "暮らし", "ルーティン", "ライフ",
        "beauty", "health", "cook", "Honami", "MAGGY",
    ],
}


def categorize_channel(title: str) -> str:
    """Assign an interest category to a channel by keyword match."""
    if not isinstance(title, str):
        return "その他・未分類"
    for category, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in title.lower():
                return category
    return "その他・未分類"


def interest_breakdown(edges_anon: pd.DataFrame, top_n_channels: int = 300):
    """Categorise the panel's most-subscribed channels into interest buckets.

    Returns a DataFrame with one row per category: number of distinct channels
    that fell into it among the top ``top_n_channels``, and the total subscriber
    links those channels attract from the panel (a "share of attention" proxy).
    """
    top = audience_top_channels(edges_anon, top_n=top_n_channels).copy()
    top["category"] = top["channel_title"].map(categorize_channel)
    grp = top.groupby("category").agg(
        n_channels=("channel_id", "nunique"),
        viewer_links=("viewers", "sum"),
    ).reset_index()
    grp["link_share"] = grp["viewer_links"] / grp["viewer_links"].sum()
    return grp.sort_values("viewer_links", ascending=False)


def affinity_vs_popularity(panel_overlap: pd.DataFrame,
                           min_commenters: int = 10) -> pd.DataFrame:
    """Table contrasting raw popularity (panel share) with size-corrected lift.

    Encodes the video's caveat that a popularity ranking alone does not reveal
    audience-specific affinity.  ``affinity_lift`` here is the size correction:
    panel share divided by the channel's share of total subscribers, calibrated
    to a median of 1.0.  Because external subscriber counts are present in the
    data we use them; the docstring of build_excel_report notes the fallback.
    """
    df = panel_overlap[panel_overlap["observed_commenters"] >= min_commenters].copy()
    df = df.dropna(subset=["affinity_lift"])
    df["popularity_rank"] = df["sample_share"].rank(ascending=False).astype(int)
    df["affinity_rank"] = df["affinity_lift"].rank(ascending=False).astype(int)
    df["rank_gap"] = df["popularity_rank"] - df["affinity_rank"]
    return df
