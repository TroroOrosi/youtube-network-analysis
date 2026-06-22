"""Build the Excel summary workbook for the YouTube audience-network analysis.

Run:
    python scripts/build_excel_report.py    # writes the workbook (self-contained)

The workbook needs no PNG inputs -- every chart is drawn natively by Excel.

Output: youtube_network_analysis_report.xlsx

All charts are NATIVE Excel chart objects (openpyxl BarChart / ScatterChart),
not embedded PNG images, so they stay editable inside Excel.

Sheets
------
  結論 (Executive Summary) plain-language conclusion for non-specialists
  Overview                 project summary + headline findings
  Video Method Reproduction Step 1/2/3 flow + video-vs-data comparison
  Chart - Top Channels     ① common-subscription-rate top channels (native bar)
  Chart - Categories       ② interest categories (native bar)
  Chart - Pop vs Affinity  ③ popularity vs affinity lift (native scatter)
  Chart - Viewer Breadth   ④ per-viewer subscription-count distribution (native bar)
  Chart - Video Compare    ⑤ video-vs-data scale comparison (native bar, log)
  Data Profile             per-dataset row/column/null/dup profile
  Data Dictionary          column-level descriptions
  Quality Checks           automated PASS/WARN checks
  Summary Metrics          single-number KPIs
  Subscriber Sample        seed-fixed Step-1 viewer sample + breadth
  Top Channels             "what viewers also watch" (video's headline question)
  Interest Categories      keyword genre breakdown (the video's interest view)
  Affinity vs Popularity   "popularity != affinity" hidden-gem table
  Affinity Lift            size-corrected audience-specific affinity
  Communities              Louvain co-viewing clusters
  Sources & Methodology    external references (incl. personality research) + ethics
"""

from __future__ import annotations

from datetime import datetime, timezone

import openpyxl
from openpyxl.chart import BarChart, Reference, ScatterChart, Series
from openpyxl.chart.label import DataLabelList
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import analysis_common as ac

# --- styling ---------------------------------------------------------------
NAVY = "1F3864"
BLUE = "2E5496"
LIGHT = "D9E1F2"
BAND = "EEF3FB"
GREEN = "C6EFCE"
AMBER = "FFEB9C"
GREY = "808080"

HEADER_FILL = PatternFill("solid", fgColor=BLUE)
TITLE_FILL = PatternFill("solid", fgColor=NAVY)
BAND_FILL = PatternFill("solid", fgColor=BAND)
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WHITE_BOLD = Font(bold=True, color="FFFFFF", size=11)
TITLE_FONT = Font(bold=True, color="FFFFFF", size=16)
WRAP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(horizontal="center", vertical="center")


def _style_header_row(ws, row, ncols, start=1):
    for c in range(start, start + ncols):
        cell = ws.cell(row=row, column=c)
        cell.fill = HEADER_FILL
        cell.font = WHITE_BOLD
        cell.alignment = Alignment(horizontal="center", vertical="center",
                                   wrap_text=True)
        cell.border = BORDER


def _autofit(ws, widths: dict[int, int]):
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = w


def _write_df(ws, df, start_row=1, start_col=1, band=True, int_cols=(),
              pct_cols=(), float_cols=()):
    """Write a DataFrame with a styled header, banding, and number formats."""
    headers = list(df.columns)
    for j, h in enumerate(headers):
        ws.cell(row=start_row, column=start_col + j, value=h)
    _style_header_row(ws, start_row, len(headers), start_col)
    for i, (_, row) in enumerate(df.iterrows(), start=1):
        r = start_row + i
        for j, h in enumerate(headers):
            cell = ws.cell(row=r, column=start_col + j, value=row[h])
            cell.border = BORDER
            if band and i % 2 == 0:
                cell.fill = BAND_FILL
            if h in int_cols:
                cell.number_format = "#,##0"
            elif h in pct_cols:
                cell.number_format = "0.0%"
            elif h in float_cols:
                cell.number_format = "0.000"
    ws.freeze_panes = ws.cell(row=start_row + 1, column=start_col)
    last_col = get_column_letter(start_col + len(headers) - 1)
    ws.auto_filter.ref = f"{get_column_letter(start_col)}{start_row}:{last_col}{start_row + len(df)}"
    return start_row + len(df) + 1


def _title_block(ws, title, subtitle=None):
    ws.merge_cells("A1:H1")
    c = ws["A1"]
    c.value = title
    c.fill = TITLE_FILL
    c.font = TITLE_FONT
    c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[1].height = 34
    if subtitle:
        ws.merge_cells("A2:H2")
        s = ws["A2"]
        s.value = subtitle
        s.font = Font(italic=True, color=GREY, size=10)
        s.alignment = Alignment(horizontal="left", indent=1)


