#!/usr/bin/env python3
"""
backfill_nan_dates.py — 回填 qlib_bin 中因 incremental_update 数据污染产生的 NaN 空洞。

背景（2026-08-27 修复）:
    incremental_update.py 每日 01:00 运行时把"今天"（当时无数据）加入日历并写入 NaN，
    导致 2026-06-01 起约一半交易日（隔日交替）全市场 NaN。本脚本用 tushare 拉取
    这些日期的真实日线 + 复权因子，按与 incremental_update 相同的 scale/复权逻辑
    定点回填 bin 文件，仅覆盖"该日期该股确为 NaN"的位置（保留真正的停牌 NaN）。

用法:
    python backfill_nan_dates.py                        # 默认只回填目标池 26 只
    python backfill_nan_dates.py --all                  # 回填全部股票
    python backfill_nan_dates.py --pool-only            # 仅目标池（默认）
    python backfill_nan_dates.py --dry-run              # 只统计不写文件
    python backfill_nan_dates.py --start 2026-06-01     # 自定义起始日期

数据映射（与 incremental_update.py / investment_data/AGENTS.md 一致，不可更改）:
    adjclose = raw_close × tushare_adj_factor
    close/open/high/low = adj_price × scale（scale = 该股最近有效 bin 的 close/adjclose）
    volume = tushare_vol / adj_factor
    amount = tushare_amount（直接用，单位千元）
    vwap = (amt/vol×10) × adj_factor × scale
    factor = tushare_adj_factor
    change = pct_chg / 100

bin 对齐规则: bin 数组长度 = 日历天数 + 1（index 0 为 0.0 占位符）。
    上市偏移 offset = len(calendar) - (len(bin) - 1)，bin[k] ↔ calendar[offset + k - 1]。
"""
import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import tushare as ts

BIN_FIELDS = ['adjclose', 'amount', 'change', 'close', 'factor', 'high', 'low', 'open', 'volume', 'vwap']
BIN_OFFSET = 1  # bin 数组比 calendar 多 1（index 0 占位符）

# 目标池（stocks_config.STOCKS 共 26 只，代码格式 600000.SH）
POOL_TS_CODES = [
    '688041.SH', '688256.SH', '688012.SH', '603986.SH', '688008.SH',
    '300442.SZ', '603019.SH', '688111.SH', '002230.SZ', '002837.SZ',
    '002049.SZ', '688027.SH', '301269.SZ', '002747.SZ', '002896.SZ',
    '688568.SH', '300458.SZ', '688295.SH', '300857.SZ', '002714.SZ',
    '600111.SH', '002460.SZ', '603927.SH', '601899.SH', '688525.SH',
    '300308.SZ',
]

QLIB_DIR = Path('/home/ubuntu/stock/qlib/qlib_bin')
CAL_PATH = QLIB_DIR / 'calendars' / 'day.txt'
FEATURES_DIR = QLIB_DIR / 'features'


def tushare_to_qlib(ts_code):
    """600000.SH -> sh600000"""
    parts = ts_code.split('.')
    if len(parts) != 2:
        return None
    code, exchange = parts
    mapping = {'SH': 'sh', 'SZ': 'sz', 'BJ': 'bj'}
    prefix = mapping.get(exchange)
    if prefix is None:
        return None
    return prefix + code


def fetch_with_retry(pro, fn_name, max_retries=3, **kwargs):
    """带重试的 tushare 调用，避免瞬时限流。"""
    fn = getattr(pro, fn_name)
    for attempt in range(max_retries):
        try:
            df = fn(**kwargs)
            if df is not None and len(df) > 0:
                return df
        except Exception as e:
            print(f"  [{fn_name} {kwargs}] 失败 (attempt {attempt+1}): {e}")
            time.sleep(1.0)
    return None


def get_polluted_dates(calendar, start_date):
    """从参考股票 sh600000（上市最早、offset=0）定位污染日期。

    污染日期 = 日历中 sh600000 bin 为 NaN 的日期（>= start_date）。
    sh600000 自 2000 年上市以来无长期停牌，其 NaN 即全市场污染的可靠指标。
    返回格式: ['2026-06-02', ...]（日历格式）。
    """
    ref_sym = 'sh600000'
    ref_path = FEATURES_DIR / ref_sym / 'adjclose.day.bin'
    if not ref_path.exists():
        raise FileNotFoundError(f'参考股票缺失: {ref_path}')
    ref_bin = np.fromfile(str(ref_path), dtype=np.float32)
    ncal = len(calendar)
    offset = ncal - (len(ref_bin) - 1)
    dates = []
    for i in range(ncal):
        bin_idx = i - offset + 1  # calendar[i] ↔ bin[bin_idx]（bin[0] 为占位符）
        if 0 < bin_idx < len(ref_bin) and np.isnan(ref_bin[bin_idx]):
            d = calendar[i]
            if d >= start_date:
                dates.append(d)
    return dates


