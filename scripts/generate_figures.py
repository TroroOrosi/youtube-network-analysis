"""Generate clean, Excel-like charts from the collected audience-network data.

Run:
    python scripts/generate_figures.py

Outputs PNGs into ``figures/``.  Every chart uses a Japanese-capable font, a
colorblind-safe palette, direct value labels, clear titles and axis labels, and
no 3D / pie clutter.
"""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np

import analysis_common as ac


def _save(fig, name: str) -> str:
    ac.FIGURES_DIR.mkdir(exist_ok=True)
    path = ac.FIGURES_DIR / name
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  wrote {path.relative_to(ac.REPO_ROOT)}")
    return str(path)


def fig_top_channels(data) -> str:
    """Horizontal bar: channels the panel most commonly also subscribes to."""
    top = ac.audience_top_channels(data["edges_anon"], top_n=20)
    top = top.iloc[::-1]  # largest at top
    fig, ax = plt.subplots(figsize=(10, 8))
    bars = ax.barh(top["channel_title"], top["viewers"], color=ac.ACCENT,
                   edgecolor="white", linewidth=0.6)
    n_viewers = data["edges_anon"]["viewer"].nunique()
    for bar, v, s in zip(bars, top["viewers"], top["panel_share"]):
        ax.text(bar.get_width() + n_viewers * 0.004,
                bar.get_y() + bar.get_height() / 2,
                f"{v}  ({s*100:.0f}%)", va="center", ha="left", fontsize=9)
    ax.set_xlim(0, top["viewers"].max() * 1.18)
    ax.set_title("視聴者が他に見ているチャンネル Top20\n"
                 f"（観測パネル {n_viewers} 人中の登録者数）")
    ax.set_xlabel("このチャンネルに登録しているパネル視聴者数（人）")
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    return _save(fig, "01_top_channels.png")


def fig_viewer_breadth(data) -> str:
    """Histogram: how many channels each viewer subscribes to."""
    breadth = ac.viewer_breadth(data["edges_anon"])
    fig, ax = plt.subplots(figsize=(10, 5.5))
    ax.hist(breadth, bins=30, color=ac.PALETTE[5], edgecolor="white")
    med = breadth.median()
    ax.axvline(med, color=ac.PALETTE[3], linewidth=2,
               label=f"中央値 = {med:.0f} チャンネル")
    ax.set_title("視聴者1人あたりの公開登録チャンネル数の分布")
    ax.set_xlabel("公開登録チャンネル数（人ごと）")
    ax.set_ylabel("視聴者数（人）")
    ax.legend()
    return _save(fig, "02_viewer_breadth.png")


def fig_community_sizes(data) -> str:
    """Bar: largest co-viewing communities by channel count."""
    comm = ac.community_sizes(data["community_summary"], min_channels=5).head(12)
    comm = comm.iloc[::-1]
    labels = [f"#{int(c)}" for c in comm["community_id"]]
    fig, ax = plt.subplots(figsize=(10, 6.5))
    bars = ax.barh(labels, comm["n_channels"], color=ac.PALETTE[2],
                   edgecolor="white")
    for bar, n in zip(bars, comm["n_channels"]):
        ax.text(bar.get_width() + comm["n_channels"].max() * 0.005,
                bar.get_y() + bar.get_height() / 2, f"{int(n):,}",
                va="center", ha="left", fontsize=9)
    ax.set_xlim(0, comm["n_channels"].max() * 1.12)
    ax.set_title("共視聴コミュニティの規模 Top12\n（Louvain クラスタ・チャンネル数）")
    ax.set_xlabel("クラスタ内のチャンネル数")
    ax.set_ylabel("コミュニティ ID")
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    return _save(fig, "03_community_sizes.png")


def fig_affinity_lift(data) -> str:
    """Diverging-ish bar: top affinity-lift channels (audience-specific)."""
    top = ac.top_affinity(data["panel_overlap"], top_n=20).iloc[::-1]
    fig, ax = plt.subplots(figsize=(10, 8))
    bars = ax.barh(top["channel"], top["affinity_lift"], color=ac.PALETTE[1],
                   edgecolor="white")
    ax.axvline(1.0, color="#555555", linewidth=1.2, linestyle="--",
               label="lift = 1（規模相応）")
    for bar, v in zip(bars, top["affinity_lift"]):
        ax.text(bar.get_width() + top["affinity_lift"].max() * 0.005,
                bar.get_y() + bar.get_height() / 2, f"{v:.1f}×",
                va="center", ha="left", fontsize=9)
    ax.set_xlim(0, top["affinity_lift"].max() * 1.15)
    ax.set_title("固有親和性の高いチャンネル Top20\n"
                 "（規模で補正した親和性 lift・中央値=1.0）")
    ax.set_xlabel("親和性 lift（>1 = 規模の割に過剰登録）")
    ax.grid(axis="x")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower right")
    return _save(fig, "04_affinity_lift.png")


def fig_penetration_vs_size(data) -> str:
    """Scatter: channel size vs panel penetration, log-x, lift-colored."""
    df = data["panel_overlap"]
    df = df[(df["total_subs"] > 0) & (df["observed_commenters"] >= 10)].copy()
    fig, ax = plt.subplots(figsize=(10, 6.5))
    sc = ax.scatter(df["total_subs"], df["penetration_per_1M_subs"],
                    c=np.clip(df["affinity_lift"], 0, 8),
                    cmap="viridis", s=22, alpha=0.7, edgecolor="none")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_title("チャンネル規模 vs パネル浸透率\n（点の色 = 親和性 lift）")
    ax.set_xlabel("チャンネル総登録者数（対数）")
    ax.set_ylabel("登録者100万人あたりのパネル浸透（対数）")
    ax.grid(True, which="both", axis="both")
    cb = fig.colorbar(sc, ax=ax)
    cb.set_label("親和性 lift（上限8でクリップ）")
    return _save(fig, "05_penetration_vs_size.png")


def fig_edge_strength(data) -> str:
    """Histogram of co-viewing edge weights (weighted cosine)."""
    edges = data["network_edges"]
    fig, ax = plt.subplots(figsize=(10, 5.5))
    ax.hist(edges["weighted_cosine"], bins=40, color=ac.PALETTE[0],
            edgecolor="white")
    ax.set_yscale("log")
    ax.set_title("チャンネル間の共視聴エッジ強度の分布\n（重み付きコサイン類似度）")
    ax.set_xlabel("重み付きコサイン類似度")
    ax.set_ylabel("エッジ数（対数）")
    ax.grid(axis="y", which="both")
    return _save(fig, "06_edge_strength.png")


def main() -> list[str]:
    fam = ac.configure_fonts()
    print(f"font family: {fam}")
    data = ac.load_all()
    paths = [
        fig_top_channels(data),
        fig_viewer_breadth(data),
        fig_community_sizes(data),
        fig_affinity_lift(data),
        fig_penetration_vs_size(data),
        fig_edge_strength(data),
    ]
    print(f"done: {len(paths)} figures")
    return paths


if __name__ == "__main__":
    main()
