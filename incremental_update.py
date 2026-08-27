"""
增量更新 qlib_bin 数据：从 Tushare 获取最新日线数据，追加到本地 qlib_bin 文件。
用法: python incremental_update.py [--qlib_dir C:/codes/qlib/qlib_bin] [--tushare_token XXX]

流程:
1. 读取 calendar 最后日期
2. 从 Tushare trade_cal 获取精确交易日列表
3. 获取增量日线 + 复权因子
4. 检测除权除息，重新校准归一化系数
5. 将数据追加到各股票的 .bin 文件（幂等）
6. 更新 calendar + instruments

数据映射（从 qlib normalize 逆向推导）:
  - adjclose = raw_close * tushare_adj_factor
  - close/open/high/low = adjclose * scale（scale 保持归一化连续性）
  - volume = tushare_vol / adj_factor（qlib normalize 缩放）
  - amount = tushare_amount（直接使用）
  - vwap = (amt/vol*10) * adj_factor * scale
  - change = pct_chg / 100
  - factor = tushare_adj_factor
"""
import numpy as np
import os
import sys
from pathlib import Path
import tushare as ts
import pandas as pd
import time
import fire
from datetime import datetime, timedelta

BIN_FIELDS = ['adjclose', 'amount', 'change', 'close', 'factor', 'high', 'low', 'open', 'volume', 'vwap']

# bin 数组比 calendar 多 1（index 0 是占位符 0.0）
BIN_OFFSET = 1


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


def qlib_to_tushare(sym):
    """sh600000 -> 600000.SH"""
    mapping = {'sh': '.SH', 'sz': '.SZ', 'bj': '.BJ'}
    for prefix, suffix in mapping.items():
        if sym.startswith(prefix):
            return sym[len(prefix):] + suffix
    return None


def _compute_scale(existing_bins):
    """从已有 bin 计算归一化 scale（close/adjclose）—— R6 加固（2026-08-28）。

    原实现只取最后一个条目（incremental_update 旧 L250-258）：
      - bin 末尾为 NaN 洞（停牌占位 / 拉取失败，2026-05-15 事故根因）时，
        last_adjclose=NaN → scale 回退 1.0，close=adjclose×1.0 归一化断裂；
      - last_close=NaN（adjclose 有效）时甚至产生 NaN scale，污染后续全部新数据。
    现回溯至最近一个有效 (close, adjclose) 对；仅全 0 占位（新股票）保持 1.0。

    返回 (scale, 跳过的无效条目数, 跳过区是否含 NaN)——调用方据此告警。
    正常路径（末尾条目有效）行为与旧实现逐位一致。
    repair-round-2（reviewer t10 边界缺陷）: 全 NaN / 全无效 bin 的 fallback 不再静默——
    has_nan 以整数组 NaN 存在性判定（原返回 False 使调用方告警条件不触发，
    静默回退 1.0 与 05-15 事故同形），全 NaN 时告警并保持 scale=1.0（不 fail，避免
    停牌股阻断流水线；但会显式告警供人工核查）。
    """
    close_arr = existing_bins['close']
    adj_arr = existing_bins['adjclose']
    for i in range(len(close_arr) - 1, -1, -1):
        adj_c = float(adj_arr[i])
        clo_c = float(close_arr[i])
        if adj_c != 0 and not np.isnan(adj_c) and not np.isnan(clo_c):
            tail_c = close_arr[i + 1:]
            tail_a = adj_arr[i + 1:]
            tail_has_nan = bool(
                (np.isnan(tail_c).any() if len(tail_c) else False)
                or (np.isnan(tail_a).any() if len(tail_a) else False)
            )
            return clo_c / adj_c, len(tail_c), tail_has_nan
        # 无效条目（NaN 或 0 占位）继续回溯
    # 全数组无有效对（全 NaN / 全 0 / 混合无效）: scale 回退 1.0，has_nan 判 NaN 存在性
    has_nan = bool(np.isnan(close_arr).any() or np.isnan(adj_arr).any())
    return 1.0, len(close_arr), has_nan


def get_trade_calendar(pro, start_date, end_date):
    """获取 SSE 交易日历（精确，不浪费 API 调用）"""
    for attempt in range(3):
        try:
            df = pro.trade_cal(exchange='SSE', is_open='1',
                               start_date=start_date, end_date=end_date,
                               fields='cal_date')
            if df is not None and len(df) > 0:
                return sorted(df['cal_date'].tolist())
        except Exception as e:
            print(f"  trade_cal 获取失败 (attempt {attempt+1}): {e}")
            time.sleep(1)
    return None


