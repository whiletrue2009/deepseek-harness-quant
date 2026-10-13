# macOS 免 Tushare 运行指南

这份手册只保留当前仓库可真实执行的 macOS 路径。默认数据链不需要 Tushare token，也不会隐式调用 Tushare。

## 当前状态

- Python：只用仓库内 `.venv/bin/python`。
- HARNESS：只用仓库内 `harness/node_modules`，默认 3080 端口。
- Deck：默认 `http://127.0.0.1:8787/`。
- 默认真实链路：Baostock 日线、腾讯实时快照、同花顺财务质量、本地流通市值反推。
- 覆盖边界：默认研究 Universe 是沪深 A 股。Baostock 不支持北交所，当前股票基础库和日线 loader 也只取 `.SH/.SZ`；腾讯适配层能识别部分北交所快照，但不等于北交所日线已入研究库。
- 30 只样本只用于体验界面和接口，不是全市场结论。全市场最新交易日一般应有 4000 只以上覆盖。
- 已验证盘后链：日线增量、历史市值、红绿灯、机会池、Pitch、科技池、三层池、今日信号。

## 一次性环境

```bash
cd /Users/sgx01/git_open/deepseek-harness-quant

uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt

cd harness
npm ci --ignore-scripts
cd ..
```

配置：

```bash
cp config/params.yaml.example config/params.yaml
```

保持 `data.cache_dir: data/cache`，`data.tushare_token` 和 `data.tushare_api_url` 留空。DeepSeek key 位于 `harness/home/.credentials.yaml`，不要写入文档或命令输出。

## 数据怎么跑

随时查看关键数据覆盖：

```bash
.venv/bin/python -X utf8 data/free_daily_pipeline.py --status
```

### 1. 小样本体验

如果已有 30 只样本缓存，可以重建或刷新样本链：

```bash
.venv/bin/python -X utf8 data/free_daily_pipeline.py --sample 30
```

该命令会执行：Baostock 日线初始化、腾讯估值快照、同花顺质量表、历史市值、沪深300、红绿灯、机会池/Pitch/三层池/今日信号/日报。

### 2. 全市场初始化

完成标准很明确：`data/free_daily_loader.py --status` 里“在市目标”和“已覆盖”基本一致，最新交易日在市覆盖不低于 4000 只，且 `failed=0`。达到这个标准后，才有资格看全市场宽度、机会池和日报结论。

空缓存先跑日线，不要一开始就等待财务全量：

```bash
.venv/bin/python -X utf8 data/free_daily_pipeline.py --init --skip-quality --skip-refresh
```

`--init` 会先用 Baostock 重建股票基础库，即使本地已有 30 只样本库也会刷新为全市场清单。日线从 2019 年开始，默认 2 个并发进程（Baostock 上 4 进程失败率很高，不要调高），支持断点续传；中断后重复执行同一命令即可继续。在市股票完成后，同一命令会继续补拉 2019 年后退市股，降低回测幸存者偏差；如只想先验证在市链路，可加 `--skip-delisted`。按当前网络速度，全市场日线通常需要数小时。

日线与财务质量 loader 都有连续 20 只失败熔断。触发后先检查本机网络或代理，再重复原命令；已成功股票会跳过，不会重复下载。

日线完成后补财务质量。命令支持断点续跑，中断后重复执行即可：

```bash
.venv/bin/python -X utf8 data/fetch_quality_ths.py --run --quarters 10 --rate 0.10 --workers 2
.venv/bin/python -X utf8 data/fetch_valuation_tencent.py
```

想完整体验基本面因子和财务摘要时，再补 `finance.db`。该步骤约需数十分钟，默认不阻塞主管线；同样无公告日，不能当 PIT 数据使用：

```bash
.venv/bin/python -X utf8 data/fetcher_finance.py
.venv/bin/python -X utf8 data/fetcher_finance.py --retry-failed
```

再补全市场历史市值并完成首次刷新。若历史市值来自旧 30 只样本，必须带 `--force` 覆盖月份断点：

```bash
.venv/bin/python -X utf8 data/build_hist_mv.py --start 2020-01 --force
.venv/bin/python -X utf8 data/fetch_index_daily.py --days 30
.venv/bin/python -X utf8 data/traffic_light.py
.venv/bin/python -X utf8 data/refresh_after_data.py
```