def load_stock_bins(sym_dir: Path) -> dict[str, np.ndarray]:
    """加载某股票的全部字段 bin。"""
    bins = {}
    for field in BIN_FIELDS:
        fpath = sym_dir / f'{field}.day.bin'
        if fpath.exists():
            bins[field] = np.fromfile(str(fpath), dtype=np.float32)
        else:
            bins[field] = None
    return bins


def compute_scale(bins: dict[str, np.ndarray], idx: int) -> float:
    """从 idx 之前最近的"有效"（非 NaN 且非 0）close/adjclose 计算 scale。

    scale = close / adjclose，与 incremental_update 逻辑一致。
    """
    adj = bins.get('adjclose')
    close = bins.get('close')
    if adj is None or close is None:
        return 1.0
    p = idx - 1
    while p >= 1:
        a, c = adj[p], close[p]
        if not np.isnan(a) and not np.isnan(c) and a != 0:
            return float(c) / float(a)
        p -= 1
    return 1.0


def backfill(args):
    calendar = CAL_PATH.read_text(encoding='utf-8').strip().split('\n')
    ncal = len(calendar)
    print(f"日历: {len(calendar)} 个交易日 ({calendar[0]} ~ {calendar[-1]})")

    polluted = get_polluted_dates(calendar, args.start)
    if not polluted:
        print('未发现污染日期，无需回填。')
        return 0
    print(f"污染日期: {len(polluted)} 个 ({polluted[0]} ~ {polluted[-1]})")
    for d in polluted:
        print(f"  {d}")

    # 确定回填范围
    if args.all:
        sym_dirs = sorted([d for d in FEATURES_DIR.iterdir() if d.is_dir()])
        print(f"回填范围: 全部股票 ({len(sym_dirs)} 只)")
    else:
        sym_dirs = []
        for code in POOL_TS_CODES:
            sym = tushare_to_qlib(code)
            d = FEATURES_DIR / sym
            if d.exists():
                sym_dirs.append(d)
            else:
                print(f"  [WARN] 池内股票 {code} ({sym}) 目录缺失，跳过")
        print(f"回填范围: 目标池 {len(sym_dirs)} 只")

    # 逐日拉取全市场数据（无论池内池外，pro.daily 一次返回全市场）
    token = os.environ.get('TUSHARE') or os.environ.get('TUSHARE_API_KEY')
    if not token:
        for env_path in [Path('/home/ubuntu/stock/TradingAgents/.env'),
                         Path('/home/ubuntu/stock/investment_data/.env')]:
            if env_path.exists():
                for line in env_path.read_text(encoding='utf-8').splitlines():
                    line = line.strip()
                    if line.startswith('TUSHARE_API_KEY='):
                        token = line.split('=', 1)[1].strip()
                        break
            if token:
                break
    if not token:
        print('ERROR: 未找到 TUSHARE token')
        return 1
    ts.set_token(token)
    pro = ts.pro_api()

    daily_by_date = {}
    adj_by_date = {}
    print('\n拉取污染日期数据...')
    for d in polluted:
        d_fmt = d.replace('-', '')
        daily = fetch_with_retry(pro, 'daily', trade_date=d_fmt)
        adj = fetch_with_retry(pro, 'adj_factor', trade_date=d_fmt)
        time.sleep(0.3)  # 限流保护
        if daily is None or adj is None:
            print(f"  [WARN] {d}: 拉取失败（daily={daily is not None}, adj={adj is not None}），跳过该日")
            continue
        daily_by_date[d] = daily
        adj_by_date[d] = adj
        print(f"  {d}: daily {len(daily)} 行, adj_factor {len(adj)} 行")

    if not daily_by_date:
        print('ERROR: 未拉取到任何日期数据')
        return 1

    print('\n回填 bin...')
    filled_total = 0
    nan_remaining = 0
    for sym_dir in sym_dirs:
        sym = sym_dir.name
        bins = load_stock_bins(sym_dir)
        if bins['adjclose'] is None:
            continue
        offset = ncal - (len(bins['adjclose']) - 1)

        filled_sym = 0
        for d in sorted(daily_by_date.keys()):
            daily = daily_by_date[d]
            adj_df = adj_by_date[d]
            cal_idx = calendar.index(d)
            idx = cal_idx - offset + 1  # bin 位置
            if idx < 1 or idx >= len(bins['adjclose']):
                continue

            # 该日该股已有有效值 → 不动（保护真实数据/停牌语义）
            if not np.isnan(bins['adjclose'][idx]):
                continue

            # 在当日全市场数据中查找该股（无行 = 停牌，保持 NaN）
            row = daily[daily['ts_code'].map(tushare_to_qlib) == sym]
            if len(row) == 0:
                continue  # 停牌 → 保持 NaN
            row = row.iloc[0]
            ts_code = row['ts_code']
            af_row = adj_df[adj_df['ts_code'] == ts_code]
            adj_f = float(af_row.iloc[0]['adj_factor']) if len(af_row) > 0 else 1.0

            scale = compute_scale(bins, idx)

            raw_close = float(row['close'])
            raw_open = float(row['open'])
            raw_high = float(row['high'])
            raw_low = float(row['low'])
            vol = float(row['vol'])
            amt = float(row['amount'])
            pct_chg = float(row['pct_chg']) if pd.notna(row['pct_chg']) else 0.0

            adj_close = raw_close * adj_f
            adj_open = raw_open * adj_f
            adj_high = raw_high * adj_f
            adj_low = raw_low * adj_f

            vals = {
                'adjclose': adj_close,
                'amount': amt,
                'change': pct_chg / 100.0 if pct_chg else 0.0,
                'close': adj_close * scale,
                'factor': adj_f,
                'high': adj_high * scale,
                'low': adj_low * scale,
                'open': adj_open * scale,
                'volume': vol / adj_f if adj_f != 0 else vol,
                'vwap': (amt / vol * 10) * adj_f * scale if vol > 0 else adj_close * scale,
            }

            if args.dry_run:
                filled_sym += 1
                continue

            for field in BIN_FIELDS:
                if bins[field] is not None:
                    bins[field][idx] = vals[field]
            filled_sym += 1

        if not args.dry_run:
            for field in BIN_FIELDS:
                if bins[field] is not None:
                    bins[field].tofile(str(sym_dir / f'{field}.day.bin'))
        filled_total += filled_sym
        nan_remaining_sym = 0
        for d in polluted:
            cal_idx = calendar.index(d)
            idx = cal_idx - offset + 1
            if 0 < idx < len(bins['adjclose']) and np.isnan(bins['adjclose'][idx]):
                nan_remaining_sym += 1
        nan_remaining += nan_remaining_sym
        if filled_sym > 0:
            print(f"  {sym}: 回填 {filled_sym} 个日期（剩余 NaN: {nan_remaining_sym}）")

    print(f"\n=== 回填完成 ===")
    print(f"  处理股票数: {len(sym_dirs)}")
    print(f"  回填格点数: {filled_total}（股票×日期）")
    if not args.dry_run:
        print(f"  残留 NaN（污染日期内）: {nan_remaining}")

    # 校验：逐字段检查残留 NaN
    print('\n--- 校验 ---')
    ok_all = True
    for sym_dir in sym_dirs:
        sym = sym_dir.name
        adj = np.fromfile(str(sym_dir / 'adjclose.day.bin'), dtype=np.float32)
        offset = ncal - (len(adj) - 1)
        bad = []
        for d in polluted:
            cal_idx = calendar.index(d)
            idx = cal_idx - offset + 1
            if 0 < idx < len(adj) and np.isnan(adj[idx]):
                bad.append(d)
        status = 'OK' if not bad else f'FAIL({len(bad)} NaN: {bad[:3]})'
        if bad:
            ok_all = False
        print(f"  {sym}: {status}")

    return 0 if ok_all else 2


def main():
    parser = argparse.ArgumentParser(description='回填 qlib_bin 的 NaN 空洞（tushare 定点回填）')
    parser.add_argument('--all', action='store_true', help='回填全部股票（默认仅目标池 26 只）')
    parser.add_argument('--pool-only', action='store_true', help='仅目标池（默认行为，显式声明）')
    parser.add_argument('--dry-run', action='store_true', help='只统计不写文件')
    parser.add_argument('--start', default='2026-06-01', help='起始日期（默认 2026-06-01）')
    args = parser.parse_args()
    if args.pool_only:
        args.all = False
    return backfill(args)


if __name__ == '__main__':
    sys.exit(main())
