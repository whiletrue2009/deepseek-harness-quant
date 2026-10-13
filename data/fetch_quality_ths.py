# -*- coding: utf-8 -*-
"""从同花顺财务三表构建质量风险缓存。

重要口径：该接口没有公告日，pub_date 一律为 NULL，质量表只能用于当前
风险提示与非 PIT 研究，不能用于前视敏感的历史回测结论。
"""
from __future__ import annotations

import argparse
import csv
import math
import multiprocessing
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.cache import CACHE_DIR
from data.fetcher_free import FreeDataSourceError, fetch_ths_finance, normalize_ticker

DB = CACHE_DIR / "finance_quality.db"
PROGRESS = BASE / "logs" / "quality_ths_progress.txt"
FAILED = BASE / "logs" / "quality_ths_failed.csv"
STOCK_DB = CACHE_DIR / "stock_basic.db"


def _conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute(
        """CREATE TABLE IF NOT EXISTS quality (
        code TEXT, period TEXT, roe_avg REAL, gp_margin REAL, np_margin REAL,
        current_ratio REAL, liability_to_asset REAL, cfo_to_np REAL, cfo_to_or REAL,
        pub_date TEXT, PRIMARY KEY(code, period))"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS quality_source (
        code TEXT, period TEXT, source TEXT, pit INTEGER, PRIMARY KEY(code, period))"""
    )
    con.commit()
    return con


def load_codes(include_delisted: bool = False) -> list[str]:
    if not STOCK_DB.exists():
        raise FileNotFoundError(f"缺少股票基础库: {STOCK_DB}")
    con = sqlite3.connect(STOCK_DB)
    sql = "SELECT code FROM stock_basic WHERE (code LIKE '%.SH' OR code LIKE '%.SZ')"
    if not include_delisted:
        sql += " AND status='1'"
    codes = [r[0] for r in con.execute(sql + " ORDER BY code").fetchall()]
    con.close()
    return codes


def _parse_yuan(value) -> float | None:
    """把 `460.33亿` / `-379.44万` 转成元；False/-- 返回空值。"""
    if value is None or isinstance(value, bool):
        return None
    s = str(value).replace(",", "").strip()
    if s in ("", "--", "-"):
        return None
    try:
        if s.endswith("亿"):
            return float(s[:-1]) * 1e8
        if s.endswith("万"):
            return float(s[:-1]) * 1e4
        return float(s)
    except ValueError:
        return None


def _find(table: dict, title: str, period: str) -> float | None:
    """按报表自己的报告期取值；三张表的披露期数组长度并不一致。"""
    periods = table.get("report") or []
    if not periods or not isinstance(periods[0], list):
        return None
    try:
        period_idx = periods[0].index(period)
    except (ValueError, AttributeError):
        return None
    for i, label in enumerate(table.get("title", [])):
        name = label[0] if isinstance(label, list) and label else label
        if name == title:
            return _parse_yuan(table["report"][i][period_idx])
    return None


def _ratio(numer, denom) -> float | None:
    if numer is None or denom is None or abs(denom) < 1e-8:
        return None
    value = numer / denom
    return round(value, 6) if math.isfinite(value) else None


def build_rows(code: str, quarters: int) -> list[tuple]:
    benefit = fetch_ths_finance(code, "benefit")
    debt = fetch_ths_finance(code, "debt")
    cash = fetch_ths_finance(code, "cash")
    benefit_periods = benefit.get("report") or []
    periods = benefit_periods[0][:quarters] if benefit_periods else []
    rows = []
    for period in periods:
        if not isinstance(period, str) or len(period) != 10:
            continue
        revenue = _find(benefit, "*营业总收入", period)
        cost = _find(benefit, "其中：营业成本", period)
        net_profit = _find(benefit, "*净利润", period)
        parent_profit = _find(benefit, "*归属于母公司所有者的净利润", period)
        parent_equity = _find(debt, "归属于母公司所有者权益合计", period)
        current_asset = _find(debt, "流动资产合计", period)
        current_liability = _find(debt, "流动负债合计", period)
        liability = _find(debt, "负债合计", period)
        asset = _find(debt, "资产合计", period)
        operating_cash = _find(cash, "经营活动产生的现金流量净额", period)
        row = (
            code,
            period,
            _ratio(parent_profit, parent_equity),
            _ratio(revenue - cost, revenue) if revenue is not None and cost is not None else None,
            _ratio(net_profit, revenue),
            _ratio(current_asset, current_liability),
            _ratio(liability, asset),
            _ratio(operating_cash, net_profit),
            _ratio(operating_cash, revenue),
            None,
        )
        if any(v is not None for v in row[2:9]):
            rows.append(row)
    return rows


def _worker_quality(job: tuple[str, int]):
    """子进程拉取同花顺三表；写库和进度只在主进程。"""
    code, quarters = job
    try:
        return code, build_rows(code, quarters), None
    except Exception as exc:
        return code, None, str(exc)[:180]


