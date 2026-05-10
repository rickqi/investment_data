# Qlib + TradingAgents + RD-Agent 集成流水线分析报告

> 日期：2026-05-10
> 分析范围：训练脚本、回测框架、RD-Agent 因子质量、AI 信号覆盖

---

## 一、当前项目整体状态

| 模块 | 状态 | 质量评估 |
|---|---|---|
| **TradingAgents** | ✅ 可运行 | 20只目标股分析完成，OHLCV数据齐全 |
| **Qlib 训练** | ✅ 可运行 | Model A (193%) / Model C (243%) 正常工作 |
| **AI 信号 (Model B)** | ❌ 无效 | 60行信号 / 132万行训练数据 = 0.0045% 覆盖率 |
| **RD-Agent 因子 (Model C)** | ✅ 有效 | 2因子已集成，+50%累计收益提升 |
| **RD-Agent 模型生成** | ⚠️ 受限 | SimpleLSTM已生成，env_type已修复但未重新运行 |
| **RD-Agent 第2轮因子发现** | 🔄 运行中 | Loop 0，3个新因子，仅 volume_turnover_5d 成功 |

### 模型对比结果

| 指标 | Model A (OHLCV) | Model B (OHLCV+AI) | Model C (RD-Agent) |
|---|---|---|---|
| 累计收益 | 193.83% | 193.83% | **243.88%** |
| 年化收益 | 133.07% | 133.07% | **163.70%** |
| 最大回撤 | 12.04% | 12.04% | **10.71%** |
| 夏普比率 | 5.14 | 5.14 | **5.74** |
| 交易次数 | 107 | 107 | 107 |
| 终值资金 | ¥2,938,336 | ¥2,938,336 | ¥3,438,812 |

**注意**：以上夏普比率存在计算错误，实际值约为报告值的 1/√2 ≈ 0.71 倍（见下方 BUG 1）。

---

## 二、训练脚本关键缺陷

### 🔴 BUG 1：夏普比率计算错误（P0 高严重度）

**位置**: `qlib_train_backtest.py` line 503
```python
daily_returns = np.diff(values) / values[:-1]
sharpe = np.mean(daily_returns) / max(np.std(daily_returns), 1e-8) * np.sqrt(252)
```

**问题**：`portfolio_values` 仅在每 2 天调仓时记录，所以 `daily_returns` 实际上是 **2 日收益**，但年化因子用了 `sqrt(252)`（日收益年化）。使用 `sqrt(252)` 年化因子**高估夏普比率 ~sqrt(2) ≈ 1.41x**。

**影响**：报告的夏普 5.14/5.74 实际约为 3.63/4.06。

**修复**：`sqrt(252)` → `sqrt(252 / hold_days)`

---

### 🔴 BUG 2：缺少 IC 评估（P0）

**问题**：没有计算预测值与实际收益的相关性（IC/ICIR），这是量化因子评估的**标准指标**。

**影响**：无法判断模型是真有预测能力还是运气好。IC > 0.03 通常被认为有效。

**修复**：添加 per-date IC 计算，报告 mean IC 和 ICIR。

---

### 🟡 BUG 3：回测无交易成本（P1 高影响）

**问题**：A 股单边交易成本约 0.15%（印花税 0.05% + 佣金 0.025%×2 + 滑点），107 笔交易下累计成本约 16-32%，显著侵蚀名义收益。

**修复**：在每次交易中扣除 0.3% 单次成本。

---

### 🟡 BUG 4：标签泄漏（P1 中严重度）

**位置**: `qlib_train_backtest.py` line 246
```python
label = close.groupby(level=0).shift(-2) / close - 1  # 2日前瞻收益
```

**问题**：训练集最后 2 天的标签使用了验证期价格，验证集最后 2 天使用了测试期价格。

**修复**：在 `build_handler_dataset` 中删除每段末尾 `hold_days` 行。

---

### 🟠 BUG 5：无基准对比（P1）

**问题**：没有对比等权买入持有 20 只目标股的表现，无法评估绝对价值。

**修复**：添加基准回测（等权持有20股，同样的测试期间）。

---

### 🟠 其他问题