# --- sheet builders --------------------------------------------------------
def sheet_executive_summary(wb, data, kpis):
    """Plain-language conclusion sheet ('結論') for non-specialists."""
    ws = wb.active
    ws.title = "結論 (Executive Summary)"
    ws.sheet_view.showGridLines = False
    _title_block(ws, "結論：このデータから何が分かるか",
                 "非専門家向けの短いまとめ（詳細は各シート参照）")

    lead = (
        f"自チャンネルの視聴者 {kpis['n_viewers']:,} 人（公開登録を持つコメント投稿者）が"
        f"他に登録している {kpis['n_channels']:,} チャンネルを集計・ネットワーク化した結果、"
        "次の4点が分かった。出力はすべて集計で、個人の特定は行っていない。"
    )
    ws.cell(row=3, column=1, value=lead).alignment = WRAP
    ws.merge_cells("A3:H3")
    ws.row_dimensions[3].height = 44

    conclusions = [
        ("① 関心の中心",
         "視聴者の関心は自己啓発・ビジネス・教養系に強い。",
         f"最頻の併用登録先は「{kpis['top1_title']}」で、パネルの "
         f"約 {kpis['top1_share']*100:.0f}% が登録。"
         "（Chart - Top Channels / Top Channels シート）"),
        ("② 関心の広がり",
         "ただし単一ジャンルに偏らず、音楽・生活・エンタメ・ニュース等にも広がる多様な関心を持つ。",
         "Top150 チャンネルの分類で約半数が単一カテゴリに収まらず、"
         "多様性そのものが特徴。（Chart - Categories / Interest Categories シート）"),
        ("③ 人気 ≠ 親和性",
         "人気チャンネルと『自視聴者に親和性の高い』チャンネルは別物。"
         "登録率の高さだけでは視聴者との類似性・親和性は判断できない。",
         f"規模補正した親和性 lift は最大 {kpis['top_lift']:.0f}×。"
         "大型チャンネルは規模相応（lift≈1）で、規模が小さく目立たないが"
         "刺さっている『隠れた親和チャンネル』が存在する。"
         "（Chart - Pop vs Affinity / Affinity vs Popularity シート）"),
        ("④ 解釈上の限界",
         "この像は公開登録者に限定されるため、全視聴者の完全な代表ではない。",
         f"観測できたのは公開登録を持つ {kpis['n_viewers']:,} 人のみ"
         "（登録を非公開にしている層は観測不可・選択バイアスあり）。"
         "結論は傾向の把握に留め、断定は避ける。"),
    ]
    r = 5
    for tag, headline, detail in conclusions:
        tcell = ws.cell(row=r, column=1, value=tag)
        tcell.font = Font(bold=True, color="FFFFFF", size=12)
        tcell.fill = PatternFill("solid", fgColor=BLUE)
        tcell.alignment = Alignment(horizontal="center", vertical="center",
                                    wrap_text=True)
        tcell.border = BORDER
        hcell = ws.cell(row=r, column=2, value=headline)
        hcell.font = Font(bold=True, size=11, color=NAVY)
        hcell.alignment = WRAP
        hcell.border = BORDER
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=8)
        ws.row_dimensions[r].height = 36
        dcell = ws.cell(row=r + 1, column=2, value=detail)
        dcell.alignment = WRAP
        dcell.border = BORDER
        ws.merge_cells(start_row=r + 1, start_column=2, end_row=r + 1,
                       end_column=8)
        ws.cell(row=r + 1, column=1).border = BORDER
        ws.row_dimensions[r + 1].height = 46
        r += 2

    nav = ("読み方：グラフは『Chart -』で始まる5シート（① 共通登録率 / ② 興味カテゴリ / "
           "③ 人気vs親和性 / ④ 登録数分布 / ⑤ 動画比較）。手法・倫理は "
           "『Sources & Methodology』シート参照。")
    ws.cell(row=r + 1, column=1, value=nav).alignment = WRAP
    ws.cell(row=r + 1, column=1).font = Font(italic=True, color=GREY, size=10)
    ws.merge_cells(start_row=r + 1, start_column=1, end_row=r + 2, end_column=8)
    _autofit(ws, {1: 14, 2: 22, 3: 14, 4: 14, 5: 14, 6: 14, 7: 14, 8: 14})


def sheet_overview(wb, data, kpis):
    ws = wb.create_sheet("Overview")
    _title_block(ws, "YouTube オーディエンス・ネットワーク分析レポート",
                 "「視聴者は普段どんなチャンネルを見ているのか？」を集計データで再現")
    ws.sheet_view.showGridLines = False

    rows = [
        ("生成日時", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        ("分析の問い",
         "自チャンネルの視聴者（コメント投稿者の代理）が、他にどんなチャンネルへ"
         "登録しているかを集計・ネットワーク化して把握する。"),
        ("参考動画",
         "「視聴者は普段どんなチャンネルを見ているのか？」"
         "（チャンネル：データで語る棒人間, 7:38）"),
        ("手法の要約",
         "コメント投稿者の公開登録チャンネルから 視聴者×チャンネル の2部グラフを"
         "構築 → 共起（共視聴）ネットワークへ射影 → Louvain でコミュニティ抽出 → "
         "規模補正した親和性 lift を算出。出力は集計のみ（個人特定なし）。"),
        ("動画手法の再現フロー",
         "Step1) 自チャンネルの登録者リストを作成 → Step2) 彼らの登録する他チャンネルを取得 "
         "→ Step3) 視聴者×チャンネルのネットワークを構築・分析。"
         "トランスクリプトが得られたため、動画の手順に沿って再現した"
         "（詳細は『Video Method Reproduction』シート）。"),
        ("", ""),
        ("主要な発見", ""),
        ("1. 観測パネル規模",
         f"視聴者 {kpis['n_viewers']:,} 人、ユニーク登録先 {kpis['n_channels']:,} "
         f"チャンネル、エッジ {kpis['n_edges']:,} 本。"),
        ("2. 視聴者が他に見るTop",
         f"最頻の併用登録先は「{kpis['top1_title']}」"
         f"（パネルの {kpis['top1_share']*100:.0f}% が登録）。"
         "自己啓発・ビジネス・教養系が上位を占める。"),
        ("3. コミュニティ構造",
         f"{kpis['n_communities']} クラスタを検出。最大クラスタは "
         f"{kpis['biggest_community']:,} チャンネル。"
         "「ビジネス/自己啓発」「エンタメ/音楽」など系統が分離。"),
        ("4. 固有親和性",
         f"規模補正後の lift 最大は {kpis['top_lift']:.1f}×。"
         "大型チャンネルの汎用人気と、自視聴者に固有の親和性を区別できる。"),
        ("", ""),
        ("重要な限界", ""),
        ("L1",
         "視聴者の代理は「コメント投稿者かつ公開登録者」。登録を非公開にしている"
         "層は観測できず、母数は 808 人と小さい（選択バイアスあり）。"),
        ("L2",
         "YouTube Data/Analytics API には『視聴者が他に見るチャンネル』は無い。"
         "唯一の地上検証は YouTube Studio『視聴者』タブ（オーナー権限・手動）。"),
        ("L3",
         "本データのパネルは動画の1,000人サンプルより小さく、全パネルを採用した。"
         "動画の母集団10万人・約9.9万チャンネル・40万弱リンクは参考比較値として"
         "『Video Method Reproduction』に併記。"),
        ("L4",
         "興味カテゴリはチャンネル名のキーワードに基づく推定分類で、"
         "厳密なジャンル定義ではない。subscriptions API は1人約1,000件で打ち切られる。"),
    ]
    r = 4
    for label, val in rows:
        ws.cell(row=r, column=1, value=label).font = Font(bold=True, size=11,
                                                          color=NAVY)
        ws.cell(row=r, column=1).alignment = WRAP
        cell = ws.cell(row=r, column=2, value=val)
        cell.alignment = WRAP
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=8)
        if val and len(str(val)) > 60:
            ws.row_dimensions[r].height = 44
        r += 1
    _autofit(ws, {1: 22, 2: 18, 3: 14, 4: 14, 5: 14, 6: 14, 7: 14, 8: 14})


