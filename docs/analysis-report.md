# investment_data 项目分析报告

> 分析日期：2026-05-10
> 仓库地址：https://github.com/rickqi/investment_data
> DoltHub 数据：https://www.dolthub.com/repositories/chenditc/investment_data

---

## 一、项目概述

`investment_data` 是一个**众包（Crowd-sourced）A 股金融数据集**项目，核心目标：

1. **多数据源融合**：整合 Tushare、Wind、财汇、AKShare、Yahoo、Baostock 等多源数据
2. **数据纠错**：通过跨源交叉验证修正错误数据
3. **填补缺失**：用多源数据补全退市公司等缺失记录
4. **输出 Qlib 格式**：最终生成 Qlib 二进制数据供量化研究使用

**核心存储**：使用 **Dolt 数据库**——一个支持 Git 式版本控制的 MySQL 兼容数据库，托管在 DoltHub。

---

## 二、仓库目录结构

```
investment_data/
├── qlib/                          # Qlib 格式导出脚本
│   ├── dump_all_to_qlib_source.py # 从 Dolt SQL 查询导出为 CSV
│   ├── normalize.py               # Qlib 官方数据规范化（复权处理）
│   └── dump_index_weight.py       # 导出指数成分股权重
├── tushare/                       # Tushare 数据源脚本
│   ├── dump_a_stock_eod_price.py       # 全量 A 股日行情下载
│   ├── dump_index_eod_price.py         # 指数行情下载
│   ├── dump_index_weight.py            # 指数权重下载
│   ├── dump_day_calendar.py            # 交易日历导出
│   ├── dump_tushare_stock_list.py      # 股票列表下载
│   ├── update_a_stock_eod_price_to_latest.py  # 增量更新行情
│   ├── initial_loading.sql             # 初始数据导入 SQL
│   ├── regular_update.sql              # 日常更新合并 SQL
│   ├── validation.sql                  # 数据校验 SQL
│   ├── fill_amount.sql                 # 成交额补全
│   ├── insert_rest.sh                  # 批量导入脚本
│   ├── update_stock_list.sh            # 更新股票列表
│   ├── index_mapping.json              # 指数代码映射
│   └── price_mapping.json              # 价格字段映射
├── yahoo/                         # Yahoo Finance 数据（预留）
├── one_time_db_scripts/           # 一次性数据库脚本
├── docs/                          # 文档
├── .github/workflows/             # CI/CD 工作流
│   ├── data_update.yml            # 每小时自动数据更新
│   ├── docker-image.yml           # Docker 镜像构建
│   └── upload_release.yml         # 发布到 GitHub Release
├── Dockerfile                     # Docker 构建文件
├── dump_qlib_bin.sh               # 导出 Qlib 二进制格式（核心脚本）
├── daily_update.sh                # 每日数据更新流程
├── upload_release.sh              # 上传发布包
├── requirements.txt               # Python 依赖
└── README.md                      # 项目说明
```

---

## 三、数据架构

### 3.1 数据源与表命名规范

DoltHub 上的数据库表以数据源前缀命名，例如 `ts_a_stock_eod_price`。前缀含义：

| 前缀 | 数据源 | 说明 |
|------|--------|------|
| `w` | Wind | 高质量静态数据源，仅到 2019 年 |
| `c` | 财汇 (Caihui) | 高质量静态数据源，仅到 2019 年 |
| `ts` | Tushare | 主要日常更新数据源 |
| `ak` | AKShare | 辅助数据源 |
| `yahoo` | Yahoo Finance | 使用 Qlib 的 yahoo collector |
| `baostock` | Baostock | 辅助历史数据 |
| **`final`** | **合并终表** | **经验证校正后的最终数据** |

### 3.2 核心数据表

