# -*- coding: utf-8 -*-
"""财报批量下载器（M2 收尾②，基本面因子数据源）

数据源：AkShare stock_financial_abstract_ths（同花顺财务摘要，免费）
入库：SQLite 新表 finance_report（本地缓存唯一读取原则）
自算：finance_calc.py 累计→单季差分、单季同比（C 因子核心，反向验证）

表结构 finance_report：
  code TEXT, period TEXT(报告期 YYYY-MM-DD), 净利润 REAL(亿), 营收 REAL(亿),
  单季净利 REAL, 单季同比 REAL, 单季营收 REAL, 单季营收同比 REAL,
  每股收益 REAL, ROE REAL, 来源 TEXT, PRIMARY KEY(code, period)

用法：
  python data/fetcher_finance.py --limit 50      # 小样本验证（只处理前 50 只）
  python data/fetcher_finance.py                 # 全量（约 40 分钟，幂等 PK code+period）
  python data/fetcher_finance.py --from-bars     # 旧模式：只处理 bars.db 已有代码
  python data/fetcher_finance.py --status        # 查看进度
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import sqlite3
import warnings
warnings.filterwarnings("ignore")

import pandas as pd

from data.finance_calc import parse_num
from data.cache import CACHE_DIR

FIN_DB = str(CACHE_DIR / "finance.db")
LOG_FILE = BASE / "logs" / "finance_load.log"
PROGRESS_FILE = BASE / "logs" / "finance_progress.txt"
FAILED_FILE = BASE / "logs" / "finance_failed_codes.json"
EMPTY_FILE = BASE / "logs" / "finance_empty_codes.json"


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def init_db():
    con = sqlite3.connect(FIN_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS finance_report (
        code TEXT, period TEXT, net_profit REAL, revenue REAL,
        sq_net_profit REAL, sq_net_yoy REAL, sq_revenue REAL, sq_rev_yoy REAL,
        eps REAL, roe REAL, source TEXT,
        PRIMARY KEY (code, period))""")
    con.commit()
    return con


def fetch_one(code6: str) -> pd.DataFrame:
    """拉取单只股票的财务摘要（按报告期），返回原始 DataFrame"""
    import akshare as ak
    try:
        return ak.stock_financial_abstract_ths(symbol=code6, indicator="按报告期")
    except Exception:
        # 少量新上市股票的同花顺页面没有 main 节点；东财 F10 字段单位为元，
        # 继续走 parse_finance 的单位归一和单季差分逻辑。
        suffix = ".SH" if code6.startswith(("6", "9")) else ".SZ"
        raw = ak.stock_financial_analysis_indicator_em(
            symbol=code6 + suffix, indicator="按报告期"
        )
        if raw is None or raw.empty:
            raise
        return pd.DataFrame(
            {
                "报告期": raw["REPORT_DATE"].astype(str).str[:10],
                "净利润": raw["PARENTNETPROFIT"],
                "营业总收入": raw["TOTALOPERATEREVE"],
                "每股收益": raw["EPSJB"],
                "净资产收益率": raw["ROEJQ"],
                "source": "akshare_em",
            }
        )


