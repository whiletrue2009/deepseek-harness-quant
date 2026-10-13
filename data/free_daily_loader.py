# -*- coding: utf-8 -*-
"""免 Tushare 全市场日线初始化与增量 loader。

写入约定：Baostock 日线 source='baostock'，amount=元、volume=股、turn=百分数；
同花顺 source='ths' 仅作为可选备源。两者均免 Tushare token。
支持断点续传与失败清单；不删除既有 bars.db，不伪装缺失字段。
"""
from __future__ import annotations

import argparse
import csv
import multiprocessing
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.cache import CACHE_DIR, DailyCache
from data.fetcher_baostock import fetch_daily as fetch_baostock_daily
from data.fetcher_free import FreeDataSourceError, fetch_ths_daily

STOCK_DB = CACHE_DIR / "stock_basic.db"


def load_codes(include_delisted: bool = False, from_bars: bool = False) -> list[str]:
    if from_bars:
        cache = DailyCache()
        codes = set()
        for path in cache._inc_paths() + [cache.db_path]:
            if not Path(path).exists():
                continue
            con = sqlite3.connect(path, timeout=5)
            try:
                codes.update(
                    r[0]
                    for r in con.execute(
                        "SELECT DISTINCT code FROM daily_bar "
                        "WHERE code LIKE '%.SH' OR code LIKE '%.SZ'"
                    ).fetchall()
                )
            finally:
                con.close()
        return sorted(codes)

    if not STOCK_DB.exists():
        raise FileNotFoundError(f"缺少股票基础库: {STOCK_DB}，请先运行 data/fetch_stock_basic.py")
    con = sqlite3.connect(STOCK_DB)
    sql = "SELECT code FROM stock_basic WHERE (code LIKE '%.SH' OR code LIKE '%.SZ')"
    if not include_delisted:
        sql += " AND status='1'"
    codes = [r[0] for r in con.execute(sql + " ORDER BY code").fetchall()]
    con.close()
    return codes


def _progress_path(source: str) -> Path:
    return BASE / "logs" / f"free_daily_{source}_progress.txt"


def _failed_path(source: str) -> Path:
    return BASE / "logs" / f"free_daily_{source}_failed.csv"


def _read_progress(source: str) -> set[str]:
    path = _progress_path(source)
    if not path.exists():
        return set()
    return {x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip()}


def _write_failed(source: str, rows: list[tuple[str, str]]) -> None:
    path = _failed_path(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["code", "reason"])
        writer.writerows(rows)


def _preserve_st(cache: DailyCache, code: str, df) -> None:
    """同花顺日线没有 is_st，写入前保留本地已审计标记，避免覆盖为 0。"""
    old = cache.get_daily(code, adjust="qfq")
    if old is None or old.empty:
        return
    mapping = dict(zip(old["date"], old["is_st"]))
    df["is_st"] = df["date"].map(mapping).fillna(0).astype(int)


def _fetch_daily(source: str, code: str, start: str):
    if source == "baostock":
        return fetch_baostock_daily(code, start_date=start, adjust="qfq")
    if source == "ths":
        return fetch_ths_daily(code, start=start)
    raise ValueError(f"不支持的数据源: {source}")


def _worker_fetch(job: tuple[str, str, str]):
    """子进程拉取单只日线；SQLite 写入和进度落盘只留在主进程。"""
    code, start_date, source = job
    try:
        df = _fetch_daily(source, code, start_date)
        if df.empty:
            raise FreeDataSourceError("无日线数据")
        return code, df, None
    except Exception as exc:
        return code, None, str(exc)[:180]


