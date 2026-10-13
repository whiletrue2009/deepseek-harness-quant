# -*- coding: utf-8 -*-
"""data/fetch_macro_cache.py — 宏观择时数据缓存（社融/国债收益率）

供择时条件池使用：信贷脉冲（社融 12 月滚动）、国债利差。
社融优先使用人民银行官方统计表，AKShare 仅作兜底；国债收益率使用 AKShare。

用法：python data/fetch_macro_cache.py [--status]
"""
import argparse
import io
import re
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "cache" / "macro.db"

_UA = {"User-Agent": "Mozilla/5.0"}
PBC_BASE = "https://www.pbc.gov.cn"
PBC_INDEX = f"{PBC_BASE}/diaochatongjisi/116219/116319/index.html"


def _macro_get(url: str, timeout: int = 30) -> str:
    import requests

    r = requests.get(url, headers=_UA, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def _abs_pbc(href: str) -> str:
    return href if href.startswith("http") else PBC_BASE + href


def fetch_pbc_social_financing(year: Optional[int] = None) -> list[tuple[str, float]]:
    """读取人民银行社会融资规模增量表，返回 (YYYYMM, 亿元)。"""
    import pandas as pd
    import requests

    idx = _macro_get(PBC_INDEX)
    years = re.findall(
        r"""href=["']([^"']+)["'][^>]*>\s*(\d{4})年统计数据\s*</a>""", idx
    )
    if not years:
        raise RuntimeError("人民银行索引页未找到年度统计数据链接")
    table = {int(y): href for href, y in years}
    target = max(table) if year is None else year
    if target not in table:
        raise ValueError(f"人民银行无 {target} 年社融数据")

    ypage = _macro_get(_abs_pbc(table[target]))
    topics = re.findall(
        r"""href=["']([^"']+)["'][^>]*>\s*(社会融资规模)\s*</a>""", ypage
    )
    if not topics:
        raise RuntimeError(f"{target} 年页未找到社会融资规模专题")
    tpage = _macro_get(_abs_pbc(topics[0][0]))
    books = re.findall(r"""href=["']([^"']+\.xlsx?)["']""", tpage)
    if not books:
        raise RuntimeError(f"{target} 年社融专题页未找到 xls 附件")

    content = requests.get(_abs_pbc(books[0]), headers=_UA, timeout=60).content
    raw = pd.read_excel(io.BytesIO(content), header=None)
    start = next(
        (i for i in range(len(raw)) if str(raw.iloc[i, 0]).strip() == "月份"), None
    )
    if start is None:
        raise RuntimeError(f"{target} 年社融表不是 2021 起的新版式")
    rows: list[tuple[str, float]] = []
    for record in raw.iloc[start + 3:].itertuples(index=False):
        label = str(record[0]).strip()
        match = re.match(r"^(\d{4})\.(\d{1,2})$", label)
        if not match:
            continue
        month_num = int(match.group(2))
        if len(match.group(2)) == 1:
            month_num *= 10
        try:
            value = float(record[1])
        except (TypeError, ValueError):
            continue
        if value == value:
            rows.append((f"{target}{month_num:02d}", value))
    if not rows:
        raise RuntimeError(f"{target} 年社融表无有效月份")
    return rows


def store_social(con: sqlite3.Connection, rows: list[tuple[str, float]]) -> int:
    con.executemany(
        "INSERT OR REPLACE INTO social_finance VALUES (?,?)", rows
    )
    con.commit()
    return len(rows)


def store_bond_yield(con: sqlite3.Connection, rows: list[tuple[str, float, float]]) -> int:
    con.executemany("INSERT OR REPLACE INTO bond_yield VALUES (?,?,?)", rows)
    con.commit()
    return len(rows)


def status() -> int:
    if not DB.exists():
        print("macro.db 不存在")
        return 0
    con = sqlite3.connect(str(DB))
    sf = con.execute(
        "SELECT COUNT(*), MIN(month), MAX(month) FROM social_finance"
    ).fetchone()
    by = con.execute(
        "SELECT COUNT(*), MIN(date), MAX(date), SUM(y10 IS NOT NULL), SUM(y2 IS NOT NULL) FROM bond_yield"
    ).fetchone()
    con.close()
    print(f"social_finance: {sf[0]} 行，{sf[1]} ~ {sf[2]}")
    print(
        f"bond_yield: {by[0]} 行，{by[1]} ~ {by[2]}；"
        f"y10={by[3]}，y2={by[4]}"
    )
    return 0


def main():
    import akshare as ak
    con = sqlite3.connect(str(DB))
    con.execute("""CREATE TABLE IF NOT EXISTS social_finance (
        month TEXT PRIMARY KEY, sf_increment REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS bond_yield (
        date TEXT PRIMARY KEY, y10 REAL, y2 REAL)""")
    con.commit()

    # 1) 社融增量：人民银行官方源优先，避免 AKShare 历史表滞后数月。
    try:
        current_year = datetime.now().year
        rows = []
        # 12 个月滚动同比需要“本年 + 前两年”的窗口。
        for year in (current_year - 2, current_year - 1, current_year):
            try:
                rows.extend(fetch_pbc_social_financing(year))
            except Exception as exc:
                print(f"人民银行 {year} 年社融失败: {str(exc)[:80]}")
        n = store_social(con, rows) if rows else 0
        print(f"社融缓存（人民银行）: {n} 个月")
    except Exception as e:
        print(f"社融失败: {str(e)[:80]}")

    # AKShare 兜底能补历史，但如果官方源已成功则不再用可能滞后的表覆盖。
    try:
        has_current = con.execute(
            "SELECT 1 FROM social_finance WHERE month LIKE ? ORDER BY month DESC LIMIT 1",
            (f"{datetime.now().year}%",),
        ).fetchone()
        if not has_current:
            df = ak.macro_china_shrzgm()
            rows = []
            for r in df.itertuples():
                m = str(r[1]).strip()
                try:
                    v = float(r[2])
                except (TypeError, ValueError):
                    continue
                if v == v:
                    rows.append((m, v))
            n = store_social(con, rows)
            print(f"社融缓存（AKShare兜底）: {n} 个月，注意确认官方发布月")
    except Exception as e:
        print(f"社融兜底失败: {str(e)[:80]}")

    # 2) 国债收益率（日频，存全量月末后续可取）
    try:
        df = ak.bond_zh_us_rate(start_date="20180101")
        n = 0
        for r in df.itertuples():
            d = str(r[1]).strip()
            if not d or d == 'nan':
                continue
            try:
                y10 = float(r[4]) if str(r[4]) not in ('nan', '-', 'None') else None
            except Exception:
                y10 = None
            try:
                y2 = float(r[3]) if str(r[3]) not in ('nan', '-', 'None') else None
            except Exception:
                y2 = None
            if y10 is not None:
                con.execute("INSERT OR REPLACE INTO bond_yield VALUES (?,?,?)", (d, y10, y2))
                n += 1
        con.commit()
        print(f"国债收益率缓存: {n} 个交易日")
    except Exception as e:
        print(f"国债收益率失败: {str(e)[:80]}")

    con.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="免费宏观择时数据缓存")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    sys.exit(status() if args.status else main())


if __name__ == "__main__":
    sys.exit(main())