`refresh_after_data.py` 会继续生成 `report/market_kb_dump.json` 和 `report/daily_report_日期.md`；如果外包因子池目录不存在，会记录跳过，不伪造因子实测结果。

也可以直接执行 `--init`，但它会串联财务全量，失败点和等待时间不如分步清楚。

### 3. 每个交易日盘后

A 股收盘后再执行：

```bash
.venv/bin/python -X utf8 data/free_daily_pipeline.py
```

这是默认入口。它会增量刷新日线、重建当月历史市值、更新基准指数和红绿灯，并重跑盘后因子链。想只更新数据不生成报告：

```bash
.venv/bin/python -X utf8 data/free_daily_pipeline.py --skip-refresh
```

控制页的“手动全域更新”也走同一条免 Tushare 日线通道；旧 Tushare 增量脚本不会被默认链路触发。

## 数据源与边界

| 数据 | 当前来源 | 说明 |
|---|---|---|
| 股票清单/日线 | Baostock | 免 token；含 OHLC、成交额、换手率、历史 `is_st` |
| 实时快照/估值快照 | 腾讯 | 实时接口动态读取在市股票；PE/PB/市值先落地 `finance.db.valuation`，因子只读本地缓存 |
| 财务质量 | 同花顺三表 | `pub_date=NULL`，非 PIT |
| 历史流通市值 | 本地反推 | `amount / (turn/100)`，不新增网络依赖 |
| 指数基准 | Baostock | 沪深300 |
| 宏观与政策 | 人民银行/AKShare/FRED | 社融官方页优先、国债收益率、中国 EPU |
| 分钟数据 | 暂未接默认链 | `minute.db` 缺失时盘中链路降级，不伪造数据 |

同花顺日线年度接口已保留在 `data/fetcher_free.py`，但本机实测部分代码/年度返回 502，所以只作为可选备源，不作为全市场初始化主源。mootdx 已在依赖中，但默认管线未使用；接入前必须以真实 K 线取数验活，TCP 可连不代表可用。

因子能力同样分两层：当前仓库可跑 7 类机会扫描（反转、低估值、突破、价值重估、事件、质量折价、量价共识）、政策/宏观/情绪/宽度择时和远期收益验证；`data/factorpool` 不存在，历史文档中的 123+ 因子池、AlphaGPT 自动挖掘和九步入池资产不能直接执行。

## 服务启动

```bash
scripts/run_macos.sh
```

入口：

| 服务 | 地址 |
|---|---|
| 量化看板 | `http://127.0.0.1:8787/` |
| Pitch 决策台 | `http://127.0.0.1:8787/pitch` |
| 控制页 | `http://127.0.0.1:8787/control` |
| HARNESS GUI | `http://127.0.0.1:3080/` |

## 验收

```bash
.venv/bin/python -X utf8 data/free_daily_loader.py --status
.venv/bin/python -X utf8 data/fetch_quality_ths.py --status
sqlite3 data/cache/hist_mv.db "SELECT COUNT(DISTINCT month), COUNT(*), MIN(month), MAX(month) FROM hist_mv;"
.venv/bin/python -m compileall -q data report factors deck strategy risk backtest

curl -fsS http://127.0.0.1:8787/ >/dev/null
curl -fsS http://127.0.0.1:8787/api/live/portal_dash >/dev/null
curl -fsS http://127.0.0.1:8787/api/live/realtime >/dev/null
curl -fsS http://127.0.0.1:8787/api/signal >/dev/null
curl -fsS http://127.0.0.1:3080/quantapi/sessions >/dev/null
```

失败清单：

```text
logs/free_daily_baostock_failed.csv
logs/quality_ths_failed.csv
```

重复执行对应 loader 会自动跳过已成功股票并重试失败股票。

## 仍未完成

1. 全市场初始化尚未在本机完整跑完，不能只凭 30 只样本下全市场结论。
2. 同花顺质量表无公告日，后续要用巨潮披露日补 PIT 审计。
3. 分钟库、launchd 定时调度还未接入默认管线。
4. 日常增量不重复拉退市股；完整初始化由 `--init` 调用 `data/backfill_delisted.py` 补 2019 年后退市股，更早期退市股仍需专项审计。