def run_init(limit, start_date, rate, include_delisted, from_bars, source, workers) -> int:
    codes = load_codes(include_delisted, from_bars)
    if limit is not None:
        codes = codes[:limit]
    progress = _progress_path(source)
    failed_path = _failed_path(source)
    done = _read_progress(source)
    todo = [c for c in codes if c not in done]
    print(f"初始化待拉 {len(todo)} / {len(codes)} 只（source={source}，起点 {start_date}）", flush=True)
    cache = DailyCache()
    failed: list[tuple[str, str]] = []
    ok = rows_written = 0
    consecutive_failures = 0
    outage = False
    started = time.time()

    def store_result(i: int, code: str, df, error: str | None) -> None:
        nonlocal ok, rows_written, consecutive_failures, outage
        if error is not None or df is None:
            failed.append((code, error or "无日线数据"))
            consecutive_failures += 1
            print(f"  {code} 失败: {error}", flush=True)
            if consecutive_failures >= 20:
                outage = True
                print("连续 20 只失败，判定数据源或本机网络异常，熔断本轮", flush=True)
            return
        try:
            if source == "ths":
                _preserve_st(cache, code, df)
            n = cache.put_daily(code, df, adjust="qfq", source=source)
            rows_written += n
            ok += 1
            consecutive_failures = 0
            with progress.open("a", encoding="utf-8") as f:
                f.write(code + "\n")
            if i % 20 == 0 or i == len(todo):
                elapsed = time.time() - started
                eta = elapsed / i * (len(todo) - i) if i else 0
                print(f"  [{i}/{len(todo)}] {code} 写入 {n} 行，预计剩余 {eta/60:.1f} 分钟", flush=True)
        except Exception as exc:
            failed.append((code, str(exc)[:180]))
            print(f"  {code} 写库失败: {exc}", flush=True)

    if workers > 1:
        jobs = [(code, start_date, source) for code in todo]
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(workers) as pool:
            for i, (code, df, error) in enumerate(
                pool.imap_unordered(_worker_fetch, jobs, chunksize=4), 1
            ):
                store_result(i, code, df, error)
                if outage:
                    break
                time.sleep(rate)
    else:
        for i, code in enumerate(todo, 1):
            result = _worker_fetch((code, start_date, source))
            store_result(i, *result)
            if outage:
                break
            time.sleep(rate)
    _write_failed(source, failed)
    print(
        f"初始化完成: 成功 {ok} / {len(todo)}，写入 {rows_written} 行；"
        f"失败 {len(failed)} 只，清单 {failed_path}",
        flush=True,
    )
    return 2 if outage else (1 if failed and ok == 0 else 0)


def run_incremental(limit, rate, include_delisted, source) -> int:
    codes = load_codes(include_delisted)
    if limit is not None:
        codes = codes[:limit]
    progress = _progress_path(source)
    failed_path = _failed_path(source)
    cache = DailyCache()
    failed: list[tuple[str, str]] = []
    ok = rows_written = 0
    for i, code in enumerate(codes, 1):
        try:
            meta = cache.get_meta(code, "qfq")
            if source == "baostock" and meta and meta.get("end_date"):
                start = (datetime.strptime(meta["end_date"], "%Y-%m-%d") - timedelta(days=14)).strftime("%Y-%m-%d")
            else:
                # 同花顺年度文件按整年返回，跨年前后各取一个文件。
                start = f"{datetime.now().year - 1}-12-01"
            df = _fetch_daily(source, code, start)
            if df.empty:
                raise FreeDataSourceError("无增量数据")
            old = cache.get_daily(code, adjust="qfq")
            if old is not None and not old.empty:
                last = str(old["date"].max())
                df = df[df["date"] > last]
            if df.empty:
                ok += 1
            else:
                if source == "ths":
                    _preserve_st(cache, code, df)
                rows_written += cache.put_daily(code, df, adjust="qfq", source=source)
                ok += 1
            if i % 100 == 0:
                print(f"  [{i}/{len(codes)}] 增量成功 {ok}，累计新写入 {rows_written} 行", flush=True)
        except Exception as exc:
            failed.append((code, str(exc)[:180]))
        time.sleep(rate)
    _write_failed(source, failed)
    print(
        f"增量完成: source={source}，成功 {ok} / {len(codes)}，"
        f"新写入 {rows_written} 行，失败 {len(failed)} 只，清单 {failed_path}",
        flush=True,
    )
    return 1 if failed and ok == 0 else 0


