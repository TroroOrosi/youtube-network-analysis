"""視聴者オーディエンス分析レポートの図表を生成する。

リポジトリ直下の各CSV（ノートブック youtube_network_analysis.ipynb の生成物）を読み、
audience_analysis/charts/ に4枚のPNGを書き出す。日本語フォントはシステムのCJKフォントを
使い、無ければ japanize-matplotlib 同梱の IPAexGothic を（ビルドせず）取得して登録する。

使い方:
    python audience_analysis/build_report.py        # リポジトリ直下から
"""
import os, sys, glob, tarfile, tempfile, subprocess
import numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib as mpl
import matplotlib.font_manager as fm
import networkx as nx

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CH = os.path.join(HERE, "charts")
os.makedirs(CH, exist_ok=True)
csv = lambda name: os.path.join(ROOT, name)

# ---------- 日本語フォント ----------
FONT = os.path.join(tempfile.gettempdir(), "ipaexg.ttf")
def setup_font():
    cand = ["IPAexGothic", "Noto Sans CJK JP", "Noto Sans JP", "IPAGothic",
            "IPAPGothic", "TakaoPGothic", "WenQuanYi Zen Hei"]
    avail = {f.name for f in fm.fontManager.ttflist}
    for c in cand:
        if c in avail:
            plt.rcParams["font.family"] = "sans-serif"
            plt.rcParams["font.sans-serif"] = [c] + [x for x in plt.rcParams["font.sans-serif"] if x != c]
            plt.rcParams["axes.unicode_minus"] = False
            return c
    if not os.path.exists(FONT):
        d = tempfile.mkdtemp()
        subprocess.run([sys.executable, "-m", "pip", "download", "--no-deps",
                        "--no-binary", ":all:", "-d", d, "japanize-matplotlib"],
                       check=True, capture_output=True, text=True)
        tar = glob.glob(os.path.join(d, "*.tar.gz"))[0]
        with tarfile.open(tar) as t:
            mm = next(x for x in t.getmembers() if x.name.endswith("ipaexg.ttf"))
            with t.extractfile(mm) as s, open(FONT, "wb") as o:
                o.write(s.read())
    fm.fontManager.addfont(FONT)
    name = fm.FontProperties(fname=FONT).get_name()
    plt.rcParams["font.family"] = "sans-serif"
    plt.rcParams["font.sans-serif"] = [name] + plt.rcParams["font.sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False
    return name
print("font:", setup_font())

# ---------- データ ----------
e  = pd.read_csv(csv("edges_anon.csv"))[["viewer", "channel_id"]].drop_duplicates()
cc = pd.read_csv(csv("channel_communities.csv"))
cs = pd.read_csv(csv("channel_community_summary.csv")).set_index("community_id")
m  = pd.read_csv(csv("channel_panel_overlap_metrics.csv"))
ne = pd.read_csv(csv("channel_network_edges_filtered.csv"))
PANEL = e["viewer"].nunique()

# テーマ名はこのデータ断面での代表チャンネルに基づく解釈ラベル（community_id は再収集で変わり得る）
THEME = {1: "ビジネス・学び直し", 0: "自己啓発・エンタメ", 2: "スピリチュアル・引き寄せ",
         4: "政治・国際ニュース", 3: "料理・雑学・VTuber", 7: "格闘技",
         5: "FX・投資トレード", 6: "音楽・ギター", 8: "野球"}

# ---------- 01: セグメント・リーチ ----------
mm = e.merge(cc[["channel_id", "community_id"]], on="channel_id")
seg = []
for c, g in mm.groupby("community_id"):
    reach = g["viewer"].nunique(); avg = g.groupby("viewer").size().mean()
    nch = int(cs.loc[c, "n_channels"]) if c in cs.index else g["channel_id"].nunique()
    seg.append((c, nch, reach, 100 * reach / PANEL, avg))
seg = pd.DataFrame(seg, columns=["c", "nch", "reach", "reachp", "avg"]).sort_values("reachp", ascending=False)
seg_top = seg.head(9).copy()
seg_top["label"] = [f"C{int(c)}・{THEME.get(int(c), 'その他')}" for c in seg_top["c"]]

fig, ax = plt.subplots(figsize=(10, 5.5))
y = np.arange(len(seg_top))[::-1]
ax.barh(y, seg_top["reachp"], color=plt.cm.tab20(np.linspace(0, 1, len(seg_top))))
ax.set_yticks(y); ax.set_yticklabels(seg_top["label"], fontsize=10)
for yi, (p, a, n) in zip(y, zip(seg_top["reachp"], seg_top["avg"], seg_top["nch"])):
    ax.text(p + 0.7, yi, f"{p:.0f}%  (平均{a:.0f}本/人, {n}ch)", va="center", fontsize=8.5)
ax.set_xlabel("リーチ率（このジャンルに1ch以上登録している視聴者の割合, %）")
ax.set_xlim(0, 108)
ax.set_title(f"オーディエンス・セグメント規模（パネル {PANEL}人）", fontsize=13)
ax.spines[["top", "right"]].set_visible(False)
plt.tight_layout(); plt.savefig(f"{CH}/01_segment_reach.png", dpi=150, bbox_inches="tight"); plt.close()

# ---------- 02: penetration 上位15 ----------
mf = m[m["observed_commenters"] >= 25].nlargest(15, "penetration_per_1M_subs").iloc[::-1]
fig, ax = plt.subplots(figsize=(10, 6.5))
y = np.arange(len(mf))
ax.barh(y, mf["penetration_per_1M_subs"], color="#c0504d")
ax.set_yticks(y); ax.set_yticklabels([s[:24] for s in mf["channel"]], fontsize=9)
for yi, (v, o) in zip(y, zip(mf["penetration_per_1M_subs"], mf["observed_commenters"])):
    ax.text(v + 20, yi, f"{v:.0f}  (n={int(o)})", va="center", fontsize=8)
ax.set_xlabel("penetration＝登録者100万人あたりの自視聴者出現数（大きいほど“規模の割に”刺さる）")
ax.set_title("規模補正の固有親和性 トップ15（引き寄せ・潜在意識・コーチング系が突出）", fontsize=12)
ax.spines[["top", "right"]].set_visible(False)
plt.tight_layout(); plt.savefig(f"{CH}/02_penetration_top.png", dpi=150, bbox_inches="tight"); plt.close()

# ---------- 03: 生の人気 vs 規模補正 ----------
d = m[(m["total_subs"] > 0) & (m["observed_commenters"] >= 5)].copy()
fig, ax = plt.subplots(figsize=(10, 7))
sc = ax.scatter(d["total_subs"], d["observed_commenters"],
                c=np.log10(d["penetration_per_1M_subs"].clip(lower=1)),
                cmap="viridis", s=22, alpha=0.6, edgecolors="none")
ax.set_xscale("log")
ax.set_xlabel("チャンネル登録者数（対数）"); ax.set_ylabel("自視聴者パネル中の出現人数")
cb = plt.colorbar(sc); cb.set_label("penetration（log10）＝規模補正の親和性")
raw = ["両学長 リベラルアーツ大学", "NAKATA UNIVERSITY", "PIVOT 公式チャンネル",
       "Paranoia_パラノイア【有益】", "本要約チャンネル【毎日18時更新】", "Naokiman Show"]
niche = ["ザ・ルール 【人生の攻略法】", "西田文郎公式チャンネル", "有馬樹里 脳科学研究家",
         "THINK.【願望・理想を、創造する。】", "量子力学的引き寄せ理論", "あおいの幸せ引き寄せ波動ラボ"]
for nm in raw + niche:
    r = d[d["channel"] == nm]
    if len(r):
        x = r["total_subs"].iloc[0]; yv = r["observed_commenters"].iloc[0]
        c2 = "#1f3b73" if nm in raw else "#b30000"
        ax.annotate(nm[:14], (x, yv), fontsize=7.5, color=c2, xytext=(4, 3), textcoords="offset points")
        ax.scatter([x], [yv], s=40, facecolors="none", edgecolors=c2, linewidths=1.2)
ax.set_title("生の人気（縦）vs 規模補正の親和性（色）\n左上＝小規模なのに刺さる“固有”ch / 右側＝巨大で汎用人気", fontsize=12)
ax.spines[["top", "right"]].set_visible(False)
plt.tight_layout(); plt.savefig(f"{CH}/03_popularity_vs_penetration.png", dpi=150, bbox_inches="tight"); plt.close()

# ---------- 04: ネットワーク・バックボーン ----------
TOPN = 600
hub = cc.sort_values("internal_strength", ascending=False).head(TOPN)
keep = set(hub["channel_id"])
G = nx.Graph()
for r in ne.itertuples(index=False):
    if r.channel_id_a in keep and r.channel_id_b in keep:
        G.add_edge(r.channel_id_a, r.channel_id_b, weight=float(r.weighted_cosine))
if G.number_of_nodes():
    G = G.subgraph(max(nx.connected_components(G), key=len)).copy()
meta = hub.set_index("channel_id")
ncomm = {n: int(meta.loc[n, "community_id"]) for n in G.nodes}
nstr = {n: float(meta.loc[n, "internal_strength"]) for n in G.nodes}
ntit = {n: str(meta.loc[n, "channel"]) for n in G.nodes}
cids = sorted(set(ncomm.values()))
pal = plt.cm.tab20(np.linspace(0, 1, max(len(cids), 1)))
col = {c: pal[i % len(pal)] for i, c in enumerate(cids)}
node_colors = [col[ncomm[n]] for n in G.nodes]
s = np.array([nstr[n] for n in G.nodes]); rng = np.ptp(s) or 1
node_sizes = 40 + 360 * (s - s.min()) / rng
pos = nx.spring_layout(G, weight="weight", seed=42, k=0.30, iterations=200)
fig, ax = plt.subplots(figsize=(16, 16))
nx.draw_networkx_edges(G, pos, alpha=0.05, width=0.5, ax=ax)
nx.draw_networkx_nodes(G, pos, node_color=node_colors, node_size=node_sizes, linewidths=0, ax=ax)
labels = {}
for c in cids:
    mem = [n for n in G.nodes if ncomm[n] == c]
    for n in sorted(mem, key=lambda x: nstr[x], reverse=True)[:2]:
        labels[n] = ntit[n]
nx.draw_networkx_labels(G, pos, labels=labels, font_size=8, ax=ax)
handles = []
for c in cids:
    mem = [n for n in G.nodes if ncomm[n] == c]
    rep = max(mem, key=lambda x: nstr[x])
    lab = THEME.get(c, ntit[rep][:14])
    handles.append(mpl.lines.Line2D([], [], marker="o", linestyle="", markersize=8,
                   markerfacecolor=col[c], markeredgewidth=0, label=f"C{c}: {lab}"))
ax.legend(handles=handles, loc="upper left", fontsize=8, framealpha=0.9, title="クラスタ")
ax.set_title(f"視聴者の登録チャンネル共起ネットワーク（内部強度上位{TOPN}chのバックボーン / {G.number_of_nodes()}ノード）", fontsize=13)
ax.axis("off"); plt.tight_layout()
plt.savefig(f"{CH}/04_network_backbone.png", dpi=150, bbox_inches="tight"); plt.close()

print("charts written to", CH)
print(f"panel={PANEL} channels={e['channel_id'].nunique()} edges={len(e)} communities={cc['community_id'].nunique()}")
