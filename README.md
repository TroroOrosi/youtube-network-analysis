# YouTube オーディエンス・ネットワーク分析

「視聴者は普段どんなチャンネルを見ているのか？」という問いを、**視聴者 × チャンネルの
2部グラフ**として集計・ネットワーク化して把握するプロジェクト。

参考動画：[視聴者は普段どんなチャンネルを見ているのか？（データで語る棒人間）](https://www.youtube.com/watch?v=ec26DzgBKIU)

> **プライバシー方針**：出力は集計のみ。個人単位の登録チャンネル一覧は保持せず、
> コメント投稿者IDは計算の中間データとしてのみ扱い、最終データは匿名化（`u0`,`u1`…）する。

## 何をしているか

1. 自チャンネルのコメント投稿者を「視聴者の代理」として収集
2. 各投稿者の**公開**登録チャンネルから `(視聴者, チャンネル)` のエッジを構築（匿名化）
3. 共通視聴者 ≥ 3 のチャンネル対を共起（共視聴）エッジとし、次数で重み付けした
   コサイン類似度を重みとする無向ネットワークへ射影
4. Louvain でコミュニティ（系統クラスタ）を検出
5. チャンネル規模で割り、中央値=1.0 に校正した**親和性 lift**（汎用人気と
   自視聴者固有の親和性の分離）を算出
6. 非復元サブサンプリング + Adjusted Rand Index でクラスタ安定性を確認

詳細な収集・分析の各ステージは `youtube_network_analysis.ipynb` にある。

## データセット

| ファイル | 内容 |
|---|---|
| `edges_anon.csv` | 視聴者×チャンネルの匿名2部グラフ（235,024 エッジ / 808 視聴者 / 106,568 チャンネル） |
| `channel_network_edges_filtered.csv` | 共起チャンネル対の重み付きエッジ（226,516 本, shared≥3） |
| `channel_communities.csv` | Louvain クラスタ割当（14,694 チャンネル） |
| `channel_community_summary.csv` | クラスタごとの規模・代表チャンネル（50 クラスタ） |
| `channel_panel_overlap_metrics.csv` | チャンネルごとの浸透率・親和性 lift（8,402 行） |
| `channel_statistics_cache.csv` | Data API の登録者数キャッシュ |

列ごとの定義は Excel レポートの **Data Dictionary** シート、または上記ノートブック参照。

## レポートと図の再生成

```bash
pip install -r requirements.txt          # 依存をインストール
python scripts/generate_figures.py       # figures/ に Excel風チャートを生成
python scripts/build_excel_report.py     # youtube_network_analysis_report.xlsx を生成
```

- 生成物：
  - `figures/01_top_channels.png` … 視聴者が他に見ているチャンネル Top20
  - `figures/02_viewer_breadth.png` … 視聴者1人あたりの登録チャンネル数
  - `figures/03_community_sizes.png` … コミュニティ規模 Top12
  - `figures/04_affinity_lift.png` … 固有親和性 lift Top20
  - `figures/05_penetration_vs_size.png` … 規模 vs 浸透率 散布図
  - `figures/06_edge_strength.png` … 共起エッジ強度の分布
  - `youtube_network_analysis_report.xlsx` … 10シートのサマリーワークブック
    （Overview / Data Profile / Data Dictionary / Quality Checks / Summary Metrics /
    Top Channels / Affinity Lift / Communities / Charts / Sources & Methodology）

図は日本語フォント（Noto Sans CJK JP）、色覚多様性に配慮した Okabe-Ito 配色、
直接ラベル付き、3D・円グラフ不使用で統一している。

## 主要な発見（収集データより）

- 視聴者の併用登録先は**自己啓発・ビジネス・教養系が上位**（両学長、本要約チャンネル、
  NAKATA UNIVERSITY、PIVOT など）。最頻併用先はパネルの約 33% が登録。
- 共視聴ネットワークは 5 つの大型クラスタ（各 2,400〜3,000 チャンネル）に分かれ、
  「ビジネス/自己啓発」「エンタメ/音楽」などの系統が分離している。
- 親和性 lift（規模補正）では、大型チャンネルの汎用人気とは別に、規模の割に
  自視聴者へ過剰登録される**ニッチで固有性の高いチャンネル**が浮かび上がる。

## 限界

- 視聴者の代理は「コメント投稿者かつ公開登録者」。登録を非公開にしている層は観測
  できず、母数は 808 人と小さい（**選択バイアス**あり）。
- YouTube Data/Analytics API には「視聴者が他に見るチャンネル」は含まれない。唯一の
  地上検証は YouTube Studio の「視聴者」タブ（オーナー権限・手動）。
- 1人あたり登録チャンネル数のヒストグラムで 1,000 付近に山が出るのは、`subscriptions`
  API の取得上限（約1,000件）による打ち切りアーティファクトである。

出典・方法論の参照先（URL付き）は Excel レポートの **Sources & Methodology** シート参照。