def run_status() -> int:
    cache = DailyCache()
    paths = cache._inc_paths() + [cache.db_path]
    rows_by_source: dict[str, int] = defaultdict(int)
    codes_by_source: dict[str, set[str]] = defaultdict(set)
    latest_rows: dict[tuple[str, str], dict] = {}
    for path in paths:
        if not Path(path).exists():
            continue
        con = sqlite3.connect(path, timeout=5)
        try:
            groups = con.execute(
                "SELECT source, COUNT(*), COUNT(DISTINCT code) FROM daily_bar "
                "WHERE adjust='qfq' GROUP BY source"
            ).fetchall()
            for source, rows, _codes in groups:
                rows_by_source[source] += rows
                codes_by_source[source].update(
                    r[0] for r in con.execute(
                        "SELECT DISTINCT code FROM daily_bar WHERE adjust='qfq' AND source=?", (source,)
                    )
                )
            max_date = con.execute(
                "SELECT MAX(date) FROM daily_bar WHERE adjust='qfq' "
                "AND (code LIKE '%.SH' OR code LIKE '%.SZ')"
            ).fetchone()[0]
            if max_date:
                for row in con.execute(
                    "SELECT code, date, amount, turn, source FROM daily_bar "
                    "WHERE adjust='qfq' AND date=?", (max_date,)
                ):
                    key = (row[0], row[1])
                    latest_rows[key] = {"amount": row[2], "turn": row[3], "source": row[4]}
        finally:
            con.close()
    latest_date = max((d for _, d in latest_rows), default=None)
    latest = {key: value for key, value in latest_rows.items() if key[1] == latest_date}
    universe = set(load_codes(include_delisted=False)) if STOCK_DB.exists() else set()
    covered = set().union(*codes_by_source.values()) if codes_by_source else set()
    covered_in_market = covered & universe
    missing = sorted(universe - covered)
    print("日线缓存状态:")
    for source in sorted(rows_by_source):
        print(f"  source={source}: {rows_by_source[source]} 行 / {len(codes_by_source[source])} 只")
    if universe:
        print(
            f"  在市目标={len(universe)} 只，已覆盖={len(covered_in_market)} 只，"
            f"缺失={len(missing)} 只"
        )
        if missing:
            preview = "、".join(missing[:10])
            suffix = " …" if len(missing) > 10 else ""
            print(f"  缺失示例: {preview}{suffix}")
    if latest_date:
        n = len(latest)
        amount_missing = sum(1 for v in latest.values() if v["amount"] is None)
        turn_missing = sum(1 for v in latest.values() if v["turn"] is None)
        latest_universe = sum(1 for code, _ in latest if code in universe) if universe else n
        print(
            f"  最新日期={latest_date}，股票数={n}"
            + (f"（在市目标内 {latest_universe}）" if universe else "")
            + f"，amount缺失={amount_missing}，turn缺失={turn_missing}"
        )
    else:
        print("  无数据")
    for source in ("baostock", "ths"):
        progress = _progress_path(source)
        failed_path = _failed_path(source)
        done = _read_progress(source) if progress.exists() else set()
        done_count = len(done)
        failed_count = 0
        if failed_path.exists():
            with failed_path.open(encoding="utf-8", newline="") as f:
                failed_count = sum(
                    1
                    for row in csv.reader(f)
                    if row and row[0] != "code" and row[0] not in done
                )
        print(f"  {source}: progress={done_count}，failed={failed_count}")
    print("  注意: 全市场完整交易日建议 >=4000 只；小样本只用于链路体验。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="免 Tushare 日线缓存 loader")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--init", action="store_true", help="按股票基础库初始化历史日线")
    group.add_argument("--incremental", action="store_true", help="刷新当前与上年年末以来的数据")
    group.add_argument("--status", action="store_true", help="查看缓存覆盖")
    parser.add_argument("--start-date", default="2019-01-01")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--rate", type=float, default=0.18, help="每股请求间隔秒")
    parser.add_argument("--workers", type=int, default=1, help="初始化并发进程数；Baostock 建议 <=2")
    parser.add_argument("--include-delisted", action="store_true")
    parser.add_argument("--from-bars", action="store_true", help="只处理当前 bars 中已有股票（小样本验证）")
    parser.add_argument("--source", choices=["baostock", "ths"], default="baostock")
    args = parser.parse_args()

    if args.status:
        return run_status()
    if args.init:
        return run_init(
            args.limit, args.start_date, args.rate, args.include_delisted,
            args.from_bars, args.source, args.workers,
        )
    return run_incremental(args.limit, args.rate, args.include_delisted, args.source)


if __name__ == "__main__":
    raise SystemExit(main())