def parse_finance(code6: str, df: pd.DataFrame) -> list:
    """原始财务摘要 → 入库行（含自算单季值）
    返回 [(code, period, ...), ...]"""
    if df is None or df.empty:
        return []

    # 定位列
    def col(df, *names):
        for n in names:
            if n in df.columns:
                return n
        return None

    c_period = col(df, "报告期", "报告日期", "日期")
    c_net = col(df, "净利润", "归母净利润")
    c_rev = col(df, "营业总收入", "营业收入", "主营收入")
    c_eps = col(df, "每股收益", "基本每股收益")
    c_roe = col(df, "净资产收益率", "ROE", "加权净资产收益率")
    if c_period is None or c_net is None:
        return []

    rows = []
    cum_net = {}
    cum_rev = {}
    for _, r in df.iterrows():
        period = str(r[c_period])[:10]
        if not period or len(period) != 10:
            continue
        net = parse_num(r.get(c_net))
        rev = parse_num(r.get(c_rev)) if c_rev else None
        eps = parse_num(r.get(c_eps)) if c_eps else None
        roe = parse_num(r.get(c_roe)) if c_roe else None
        if net is None:
            continue
        # ★单位归一：akshare 返回"元"时转亿（>1e7 视为元；字符串"145.23亿"已被 parse_num 转亿）
        net = net / 1e8 if abs(net) > 1e7 else net
        if rev is not None:
            rev = rev / 1e8 if abs(rev) > 1e7 else rev
        cum_net[period] = net
        if rev is not None:
            cum_rev[period] = rev

    # 自算单季值（复用 finance_calc 逻辑）
    def single_quarter(cum_map):
        sq = {}
        for (y, m), v in sorted(cum_map.items()):
            if m == 3:
                # ★Q1 累计 = 单季本身（不扣上年年报！之前修过的坑）
                sq[(y, m)] = v
            elif m == 6:
                base = cum_map.get((y, 3))
                sq[(y, m)] = (v - base) if base is not None else None
            elif m == 9:
                base = cum_map.get((y, 6))
                sq[(y, m)] = (v - base) if base is not None else None
            elif m == 12:
                base = cum_map.get((y, 9))
                sq[(y, m)] = (v - base) if base is not None else None
        return sq

    def to_ym(period):
        try:
            return (int(period[:4]), int(period[5:7]))
        except Exception:
            return None

    net_ym = {to_ym(p): v for p, v in cum_net.items() if to_ym(p)}
    rev_ym = {to_ym(p): v for p, v in cum_rev.items() if to_ym(p)}
    sq_net = single_quarter(net_ym)
    sq_rev = single_quarter(rev_ym)

    for period, net in sorted(cum_net.items()):
        ym = to_ym(period)
        if ym is None:
            continue
        sqn = sq_net.get(ym)
        # 单季同比：今年该季度 / 去年同季 - 1
        sqn_yoy = None
        prev_ym = (ym[0] - 1, ym[1])
        prev_sqn = sq_net.get(prev_ym)
        if sqn is not None and prev_sqn not in (None, 0):
            sqn_yoy = sqn / prev_sqn - 1

        sqr = sq_rev.get(ym)
        sqr_yoy = None
        prev_sqr = sq_rev.get(prev_ym)
        if sqr is not None and prev_sqr not in (None, 0):
            sqr_yoy = sqr / prev_sqr - 1

        rows.append((
            code6, period, net, cum_rev.get(period),
            sqn, sqn_yoy, sqr, sqr_yoy,
            eps, roe, str(r.get("source") or "akshare_ths")
        ))
    return rows


