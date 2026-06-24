"""内田さんチャンネル分析資料.xlsx を、リポジトリの CSV データを基に改善する。

改善内容
--------
1. **ジャンル分けの深化**（② シート）:
   従来は浅い 7 ジャンル辞書で Top150 の 89 ch / 6,767 リンク（約半分）が
   「その他・未分類」に落ちていた。``genre_classifier`` の 10 ジャンル深堀り分類
   （✨スピリチュアル・引き寄せ / 🧠心理・メンタルを新設）に置き換え、未分類を 0 件に。
   これにより「スピリチュアル・引き寄せ」がこの層の第 2 の関心であることが可視化される。
2. **ネイティブ Excel チャートの追加**（PNG ではなく編集可能なチャートオブジェクト）:
   ① 人気チャンネル横棒 / ② ジャンル別 縦棒＋円 / ③ 隠れた相性 横棒 /
   ④ 登録数分布 縦棒 / ⑤ 視聴者グループ 縦棒 / 📊 ダッシュボードにジャンル概況。
3. **① の「ジャンル」列を ② と同じ 10 ジャンル体系へ統一**（従来は ①②で不一致）。

すべての数値はリポジトリ直下の CSV（ノートブックの生成物）から再計算する。
スクリプトは冪等（実行のたびにチャートをクリアして再生成、② を作り直す）。

使い方:
    python audience_analysis/improve_excel.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import openpyxl
from openpyxl.chart import BarChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.chart.marker import DataPoint
from openpyxl.styles import Font, PatternFill, Alignment

sys.path.insert(0, str(Path(__file__).resolve().parent))
from genre_classifier import classify, GENRE_KEYWORDS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
XLSX = ROOT / "内田さんチャンネル分析資料.xlsx"

# --- 既存レポートの配色パレット（変更しない） ---
DARK = "FF1F2937"
GRAY = "FF6B7280"
WHITE = "FFFFFFFF"
ZEBRA = "FFF9FAFB"
TOTAL_BG = "FFEFF6FF"
GREEN = "FF0E9F6E"     # ② テーマ
BLUE = "FF1A56DB"      # ① テーマ
AMBER = "FFD97706"     # ③ テーマ
PURPLE = "FF7C3AED"    # ④ テーマ
CYAN = "FF0891B2"      # ⑤ テーマ

# 10 ジャンルの表示順とチャート用カラー（絵文字の色味に合わせる）
GENRE_COLORS = {
    "🌱 自己啓発・マインド": "10B981",
    "✨ スピリチュアル・引き寄せ": "8B5CF6",
    "💼 ビジネス・投資・お金": "0E9F6E",
    "📚 読書・要約・教養": "F59E0B",
    "📰 ニュース・社会・政治": "EF4444",
    "🔮 雑学・ミステリー・都市伝説": "6366F1",
    "💆 美容・健康・生活": "EC4899",
    "🎵 エンタメ・音楽・芸能": "F97316",
    "🧠 心理・メンタル": "14B8A6",
    "🔬 科学・数学・テクノロジー": "3B82F6",
}


def font(bold=False, sz=10, color=DARK):
    return Font(name="Calibri", bold=bold, size=sz, color=color)


def fill(rgb):
    return PatternFill("solid", fgColor=rgb)


# --------------------------------------------------------------------------
# データ計算
# --------------------------------------------------------------------------
def load_data():
    e = pd.read_csv(ROOT / "edges_anon.csv", dtype={"channel_id": str})
    m = pd.read_csv(ROOT / "channel_panel_overlap_metrics.csv", dtype={"channel_id": str})
    cs = pd.read_csv(ROOT / "channel_community_summary.csv")
    cc = pd.read_csv(ROOT / "channel_communities.csv", dtype={"channel_id": str})
    return e, m, cs, cc


def top_channels(e, n):
    title = (e.dropna(subset=["channel_title"]).drop_duplicates("channel_id")
             .set_index("channel_id")["channel_title"])
    counts = e.groupby("channel_id")["viewer"].nunique().sort_values(ascending=False)
    out = counts.head(n).rename("viewers").reset_index()
    out["title"] = out["channel_id"].map(title)
    out["genre"] = out["title"].map(classify)
    panel = e["viewer"].nunique()
    out["share"] = out["viewers"] / panel
    return out, panel


def genre_breakdown(e, top_n=150):
    top, _ = top_channels(e, top_n)
    g = (top.groupby("genre")
         .agg(n_channels=("channel_id", "size"), links=("viewers", "sum"))
         .reset_index())
    g["share"] = g["links"] / g["links"].sum()
    # 表示順 = リンク数降順
    return g.sort_values("links", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------
# チャート・ヘルパー
# --------------------------------------------------------------------------
def clear_charts(wb):
    for ws in wb.worksheets:
        ws._charts = []


def style_bar(ch, color_hex, title, horizontal=False):
    ch.type = "bar" if horizontal else "col"
    ch.title = title
    ch.legend = None
    ch.height = 8.5
    ch.width = 17
    ch.gapWidth = 60
    s = ch.series[0]
    s.graphicalProperties.solidFill = color_hex
    ch.dLbls = DataLabelList()
    ch.dLbls.showVal = True
    ch.dLbls.numFmt = "#,##0"
    return ch


# --------------------------------------------------------------------------
# ② 視聴者の興味ジャンル を作り直す
# --------------------------------------------------------------------------
def rebuild_genre_sheet(wb, gb):
    ws = wb["② 視聴者の興味ジャンル"]
    # 既存セル・結合を全消去
    for mc in list(ws.merged_cells.ranges):
        ws.unmerge_cells(str(mc))
    for row in ws.iter_rows():
        for c in row:
            c.value = None
            c.fill = PatternFill()
            c.font = Font()
            c.alignment = Alignment()

    total_links = int(gb["links"].sum())
    total_ch = int(gb["n_channels"].sum())

    # タイトル
    ws.merge_cells("B2:E2")
    ws["B2"] = "② 視聴者が興味を持っているジャンルの内訳（Top150チャンネル分析）"
    ws["B2"].font = font(bold=True, sz=15, color=DARK)
    ws["B2"].alignment = Alignment(horizontal="left", vertical="center")
    ws.merge_cells("B3:E3")
    ws["B3"] = ("視聴者がよく見るTop150チャンネルを内容ジャンル別に整理（10ジャンルに深堀り分類、"
                "未分類0件）。最大は「ビジネス・投資・お金」、次いで「スピリチュアル・引き寄せ」が続く。")
    ws["B3"].font = font(sz=10, color=GRAY)
    ws["B3"].alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

    # ヘッダー（row 5）
    headers = ["ジャンル", "チャンネル数", "視聴者の登録数\n（延べ人数）", "このジャンルの\n占める割合"]
    for i, h in enumerate(headers):
        c = ws.cell(5, 2 + i, h)
        c.fill = fill(GREEN)
        c.font = font(bold=True, sz=10, color=WHITE)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # データ行（row 6..）
    r = 6
    for _, row in gb.iterrows():
        bg = ZEBRA if (r % 2 == 0) else WHITE
        c2 = ws.cell(r, 2, row["genre"]); c2.font = font(bold=True)
        c3 = ws.cell(r, 3, int(row["n_channels"])); c3.font = font(); c3.number_format = "#,##0"
        c4 = ws.cell(r, 4, int(row["links"])); c4.font = font(); c4.number_format = "#,##0"
        c5 = ws.cell(r, 5, float(row["share"])); c5.font = font(bold=True); c5.number_format = "0.0%"
        for col in range(2, 6):
            cell = ws.cell(r, col)
            cell.fill = fill(bg)
            cell.alignment = Alignment(horizontal=("left" if col == 2 else "center"),
                                       vertical="center")
        r += 1

    # 合計行
    tot = r
    ws.cell(tot, 2, "合計（全Top150・未分類0）").font = font(bold=True)
    ws.cell(tot, 3, total_ch).number_format = "#,##0"
    ws.cell(tot, 4, total_links).number_format = "#,##0"
    ws.cell(tot, 5, 1.0).number_format = "0.0%"
    for col in range(2, 6):
        cell = ws.cell(tot, col)
        cell.fill = fill(TOTAL_BG)
        cell.font = font(bold=True)
        cell.alignment = Alignment(horizontal=("left" if col == 2 else "center"),
                                   vertical="center")

    # 注記
    note_row = tot + 2
    ws.merge_cells(start_row=note_row, start_column=2, end_row=note_row, end_column=5)
    nc = ws.cell(note_row, 2,
                 "※ ジャンルはチャンネル名のキーワード辞書＋著名チャンネルの手動対応表で推定（推定分類であり"
                 "厳密なジャンル定義ではない）。従来版は7ジャンルで89ch・6,767リンクが「未分類」だったが、"
                 "10ジャンルへ深堀りし未分類を0件に改善した。延べ人数＝そのジャンルの各chを登録する視聴者数の合計。")
    nc.font = font(sz=9, color=GRAY)
    nc.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
    ws.row_dimensions[note_row].height = 46

    # 列幅
    ws.column_dimensions["A"].width = 2
    ws.column_dimensions["B"].width = 26
    ws.column_dimensions["C"].width = 13
    ws.column_dimensions["D"].width = 17
    ws.column_dimensions["E"].width = 15

    data_last = tot - 1  # 最終データ行

    # --- 縦棒チャート（ジャンル別 延べ登録数） ---
    bar = BarChart()
    data = Reference(ws, min_col=4, min_row=5, max_row=data_last)      # D列 + ヘッダ
    cats = Reference(ws, min_col=2, min_row=6, max_row=data_last)      # B列
    bar.add_data(data, titles_from_data=True)
    bar.set_categories(cats)
    style_bar(bar, GREEN.replace("FF", ""), "ジャンル別 視聴者の登録数（延べ人数・Top150）")
    bar.y_axis.title = "延べ登録数（人）"
    bar.x_axis.delete = False
    bar.y_axis.delete = False
    ws.add_chart(bar, "G2")

    # --- 円グラフ（ジャンル構成比） ---
    pie = PieChart()
    pdata = Reference(ws, min_col=4, min_row=5, max_row=data_last)
    pie.add_data(pdata, titles_from_data=True)
    pie.set_categories(cats)
    pie.title = "ジャンル構成比（延べ登録数ベース）"
    pie.height = 9.5
    pie.width = 13
    pie.dLbls = DataLabelList()
    pie.dLbls.showPercent = True
    # 各スライスにジャンル色
    series = pie.series[0]
    for i in range(len(gb)):
        genre = gb.iloc[i]["genre"]
        hexc = GENRE_COLORS.get(genre, "9CA3AF")
        dp = DataPoint(idx=i)
        dp.graphicalProperties.solidFill = hexc
        series.data_points.append(dp)
    ws.add_chart(pie, "G20")

    return data_last, total_links


# --------------------------------------------------------------------------
# ① ジャンル列の統一 + 横棒チャート
# --------------------------------------------------------------------------
def update_top_sheet(wb, top50):
    ws = wb["① 人気チャンネルTop50"]
    # genre 列 = F（row 6..55）。順位順に並んでいる前提で title をキーに更新。
    by_title = {t: g for t, g in zip(top50["title"], top50["genre"])}
    for r in range(6, 56):
        name = ws.cell(r, 3).value
        if name in by_title:
            ws.cell(r, 6).value = by_title[name]

    # 横棒チャート: 上位15chの登録者率
    bar = BarChart()
    data = Reference(ws, min_col=5, min_row=5, max_row=20)   # E列(登録者率) + ヘッダ, 上位15
    cats = Reference(ws, min_col=3, min_row=6, max_row=20)   # C列(チャンネル名)
    bar.add_data(data, titles_from_data=True)
    bar.set_categories(cats)
    bar.type = "bar"
    bar.title = "登録者率トップ15（本チャンネル視聴者808人に占める割合）"
    bar.legend = None
    bar.height = 9.5
    bar.width = 18
    bar.gapWidth = 50
    s = bar.series[0]
    s.graphicalProperties.solidFill = BLUE.replace("FF", "")
    bar.dLbls = DataLabelList()
    bar.dLbls.showVal = True
    bar.dLbls.numFmt = "0.0%"
    bar.x_axis.numFmt = "0%"
    bar.y_axis.delete = False
    bar.x_axis.delete = False
    ws.add_chart(bar, "H5")


# --------------------------------------------------------------------------
# ③ 隠れた相性 横棒チャート
# --------------------------------------------------------------------------
def add_affinity_chart(wb):
    ws = wb["③ 隠れた相性チャンネル"]
    # 表ヘッダ row8, データ row9..48。スコア列 = G(7), 名前列 = C(3)。上位15。
    bar = BarChart()
    data = Reference(ws, min_col=7, min_row=8, max_row=23)
    cats = Reference(ws, min_col=3, min_row=9, max_row=23)
    bar.add_data(data, titles_from_data=True)
    bar.set_categories(cats)
    bar.type = "bar"
    bar.title = "隠れた相性スコア トップ15（規模補正の親和性 lift・倍）"
    bar.legend = None
    bar.height = 9.5
    bar.width = 18
    bar.gapWidth = 50
    s = bar.series[0]
    s.graphicalProperties.solidFill = AMBER.replace("FF", "")
    bar.dLbls = DataLabelList()
    bar.dLbls.showVal = True
    bar.dLbls.numFmt = "0"
    bar.x_axis.title = "一般ユーザー比の登録されやすさ（倍）"
    bar.y_axis.delete = False
    bar.x_axis.delete = False
    ws.add_chart(bar, "I9")


# --------------------------------------------------------------------------
# ④ 登録数分布 縦棒チャート（再生成）
# --------------------------------------------------------------------------
def add_distribution_chart(wb):
    ws = wb["④ 登録数の分布"]
    # ヘッダ row5, データ row6..16(0-99..1000-1099), 合計 row17。範囲列=B, 人数列=C。
    bar = BarChart()
    data = Reference(ws, min_col=3, min_row=5, max_row=16)
    cats = Reference(ws, min_col=2, min_row=6, max_row=16)
    bar.add_data(data, titles_from_data=True)
    bar.set_categories(cats)
    style_bar(bar, PURPLE.replace("FF", ""), "視聴者1人あたりの公開登録チャンネル数の分布")
    bar.y_axis.title = "視聴者数（人）"
    bar.x_axis.title = "登録チャンネル数の範囲"
    bar.height = 9
    bar.width = 17
    bar.y_axis.delete = False
    bar.x_axis.delete = False
    ws.add_chart(bar, "F5")


# --------------------------------------------------------------------------
# ⑤ 視聴者グループ 縦棒チャート
# --------------------------------------------------------------------------
def add_community_chart(wb):
    ws = wb["⑤ 視聴者グループ"]
    # ヘッダ row5, データ row6..25。グループ名=B, ch数=C。上位12を対数で。
    bar = BarChart()
    data = Reference(ws, min_col=3, min_row=5, max_row=17)   # 上位12
    cats = Reference(ws, min_col=2, min_row=6, max_row=17)
    bar.add_data(data, titles_from_data=True)
    bar.set_categories(cats)
    style_bar(bar, CYAN.replace("FF", ""), "視聴者グループ別 チャンネル数（上位12クラスタ）")
    bar.y_axis.title = "グループ内チャンネル数"
    bar.height = 9
    bar.width = 18
    bar.y_axis.delete = False
    bar.x_axis.delete = False
    ws.add_chart(bar, "G5")


# --------------------------------------------------------------------------
# 📊 ダッシュボード ジャンル概況チャート（② を参照）
# --------------------------------------------------------------------------
def update_dashboard_text(wb, gb):
    """ジャンル深堀りの結果をダッシュボードの説明文へ反映する（.value のみ更新）。"""
    g = gb.set_index("genre")
    biz = int(g.loc["💼 ビジネス・投資・お金", "links"]) if "💼 ビジネス・投資・お金" in g.index else 0
    spi = int(g.loc["✨ スピリチュアル・引き寄せ", "links"]) if "✨ スピリチュアル・引き寄せ" in g.index else 0
    wsd = wb["📊 ダッシュボード"]
    wsd["B13"].value = "② 関心はビジネス・投資とスピリチュアル・引き寄せの二本柱"
    wsd["C13"].value = (
        f"Top150チャンネルを10ジャンルに深堀り分類（未分類0件）すると、最大は「ビジネス・投資・お金」"
        f"（延べ{biz:,}登録）、僅差で「スピリチュアル・引き寄せ」（延べ{spi:,}登録）が続く。後者はこの層に"
        "特徴的で、隠れた相性スコア上位や視聴者グループ#3とも符合する。自己啓発・読書教養・エンタメ等にも"
        "幅広く広がり、関心は単一ジャンルに偏らない。")
    wsd["E22"].value = "どちらも学習・自己成長志向。本データはスピリチュアル・引き寄せも特徴的"


def add_dashboard_chart(wb, gb_last_row):
    """② のジャンル集計を参照する概況チャートをダッシュボードへ追加。"""
    wsd = wb["📊 ダッシュボード"]
    ws2 = wb["② 視聴者の興味ジャンル"]
    wsd["B27"].value = "📊 視聴者の興味ジャンル内訳（Top150を10ジャンルに深堀り分類・延べ登録数）"
    wsd["B27"].font = font(bold=True, sz=11, color=DARK)
    bar = BarChart()
    data = Reference(ws2, min_col=4, min_row=5, max_row=gb_last_row)
    cats = Reference(ws2, min_col=2, min_row=6, max_row=gb_last_row)
    bar.add_data(data, titles_from_data=True)
    bar.set_categories(cats)
    bar.type = "bar"
    bar.title = "ジャンル別 延べ登録数（Top150）"
    bar.legend = None
    bar.height = 9
    bar.width = 17
    bar.gapWidth = 60
    s = bar.series[0]
    s.graphicalProperties.solidFill = GREEN.replace("FF", "")
    bar.dLbls = DataLabelList()
    bar.dLbls.showVal = True
    bar.dLbls.numFmt = "#,##0"
    bar.y_axis.delete = False
    bar.x_axis.delete = False
    wsd.add_chart(bar, "B28")


def main():
    e, m, cs, cc = load_data()
    top50, panel = top_channels(e, 50)
    top150, _ = top_channels(e, 150)
    gb = genre_breakdown(e, 150)

    print(f"panel={panel}  Top150 genres:")
    print(gb.to_string(index=False))
    unclassified = gb[gb["genre"].str.contains("未分類")]["links"].sum() if len(
        gb[gb["genre"].str.contains("未分類")]) else 0
    print(f"未分類リンク: {int(unclassified)}")

    wb = openpyxl.load_workbook(XLSX)
    clear_charts(wb)
    gb_last_row, total_links = rebuild_genre_sheet(wb, gb)
    update_top_sheet(wb, top50)
    add_affinity_chart(wb)
    add_distribution_chart(wb)
    add_community_chart(wb)
    update_dashboard_text(wb, gb)
    add_dashboard_chart(wb, gb_last_row)
    wb.save(XLSX)
    print(f"\nsaved -> {XLSX}")
    print(f"charts: ① 横棒 / ② 縦棒+円 / ③ 横棒 / ④ 縦棒 / ⑤ 縦棒 / 📊 概況")


if __name__ == "__main__":
    main()