def run(limit: int | None, quarters: int, rate: float, force: bool,
        include_delisted: bool, workers: int) -> int:
    codes = load_codes(include_delisted)
    if limit is not None:
        codes = codes[:limit]
    con = _conn()
    if not force:
        done_source = {
            r[0]
            for r in con.execute("SELECT DISTINCT code FROM quality_source WHERE source='ths'").fetchall()
        }
    else:
        done_source = set()
    done_progress = set()
    if PROGRESS.exists():
        done_progress = {x.strip() for x in PROGRESS.read_text(encoding="utf-8").splitlines() if x.strip()}
    todo = [c for c in codes if c not in done_source and c not in done_progress]
    print(f"同花顺质量数据待拉 {len(todo)} / {len(codes)} 只 × {quarters} 期", flush=True)
    failed = []
    ok = rows_written = 0
    consecutive_failures = 0
    outage = False

    def store_result(i: int, code: str, rows: list[tuple] | None, error: str | None) -> None:
        nonlocal ok, rows_written, consecutive_failures, outage
        if error is not None or not rows:
            failed.append((code, error or "无有效质量指标"))
            consecutive_failures += 1
            print(f"  {code} 失败: {error or '无有效质量指标'}", flush=True)
            if consecutive_failures >= 20:
                outage = True
                print("连续 20 只失败，判定数据源或本机网络异常，熔断本轮", flush=True)
            return
        try:
            con.executemany("INSERT OR REPLACE INTO quality VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            con.executemany(
                "INSERT OR REPLACE INTO quality_source VALUES (?,?,?,0)",
                [(code, r[1], "ths") for r in rows],
            )
            con.commit()
            ok += 1
            rows_written += len(rows)
            consecutive_failures = 0
            with PROGRESS.open("a", encoding="utf-8") as f:
                f.write(code + "\n")
            if i % 20 == 0 or i == len(todo):
                print(f"  [{i}/{len(todo)}] 成功 {ok}，累计 {rows_written} 期", flush=True)
        except Exception as exc:
            failed.append((code, str(exc)[:180]))
            print(f"  {code} 写库失败: {exc}", flush=True)

    if workers > 1:
        jobs = [(code, quarters) for code in todo]
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(workers) as pool:
            for i, (code, rows, error) in enumerate(
                pool.imap_unordered(_worker_quality, jobs, chunksize=4), 1
            ):
                store_result(i, code, rows, error)
                if outage:
                    break
                time.sleep(rate)
    else:
        for i, code in enumerate(todo, 1):
            result = _worker_quality((code, quarters))
            store_result(i, *result)
            if outage:
                break
            time.sleep(rate)
    if failed:
        FAILED.parent.mkdir(parents=True, exist_ok=True)
        with FAILED.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["code", "reason"])
            writer.writerows(failed)
    total = con.execute("SELECT COUNT(*) FROM quality").fetchone()[0]
    nstocks = con.execute("SELECT COUNT(DISTINCT code) FROM quality").fetchone()[0]
    con.close()
    print(f"质量数据完成: 本次成功 {ok}/{len(todo)}，{rows_written} 期；累计 {nstocks} 只/{total} 期", flush=True)
    print("口径提示: 同花顺质量表无公告日，pub_date=NULL，禁止用于 PIT 回测。", flush=True)
    return 2 if outage else (1 if ok == 0 and todo else 0)


def run_status() -> int:
    con = _conn()
    total = con.execute("SELECT COUNT(*) FROM quality").fetchone()[0]
    stock_rows = {r[0] for r in con.execute("SELECT DISTINCT code FROM quality").fetchall()}
    ths = con.execute("SELECT COUNT(*) FROM quality_source WHERE source='ths'").fetchone()[0]
    pub = con.execute("SELECT COUNT(*) FROM quality WHERE pub_date IS NOT NULL AND pub_date!=''").fetchone()[0]
    con.close()
    universe = set(load_codes(include_delisted=False)) if STOCK_DB.exists() else set()
    covered = stock_rows & universe if universe else stock_rows
    missing = sorted(universe - stock_rows) if universe else []
    progress = len({x for x in PROGRESS.read_text(encoding="utf-8").splitlines() if x.strip()}) if PROGRESS.exists() else 0
    failed = 0
    if FAILED.exists():
        with FAILED.open(encoding="utf-8", newline="") as f:
            failed = max(0, sum(1 for _ in csv.reader(f)) - 1)
    print(f"quality: {len(stock_rows)} 只 / {total} 期；ths 来源 {ths} 期；含公告日 {pub} 期")
    if universe:
        print(f"  在市目标={len(universe)} 只，已覆盖={len(covered)} 只，缺失={len(missing)} 只")
        if missing:
            preview = "、".join(missing[:10])
            suffix = " …" if len(missing) > 10 else ""
            print(f"  缺失示例: {preview}{suffix}")
    print(f"  progress={progress}，failed={failed}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="同花顺财务质量缓存")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run", action="store_true")
    group.add_argument("--status", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--quarters", type=int, default=10)
    parser.add_argument("--rate", type=float, default=0.12)
    parser.add_argument("--workers", type=int, default=1, help="并发进程数；HTTP 源建议 <=2")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--include-delisted", action="store_true")
    args = parser.parse_args()
    if args.status:
        return run_status()
    return run(args.limit, args.quarters, args.rate, args.force,
               args.include_delisted, args.workers)


if __name__ == "__main__":
    raise SystemExit(main())