def load_codes(limit=None, from_bars=False):
    """默认读全市场股票基础表；可选沿用 bars 小样本模式。"""
    import sqlite3 as s3
    if from_bars:
        con = s3.connect(str(CACHE_DIR / "bars.db"))
        codes = [r[0] for r in con.execute(
            "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")]
        con.close()
        codes = [c.split(".")[0] for c in codes]
    else:
        con = s3.connect(str(CACHE_DIR / "stock_basic.db"))
        codes = [r[0][:6] for r in con.execute(
            "SELECT code FROM stock_basic WHERE status='1' ORDER BY code")]
        con.close()
    if limit:
        codes = codes[:limit]
    return codes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 只")
    ap.add_argument("--start", type=int, default=0, help="从第 N 只开始")
    ap.add_argument("--from-bars", action="store_true",
                    help="只处理 bars.db 已有代码；默认读 stock_basic.db 全市场")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--retry-failed", action="store_true", help="只补拉上次失败的代码（读 logs/finance_failed_codes.json）")
    args = ap.parse_args()

    if args.status:
        con = init_db()
        n_rows = con.execute("SELECT COUNT(*) FROM finance_report").fetchone()[0]
        n_codes = con.execute("SELECT COUNT(DISTINCT code) FROM finance_report").fetchone()[0]
        con.close()
        failed, empty = [], []
        if FAILED_FILE.exists():
            failed = json.loads(FAILED_FILE.read_text(encoding="utf-8"))
        if EMPTY_FILE.exists():
            empty = json.loads(EMPTY_FILE.read_text(encoding="utf-8"))
        print(f"finance_report: {n_codes} 只 / {n_rows} 期；失败 {len(failed)} 只；空数据 {len(empty)} 只")
        if PROGRESS_FILE.exists():
            print(PROGRESS_FILE.read_text(encoding="utf-8"))
        return 0

    # ★#394 失败代码落盘 + --retry-failed 定向补拉（免费接口限流常致大量失败，补拉避开全量重跑）
    if args.retry_failed:
        if not FAILED_FILE.exists():
            log("无失败清单文件，无需补拉")
            return 0
        import json as _json
        codes = _json.loads(FAILED_FILE.read_text(encoding="utf-8"))
        if not codes:
            log("失败清单为空，无需补拉")
            return 0
        log(f"定向补拉：{len(codes)} 只上次失败的代码")
    else:
        codes = load_codes(args.limit, args.from_bars)
    total = len(codes)
    if args.start > 0:
        codes = codes[args.start:]

    con = init_db()
    done_db = {r[0] for r in con.execute("SELECT DISTINCT code FROM finance_report").fetchall()}
    empty = set()
    if EMPTY_FILE.exists():
        empty = set(json.loads(EMPTY_FILE.read_text(encoding="utf-8")))
    codes = [c for c in codes if c not in done_db and c not in empty]
    log(f"财报批量下载: 共 {total} 只，本次处理 {len(codes)} 只（start={args.start}）")

    t0 = time.time()
    done, fail = 0, []

    for i, code in enumerate(codes):
        # ★#393 限流防护：0.30s 间隔把有效速率压到 ~2只/s（AkShare 同花顺免费接口限流——
        #   08-07 2.3只/s 成功、08-13 4.5只/s 触发 2992 失败；0.30s+网络~0.22s ≈ 1.9只/s 安全）
        time.sleep(0.30)
        try:
            df = fetch_one(code)
            rows = parse_finance(code, df)
            if not rows:
                empty.add(code)
                continue
            con.executemany(
                "INSERT OR REPLACE INTO finance_report VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            con.commit()
            done += 1
        except Exception as e:
            # ★#393 限流重试一次（限流通常是瞬时的，等 2s 重试可挽回大部分）
            time.sleep(2)
            try:
                df = fetch_one(code)
                rows = parse_finance(code, df)
                if rows:
                    con.executemany(
                        "INSERT OR REPLACE INTO finance_report VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
                    con.commit()
                    done += 1
                    continue
                empty.add(code)
                continue
            except Exception:
                pass
            fail.append((code, str(e)[:60]))
        if (i + 1) % 20 == 0:
            el = time.time() - t0
            rate = (i + 1) / el
            eta = (len(codes) - i - 1) / rate if rate > 0 else 0
            log(f"[进度] {i+1}/{len(codes)} 成功{done} 空{len(empty)} 失败{len(fail)} "
                f"速率{rate:.2f}只/s 剩余约{eta/60:.0f}分钟")
        if (i + 1) % 100 == 0:
            PROGRESS_FILE.write_text(
                f"更新 {datetime.now():%H:%M:%S} | 位置 {i+1}/{len(codes)} | 成功 {done} | 失败 {len(fail)}\n",
                encoding="utf-8")

    el = time.time() - t0
    n_rows = con.execute("SELECT COUNT(*) FROM finance_report").fetchone()[0]
    n_codes = con.execute("SELECT COUNT(DISTINCT code) FROM finance_report").fetchone()[0]
    con.close()
    log(f"== 财报下载完成 == 处理 {len(codes)} 只，成功 {done}，空 {len(empty)}，失败 {len(fail)}，"
        f"耗时 {el/60:.1f} 分钟 | 库内 {n_codes} 只 {n_rows} 行")
    EMPTY_FILE.parent.mkdir(parents=True, exist_ok=True)
    EMPTY_FILE.write_text(json.dumps(sorted(empty), ensure_ascii=False), encoding="utf-8")
    if fail:
        log(f"失败清单（前20）: {fail[:20]}")
        # ★#394 失败代码落盘（供 --retry-failed 定向补拉）
        import json as _json
        FAILED_FILE.write_text(
            _json.dumps([c for c, _ in fail], ensure_ascii=False),
            encoding="utf-8")
    elif FAILED_FILE.exists():
        # 本次全部成功 → 清空失败清单
        FAILED_FILE.write_text("[]", encoding="utf-8")
    PROGRESS_FILE.write_text(
        f"最终 {datetime.now():%H:%M:%S} | 库内 {n_codes} 只 {n_rows} 行 | 本次成功 {done} 失败 {len(fail)}\n",
        encoding="utf-8")
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