def fetch_daily_with_retry(pro, trade_date, max_retries=3):
    """带重试的日线数据获取"""
    for attempt in range(max_retries):
        try:
            df = pro.daily(trade_date=trade_date)
            return df
        except Exception as e:
            print(f"  daily {trade_date} 获取失败 (attempt {attempt+1}): {e}")
            time.sleep(1)
    return None


def fetch_adj_factor_with_retry(pro, trade_date, max_retries=3):
    """带重试的复权因子获取"""
    for attempt in range(max_retries):
        try:
            df = pro.adj_factor(trade_date=trade_date)
            return df
        except Exception as e:
            print(f"  adj_factor {trade_date} 获取失败 (attempt {attempt+1}): {e}")
            time.sleep(1)
    return None


def incremental_update(qlib_dir=r'C:\codes\qlib\qlib_bin', tushare_token=None):
    if tushare_token is None:
        tushare_token = os.environ.get('TUSHARE')
    if not tushare_token:
        print("ERROR: 需要设置 TUSHARE 环境变量或传入 --tushare_token")
        return False

    ts.set_token(tushare_token)
    pro = ts.pro_api()

    qlib_path = Path(qlib_dir)
    cal_path = qlib_path / 'calendars' / 'day.txt'
    features_dir = qlib_path / 'features'

    # ========== 1. 读取现有 calendar ==========
    calendar = cal_path.read_text(encoding='utf-8').strip().split('\n')
    last_date = calendar[-1]
    expected_bin_len = len(calendar) + BIN_OFFSET
    print(f"当前数据截至: {last_date} ({len(calendar)} 个交易日, bin 长度应为 {expected_bin_len})")

    # ========== 2. 幂等性检查 ==========
    # 检查 bin 文件是否已被追加过（避免重复运行产生重复数据）
    sample_sym_dir = features_dir / 'sh600000'
    if sample_sym_dir.exists():
        sample_bin = np.fromfile(str(sample_sym_dir / 'adjclose.day.bin'), dtype=np.float32)
        if len(sample_bin) > expected_bin_len:
            excess = len(sample_bin) - expected_bin_len
            print(f"WARNING: bin 文件比 calendar 多 {excess} 条记录，可能上次更新不完整")
            print(f"  建议：从备份恢复后重新运行，或手动修复 calendar")
            return False
        elif len(sample_bin) == expected_bin_len:
            # bin 和 calendar 对齐，说明之前已成功更新或无需更新
            pass
        elif len(sample_bin) < expected_bin_len:
            print(f"WARNING: bin 文件比 calendar 少 {expected_bin_len - len(sample_bin)} 条")
            print(f"  calendar 可能被手动修改过，建议检查数据一致性")
            return False

    # ========== 3. 获取精确交易日列表 ==========
    # 查询到今天：Tushare 收盘后会更新当天数据
    today_fmt = datetime.now().strftime('%Y%m%d')
    start_date_fmt = last_date.replace('-', '')
    print(f"查询交易日历: {start_date_fmt} ~ {today_fmt}")

    trade_dates = get_trade_calendar(pro, start_date_fmt, today_fmt)
    if trade_dates is None:
        print("ERROR: 无法获取交易日历，尝试 fallback 模式...")
        trade_dates = []
        d = datetime.strptime(last_date, '%Y-%m-%d') + timedelta(days=1)
        end = datetime.now()
        while d <= end:
            ds = d.strftime('%Y%m%d')
            df = fetch_daily_with_retry(pro, ds)
            time.sleep(0.2)
            if df is not None and len(df) > 0:
                trade_dates.append(ds)
                print(f"  {ds}: {len(df)} 条记录 (fallback)")
            d += timedelta(days=1)

    # 排除已有日期；且只保留严格早于今天的日期（01:00 运行时"今天"尚无数据，
    # 若把今天写入日历会以 NaN 占位且永不补拉 —— 数据污染根因，2026-08-27 修复）
    new_trade_dates = [d for d in trade_dates if start_date_fmt < d < today_fmt]
    if not new_trade_dates:
        print("没有新的交易日，无需更新。")
        return True

    print(f"新增交易日: {len(new_trade_dates)} 个 ({new_trade_dates[0]} ~ {new_trade_dates[-1]})")

    # ========== 4. 获取增量日线数据 ==========
    print("获取增量日线数据...")
    all_daily = []
    fetched_dates = []  # 实际获取到数据的日期（仅这些日期写入 bin 并加入日历）
    for d in new_trade_dates:
        df = fetch_daily_with_retry(pro, d)
        time.sleep(0.2)
        if df is not None and len(df) > 0:
            all_daily.append(df)
            fetched_dates.append(d)
            print(f"  {d}: {len(df)} 条记录")
        else:
            print(f"  {d}: 无数据（可能为未来交易日）")

    if not all_daily:
        # new_trade_dates 已过滤为过去日期，此处仅报告失败（日历保持不变，下轮重试）
        print(f"未获取到任何数据（{len(new_trade_dates)} 个过去交易日均无数据）。")
        return False

    daily_df = pd.concat(all_daily, ignore_index=True)
    print(f"共获取 {len(daily_df)} 条日线数据")

    # ========== 5. 获取复权因子 ==========
    print("获取复权因子...")
    all_adj_dfs = []
    for d in fetched_dates:
        af = fetch_adj_factor_with_retry(pro, d)
        time.sleep(0.2)
        if af is not None and len(af) > 0:
            all_adj_dfs.append(af)

    adj_df = pd.concat(all_adj_dfs, ignore_index=True) if all_adj_dfs else pd.DataFrame()
    print(f"  复权因子: {len(adj_df)} 条")

    # ========== 6. 构建数据索引 ==========
    daily_by_key = {}
    for _, row in daily_df.iterrows():
        daily_by_key[(row['trade_date'], row['ts_code'])] = row

    adj_by_key = {}
    if len(adj_df) > 0:
        for _, row in adj_df.iterrows():
            adj_by_key[(row['trade_date'], row['ts_code'])] = row['adj_factor']

    existing_symbols = set(d.name for d in features_dir.iterdir() if d.is_dir())
    all_ts_codes = set(daily_df['ts_code'].unique())
    if len(adj_df) > 0:
        all_ts_codes.update(adj_df['ts_code'].unique())

    # ========== 7. 检测除权除息并追加 bin 数据 ==========
    print("\n追加 bin 数据...")
    updated_symbols = 0
    new_symbols_created = 0
    split_adjusted_count = 0

    for ts_code in sorted(all_ts_codes):
        qlib_sym = tushare_to_qlib(ts_code)
        if qlib_sym is None:
            continue

        sym_dir = features_dir / qlib_sym
        is_new = qlib_sym not in existing_symbols

        if is_new:
            sym_dir.mkdir(parents=True, exist_ok=True)
            for field in BIN_FIELDS:
                np.array([0.0], dtype=np.float32).tofile(str(sym_dir / f'{field}.day.bin'))
            new_symbols_created += 1

        # 读取现有 bin 数据
        existing_bins = {}
        for field in BIN_FIELDS:
            fpath = sym_dir / f'{field}.day.bin'
            if fpath.exists():
                existing_bins[field] = np.fromfile(str(fpath), dtype=np.float32)
            else:
                existing_bins[field] = np.array([0.0], dtype=np.float32)

        # 幂等性：截断到 expected_bin_len，丢弃上次可能的部分追加
        for field in BIN_FIELDS:
            if len(existing_bins[field]) > expected_bin_len:
                existing_bins[field] = existing_bins[field][:expected_bin_len]

        # 计算归一化 scale（R6 加固: 回溯最近有效 (close, adjclose) 对，
        # 防 NaN 洞导致 scale 回退 1.0 归一化断裂 / NaN 传播——2026-05 事故根因）
        scale, scale_skipped, tail_has_nan = _compute_scale(existing_bins)
        if scale_skipped and tail_has_nan:
            if scale_skipped >= len(existing_bins["close"]):
                # 全数组无有效 (close, adjclose) 对（全 NaN/全无效）——05-15 事故同形，显式告警
                print(f"  [WARN][R6] {qlib_sym}: bin 全部 {scale_skipped} 条为 NaN/无效条目，"
                      f"scale 回退 1.0（与 05-15 事故同形，需人工核查）")
            else:
                print(f"  [WARN][R6] {qlib_sym}: bin 末尾跳过 {scale_skipped} 条 NaN/无效条目，"
                      f"scale 回溯自最近有效对 = {scale}")
        last_factor = float(existing_bins['factor'][-1])
        if last_factor == 0 or np.isnan(last_factor):
            last_factor = 1.0

        # 检测除权除息：比较第一个新交易日的 adj_factor 与 last_factor
        first_td = fetched_dates[0]
        first_adj_f = adj_by_key.get((first_td, ts_code))
        if first_adj_f is not None and not np.isnan(first_adj_f):
            if abs(float(first_adj_f) - last_factor) > 0.001:
                # adj_factor 变化了 = 除权除息发生
                # 重新校准 scale 使 adjclose 保持连续
                # 新的 scale = close[-1] / (raw_close_last * new_adj_factor)
                # 但我们没有 raw_close_last，只有 adjclose[-1] = raw_close * old_adj_factor
                # raw_close_last = adjclose[-1] / last_factor
                # 新 adjclose 应该 = raw_close_last * new_adj_factor
                # 但 scale 需要保持 close 连续
                # close = adjclose * scale
                # 新 close = (raw_close * new_adj_f) * new_scale
                # 要求 new_close 与 old close 保持连续
                # 由于 raw_close 不变（同一天收盘价），只需 scale 不变即可
                # 实际上 adj_factor 变化后，复权价会跳变，这是正常的
                # scale 不需要改变——它只影响 close/open/high/low 的归一化
                split_adjusted_count += 1

        # 为每个新交易日生成数据
        new_values = {field: [] for field in BIN_FIELDS}

        for td in sorted(fetched_dates):
            key = (td, ts_code)
            daily_row = daily_by_key.get(key)

            if daily_row is None:
                # 停牌：填充 NaN
                for field in BIN_FIELDS:
                    new_values[field].append(np.nan)
                continue

            adj_f = adj_by_key.get(key, last_factor)
            if adj_f is None or (isinstance(adj_f, float) and np.isnan(adj_f)):
                adj_f = last_factor
            adj_f = float(adj_f)

            raw_close = float(daily_row['close'])
            raw_open = float(daily_row['open'])
            raw_high = float(daily_row['high'])
            raw_low = float(daily_row['low'])
            vol = float(daily_row['vol'])
            amt = float(daily_row['amount'])
            pct_chg = float(daily_row['pct_chg']) if pd.notna(daily_row['pct_chg']) else 0.0

            adj_close = raw_close * adj_f
            adj_open = raw_open * adj_f
            adj_high = raw_high * adj_f
            adj_low = raw_low * adj_f

            new_values['adjclose'].append(adj_close)
            new_values['amount'].append(amt)
            new_values['change'].append(pct_chg / 100.0 if pct_chg else 0.0)
            new_values['factor'].append(adj_f)
            new_values['close'].append(adj_close * scale)
            new_values['high'].append(adj_high * scale)
            new_values['low'].append(adj_low * scale)
            new_values['open'].append(adj_open * scale)
            new_values['volume'].append(vol / adj_f if adj_f != 0 else vol)

            if vol > 0:
                raw_vwap = amt / vol * 10  # 千元/手 → 元/股
                new_values['vwap'].append(raw_vwap * adj_f * scale)
            else:
                new_values['vwap'].append(adj_close * scale)

        # 追加到 bin 文件
        for field in BIN_FIELDS:
            arr = np.array(new_values[field], dtype=np.float32)
            combined = np.concatenate([existing_bins[field], arr])
            combined.tofile(str(sym_dir / f'{field}.day.bin'))

        updated_symbols += 1
        if updated_symbols % 500 == 0:
            print(f"  已更新 {updated_symbols} 只股票...")

    # ========== 8. 更新 calendar ==========
    new_cal_entries = [d[:4] + '-' + d[4:6] + '-' + d[6:8] for d in sorted(fetched_dates)]
    updated_calendar = calendar + new_cal_entries
    cal_path.write_text('\n'.join(updated_calendar) + '\n', encoding='utf-8')
    print(f"\nCalendar 更新: {calendar[-1]} -> {updated_calendar[-1]} ({len(updated_calendar)} 天)")

    # ========== 9. 更新 instruments ==========
    instruments_dir = qlib_path / 'instruments'

    # all.txt：添加新股票（t8 协调: 新成员以 3 列 "<sym> <首交易日> 2099-12-31" 追加，
    # 取代旧裸代码追加——统一格式且保证长期有效不截断）
    all_txt_path = instruments_dir / 'all.txt'
    if all_txt_path.exists():
        lines = all_txt_path.read_text(encoding='utf-8').strip().split('\n')
        known = {ln.split()[0] for ln in lines if ln.split()}
        additions = []
        for ts_code in all_ts_codes:
            qlib_sym = tushare_to_qlib(ts_code)
            if qlib_sym and qlib_sym.upper() not in known and qlib_sym.lower() not in known:
                start_d = fetched_dates[0][:4] + '-' + fetched_dates[0][4:6] + '-' + fetched_dates[0][6:8]
                additions.append(f"{qlib_sym.upper()}\t{start_d}\t2099-12-31")
        if additions:
            all_txt_path.write_text('\n'.join(sorted(lines + additions)) + '\n', encoding='utf-8')

    # 指数成分文件：从 Tushare 获取最新成分并更新（t8 协调: 合并写入 3 列格式，
    # 保留历史区间——原实现整文件替换为裸代码会丢失历史并造成格式不一致）
    index_mapping = {
        'csi300.txt': '399300.SZ',
        'csi500.txt': '000905.SH',
        'csi800.txt': '000906.SH',
        'csi1000.txt': '000852.SH',
        'csiall.txt': '000985.SH',
    }
    for filename, index_code in index_mapping.items():
        filepath = instruments_dir / filename
        try:
            # 获取指数最新成分
            df = pro.index_weight(index_code=index_code, start_date=fetched_dates[-1],
                                   end_date=fetched_dates[-1])
            time.sleep(0.2)
            if df is not None and len(df) > 0:
                # 转换为 qlib 格式（大写 SH/SZ/BJ 前缀，与指数文件约定一致）
                constituents = set()
                for _, row in df.iterrows():
                    qlib_sym = tushare_to_qlib(row['con_code'])
                    if qlib_sym:
                        constituents.add(qlib_sym.upper())

                if filepath.exists():
                    # 合并: 保留历史行；当前成分 end→2099-12-31；新成分追加 "<sym> <快照日> 2099-12-31"
                    rows = []
                    for ln in filepath.read_text(encoding='utf-8').strip().split('\n'):
                        p = ln.split()
                        if len(p) == 3:
                            rows.append([p[0], p[1], p[2]])
                        elif len(p) == 1:
                            rows.append([p[0], '2000-01-01', '2099-12-31'])
                    max_end = max(r[2] for r in rows) if rows else ''
                    max_set = {r[0] for r in rows if r[2] == max_end}
                    cur = {}
                    for r in rows:
                        cur.setdefault(r[0], []).append(r)
                    snap_d = fetched_dates[-1][:4] + '-' + fetched_dates[-1][4:6] + '-' + fetched_dates[-1][6:8]
                    for sym in sorted(constituents):
                        if sym in max_set:
                            last = max(cur[sym], key=lambda x: x[1])
                            if last[2] != '2099-12-31':
                                last[2] = '2099-12-31'
                        else:
                            rows.append([sym, snap_d, '2099-12-31'])
                    rows.sort(key=lambda r: (r[0], r[1]))
                    filepath.write_text('\n'.join('\t'.join(r) for r in rows) + '\n', encoding='utf-8')
                else:
                    filepath.write_text('\n'.join(sorted(constituents)) + '\n', encoding='utf-8')
                print(f"  更新 {filename}: {len(constituents)} 只成分股（t8 合并写入 3 列格式）")
        except Exception as e:
            # 指数成分更新失败不影响主流程
            print(f"  {filename} 更新跳过: {e}")

    # ========== 10. 验证 ==========
    print("\n--- 验证 ---")
    verify_sample = features_dir / 'sh600000'
    if verify_sample.exists():
        v_adj = np.fromfile(str(verify_sample / 'adjclose.day.bin'), dtype=np.float32)
        v_cal = cal_path.read_text(encoding='utf-8').strip().split('\n')
        expected_len = len(v_cal) + BIN_OFFSET
        if len(v_adj) == expected_len:
            print(f"  sh600000: bin len {len(v_adj)} = calendar {len(v_cal)} + {BIN_OFFSET} OK")
        else:
            print(f"  sh600000: bin len {len(v_adj)} != expected {expected_len} FAIL")

    print(f"\n=== 更新完成 ===")
    print(f"  新增交易日: {len(fetched_dates)}")
    print(f"  更新股票数: {updated_symbols}")
    print(f"  新建股票数: {new_symbols_created}")
    print(f"  除权除息股票: {split_adjusted_count}")
    print(f"  Calendar: {updated_calendar[0]} ~ {updated_calendar[-1]}")

    return True


if __name__ == '__main__':
    fire.Fire(incremental_update)