def sheet_data_profile(wb, data):
    ws = wb.create_sheet("Data Profile")
    ws.sheet_view.showGridLines = False
    _title_block(ws, "データプロファイル", "収集された各データセットの構造と品質指標")
    import pandas as pd
    rows = []
    for name, df in data.items():
        rows.append({
            "データセット": name,
            "行数": len(df),
            "列数": df.shape[1],
            "欠損セル合計": int(df.isna().sum().sum()),
            "完全重複行": int(df.duplicated().sum()),
            "主な列": ", ".join(map(str, df.columns[:6])),
        })
    prof = pd.DataFrame(rows)
    _write_df(ws, prof, start_row=4, int_cols=("行数", "列数", "欠損セル合計",
                                               "完全重複行"))
    _autofit(ws, {1: 32, 2: 12, 3: 8, 4: 14, 5: 12, 6: 60})


def sheet_data_dictionary(wb):
    ws = wb.create_sheet("Data Dictionary")
    ws.sheet_view.showGridLines = False
    _title_block(ws, "データ辞書", "列ごとの意味と算出根拠")
    import pandas as pd
    rows = [
        ("edges_anon", "channel_id", "登録先チャンネルID"),
        ("edges_anon", "channel_title", "登録先チャンネル名"),
        ("edges_anon", "viewer", "匿名化された視聴者ID（u0..）。コメント投稿者の代理"),
        ("channel_network_edges_filtered", "channel_id_a/b", "共起チャンネルのペア"),
        ("channel_network_edges_filtered", "shared_viewers", "両方に登録する共通視聴者数（≥3で採用）"),
        ("channel_network_edges_filtered", "fractional_overlap", "小さい側の登録者数で正規化した重なり率"),
        ("channel_network_edges_filtered", "weighted_cosine", "次数で重み付けしたコサイン類似度（エッジ重み）"),
        ("channel_communities", "community_id", "Louvain クラスタID"),
        ("channel_communities", "sample_viewers", "このチャンネルに登録するパネル視聴者数"),
        ("channel_communities", "internal_strength", "同一クラスタ内エッジ重みの合計（ハブ度）"),
        ("channel_community_summary", "n_channels / n_edges", "クラスタの規模（チャンネル数 / 内部エッジ数）"),
        ("channel_community_summary", "internal_weight", "クラスタ内エッジ重みの総和"),
        ("channel_community_summary", "representative_channels", "内部強度上位の代表チャンネル"),
        ("channel_panel_overlap_metrics", "observed_commenters", "このチャンネルに登録する観測コメント投稿者数"),
        ("channel_panel_overlap_metrics", "observed_panel_size", "公開登録が取得できたパネル総数（808）"),
        ("channel_panel_overlap_metrics", "sample_share", "パネル内浸透率 = observed / panel_size"),
        ("channel_panel_overlap_metrics", "sample_share_lcb95", "Wilson 95% 下限（少数サンプル補正）"),
        ("channel_panel_overlap_metrics", "total_subs", "チャンネル総登録者数（Data API）"),
        ("channel_panel_overlap_metrics", "penetration_per_1M_subs", "登録者100万人あたりのパネル浸透"),
        ("channel_panel_overlap_metrics", "affinity_lift", "規模補正済み親和性（中央値=1.0、>1で過剰登録）"),
        ("channel_statistics_cache", "total_subs", "Data API から取得した総登録者数キャッシュ"),
        ("channel_statistics_cache", "subscriber_count_hidden", "登録者数が非公開か"),
    ]
    dd = pd.DataFrame(rows, columns=["データセット", "列", "説明"])
    _write_df(ws, dd, start_row=4)
    _autofit(ws, {1: 34, 2: 24, 3: 70})


def sheet_quality(wb, data):
    ws = wb.create_sheet("Quality Checks")
    ws.sheet_view.showGridLines = False
    _title_block(ws, "品質チェック", "自動判定（PASS / WARN）")
    import pandas as pd
    ea = data["edges_anon"]
    ne = data["network_edges"]
    po = data["panel_overlap"]
    checks = [
        ("edges_anon に重複(channel_id,viewer)が無い",
         "PASS" if ea.duplicated(["channel_id", "viewer"]).sum() == 0 else "WARN",
         f"{ea.duplicated(['channel_id','viewer']).sum()} 件"),
        ("共起エッジは shared_viewers>=3",
         "PASS" if ne["shared_viewers"].min() >= 3 else "WARN",
         f"min={ne['shared_viewers'].min()}"),
        ("weighted_cosine は (0,1] の範囲",
         "PASS" if ne["weighted_cosine"].between(0, 1.0001).all() else "WARN",
         f"max={ne['weighted_cosine'].max():.3f}"),
        ("affinity_lift の中央値が約1.0に校正",
         "PASS" if abs(po["affinity_lift"].median() - 1.0) < 0.05 else "WARN",
         f"median={po['affinity_lift'].median():.3f}"),
        ("panel サイズが全行で一定(=観測パネル)",
         "PASS" if po["observed_panel_size"].nunique() == 1 else "WARN",
         f"unique={po['observed_panel_size'].nunique()}"),
        ("チャンネル統計の登録者数が非負",
         "PASS" if (data["channel_stats"]["total_subs"] >= 0).all() else "WARN",
         f"min={data['channel_stats']['total_subs'].min():,}"),
        ("観測パネル規模が小さい（解釈上の注意）",
         "WARN", f"viewers={ea['viewer'].nunique()}（選択バイアスに注意）"),
    ]
    qc = pd.DataFrame(checks, columns=["チェック項目", "判定", "詳細"])
    end = _write_df(ws, qc, start_row=4)
    # color the judgement column
    for i in range(len(qc)):
        cell = ws.cell(row=5 + i, column=2)
        cell.alignment = CENTER
        cell.fill = PatternFill("solid",
                                fgColor=GREEN if cell.value == "PASS" else AMBER)
        cell.font = Font(bold=True)
    _autofit(ws, {1: 44, 2: 10, 3: 40})