- **Dead Code**: `test_price_df` (line 634) 计算后从未使用
- **数据加载性能**: `build_stock_df` 使用 Python for-loop 对齐日历，O(N²) 复杂度
- **Portfolio 追踪不完整**: 仅在调仓日记录净值，最大回撤可能被低估
- **无随机种子**: 结果不可复现

---

## 三、RD-Agent 因子分析

### 因子库（10 个实现，去重后 6-7 个独立因子）

| 因子 | 状态 | 公式 | 本质 |
|---|---|---|---|
| `short_term_momentum_5d` | ✅ 已集成 | close_t / close_{t-5} - 1 | 5日价格动量 |
| `volume_change_5d` | ✅ 已集成 | avg_vol(5d) / avg_vol(5d shifted) | 5日成交量比率 |
| `5_day_momentum` | 🔄 待集成 | close_t / close_{t-5} - 1 | 与 short_term_momentum_5d **完全重复** |
| `Momentum_5d` | 🔄 待集成 | close_t / close_{t-5} - 1 | 与 short_term_momentum_5d **完全重复** |
| `5_day_return` | 🔄 待集成 | adj_close_t / adj_close_{t-5} - 1 | 与上述 IC>0.99 |
| `momentum_10d` | ⚠️ FAIL | ln(close_t / close_{t-10}) | 10日对数收益 |
| `volume_turnover_5d` | ✅ 成功 | mean(volume * close, 5d) | 5日成交额均值 |
| `5_day_avg_turnover` | 🔄 待集成 | mean(volume/1e8, 5d) | 5日换手率 |
| `volatility_20d` | ⚠️ FAIL | std(log_returns, 20d) | 20日波动率 |
| `5_day_volatility` | 🔄 待集成 | std(log_returns, 5d) | 5日波动率 |

**去重后独立因子类别**：
1. 一个 5 日动量（从 3 个重复中选 1 个）
2. momentum_10d（不同窗口）
3. volume_change_5d（成交量比率）
4. volume_turnover_5d（成交额）
5. 5_day_avg_turnover（换手率）
6. 5_day_volatility（短期波动率）
7. volatility_20d（长期波动率）

**质量评估**：所有因子均为基础技术指标，没有 alpha 创新。RD-Agent 当前运行类似自动化的因子回测引擎。

### RD-Agent 运行成本

- 第1轮（2因子）：$0.014
- 第2轮（进行中）：$0.014（截至目前）
- 模型：DeepSeek deepseek-chat（极低成本）

---

## 四、AI 信号集成根本性缺陷

```
AI 信号覆盖：
  训练数据总量：  1,320,817 行 (2000-2026, 220只股票)
  AI 信号可用：       60 行 (2026-04-30 ~ 2026-05-09)
  覆盖率：         0.0045%
```

**根本原因**：TradingAgents 每次分析一只股票需要 ~10 分钟，20只×6日期 = ~20小时。

**结论**：Model B 路线在当前架构下**不可行**，除非批量化运行或缩小训练窗口。

---

## 五、RD-Agent 工作空间

- 因子实现：`D:\codes\RD-Agent\git_ignore_folder\RD-Agent_workspace\{UUID}\factor.py`
- 模型实现：`D:\codes\RD-Agent\git_ignore_folder\RD-Agent_workspace\f4c27c0205294c19a397d6314b2748b8\model.py`
- 因子结果：每个 workspace 文件夹中的 `result.h5`
- 源数据：`D:\codes\RD-Agent\git_ignore_folder\factor_implementation_source_data\daily_pv.h5`
- 运行日志：`D:\codes\RD-Agent\log\` (22 个时间戳目录)
- 缓存：`D:\codes\RD-Agent\pickle_cache\` (24个因子执行缓存, 2个模型缓存)

---

## 六、修复优先级

| 优先级 | 修复项 | 改动量 | 预期收益 |
|---|---|---|---|
| **P0** | 夏普比率：`sqrt(252)` → `sqrt(252/hold_days)` | 1行 | 修正核心指标 |
| **P0** | 添加 IC 评估 | ~30行 | 量化模型真实预测能力 |
| **P1** | 交易成本（0.3%/次） | ~10行 | 真实收益估计 |
| **P1** | 标签泄漏修复 | ~5行 | 消除数据泄漏 |
| **P1** | 基准线（等权持有） | ~40行 | 判断是否跑赢被动投资 |