| 表名 | 内容 | 说明 |
|------|------|------|
| `final_a_stock_eod_price` | A 股日行情终表 | OHLCV + 复权价 + 成交额，所有数据源合并后的最终版本 |
| `final_a_stock_limit` | A 股涨跌停数据 | 涨跌停价格 |
| `ts_a_stock_eod_price` | Tushare 原始行情 | 直接从 Tushare API 下载的 A 股日行情 |
| `ts_index_weight` | 指数成分股权重 | 沪深 300、中证 500 等指数的成分股及权重 |
| `ts_trade_day_calendar` | 交易日历 | SSE 交易日历 |
| `ts_link_table` | 数据源映射表 | 不同数据源之间的复权因子映射，确保多源复权价一致 |
| `max_index_date` | 指数最新日期 | 记录指数数据的最新更新日期 |

### 3.3 导出指数

| 文件名 | 指数 | 代码 |
|--------|------|------|
| `csi300.txt` | 沪深 300 | 399300.SZ |
| `csi500.txt` | 中证 500 | 000905.SH |
| `csi800.txt` | 中证 800 | 000906.SH |
| `csi1000.txt` | 中证 1000 | 000852.SH |
| `csiall.txt` | 中证全指 | 000985.SH |

---

## 四、完整数据流

```
┌─────────────────────────────────────────────────────────────────┐
│                    数据采集 (daily_update.sh)                     │
│                                                                  │
│  Tushare API                                                     │
│    ├─ dump_index_weight.py    → ts_index_weight 表                │
│    ├─ dump_index_eod_price.py → ts_a_stock_eod_price 表           │
│    └─ update_a_stock_eod_price_to_latest.py → 增量行情下载        │
│                                                                  │
│  Dolt SQL (regular_update.sql)                                    │
│    ├─ 新股自动加入 ts_link_table                                   │
│    ├─ 按 adj_ratio 缩放复权价，确保多源数据复权价一致               │
│    └─ 合并到 final_a_stock_eod_price 终表                         │
│                                                                  │
│  dolt add -A && dolt commit -m "Daily update" && dolt push        │
│    → 推送到 DoltHub: dolthub.com/chenditc/investment_data         │
└───────────────────────┬─────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│              Qlib 导出 (dump_qlib_bin.sh)                         │
│                                                                  │
│  步骤 1: dolt clone → 启动 dolt sql-server (MySQL 协议)           │
│                                                                  │
│  步骤 2: dump_all_to_qlib_source.py                              │
│    → SQL 查询 final_a_stock_eod_price                             │
│    → 计算 VWAP = amount / volume * 10                             │
│    → 按股票分文件写 CSV (qlib_source/{symbol}.csv)                 │
│                                                                  │
│  步骤 3: normalize.py (基于 Qlib 官方 YahooNormalizeCN1d)         │
│    → 复权处理、VWAP 调整                                           │
│                                                                  │
│  步骤 4: dump_bin.py (Qlib 官方工具)                               │
│    → CSV → 二进制 .bin 文件                                        │
│                                                                  │
│  步骤 5: dump_index_weight.py                                     │
│    → SQL 查询 ts_index_weight → csi300/500/800/1000/all.txt       │
│                                                                  │
│  步骤 6: dump_day_calendar.py                                     │
│    → 生成交易日历 day.txt                                          │
│                                                                  │
│  步骤 7: 打包 qlib_bin.tar.gz → 上传到 GitHub Release             │
└───────────────────────┬─────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│                       用户使用                                     │
│                                                                  │
│  wget github.com/.../qlib_bin.tar.gz                              │
│  tar -zxvf qlib_bin.tar.gz -C ~/.qlib/qlib_data/cn_data          │
│  qlib.init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")│
└─────────────────────────────────────────────────────────────────┘
```

### 4.1 关键脚本详解

#### `dump_all_to_qlib_source.py` — Dolt 数据导出

```python
# 连接本地 Dolt SQL Server（MySQL 协议）
sqlEngine = create_engine('mysql+pymysql://root:@127.0.0.1/investment_data')
# 查询终表并计算 VWAP
stock_df = pd.read_sql("select *, amount/volume*10 as vwap from final_a_stock_eod_price", dbConnection)
# 按股票代码分组写入 CSV
for symbol, df in stock_df.groupby("symbol"):
    df.to_csv(f'qlib_source/{symbol}.csv', index=False)
```