def sheet_summary_metrics(wb, kpis):
    ws = wb.create_sheet("Summary Metrics")
    ws.sheet_view.showGridLines = False
    _title_block(ws, "サマリー指標", "主要KPI（単一値）")
    import pandas as pd
    rows = [
        ("観測視聴者（パネル）数", kpis["n_viewers"], "公開登録が取れたコメント投稿者"),
        ("ユニーク登録先チャンネル数", kpis["n_channels"], "視聴者が登録する全チャンネル"),
        ("視聴者×チャンネル エッジ数", kpis["n_edges"], "2部グラフのエッジ"),
        ("視聴者あたり登録数（中央値）", kpis["median_breadth"], "1人が登録するチャンネル数"),
        ("共起ネットワーク・エッジ数", kpis["n_net_edges"], "shared_viewers>=3"),
        ("ネットワーク・ノード数", kpis["n_net_nodes"], "共起グラフ上のチャンネル"),
        ("検出コミュニティ数", kpis["n_communities"], "Louvain クラスタ"),
        ("最大コミュニティ規模", kpis["biggest_community"], "チャンネル数"),
        ("最大親和性 lift", round(kpis["top_lift"], 2), "規模補正後・中央値=1.0"),
        ("最頻併用登録先のパネル浸透", round(kpis["top1_share"], 4), kpis["top1_title"]),
    ]
    sm = pd.DataFrame(rows, columns=["指標", "値", "説明"])
    _write_df(ws, sm, start_row=4, int_cols=("値",))
    # last two rows are not ints -> fix format
    ws.cell(row=4 + 9, column=2).number_format = "0.00"
    ws.cell(row=4 + 10, column=2).number_format = "0.0%"
    _autofit(ws, {1: 34, 2: 16, 3: 44})


def sheet_top_channels(wb, data):
    ws = wb.create_sheet("Top Channels")
    ws.sheet_view.showGridLines = False
    _title_block(ws, "視聴者が他に見ているチャンネル Top50",
                 "動画の中心的な問いの再現：パネル視聴者の併用登録先ランキング")
    top = ac.audience_top_channels(data["edges_anon"], top_n=50)
    top.insert(0, "順位", range(1, len(top) + 1))
    top = top.rename(columns={"channel_title": "チャンネル名", "viewers": "登録視聴者数",
                              "panel_share": "パネル浸透率", "channel_id": "チャンネルID"})
    top = top[["順位", "チャンネル名", "登録視聴者数", "パネル浸透率", "チャンネルID"]]
    _write_df(ws, top, start_row=4, int_cols=("順位", "登録視聴者数"),
              pct_cols=("パネル浸透率",))
    _autofit(ws, {1: 6, 2: 42, 3: 14, 4: 14, 5: 26})


def sheet_affinity(wb, data):
    ws = wb.create_sheet("Affinity Lift")
    ws.sheet_view.showGridLines = False
    _title_block(ws, "固有親和性 lift Top40",
                 "規模で補正した親和性（>1 = サイズの割に自視聴者へ過剰登録）")
    top = ac.top_affinity(data["panel_overlap"], top_n=40)
    cols = ["channel", "observed_commenters", "sample_share", "total_subs",
            "penetration_per_1M_subs", "affinity_lift"]
    top = top[cols].rename(columns={
        "channel": "チャンネル名", "observed_commenters": "観測登録者数",
        "sample_share": "パネル浸透率", "total_subs": "総登録者数",
        "penetration_per_1M_subs": "100万人あたり浸透", "affinity_lift": "親和性lift"})
    top.insert(0, "順位", range(1, len(top) + 1))
    _write_df(ws, top, start_row=4,
              int_cols=("順位", "観測登録者数", "総登録者数"),
              pct_cols=("パネル浸透率",),
              float_cols=("100万人あたり浸透", "親和性lift"))
    _autofit(ws, {1: 6, 2: 40, 3: 12, 4: 12, 5: 14, 6: 16, 7: 12})


def sheet_communities(wb, data):
    ws = wb.create_sheet("Communities")
    ws.sheet_view.showGridLines = False
    _title_block(ws, "共視聴コミュニティ", "Louvain クラスタと代表チャンネル")
    cs = ac.community_sizes(data["community_summary"], min_channels=2)
    cs = cs.rename(columns={
        "community_id": "ID", "n_channels": "チャンネル数", "n_edges": "内部エッジ数",
        "internal_weight": "内部重み", "representative_channels": "代表チャンネル"})
    _write_df(ws, cs, start_row=4, int_cols=("ID", "チャンネル数", "内部エッジ数"),
              float_cols=("内部重み",))
    _autofit(ws, {1: 6, 2: 12, 3: 12, 4: 12, 5: 90})


def _comment(ws, cell_ref, text):
    """Write a 1-2 sentence read-out note in italic grey."""
    c = ws[cell_ref]
    c.value = text
    c.font = Font(italic=True, color=GREY, size=10)
    c.alignment = WRAP


def _write_chart_data(ws, df, start_row, start_col=1, int_cols=(), pct_cols=(),
                      float_cols=()):
    """Write a compact data block (header + rows) used as a chart source."""
    headers = list(df.columns)
    for j, h in enumerate(headers):
        cell = ws.cell(row=start_row, column=start_col + j, value=h)
        cell.fill = HEADER_FILL
        cell.font = WHITE_BOLD
        cell.border = BORDER
        cell.alignment = CENTER
    for i, (_, row) in enumerate(df.iterrows(), start=1):
        for j, h in enumerate(headers):
            cell = ws.cell(row=start_row + i, column=start_col + j, value=row[h])
            cell.border = BORDER
            if i % 2 == 0:
                cell.fill = BAND_FILL
            if h in int_cols:
                cell.number_format = "#,##0"
            elif h in pct_cols:
                cell.number_format = "0.0%"
            elif h in float_cols:
                cell.number_format = "0.00"
    return start_row + len(df)  # last data row


