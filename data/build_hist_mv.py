# -*- coding: utf-8 -*-
"""data/build_hist_mv.py — 历史流通市值 PIT 构建（换手率反推版，2026-08-07）

背景：原 fetcher_hist_mv.py 用 Tushare daily_basic，但免费积分限频 1 次/小时
      → 72 个月需 72 小时，不可行。改用本地已有数据反推：
        流通市值(亿) = amount(元) / (turn% / 100) / 1e8
      精度实测：600519/000001/300750/601318/000002/688981 对照快照误差全部 <1%。

数据源：daily_bar（本地，无网络依赖、无限频）
产出：data/cache/hist_mv.db 表 hist_mv (month, code, circ_mv 亿元)
      → 供 test_ewt_pt_backtest.py 正式验收（消除市值快照 look-ahead）

用法：
  python data/build_hist_mv.py                 # 全量 2020-01 ~ 最新月末
  python data/build_hist_mv.py --start 2023-01 # 断点续拉
  python data/build_hist_mv.py --force         # 忽略月份断点，覆盖补齐全部股票
"""
import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.cache import CACHE_DIR

HIST_DB = CACHE_DIR / "hist_mv.db"
BARS_DB = CACHE_DIR / "bars.db"


def _bar_paths() -> list[Path]:
    """主库 + 最近增量库；顺序在后者覆盖同代码同日数据。"""
    paths = [BARS_DB]
    inc = CACHE_DIR / "bars_incr.db"
    if inc.exists():
        paths.append(inc)
    paths.extend(sorted(CACHE_DIR.glob("bars_incr_*.db"))[-3:])
    out = []
    for p in paths:
        if p.exists() and p not in out:
            out.append(p)
    return out


def _read_sql(sql: str, args: tuple) -> list:
    rows = []
    for path in _bar_paths():
        try:
            con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
            try:
                rows.extend(con.execute(sql, args).fetchall())
            finally:
                con.close()
        except sqlite3.Error:
            continue
    return rows


def _conn():
    con = sqlite3.connect(str(HIST_DB))
    con.execute("""
    CREATE TABLE IF NOT EXISTS hist_mv (
        month TEXT, code TEXT, circ_mv REAL,
        PRIMARY KEY (month, code)
    )""")
    con.commit()
    return con


def month_end_dates(start: str) -> list:
    """每月末交易日列表（主库 + 增量库合并后按月取最大日期）。"""
    by_month: dict[str, str] = {}
    rows = _read_sql(
        "SELECT substr(date,1,7) ym, MAX(date) md FROM daily_bar "
        "WHERE code NOT LIKE 'SH.%' AND code NOT LIKE 'sh.%' AND substr(date,1,7) >= ? "
        "GROUP BY ym", (start[:7],))
    for ym, md in rows:
        by_month[ym] = max(by_month.get(ym, ""), md)
    return [(ym, by_month[ym]) for ym in sorted(by_month)]


def build_month(ym: str, md: str) -> int:
    """某月末全市场流通市值反推 → 入库，返回行数"""
    # ★2026-08-15 单位归一：tushare/tushare_backup amount=千元（×1000→元），baostock/akshare/ths=元；
    #   排除指数行 SH.000300（amount 单位=元且非个股，混入会污染反推）
    rows_raw = _read_sql(
        "SELECT code, amount, turn, source FROM daily_bar WHERE date=? AND adjust='qfq' "
        "AND amount IS NOT NULL AND turn IS NOT NULL AND amount > 0 AND turn > 0 "
        "AND code NOT LIKE 'SH.%' AND code NOT LIKE 'sh.%'", (md,))
    merged: dict[str, tuple] = {}
    for code, amount, turn, source in rows_raw:
        merged[code] = (code, amount, turn, source)
    rows = []
    for code, amount, turn, source in merged.values():
        amt_yuan = amount * 1000.0 if (source or "").lower() in ("tushare", "tushare_backup") else amount
        mv_yi = amt_yuan / (turn / 100) / 1e8
        if mv_yi <= 0 or mv_yi > 1e6:      # 滤异常（>100 万亿元）
            continue
        rows.append((ym, code.upper(), round(mv_yi, 2)))
    if rows:
        hcon = _conn()
        old_count = hcon.execute(
            "SELECT COUNT(*) FROM hist_mv WHERE month=?", (ym,)
        ).fetchone()[0]
        if old_count and len(rows) < old_count * 0.8:
            hcon.close()
            print(f"  {ym} 跳过：新快照 {len(rows)} 只少于既有 {old_count} 只，疑似增量未完整", flush=True)
            return 0
        hcon.executemany("INSERT OR REPLACE INTO hist_mv VALUES (?,?,?)", rows)
        hcon.commit()
        hcon.close()
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2020-01")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m"))
    ap.add_argument("--force", action="store_true",
                    help="忽略已完成月份标记，用当前 bars.db 覆盖补齐（不删除额外月份）")
    args = ap.parse_args()

    hcon = _conn()
    done = {r[0] for r in hcon.execute("SELECT DISTINCT month FROM hist_mv").fetchall()}
    hcon.close()

    months = [(ym, md) for ym, md in month_end_dates(args.start)
              if args.start <= ym <= args.end and (args.force or ym not in done)]
    print(f"月末交易日 {len(months)} 个，待建 {len(months)}（已完成 {len(done)}）", flush=True)

    t0 = time.time()
    total_rows = 0
    for i, (ym, md) in enumerate(months, 1):
        n = build_month(ym, md)
        total_rows += n
        if i % 12 == 0 or i == len(months):
            print(f"  [{i}/{len(months)}] {ym}: {n} 只，累计 {total_rows} 行，"
                  f"耗时 {(time.time()-t0)/60:.1f} 分钟", flush=True)
    hcon = _conn()
    n_months = hcon.execute("SELECT COUNT(DISTINCT month) FROM hist_mv").fetchone()[0]
    n_rows = hcon.execute("SELECT COUNT(*) FROM hist_mv").fetchone()[0]
    hcon.close()
    print(f"完成：{n_months} 个月 / {n_rows} 行 ｜ {HIST_DB}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