#### `normalize.py` — 数据规范化

继承 Qlib 官方的 `YahooNormalizeCN1d`，增加 VWAP 字段处理：
- 对 open/close/high/low/volume/vwap 进行复权调整
- 保留原始 amount 值（确保 adjusted_volume × adjusted_vwap = amount）

#### `update_a_stock_eod_price_to_latest.py` — 增量更新

```python
# 1. 查询数据库中最新交易日期
# 2. 从 Tushare API 下载该日期之后的所有交易日行情
# 3. 合并复权因子：adj_close = close × adj_factor
# 4. 追加到 ts_a_stock_eod_price 表
```

#### `regular_update.sql` — 数据合并逻辑

```sql
-- 1. 新股自动加入映射表
INSERT IGNORE INTO ts_link_table (w_symbol, link_symbol, link_date)
SELECT ... FROM ts_a_stock_eod_price WHERE ...;

-- 2. 新股价格填入终表（adj_ratio=1）
INSERT IGNORE INTO final_a_stock_eod_price ...
SELECT ts.high, ts.low, ... ROUND(ts.adjclose, 2) ...
FROM ts_a_stock_eod_price ts LEFT JOIN ts_link_table ON ...;

-- 3. 增量行情按复权因子缩放后合并
INSERT IGNORE INTO final_a_stock_eod_price ...
SELECT ... ROUND(ts_raw_table.adjclose / ts_link_table.adj_ratio, 2) ...
FROM ts_a_stock_eod_price LEFT JOIN ts_link_table ON ...;
```

---

## 五、Dolt 数据库详解

### 5.1 什么是 Dolt？

Dolt 是世界上第一个**版本控制关系数据库**——"Git for Data"。

| 特性 | Dolt | 传统 MySQL |
|------|------|-----------|
| 版本控制 | ✅ 每次修改有 commit 历史 | ❌ 需外部备份 |
| 分支 | ✅ `dolt checkout -b feature` | ❌ 不支持 |
| 合并 | ✅ `dolt merge` | ❌ 不支持 |
| 差异对比 | ✅ `dolt diff` 查看行级变化 | ❌ 不支持 |
| 远程同步 | ✅ DoltHub（类似 GitHub） | ❌ 需自建复制 |
| SQL 兼容 | ✅ MySQL 协议，支持标准 SQL | — |
| Python 连接 | ✅ pymysql / SQLAlchemy | ✅ 相同 |

### 5.2 在本项目中的作用

```
DoltHub (云端)         ←→      本地 Dolt 数据库         ←→      Python 脚本
 dolthub.com/                  dolt sql-server                   pymysql/SQLAlchemy
 chenditc/investment_data      (MySQL 协议)                      数据处理+导入
     ↑                            ↑                                 ↑
  数据托管                    版本控制 + SQL 服务                  数据处理 + 导入
```

- **DoltHub** 托管于 `dolthub.com/chenditc/investment_data`，类似 GitHub 托管代码
- **dolt sql-server** 在本地启动 MySQL 兼容服务（默认端口 3306）
- **Python 脚本**通过 `mysql+pymysql://root:@127.0.0.1/investment_data` 连接操作数据

---

## 六、Dolt 安装与使用指南（Windows）

### 6.1 安装 Dolt

**方法 A：winget（推荐）**
```powershell
winget install DoltHub.Dolt
```

**方法 B：Chocolatey**
```powershell
choco install dolt
```

**方法 C：Scoop**
```powershell
scoop install dolt
```

**方法 D：手动下载**
1. 访问 https://github.com/dolthub/dolt/releases
2. 下载 `dolt-windows-amd64.msi`
3. 双击安装

**验证安装：**
```powershell
dolt version
# 输出示例: dolt 1.88.0
```

### 6.2 初始配置

```powershell
dolt config --global --add user.name "你的名字"
dolt config --global --add user.email "your@email.com"
```