def sheet_chart_top_channels(wb, data):
    ws = wb.create_sheet("Chart - Top Channels")
    ws.sheet_view.showGridLines = False
    _title_block(ws, "① 共通登録率 Top チャンネル",
                 "視聴者が他に見ているチャンネル（パネル浸透率の高い順）")
    _comment(ws, "A3",
             "読み取り：自己啓発・ビジネス・教養系が上位を占める。"
             "最上位チャンネルはパネルの約3割が登録している。")
    ws.merge_cells("A3:H3")
    top = ac.audience_top_channels(data["edges_anon"], top_n=15)
    df = top[["channel_title", "panel_share"]].rename(
        columns={"channel_title": "チャンネル", "panel_share": "パネル浸透率"})
    last = _write_chart_data(ws, df, start_row=5, pct_cols=("パネル浸透率",))

    chart = BarChart()
    chart.type = "bar"
    chart.title = "共通登録率 Top15（パネル浸透率）"
    chart.y_axis.title = "パネル浸透率"
    chart.x_axis.title = "チャンネル"
    chart.height = 11
    chart.width = 24
    chart.legend = None
    data_ref = Reference(ws, min_col=2, min_row=5, max_row=last)
    cats = Reference(ws, min_col=1, min_row=6, max_row=last)
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(cats)
    chart.dataLabels = DataLabelList()
    chart.dataLabels.showVal = True
    chart.dataLabels.numFmt = "0.0%"
    ws.add_chart(chart, "D5")
    _autofit(ws, {1: 38, 2: 14})


def sheet_chart_categories(wb, data):
    ws = wb.create_sheet("Chart - Categories")
    ws.sheet_view.showGridLines = False
    _title_block(ws, "② 興味カテゴリ別の構成",
                 "Top150 チャンネルをキーワード分類した登録リンク数")
    _comment(ws, "A3",
             "読み取り：自己啓発・ビジネス・教養が中心だが、"
             "音楽/生活/ニュース等にも広がり、関心は単一ジャンルに偏らない。")
    ws.merge_cells("A3:H3")
    ib = ac.interest_breakdown(data["edges_anon"], top_n_channels=150)
    ib = ib[ib["category"] != "その他・未分類"].sort_values(
        "viewer_links", ascending=False)
    df = ib[["category", "viewer_links"]].rename(
        columns={"category": "興味カテゴリ", "viewer_links": "登録リンク数"})
    last = _write_chart_data(ws, df, start_row=5, int_cols=("登録リンク数",))

    chart = BarChart()
    chart.type = "bar"
    chart.title = "興味カテゴリ別の登録リンク数"
    chart.y_axis.title = "登録リンク数"
    chart.x_axis.title = "興味カテゴリ"
    chart.height = 10
    chart.width = 22
    chart.legend = None
    data_ref = Reference(ws, min_col=2, min_row=5, max_row=last)
    cats = Reference(ws, min_col=1, min_row=6, max_row=last)
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(cats)
    chart.dataLabels = DataLabelList()
    chart.dataLabels.showVal = True
    ws.add_chart(chart, "D5")
    _autofit(ws, {1: 26, 2: 14})


def sheet_chart_pop_vs_affinity(wb, data):
    ws = wb.create_sheet("Chart - Pop vs Affinity")
    ws.sheet_view.showGridLines = False
    _title_block(ws, "③ 人気 vs 親和性 lift",
                 "横軸=パネル浸透率（人気）、縦軸=規模補正 lift（親和性）")
    _comment(ws, "A3",
             "読み取り：人気と親和性は別物。人気上位でも lift≈1（規模相応）の"
             "チャンネルが多く、登録率だけでは類似性・親和性は判断できない。")
    ws.merge_cells("A3:H3")
    avp = ac.affinity_vs_popularity(data["panel_overlap"])
    # cap extreme lifts for a readable axis, keep a representative spread
    avp = avp.copy()
    avp["lift_capped"] = avp["affinity_lift"].clip(upper=20)
    df = avp[["sample_share", "lift_capped"]].rename(
        columns={"sample_share": "パネル浸透率", "lift_capped": "親和性lift(上限20)"})
    df = df.sort_values("パネル浸透率")
    last = _write_chart_data(ws, df, start_row=5, pct_cols=("パネル浸透率",),
                             float_cols=("親和性lift(上限20)",))

    chart = ScatterChart()
    chart.title = "人気（浸透率） vs 親和性 lift"
    chart.x_axis.title = "パネル浸透率（人気）"
    chart.y_axis.title = "親和性 lift（規模補正・上限20）"
    chart.height = 12
    chart.width = 22
    chart.legend = None
    xref = Reference(ws, min_col=1, min_row=6, max_row=last)
    yref = Reference(ws, min_col=2, min_row=6, max_row=last)
    series = Series(yref, xref, title="チャンネル")
    series.marker.symbol = "circle"
    series.marker.size = 4
    series.graphicalProperties.line.noFill = True
    chart.series.append(series)
    # reference line at lift=1 would need a second series; note it in the comment
    ws.add_chart(chart, "D5")
    _comment(ws, "D4", "（参考線 lift=1 = 規模相応の人気。これより上が固有親和性が高い）")
    _autofit(ws, {1: 16, 2: 18})


