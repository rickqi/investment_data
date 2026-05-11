# AGENTS.md — investment_data 项目指南

## 项目定位

A股量化数据的众包采集与 qlib 格式转换工具。核心流程：从 Tushare 等数据源获取日线数据 → 存入 Dolt(MySQL兼容) → 合并校验 → 导出为 Microsoft Qlib 的 `.bin` 格式。

## 技术栈

- **语言**: Python 3.9
- **数据库**: Dolt（Git-like MySQL，通过 `dolt sql-server` 暴露 MySQL 协议）
- **数据源**: Tushare（主）、Akshare、Yahoo、Baostock、Wind、财汇
- **目标格式**: Qlib binary（`.day.bin`）
- **运行环境**: Docker（生产）、本地 bash（开发）
- **依赖**: `requirements.txt` — tushare, SQLAlchemy, pymysql, fire

## 关键环境变量

- `TUSHARE` — Tushare API Token（**必须**，从 https://tushare.pro/ 获取）
- `DOLT_CONFIG_GLOBAL_JSON` / `DOLT_JWK` — CI 中 Dolt 认证凭据
- `GITHUB_PAT` — 发布 Release 时需要

## 数据管道（端到端）

```
1. Tushare API 获取数据 → MySQL ts_a_stock_eod_price 表
   └─ tushare/update_a_stock_eod_price_to_latest.py（增量，自动检测最新日期）

2. 合并到 final 表 → MySQL final_a_stock_eod_price
   └─ tushare/regular_update.sql（处理复权因子对齐、新股入库）

3. 导出 CSV → qlib/qlib_source/{symbol}.csv
   └─ qlib/dump_all_to_qlib_source.py

4. Qlib Normalize → qlib/qlib_normalize/
   └─ qlib/normalize.py（依赖 qlib/scripts 的 PYTHONPATH）

5. 转为 .bin → qlib_bin/
   └─ qlib/scripts/dump_bin.py

6. 打包发布 → qlib_bin.tar.gz
   └─ dump_qlib_bin.sh
```

## 目录结构

```
tushare/          — 数据采集脚本（Tushare 数据源）
  dump_a_stock_eod_price.py      — 全量历史下载（CSV）
  update_a_stock_eod_price_to_latest.py — 增量更新（直写MySQL）
  dump_index_eod_price.py        — 指数日线
  dump_index_weight.py           — 指数成分权重
  dump_day_calendar.py           — 交易日历
  regular_update.sql             — 增量合并SQL（复权对齐、新股处理）
  validation.sql                 — 数据校验SQL
  initial_loading.sql            — 初始化导入SQL

qlib/             — Qlib 格式导出
  dump_all_to_qlib_source.py    — MySQL → CSV
  normalize.py                   — Qlib 标准化（需 PYTHONPATH 含 qlib/scripts）
  dump_index_weight.py           — 指数成分导出为 qlib instruments 格式

daily_update.sh   — 每日更新入口（Dolt fetch → 数据下载 → SQL合并 → Dolt push）
dump_qlib_bin.sh  — 全量导出为 qlib binary 并打包 tarball
Dockerfile        — CI/CD 用（含 Dolt + Qlib 源码编译）
```

## 常用命令

```bash
# 每日更新（需要 TUSHARE 环境变量 + Dolt 已 clone）
export TUSHARE=<token>
bash daily_update.sh

# 导出 qlib binary
bash dump_qlib_bin.sh <working_dir>

# Docker 一键更新+导出
docker run -v /output:/output -e TUSHARE=<token> chenditc/investment_data \
  bash -c "bash daily_update.sh && bash dump_qlib_bin.sh && cp qlib_bin.tar.gz /output/"

# 单独运行某个 Python 脚本（使用 fire CLI）
python tushare/update_a_stock_eod_price_to_latest.py
python qlib/dump_all_to_qlib_source.py --skip_exists=True
```

## 数据库连接

默认 MySQL 连接串（硬编码在各脚本中）：
```
mysql+pymysql://root:@127.0.0.1/investment_data
```
Dolt 通过 `dolt sql-server` 提供 MySQL 兼容协议。

## 数据表命名约定

| 前缀 | 含义 | 说明 |
|------|------|------|
| `ts_` | Tushare 数据 | 主要数据源，持续更新 |
| `w_` | Wind 数据 | 高质量静态数据，仅到 2019 年 |
| `c_` | 财汇数据 | 高质量静态数据，仅到 2019 年 |
| `ak_` | Akshare 数据 | 备选数据源 |
| `final_` | 最终合并数据 | 经过多源校验和修正 |

关键表：
- `ts_a_stock_eod_price` — Tushare A股日线原始数据
- `final_a_stock_eod_price` — 合并后的最终日线数据（**qlib 导出来源**）
- `ts_link_table` — 复权因子对齐表（不同数据源的 adjust_ratio 映射）
- `ts_index_weight` — 指数成分权重
- `ts_trade_day_calendar` — 交易日历

## 复权因子机制

不同数据源的复权基准日不同（各自首日 adjust_factor = 1.0）。`ts_link_table` 存储各源与 final 表的 `adj_ratio`，合并时通过 `adjclose / adj_ratio` 统一复权基准。新增股票默认 `adj_ratio = 1`。

## Qlib 输出格式

目标路径 `qlib_bin/` 结构：
```
calendars/day.txt          — 交易日历（YYYY-MM-DD，每行一天）
features/{symbol}/         — 每只股票一个目录
  open.day.bin, close.day.bin, high.day.bin, low.day.bin,
  vwap.day.bin, volume.day.bin, amount.day.bin,
  adjclose.day.bin, change.day.bin, factor.day.bin
instruments/               — 指数成分列表
  csi300.txt, csi500.txt, csi800.txt, csi1000.txt, csiall.txt, all.txt
```

股票代码格式：`{交易所小写}{6位代码}`，如 `sh600000`、`sz000001`、`bj430017`。

## CI/CD

- `.github/workflows/data_update.yml` — 每小时第30分钟触发（self-hosted runner），Docker 内执行 daily_update.sh
- `.github/workflows/upload_release.yml` — 手动触发，构建 qlib_bin.tar.gz 上传到 GitHub Release
- Runner 需要 30G+ 内存、4核+ CPU

## 注意事项

- **normalize.py 需要设置 PYTHONPATH** 包含 qlib/scripts 目录，否则 `from data_collector.base import Normalize` 会失败
- **.gitignore 忽略所有 .csv 文件**，中间数据不会入库
- Docker 镜像内含 Qlib 源码编译（`numpy==1.23.5` + cython 编译）
- `daily_update.sh` 依赖 Linux 环境（`dolt`、`killall` 命令），Windows 需自行适配
- 所有 Python 脚本使用 `python-fire` 提供 CLI 接口
- multiprocessing 启动方式强制为 `spawn`（Docker 中通过 sitecustomize.py 全局设置）

## 支持的指数

CSI300（399300.SZ）、CSI500（000905.SH）、CSI800（000906.SH）、CSI1000（000852.SH）、CSI All（000985.SH）
