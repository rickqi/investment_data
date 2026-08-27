#!/usr/bin/env python3
"""
fix_scale_break_20260827.py — 修复 qlib_bin close/open/high/low/vwap 的 05-15/05-18 边界 scale 断裂。

背景（T1 发现 + T2 定案）:
    2026-05-08 全市场 adj_factor 再基准（0.649→16.5935）后，05-15 NaN 洞使 incremental_update
    的 scale 计算（close/adjclose）回退到 1.0，导致 05-18 起 close/open/high/low/vwap
    以 adjclose×1.0 写入（如 sh600000 close 5.85→150.67，×25.7），而 adjclose/factor 连续。
    修复：对每只股票，以 05-15 前最后一个有效 close/adjclose 比值 scale_pre 为基准，
    将 >= 2026-05-18 段的 close/open/high/low/vwap 乘以 scale_pre，恢复边界连续性
    （与 pre-break 历史 convention 一致；ratio 特征跨窗口变正确；最新预测窗口内均匀缩放不受影响）。

用法:
    python fix_scale_break_20260827.py --dry-run     # 只统计不写
    python fix_scale_break_20260827.py               # 写回 bin
    python fix_scale_break_20260827.py --verify      # 修复后校验（sh600000 + 抽样）

注意：一次性维护工具（T2 2026-08-27 入库，与 backfill_nan_dates.py 并列），写入前请确保已备份
    qlib_bin/features（参考 /tmp/qlib_bin_features_backup_pre_may_fix_20260827.tar.gz）。
    2026-08-27 已执行：6,091 只股票 05-18~08-26 段 scale 修复完成；后续增量更新从修复尾部自动取回旧 scale 惯例。
"""
import argparse
import sys
from pathlib import Path

import numpy as np

QLIB_DIR = Path('/home/ubuntu/stock/qlib/qlib_bin')
CAL_PATH = QLIB_DIR / 'calendars' / 'day.txt'
FEATURES_DIR = QLIB_DIR / 'features'
SCALE_FIELDS = ['close', 'open', 'high', 'low', 'vwap']
BOUNDARY = '2026-05-15'   # 断裂参考日（此前为旧 scale，此后为 1.0 错误 scale）
FIX_START = '2026-05-18'  # 修复段起点（05-15 后首个交易日）


def load_calendar():
    return CAL_PATH.read_text(encoding='utf-8').strip().split('\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--limit', type=int, default=0, help='只处理前 N 只（调试）')
    args = parser.parse_args()

    calendar = load_calendar()
    ncal = len(calendar)
    fix_start_idx = calendar.index(FIX_START)
    print(f"日历 {len(calendar)} 日；修复段起点 {FIX_START} (idx {fix_start_idx})")

    sym_dirs = sorted([d for d in FEATURES_DIR.iterdir() if d.is_dir()])
    if args.limit:
        sym_dirs = sym_dirs[:args.limit]
    print(f"股票数: {len(sym_dirs)}")

    fixed_count = 0
    skipped_no_pre = 0
    skipped_short = 0
    scale_sample = {}

    for i, sym_dir in enumerate(sym_dirs):
        sym = sym_dir.name
        close = np.fromfile(str(sym_dir / 'close.day.bin'), dtype=np.float32)
        adj = np.fromfile(str(sym_dir / 'adjclose.day.bin'), dtype=np.float32)
        if len(close) < 2:
            skipped_short += 1
            continue
        offset = ncal - (len(close) - 1)
        if offset > fix_start_idx:
            # bin 长度不足覆盖修复段（IPO 晚于 05-18）→ 整段内部一致，无需修
            skipped_short += 1
            continue

        # scale_pre = 05-15 前最后一个有效 close/adjclose 比值
        scale_pre = None
        for k in range(fix_start_idx - offset, 0, -1):
            a, c = adj[k], close[k]
            if not np.isnan(a) and not np.isnan(c) and a != 0:
                scale_pre = float(c) / float(a)
                break
        if scale_pre is None:
            skipped_no_pre += 1
            continue
        if not np.isfinite(scale_pre) or scale_pre <= 0:
            skipped_no_pre += 1
            continue

        if len(scale_sample) < 5:
            scale_sample[sym] = scale_pre

        # 修复段 bin 索引: calendar[fix_start_idx .. ] ↔ bin[fix_start_idx-offset+1 .. ]
        start_bin = fix_start_idx - offset + 1
        if start_bin < 1 or start_bin >= len(close):
            skipped_short += 1
            continue

        changed = False
        for field in SCALE_FIELDS:
            fpath = sym_dir / f'{field}.day.bin'
            if not fpath.exists():
                continue
            arr = np.fromfile(str(fpath), dtype=np.float32)
            if len(arr) != len(close):
                continue
            seg = arr[start_bin:]
            seg_clean = seg[~np.isnan(seg)]
            if seg_clean.size == 0:
                continue
            new_seg = seg * scale_pre
            if not args.dry_run:
                arr[start_bin:] = new_seg
                arr.tofile(str(fpath))
            changed = True
        if changed:
            fixed_count += 1
        if (i + 1) % 1000 == 0:
            print(f"  ... {i + 1}/{len(sym_dirs)}")

    print(f"\n=== 完成 ===")
    print(f"  修复股票: {fixed_count}")
    print(f"  跳过(无 pre-05-15 有效数据): {skipped_no_pre}")
    print(f"  跳过(段覆盖不足): {skipped_short}")
    print(f"  参考 scale 抽样: { {k: round(v, 6) for k, v in scale_sample.items()} }")
    return 0


if __name__ == '__main__':
    sys.exit(main())