def sheet_chart_viewer_breadth(wb, data):
    ws = wb.create_sheet("Chart - Viewer Breadth")
    ws.sheet_view.showGridLines = False
    _title_block(ws, "④ 視聴者あたり登録チャンネル数の分布",
                 "1人の視聴者が公開登録しているチャンネル数のビン集計")
    _comment(ws, "A3",
             "読み取り：少数登録の視聴者が多い一方、1,000付近の山は "
             "subscriptions API の取得上限（約1,000件）による打ち切り。")
    ws.merge_cells("A3:H3")
    import numpy as np
    import pandas as pd
    breadth = ac.viewer_breadth(data["edges_anon"])
    bins = list(range(0, 1101, 100))
    labels = [f"{bins[i]}-{bins[i+1]-1}" for i in range(len(bins) - 1)]
    cut = pd.cut(breadth, bins=bins, labels=labels, include_lowest=True)
    counts = cut.value_counts().reindex(labels).fillna(0).astype(int)
    df = pd.DataFrame({"登録数ビン": labels, "視聴者数": counts.values})
    last = _write_chart_data(ws, df, start_row=5, int_cols=("視聴者数",))

    chart = BarChart()
    chart.type = "col"
    chart.title = "視聴者あたり公開登録チャンネル数の分布"
    chart.y_axis.title = "視聴者数（人）"
    chart.x_axis.title = "公開登録チャンネル数（ビン）"
    chart.height = 10
    chart.width = 22
    chart.legend = None
    data_ref = Reference(ws, min_col=2, min_row=5, max_row=last)
    cats = Reference(ws, min_col=1, min_row=6, max_row=last)
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(cats)
    chart.dataLabels = DataLabelList()
    chart.dataLabels.showVal = True
    ws.add_chart(chart, "D5")
    _autofit(ws, {1: 16, 2: 12})


def sheet_chart_video_compare(wb, kpis):
    ws = wb.create_sheet("Chart - Video Compare")
    ws.sheet_view.showGridLines = False
    _title_block(ws, "⑤ 動画手法との規模比較",
                 "動画で言及された値と本データ実測値（対数スケール推奨）")
    _comment(ws, "A3",
             "読み取り：本データは動画と同じ桁感のネットワーク規模を再現。"
             "視聴者サンプルは808人（動画は1,000人）とやや小さい。")
    ws.merge_cells("A3:H3")
    import pandas as pd
    ref = ac.VIDEO_REF
    df = pd.DataFrame({
        "項目": ["サンプル視聴者数", "チャンネル数", "リンク（エッジ）数"],
        "動画（参考値）": [ref["sample"], ref["channels"], ref["links"]],
        "本データ（実測）": [kpis["n_viewers"], kpis["n_channels"], kpis["n_edges"]],
    })
    last = _write_chart_data(ws, df, start_row=5,
                             int_cols=("動画（参考値）", "本データ（実測）"))

    chart = BarChart()
    chart.type = "col"
    chart.title = "動画 vs 本データ（規模比較）"
    chart.y_axis.title = "件数"
    chart.x_axis.title = "項目"
    chart.y_axis.scaling.logBase = 10  # wide range -> log scale for readability
    chart.height = 10
    chart.width = 20
    data_ref = Reference(ws, min_col=2, min_row=5, max_col=3, max_row=last)
    cats = Reference(ws, min_col=1, min_row=6, max_row=last)
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(cats)
    ws.add_chart(chart, "E5")
    _autofit(ws, {1: 18, 2: 16, 3: 16})


def sheet_sources(wb):
    ws = wb.create_sheet("Sources & Methodology")
    ws.sheet_view.showGridLines = False
    _title_block(ws, "出典・手法", "方法論と背景の参照先（URL付き）")
    import pandas as pd
    rows = [
        ("参考動画", "視聴者は普段どんなチャンネルを見ているのか？（データで語る棒人間）",
         "https://www.youtube.com/watch?v=ec26DzgBKIU"),
        ("YouTube Data API", "channels/commentThreads/subscriptions エンドポイント仕様",
         "https://developers.google.com/youtube/v3/docs"),
        ("YouTube Analytics API", "視聴者指標（『他に見るチャンネル』は非提供）",
         "https://developers.google.com/youtube/analytics"),
        ("NetworkX", "グラフ構築・中心性・連結成分",
         "https://networkx.org/documentation/stable/"),
        ("Louvain 法", "コミュニティ検出（モジュラリティ最大化）",
         "https://python-louvain.readthedocs.io/"),
        ("Adjusted Rand Index", "クラスタ安定性の指標（scikit-learn）",
         "https://scikit-learn.org/stable/modules/generated/sklearn.metrics.adjusted_rand_score.html"),
        ("Wilson score interval", "少数サンプルの二項割合の信頼下限",
         "https://en.wikipedia.org/wiki/Binomial_proportion_confidence_interval"),
        ("Okabe-Ito palette", "色覚多様性に配慮した配色",
         "https://jfly.uni-koeln.de/color/"),
        ("Cosine similarity", "共起ベクトルの類似度（エッジ重み）",
         "https://en.wikipedia.org/wiki/Cosine_similarity"),
        ("性格推定研究 (2013)",
         "Kosinski, Stillwell & Graepel『Private traits and attributes are "
         "predictable from digital records of human behavior』PNAS. "
         "Facebook の Like から性格・属性が予測可能（動画で言及）",
         "https://www.pnas.org/doi/10.1073/pnas.1218772110"),
        ("性格推定研究 (2015)",
         "Youyou, Kosinski & Stillwell『Computer-based personality judgments "
         "are more accurate than those made by humans』PNAS",
         "https://www.pnas.org/doi/10.1073/pnas.1418680112"),
    ]
    src = pd.DataFrame(rows, columns=["カテゴリ", "内容", "URL"])
    end = _write_df(ws, src, start_row=4)
    # hyperlink the URL column
    for i in range(len(src)):
        cell = ws.cell(row=5 + i, column=3)
        cell.hyperlink = cell.value
        cell.font = Font(color="0563C1", underline="single")
    methodology = (
        "手法（動画の手順に沿った再現）: "
        "Step1) 自チャンネルに登録しているユーザー（公開登録を持つコメント投稿者を代理）を収集。"
        "Step2) 各ユーザーの『公開』登録チャンネルを取得し 視聴者×チャンネル の2部グラフを構築（匿名化）。"
        "Step3) 視聴者とチャンネルを線でつないだネットワークを構築・分析。"
        "サンプリング：動画は10万人から1,000人を無作為抽出（公開登録ONのみ、非公開は除外し公開で補充）。"
        "本データのパネルは1,000人未満のため全員を採用し、seed固定のサンプル版も用意。"
        "共起ネットワークは 共通視聴者≥3 のチャンネル対を次数重み付きコサイン類似度で射影し、"
        "Louvain でコミュニティ検出。親和性 lift はチャンネル規模で割り中央値=1.0 に校正"
        "（人気＝親和性ではない、という動画の注意点に対応）。"
        "制約：『他に見るチャンネル』は YouTube Data/Analytics API に無く、地上検証は Studio『視聴者』タブのみ。"
        "subscriptions API は1ユーザー約1,000件で打ち切られる。"
    )
    ethics = (
        "プライバシー・倫理: 本分析は公開された登録情報のみを扱い、個人の特定や"
        "個人単位の性格・属性の推定は行わない（出力は集計のみ）。"
        "ただし SNS 上の行動（Like・登録など）から性格や属性が高い精度で推測され得ることは"
        "上記 Kosinski ら(2013)・Youyou ら(2015) が示しており、集計であっても扱いには配慮する。"
        "視聴者像はジャンル・系統という粗い粒度に留め、コメント投稿者IDは中間データとしてのみ"
        "メモリ上で扱い最終出力では匿名化（u0, u1…）する。"
    )
    mr = end + 1
    ws.cell(row=mr, column=1, value="方法論ノート").font = Font(bold=True, color=NAVY)
    mc = ws.cell(row=mr + 1, column=1, value=methodology)
    mc.alignment = WRAP
    ws.merge_cells(start_row=mr + 1, start_column=1, end_row=mr + 6, end_column=3)
    er = mr + 7
    ws.cell(row=er, column=1, value="プライバシー・倫理").font = Font(bold=True, color=NAVY)
    ec = ws.cell(row=er + 1, column=1, value=ethics)
    ec.alignment = WRAP
    ws.merge_cells(start_row=er + 1, start_column=1, end_row=er + 6, end_column=3)
    _autofit(ws, {1: 22, 2: 56, 3: 70})


