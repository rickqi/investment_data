# AGENTS.md — investment_data 项目指南

## 项目定位

A股量化数据的采集与 Microsoft Qlib `.bin` 格式转换。两种使用方式：

1. **快速增量更新**（推荐）— 跳过 Dolt，直接从 Tushare API 增量追加到本地 qlib_bin
2. **完整 Dolt 流水线** — Dolt clone → MySQL → normalize → dump_bin（需要 Linux + Docker）

## 关键环境变量

- `TUSHARE` — Tushare API Token（**必须**，从 https://tushare.pro/ 获取）
- `DOLT_CONFIG_GLOBAL_JSON` / `DOLT_JWK` — CI 中 Dolt 认证
- `GITHUB_PAT` — 发布 Release 时需要

## 快速增量更新（推荐日常使用）

```powershell
# Windows / 通用
pip install tushare sqlalchemy pymysql numpy pandas fire
$env:TUSHARE="<token>"
python incremental_update.py --qlib_dir "C:\codes\qlib\qlib_bin"
```

**特性**：幂等（重复运行安全）、用 trade_cal 精确获取交易日、检测除权除息、自动更新指数成分、新股票自动创建。

**数据映射**（从 qlib normalize 逆向推导，不可随意更改）：

| bin 字段 | 计算公式 |
|---|---|
| adjclose | `raw_close × tushare_adj_factor` |
| close/open/high/low | `adj_price × scale`（scale = 已有 bin 末尾 close/adjclose 比值） |
| volume | `tushare_vol / adj_factor` |
| amount | `tushare_amount`（直接用，单位千元） |
| vwap | `(amt/vol × 10) × adj_factor × scale` |
| factor | `tushare_adj_factor` |
| change | `pct_chg / 100` |

**bin 对齐规则**：bin 数组长度 = calendar 天数 + 1（index 0 是 0.0 占位符）。不同股票 bin 长度不同（取决于上市时间），每次增量所有股票追加相同天数。

**股票代码转换**：qlib 用 `sh600000`/`sz000001`/`bj430017`，Tushare 用 `600000.SH`/`000001.SZ`/`430017.BJ`。

## 完整 Dolt 流水线

```bash
# 需要 Linux + Dolt + Docker
export TUSHARE=<token>
bash daily_update.sh          # Dolt fetch → Tushare 下载 → SQL 合并 → Dolt push
bash dump_qlib_bin.sh <dir>   # 全量导出 qlib_bin.tar.gz

# Docker 一键
docker run -v /output:/output -e TUSHARE=<token> chenditc/investment_data \
  bash -c "bash daily_update.sh && bash dump_qlib_bin.sh && cp qlib_bin.tar.gz /output/"
```

### Dolt 管道步骤

```
Tushare API → ts_a_stock_eod_price (MySQL)
  → regular_update.sql 合并 → final_a_stock_eod_price
  → dump_all_to_qlib_source.py → CSV
  → normalize.py (需 PYTHONPATH 含 qlib/scripts)
  → dump_bin.py → .bin 文件
  → tar 打包发布
```

### 数据库连接

硬编码在各脚本中：`mysql+pymysql://root:@127.0.0.1/investment_data`

### 数据表前缀

| 前缀 | 含义 |
|---|---|
| `ts_` | Tushare 数据（主数据源，持续更新） |
| `w_` | Wind 数据（静态，仅到 2019） |
| `c_` | 财汇数据（静态，仅到 2019） |
| `ak_` | Akshare 数据（备选） |
| `final_` | 最终合并数据（qlib 导出来源） |

`ts_link_table` 存储不同数据源的复权因子对齐映射（`adj_ratio`）。

## Qlib 输出格式

```
qlib_bin/
  calendars/day.txt           — 交易日历（YYYY-MM-DD）
  features/{symbol}/          — 每只股票 10 个 .day.bin（float32 数组）
    adjclose, amount, change, close, factor, high, low, open, volume, vwap
  instruments/                — 指数成分列表
    csi300.txt, csi500.txt, csi800.txt, csi1000.txt, csiall.txt, all.txt
```

支持的指数：CSI300(399300.SZ)、CSI500(000905.SH)、CSI800(000906.SH)、CSI1000(000852.SH)、CSI All(000985.SH)

## 技术栈

- Python 3.9（Docker）、本地可用 3.12+
- Dolt（Git-like MySQL，`dolt sql-server` 暴露 MySQL 协议）
- 依赖：`requirements.txt` — tushare, SQLAlchemy, pymysql, fire
- 增量更新额外依赖：numpy, pandas

## 目录结构

```
tushare/                         — Tushare 数据采集脚本
  update_a_stock_eod_price_to_latest.py  — 增量更新（直写 MySQL）
  regular_update.sql                      — 合并 SQL（复权对齐、新股入库）
  dump_index_weight.py                    — 指数成分权重
  dump_day_calendar.py                    — 交易日历

qlib/                            — Qlib 格式导出
  dump_all_to_qlib_source.py    — MySQL → CSV
  normalize.py                   — 标准化（需 PYTHONPATH 含 qlib/scripts）
  dump_index_weight.py           — 指数成分导出

incremental_update.py            — 快速增量更新（无 Dolt 依赖）
import_qlib_bin_to_dolt.py       — 反向导入：qlib_bin → Dolt MySQL
daily_update.sh                  — 每日更新入口（Dolt 流水线）
dump_qlib_bin.sh                 — 全量导出 + 打包
```

## CI/CD

- `.github/workflows/data_update.yml` — 每小时第 30 分钟（self-hosted runner）
- `.github/workflows/upload_release.yml` — 手动触发，上传 qlib_bin.tar.gz 到 Release
- Runner 需要 30G+ 内存、4 核+

## 注意事项

- **normalize.py 需 PYTHONPATH** 包含 qlib/scripts，否则 `from data_collector.base import Normalize` 失败
- **.gitignore 忽略 .csv 和 .venv/**，中间数据和虚拟环境不入库
- Docker 镜像含 Qlib 源码编译（`numpy==1.23.5` + cython）
- `daily_update.sh` 依赖 Linux（`dolt`、`killall`），Windows 需用 `incremental_update.py`
- 所有 Python 脚本用 `python-fire` 提供 CLI 接口
- multiprocessing 启动方式强制 `spawn`（Docker 中通过 sitecustomize.py）
- **复权因子变化**只在除权除息日发生，日常增量不受影响
