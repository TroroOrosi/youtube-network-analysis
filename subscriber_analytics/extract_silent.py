"""サイレント登録者の抽出 CLI（API 呼び出しなし・クォータ消費ゼロ）。

collect_subscribers.py が育てたレジストリと collect_comments.py のコメント
キャッシュを突合し、期間条件でフィルタした登録者一覧を CSV 出力する。
期間を変えた再抽出はいつでもクォータ消費ゼロで行える。

期間条件（組み合わせ可）:
    --subscribed-within 90d      直近90日（≒3ヶ月）以内の登録者に限定
    --subscribed-since 2026-04-01 / --subscribed-until 2026-06-30  絶対日付（両端含む）
    --never-commented            一度もコメントしていない人のみ
    --no-comment-within 90d      直近90日コメントしていない人（過去のコメント有無は不問）

例: 直近3ヶ月以内に登録したが一度もコメントしていない人
    python subscriber_analytics/extract_silent.py --subscribed-within 90d --never-commented

登録日の決定規則: API の publishedAt（api_published_at）があればそれを使い、
無ければレジストリの first_seen_at（初観測日時）で代用する。どちらを使ったかは
出力の subscribed_at_source 列で確認できる。

フィルタ指定なしで実行すると、全登録者を4象限セグメント付きで出力する。
セグメント: 新規サイレント / 古参サイレント / 休眠（過去コメントあり・期間内なし）/ アクティブ

解約済みの扱い: レジストリには過去に観測した全員が残るため、既定では
「最新スナップショットに出現した人（=現役の公開登録者）」のみを対象にする。
解約した可能性のある人も含めるには --include-unsubscribed を指定する
（登録者数が API の返却上限を超えるチャンネルでは、現役でも最新スナップ
ショットから漏れる場合があるため、その際もこのフラグが有用）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

OUTPUT_COLUMNS = [
    "channel_id",
    "title",
    "subscribed_at",
    "subscribed_at_source",
    "first_seen_at",
    "last_seen_at",
    "comment_count",
    "last_comment_at",
    "segment",
]

SEG_NEW_SILENT = "新規サイレント"
SEG_OLD_SILENT = "古参サイレント"
SEG_DORMANT = "休眠"
SEG_ACTIVE = "アクティブ"


def _to_utc(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, format="ISO8601", errors="coerce")


def load_registry(data_dir: Path) -> pd.DataFrame:
    path = common.registry_path(data_dir)
    if not path.exists():
        raise SystemExit(
            f"レジストリがありません: {path}\n先に collect_subscribers.py を実行してください。"
        )
    return pd.read_csv(path, dtype=str).fillna("")


def load_comment_stats(data_dir: Path):
    """コメントキャッシュ全件から投稿者ごとの (コメント数, 最終コメント日時) を集計する。"""
    files = sorted(common.comments_dir(data_dir).glob("*.csv"))
    frames = []
    for path in files:
        frame = pd.read_csv(path, dtype=str)
        if not frame.empty:
            frames.append(frame[["author_channel_id", "published_at"]])
    if not frames:
        empty = pd.DataFrame(
            columns=["author_channel_id", "comment_count", "last_comment_at"]
        )
        return empty, len(files)
    comments = pd.concat(frames, ignore_index=True)
    comments = comments[comments["author_channel_id"].fillna("") != ""]
    comments["published_at"] = _to_utc(comments["published_at"])
    stats = (
        comments.groupby("author_channel_id")
        .agg(comment_count=("published_at", "size"), last_comment_at=("published_at", "max"))
        .reset_index()
    )
    return stats, len(files)


def build_table(registry: pd.DataFrame, comment_stats: pd.DataFrame) -> pd.DataFrame:
    """レジストリとコメント集計を突合して抽出用テーブルを作る。"""
    table = registry.copy()
    has_api = table["api_published_at"] != ""
    table["subscribed_at_source"] = has_api.map(
        {True: "api_published_at", False: "first_seen_at"}
    )
    table["subscribed_at"] = _to_utc(
        table["api_published_at"].where(has_api, table["first_seen_at"])
    )
    table = table.merge(
        comment_stats,
        how="left",
        left_on="channel_id",
        right_on="author_channel_id",
    )
    table["comment_count"] = table["comment_count"].fillna(0).astype(int)
    table["last_comment_at"] = pd.to_datetime(table["last_comment_at"], utc=True)
    return table


def add_segments(table: pd.DataFrame, now, recent_window, activity_window) -> pd.DataFrame:
    """4象限セグメントを付与する。

    登録の新旧（recent_window 以内か）× コメント活動（activity_window 以内に
    コメントしたか）で分類する。
    """
    recent_cutoff = now - recent_window
    activity_cutoff = now - activity_window
    has_comment = table["comment_count"] > 0
    is_recent_sub = table["subscribed_at"] >= recent_cutoff
    is_recent_comment = has_comment & (table["last_comment_at"] >= activity_cutoff)

    segment = pd.Series(SEG_OLD_SILENT, index=table.index)
    segment[~has_comment & is_recent_sub] = SEG_NEW_SILENT
    segment[has_comment & ~is_recent_comment] = SEG_DORMANT
    segment[is_recent_comment] = SEG_ACTIVE
    table = table.copy()
    table["segment"] = segment
    return table


def apply_filters(table: pd.DataFrame, args, now) -> tuple[pd.DataFrame, list[str]]:
    mask = pd.Series(True, index=table.index)
    applied: list[str] = []

    if args.subscribed_within:
        cutoff = now - common.parse_duration(args.subscribed_within)
        mask &= table["subscribed_at"] >= cutoff
        applied.append(f"登録が直近 {args.subscribed_within} 以内（{common.format_ts(cutoff)} 以降）")
    if args.subscribed_since:
        since = pd.Timestamp(args.subscribed_since, tz="UTC")
        mask &= table["subscribed_at"] >= since
        applied.append(f"登録日 {args.subscribed_since} 以降")
    if args.subscribed_until:
        until = pd.Timestamp(args.subscribed_until, tz="UTC") + pd.Timedelta(days=1)
        mask &= table["subscribed_at"] < until
        applied.append(f"登録日 {args.subscribed_until} 以前")
    if args.never_commented:
        mask &= table["comment_count"] == 0
        applied.append("一度もコメントしていない")
    if args.no_comment_within:
        cutoff = now - common.parse_duration(args.no_comment_within)
        commented_recently = (table["comment_count"] > 0) & (
            table["last_comment_at"] >= cutoff
        )
        mask &= ~commented_recently
        applied.append(f"直近 {args.no_comment_within} コメントなし")

    return table[mask], applied


def format_output(table: pd.DataFrame) -> pd.DataFrame:
    out = table.sort_values("subscribed_at", ascending=False, na_position="last").copy()
    for col in ("subscribed_at", "last_comment_at"):
        out[col] = out[col].dt.strftime(common.TIME_FMT).fillna("")
    return out[OUTPUT_COLUMNS]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="サイレント登録者の抽出（ローカルのみ・クォータ消費ゼロ）"
    )
    parser.add_argument("--subscribed-within", help="登録が直近この期間以内（例: 90d, 12w, 3m）")
    parser.add_argument("--subscribed-since", help="登録日の下限（YYYY-MM-DD、当日含む）")
    parser.add_argument("--subscribed-until", help="登録日の上限（YYYY-MM-DD、当日含む）")
    parser.add_argument(
        "--never-commented", action="store_true", help="一度もコメントしていない人のみ"
    )
    parser.add_argument("--no-comment-within", help="直近この期間コメントしていない人（例: 90d）")
    parser.add_argument(
        "--include-unsubscribed",
        action="store_true",
        help="最新スナップショットに出現しなかった人（解約の可能性あり）も対象に含める",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=common.DATA_DIR,
        help=f"データ読み込み元（既定: {common.DATA_DIR}）",
    )
    parser.add_argument("--out", type=Path, help="出力CSVパス（既定: output/silent_subscribers_<日付>.csv）")
    parser.add_argument("--now", help="基準時刻の上書き（ISO形式。テスト・再現用）")
    args = parser.parse_args()

    now = common.parse_ts(args.now) if args.now else common.utcnow()

    registry = load_registry(args.data_dir)

    # 既定では最新スナップショットに出現した人（現役の公開登録者）のみを対象にする。
    # レジストリには解約済みの人も last_seen_at が古いまま残るため。
    excluded_unsubscribed = 0
    latest_seen = registry["last_seen_at"].max() if len(registry) else ""
    if not args.include_unsubscribed and latest_seen:
        current = registry["last_seen_at"] == latest_seen
        excluded_unsubscribed = int((~current).sum())
        registry = registry[current]

    comment_stats, n_comment_files = load_comment_stats(args.data_dir)
    if n_comment_files == 0:
        print(
            "警告: コメントデータがありません（collect_comments.py 未実行）。"
            "全員が「コメントなし」として扱われます。"
        )

    table = build_table(registry, comment_stats)
    recent_window = common.parse_duration(args.subscribed_within or "90d")
    activity_window = common.parse_duration(args.no_comment_within or "90d")
    table = add_segments(table, now, recent_window, activity_window)

    filtered, applied = apply_filters(table, args, now)
    out_path = args.out or (
        common.OUTPUT_DIR / f"silent_subscribers_{now.strftime('%Y%m%d')}.csv"
    )
    common.atomic_write_csv(format_output(filtered), out_path)

    scope = "現役登録者" if not args.include_unsubscribed else "レジストリ全体"
    print(f"=== セグメント集計（{scope} {len(table)} 人 / 基準時刻 {common.format_ts(now)}） ===")
    if excluded_unsubscribed:
        print(
            f"  ※最新スナップショット（{latest_seen}）に出現しなかった {excluded_unsubscribed} 人"
            "（解約の可能性）を除外済み。含めるには --include-unsubscribed"
        )
    for name in (SEG_NEW_SILENT, SEG_OLD_SILENT, SEG_DORMANT, SEG_ACTIVE):
        print(f"  {name}: {int((table['segment'] == name).sum())} 人")
    if applied:
        print("適用フィルタ: " + " AND ".join(applied))
    else:
        print("適用フィルタ: なし（全登録者をセグメント付きで出力）")
    print(f"出力: {len(filtered)} 人 → {out_path}")
    print("注記: 対象は登録を公開しているユーザーのみのため、実際の人数の下限値です。")


if __name__ == "__main__":
    main()