def sheet_video_method(wb, data, kpis):
    ws = wb.create_sheet("Video Method Reproduction")
    ws.sheet_view.showGridLines = False
    _title_block(ws, "動画手法の再現フロー",
                 "「視聴者は普段どんなチャンネルを見ているのか？」の手順を本データで再現")
    ref = ac.VIDEO_REF
    steps = [
        ("Step 1", "自チャンネルに登録しているユーザー（視聴者）のリストを作成",
         f"本データ：公開登録を持つコメント投稿者 {kpis['n_viewers']:,} 人を視聴者の代理として観測"),
        ("Step 2", "そのユーザーたちが登録している他チャンネルのリストを取得",
         f"本データ：ユニーク登録先 {kpis['n_channels']:,} チャンネル / エッジ {kpis['n_edges']:,} 本"),
        ("Step 3", "視聴者とチャンネルを線でつないだ2部ネットワークを構築・分析",
         f"本データ：共起ネットワーク ノード {kpis['n_net_nodes']:,} / エッジ {kpis['n_net_edges']:,}"),
        ("サンプリング", "母集団から無作為抽出。公開登録設定ONのユーザーのみ採用、"
         "非公開は除外し公開で補充",
         f"本データのパネルは {kpis['n_viewers']:,} 人（動画の {ref['sample']:,} 人サンプル未満）。"
         "全員が公開登録者のため公開のみで構成。seed=42 のサンプル版も用意"),
        ("可視化", "中心チャンネル→視聴者→他チャンネルの構造を図示し、"
         "登録リンクを多く集めるチャンネルをランキング",
         "Chart - Top Channels（①Top15）/ Chart - Categories（②興味カテゴリ）シート"),
        ("親和性", "人気ランキングだけでは親和性は分からない、という注意点に基づき"
         "規模を考慮した親和性指標を算出",
         "Chart - Pop vs Affinity（③人気vs親和性）/ Affinity Lift / "
         "Affinity vs Popularity シート参照"),
    ]
    import pandas as pd
    df = pd.DataFrame(steps, columns=["段階", "動画での手法", "本データでの再現"])
    end = _write_df(ws, df, start_row=4)

    # comparison table: video's stated figures vs our measured ones
    cmp_row = end + 1
    ws.cell(row=cmp_row, column=1,
            value="動画で言及された数値 vs 本データ実測").font = Font(bold=True,
                                                                color=NAVY, size=12)
    cmp = pd.DataFrame([
        ("サンプル視聴者数", f"{ref['sample']:,} 人（10万人から抽出）",
         f"{kpis['n_viewers']:,} 人（全パネル）"),
        ("チャンネル数", f"約 {ref['channels']:,}", f"{kpis['n_channels']:,}"),
        ("リンク（エッジ）数", f"約 {ref['links']:,}", f"{kpis['n_edges']:,}"),
    ], columns=["項目", "動画（参考値）", "本データ（実測）"])
    _write_df(ws, cmp, start_row=cmp_row + 1, band=True)
    _autofit(ws, {1: 16, 2: 52, 3: 52})


def sheet_subscriber_sample(wb, data):
    ws = wb.create_sheet("Subscriber Sample")
    ws.sheet_view.showGridLines = False
    _title_block(ws, "視聴者サンプル（Step 1）",
                 f"seed={ac.VIDEO_SEED} 固定。動画の1,000人抽出を再現（パネルが小さい場合は全員）")
    import pandas as pd
    ea = data["edges_anon"]
    sample = ac.subscriber_sample(ea)
    breadth = sample.groupby("viewer")["channel_id"].nunique()
    note = (f"観測パネル {ea['viewer'].nunique():,} 人 ≦ 動画サンプル {ac.VIDEO_SAMPLE_SIZE:,} 人 "
            f"のため全員を採用。全員が公開登録者（非公開除外の前提を満たす）。"
            f"1人あたり公開登録数：中央値 {int(breadth.median())} / 最大 {int(breadth.max())} "
            f"（最大値1,000付近は subscriptions API の取得上限による打ち切り）。")
    ws.cell(row=3, column=1, value=note).alignment = WRAP
    ws.merge_cells("A3:H3")
    ws.row_dimensions[3].height = 44

    # per-viewer breadth table (anonymous ids only)
    tbl = (breadth.sort_values(ascending=False).reset_index()
           .rename(columns={"viewer": "視聴者ID（匿名）", "channel_id": "公開登録チャンネル数"}))
    tbl.insert(0, "順位", range(1, len(tbl) + 1))
    _write_df(ws, tbl, start_row=5, int_cols=("順位", "公開登録チャンネル数"))
    _autofit(ws, {1: 8, 2: 20, 3: 20})