### 6.3 克隆本项目数据

```powershell
# 克隆 DoltHub 上的数据（类似 git clone）
dolt clone chenditc/investment_data
cd investment_data

# 查看所有表
dolt ls

# 查看表结构
dolt schema show final_a_stock_eod_price

# SQL 查询数据
dolt sql -q "SELECT COUNT(*) FROM final_a_stock_eod_price"
dolt sql -q "SELECT * FROM final_a_stock_eod_price WHERE symbol='SH600519' ORDER BY tradedate DESC LIMIT 10"

# 查看提交历史
dolt log
```

### 6.4 启动 SQL Server（供 Python 连接）

```powershell
# 在数据目录中启动 MySQL 兼容服务
cd investment_data
dolt sql-server
# 默认监听 127.0.0.1:3306，用户 root，无密码
```

然后用 Python 连接：
```python
from sqlalchemy import create_engine
import pandas as pd

engine = create_engine('mysql+pymysql://root:@127.0.0.1/investment_data')

# 查询贵州茅台行情
df = pd.read_sql(
    "SELECT * FROM final_a_stock_eod_price WHERE symbol='SH600519' ORDER BY tradedate DESC LIMIT 10",
    engine
)
print(df)
```

### 6.5 常用 Dolt 命令对照表

| 操作 | Git 命令 | Dolt 命令 |
|------|---------|----------|
| 克隆 | `git clone URL` | `dolt clone org/repo` |
| 查看状态 | `git status` | `dolt status` |
| 查看差异 | `git diff` | `dolt diff` |
| 添加变更 | `git add .` | `dolt add -A` |
| 提交 | `git commit -m "msg"` | `dolt commit -m "msg"` |
| 推送 | `git push` | `dolt push origin main` |
| 拉取 | `git pull` | `dolt pull` |
| 查看日志 | `git log` | `dolt log` |
| 创建分支 | `git branch feature` | `dolt checkout -b feature` |
| SQL 查询 | — | `dolt sql -q "SELECT ..."` |
| 启动 SQL 服务 | — | `dolt sql-server` |
| 导入 CSV | — | `dolt table import -u table_name file.csv` |
| 查看表列表 | — | `dolt ls` |
| 查看表结构 | — | `dolt schema show table_name` |

### 6.6 Python 连接方式

Dolt 的 `sql-server` 完全兼容 MySQL 协议，可直接使用 MySQL 生态工具：

```python
# 方式 1：SQLAlchemy（推荐）
from sqlalchemy import create_engine
engine = create_engine('mysql+pymysql://root:@127.0.0.1/investment_data')

# 方式 2：原生 pymysql
import pymysql
conn = pymysql.connect(host='127.0.0.1', user='root', database='investment_data')

# 方式 3：pandas 直接读取
import pandas as pd
df = pd.read_sql("SELECT * FROM final_a_stock_eod_price LIMIT 100", engine)
```

---

## 七、CI/CD 自动化

### 7.1 自动更新流程

```
GitHub Actions (每小时触发 cron: '30 * * * *')
    ↓
data_update.yml → self-hosted Docker 容器
    ↓
daily_update.sh
    ├─ dolt clone / pull (拉取最新数据)
    ├─ Tushare API 下载当日行情、指数权重
    ├─ SQL 合并到 final 表
    └─ dolt push (推送到 DoltHub)
```

### 7.2 自动发布流程

```
upload_release.yml
    ↓
dump_qlib_bin.sh
    ├─ dolt clone → 启动 sql-server
    ├─ dump_all_to_qlib_source.py → CSV
    ├─ normalize.py → 规范化
    ├─ dump_bin.py → 二进制 .bin
    ├─ dump_index_weight.py → 指数成分文件
    ├─ dump_day_calendar.py → 交易日历
    ├─ 数据新鲜度检查（CHECK_FRESHNESS=1）
    └─ tar 打包 → GitHub Release
```

---

## 八、快速使用指南

### 8.1 最简方式：直接下载预构建数据