def sheet_interest_categories(wb, data):
    ws = wb.create_sheet("Interest Categories")
    ws.sheet_view.showGridLines = False
    _title_block(ws, "興味カテゴリ（ジャンル整理）",
                 "Top150 チャンネルをキーワードで分類し、視聴者の興味関心を推定")
    import pandas as pd
    ib = ac.interest_breakdown(data["edges_anon"], top_n_channels=150)
    ib = ib.rename(columns={
        "category": "興味カテゴリ", "n_channels": "チャンネル数",
        "viewer_links": "登録リンク数", "link_share": "リンク占有率"})
    _write_df(ws, ib, start_row=4, int_cols=("チャンネル数", "登録リンク数"),
              pct_cols=("リンク占有率",))
    note = ("分類は『数学/科学/生物・株/お金・読書・ホラー/ミステリー・教養・ビジネス・"
            "雑学』等の動画の興味例に対応するキーワード辞書による自動分類。"
            "『その他・未分類』が一定割合あるのは、視聴者の関心が特定ジャンルに偏らず"
            "多様（音楽/アート/映画/生活など）であることを示す。"
            "カテゴリは channel タイトル文字列に基づく推定であり厳密な分類ではない。")
    nr = 4 + len(ib) + 2
    ws.cell(row=nr, column=1, value=note).alignment = WRAP
    ws.merge_cells(start_row=nr, start_column=1, end_row=nr + 3, end_column=6)
    _autofit(ws, {1: 26, 2: 12, 3: 14, 4: 12})


def sheet_affinity_vs_popularity(wb, data):
    ws = wb.create_sheet("Affinity vs Popularity")
    ws.sheet_view.showGridLines = False
    _title_block(ws, "人気 ≠ 親和性",
                 "人気ランキングだけでは親和性は分からない（動画の注意点）を指標化")
    import pandas as pd
    avp = ac.affinity_vs_popularity(data["panel_overlap"])
    # channels that are far more 'affine' than their popularity rank suggests
    hidden_gems = avp.sort_values("rank_gap", ascending=False).head(25)
    cols = ["channel", "sample_share", "popularity_rank", "affinity_lift",
            "affinity_rank", "rank_gap", "total_subs"]
    gems = hidden_gems[cols].rename(columns={
        "channel": "チャンネル名", "sample_share": "パネル浸透率(人気)",
        "popularity_rank": "人気順位", "affinity_lift": "親和性lift",
        "affinity_rank": "親和性順位", "rank_gap": "順位差(人気-親和性)",
        "total_subs": "総登録者数"})
    note = ("『順位差』が大きい = 人気順位は低いのに親和性順位は高い、"
            "つまり規模が小さいため目立たないが自視聴者には刺さっている"
            "『隠れた親和チャンネル』。lift は チャンネル規模（総登録者数）で割って"
            "中央値=1.0 に校正した値。外部の登録者数が無いチャンネルはデータ内の"
            "サイズ代理指標で補正する設計（本データには Data API の登録者数あり）。")
    ws.cell(row=3, column=1, value=note).alignment = WRAP
    ws.merge_cells("A3:H3")
    ws.row_dimensions[3].height = 56
    _write_df(ws, gems, start_row=5,
              int_cols=("人気順位", "親和性順位", "順位差(人気-親和性)", "総登録者数"),
              pct_cols=("パネル浸透率(人気)",), float_cols=("親和性lift",))
    _autofit(ws, {1: 40, 2: 16, 3: 10, 4: 12, 5: 10, 6: 18, 7: 14})


def compute_kpis(data):
    ea = data["edges_anon"]
    ne = data["network_edges"]
    cs = data["community_summary"]
    po = data["panel_overlap"]
    top = ac.audience_top_channels(ea, top_n=1).iloc[0]
    nodes = set(ne["channel_id_a"]) | set(ne["channel_id_b"])
    return {
        "n_viewers": int(ea["viewer"].nunique()),
        "n_channels": int(ea["channel_id"].nunique()),
        "n_edges": int(len(ea)),
        "median_breadth": int(ac.viewer_breadth(ea).median()),
        "n_net_edges": int(len(ne)),
        "n_net_nodes": int(len(nodes)),
        "n_communities": int(cs["community_id"].nunique()),
        "biggest_community": int(cs["n_channels"].max()),
        "top_lift": float(po["affinity_lift"].max()),
        "top1_title": str(top["channel_title"]),
        "top1_share": float(top["panel_share"]),
    }


def main() -> str:
    ac.configure_fonts()
    data = ac.load_all()
    kpis = compute_kpis(data)

    wb = openpyxl.Workbook()
    sheet_executive_summary(wb, data, kpis)
    sheet_overview(wb, data, kpis)
    sheet_video_method(wb, data, kpis)
    # native Excel charts (no embedded PNGs) -- the visual story of the report
    sheet_chart_top_channels(wb, data)
    sheet_chart_categories(wb, data)
    sheet_chart_pop_vs_affinity(wb, data)
    sheet_chart_viewer_breadth(wb, data)
    sheet_chart_video_compare(wb, kpis)
    sheet_data_profile(wb, data)
    sheet_data_dictionary(wb)
    sheet_quality(wb, data)
    sheet_summary_metrics(wb, kpis)
    sheet_subscriber_sample(wb, data)
    sheet_top_channels(wb, data)
    sheet_interest_categories(wb, data)
    sheet_affinity_vs_popularity(wb, data)
    sheet_affinity(wb, data)
    sheet_communities(wb, data)
    sheet_sources(wb)

    out = ac.REPO_ROOT / "youtube_network_analysis_report.xlsx"
    wb.save(out)
    print(f"wrote {out.relative_to(ac.REPO_ROOT)}  ({out.stat().st_size//1024} KB)")
    return str(out)


if __name__ == "__main__":
    main()