```powershell
# 1. 下载最新数据包
Invoke-WebRequest -Uri "https://github.com/chenditc/investment_data/releases/latest/download/qlib_bin.tar.gz" -OutFile "qlib_bin.tar.gz"

# 2. 解压到 Qlib 默认目录
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.qlib\qlib_data\cn_data"
tar -zxvf qlib_bin.tar.gz -C "$env:USERPROFILE\.qlib\qlib_data\cn_data" --strip-components=1

# 3. 在 Python 中使用
import qlib
qlib.init(provider_uri="~/.qlib/qlib_data/cn_data", region="cn")
```

### 8.2 完整开发方式：Dolt + Docker

```powershell
# 1. 安装 Dolt
winget install DoltHub.Dolt
dolt config --global --add user.name "Your Name"
dolt config --global --add user.email "your@email.com"

# 2. 克隆数据
dolt clone chenditc/investment_data

# 3. 启动 SQL 服务查询
cd investment_data
dolt sql-server &
# Python: mysql+pymysql://root:@127.0.0.1/investment_data

# 4. 或使用 Docker 一键导出 Qlib 格式
docker run -v D:\output:/output -it --rm chenditc/investment_data bash dump_qlib_bin.sh
```

### 8.3 每日更新方式（需 Tushare Token）

```powershell
# 设置 Tushare Token
$env:TUSHARE = "your_tushare_token"

# 运行每日更新
bash daily_update.sh

# 或 Docker 一键更新+导出
docker run -v D:\output:/output -e TUSHARE=your_token -it --rm `
  chenditc/investment_data `
  bash -c "bash daily_update.sh && bash dump_qlib_bin.sh && cp ./qlib_bin.tar.gz /output/"
```

---

## 九、与 TradingAgents + Qlib 集成的建议

### 9.1 数据对比

| | `cn_data`（investment_data） | `ta_data`（TradingAgents 自建） |
|---|---|---|
| 数据范围 | 全 A 股（4000+ 只） | 20 只目标股票 |
| 历史深度 | 2007 年至今 | 2023 年至今 |
| 字段 | OHLCV + VWAP | OHLCV + TA 分析信号 |
| 数据质量 | 多源交叉验证，高质量 | 单源（腾讯 K 线） |
| TA 分析信号 | ❌ 无 | ✅ 有（ta_rating 等） |
| 指数成分 | ✅ 有（沪深 300 等） | ❌ 无 |
| 交易日历 | ✅ 有 | ✅ 有 |
| 适用场景 | 标准 Qlib 回测、指数策略 | TA 信号增强策略 |

### 9.2 推荐组合使用

```
cn_data (investment_data)          ta_data (TradingAgents)
     │                                    │
     ├─ 标准 OHLCV 行情                    ├─ TA 分析信号
     ├─ 指数成分权重                        ├─ AI 评级信号
     ├─ 交易日历                            └─ 交易决策信号
     └─ 高质量历史数据                            │
     │                                            │
     └───────────── 合并使用 ─────────────────────┘
                        │
                        ▼
              Qlib 量化研究框架
         （标准因子 + AI 信号增强）
```

**具体方案**：
1. 用 `cn_data` 作为 Qlib 主数据源（覆盖全 A 股、历史完整、数据质量高）
2. 用 `ta_data` 中的 TradingAgents 信号作为**额外特征**叠加
3. 在模型训练时同时使用标准量价因子和 AI 分析信号

---

## 十、关键依赖

| 依赖 | 版本 | 用途 |
|------|------|------|
| Python | 3.9 | Docker 环境运行时 |
| tushare | 1.2.89 | Tushare 数据 API |
| SQLAlchemy | 2.0.29 | 数据库 ORM |
| pymysql | latest | MySQL/Dolt 连接驱动 |
| fire | latest | CLI 参数解析 |
| numpy | 1.23.5 | 数值计算 |
| qlib | latest (editable) | Qlib 量化框架（从源码安装） |
| Dolt | 1.88.0+ | 版本控制数据库 |
