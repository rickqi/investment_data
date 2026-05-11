"""
增量更新 qlib_bin 数据：从 Tushare 获取最新日线数据，追加到本地 qlib_bin 文件。
用法: python incremental_update.py [--qlib_dir C:/codes/qlib/qlib_bin] [--tushare_token XXX]

流程:
1. 读取 calendar 最后日期
2. 从 Tushare 获取该日期之后所有交易日的日线 + 复权因子
3. 将数据追加到各股票的 .bin 文件
4. 更新 calendar

数据映射:
  - adjclose = close * adj_factor (Tushare 复权价)
  - open/high/low/close = 原始价 * adj_factor (复权后)
  - volume = vol (成交量，股)
  - amount = amount (成交额，千元→元 *1000)
  - vwap = amount / volume
  - change = pct_chg / 100
  - factor = adj_factor / qlib基准adj_factor (需要从现有bin推算)

  但注意 qlib normalize 后的值是归一化的，不是原始复权价。
  本脚本保持与原有 bin 相同的坐标系，直接使用 adjclose 风格的值。
"""
import numpy as np
import os
import sys
from pathlib import Path
import tushare as ts
import pandas as pd
import time
import fire
from datetime import datetime

# qlib bin 中存储的字段
BIN_FIELDS = ['adjclose', 'amount', 'change', 'close', 'factor', 'high', 'low', 'open', 'volume', 'vwap']


def incremental_update(qlib_dir=r'C:\codes\qlib\qlib_bin', tushare_token=None):
    if tushare_token is None:
        tushare_token = os.environ.get('TUSHARE')
    if not tushare_token:
        print("ERROR: 需要设置 TUSHARE 环境变量或传入 --tushare_token")
        return

    ts.set_token(tushare_token)
    pro = ts.pro_api()

    # 1. 读取现有 calendar
    cal_path = Path(qlib_dir) / 'calendars' / 'day.txt'
    calendar = cal_path.read_text().strip().split('\n')
    last_date = calendar[-1]
    print(f"当前数据截至: {last_date} ({len(calendar)} 个交易日)")

    # 2. 获取新的交易日
    start_date_fmt = last_date.replace('-', '')
    today_fmt = datetime.now().strftime('%Y%m%d')

    print(f"获取增量数据: {start_date_fmt} ~ {today_fmt}")

    # 3. 从 Tushare 获取日线数据（逐日获取，排除 last_date 本身）
    all_daily = []
    all_adj = []

    # 获取交易日列表 — 用 daily 接口逐日试探
    test_dates = pd.date_range(start=last_date, end=datetime.now().strftime('%Y-%m-%d'), freq='B')
    new_trade_dates = []
    for d in test_dates:
        ds = d.strftime('%Y%m%d')
        if ds <= start_date_fmt:
            continue
        try:
            df = pro.daily(trade_date=ds)
            time.sleep(0.2)
            if df is not None and len(df) > 0:
                new_trade_dates.append(ds)
                all_daily.append(df)
                print(f"  {ds}: {len(df)} 条记录")
        except Exception as e:
            print(f"  {ds}: 跳过 ({e})")
            time.sleep(1)

    if not new_trade_dates:
        print("没有新的交易日数据，无需更新。")
        return

    daily_df = pd.concat(all_daily, ignore_index=True)
    print(f"\n获取到 {len(daily_df)} 条日线数据，覆盖 {len(new_trade_dates)} 个交易日")

    # 4. 获取复权因子
    print("获取复权因子...")
    all_adj_dfs = []
    for d in new_trade_dates:
        try:
            af = pro.adj_factor(trade_date=d)
            time.sleep(0.2)
            if af is not None and len(af) > 0:
                all_adj_dfs.append(af)
        except Exception as e:
            print(f"  adj_factor {d}: {e}")
            time.sleep(1)

    adj_df = pd.concat(all_adj_dfs, ignore_index=True) if all_adj_dfs else pd.DataFrame()
    print(f"  复权因子: {len(adj_df)} 条")

    # 5. 构建股票代码映射: qlib symbol -> tushare ts_code
    # qlib: sh600000, sz000001, bj430017
    # tushare: 600000.SH, 000001.SZ, 430017.BJ
    features_dir = Path(qlib_dir) / 'features'
    existing_symbols = set(d.name for d in features_dir.iterdir() if d.is_dir())

    def qlib_to_tushare(sym):
        if sym.startswith('sh'):
            return sym[2:] + '.SH'
        elif sym.startswith('sz'):
            return sym[2:] + '.SZ'
        elif sym.startswith('bj'):
            return sym[2:] + '.BJ'
        return None

    def tushare_to_qlib(ts_code):
        code, exchange = ts_code.split('.')
        if exchange == 'SH':
            return 'sh' + code
        elif exchange == 'SZ':
            return 'sz' + code
        elif exchange == 'BJ':
            return 'bj' + code
        return None

    # 6. 为每只股票追加 bin 数据
    print("\n追加 bin 数据...")

    # 准备每日数据索引: date -> {ts_code: row}
    daily_df['trade_date_fmt'] = daily_df['trade_date']
    daily_by_date_symbol = {}
    for _, row in daily_df.iterrows():
        key = (row['trade_date'], row['ts_code'])
        daily_by_date_symbol[key] = row

    adj_by_symbol = {}
    if len(adj_df) > 0:
        for _, row in adj_df.iterrows():
            key = (row['trade_date'], row['ts_code'])
            adj_by_symbol[key] = row['adj_factor']

    updated_symbols = 0
    new_symbols_created = 0

    # 处理已在 daily 数据中出现的所有 ts_code
    all_ts_codes = set(daily_df['ts_code'].unique())
    # 也合并 adj_factor 中的 code
    if len(adj_df) > 0:
        all_ts_codes.update(adj_df['ts_code'].unique())

    for ts_code in sorted(all_ts_codes):
        qlib_sym = tushare_to_qlib(ts_code)
        if qlib_sym is None:
            continue

        sym_dir = features_dir / qlib_sym
        is_new = qlib_sym not in existing_symbols

        if is_new:
            # 新股票：创建目录和空的初始 bin 文件
            sym_dir.mkdir(parents=True, exist_ok=True)
            for field in BIN_FIELDS:
                np.array([0.0], dtype=np.float32).tofile(str(sym_dir / f'{field}.day.bin'))
            new_symbols_created += 1

        # 一次性读取所有现有 bin 数据
        existing_bins = {}
        for field in BIN_FIELDS:
            fpath = sym_dir / f'{field}.day.bin'
            if fpath.exists():
                existing_bins[field] = np.fromfile(str(fpath), dtype=np.float32)
            else:
                existing_bins[field] = np.array([0.0], dtype=np.float32)

        # 从已有数据推算归一化缩放比：scale = close[-1] / adjclose[-1]
        last_adjclose = float(existing_bins['adjclose'][-1])
        last_close = float(existing_bins['close'][-1])
        if last_adjclose != 0 and not np.isnan(last_adjclose):
            scale = last_close / last_adjclose
        else:
            scale = 1.0

        # 获取最后一个 factor 作为 fallback
        last_factor = float(existing_bins['factor'][-1])
        if last_factor == 0 or np.isnan(last_factor):
            last_factor = 1.0

        # 为每个新交易日生成数据
        new_values = {field: [] for field in BIN_FIELDS}

        for td in sorted(new_trade_dates):
            key = (td, ts_code)
            daily_row = daily_by_date_symbol.get(key)

            if daily_row is None:
                # 停牌：填充 NaN
                for field in BIN_FIELDS:
                    new_values[field].append(np.nan)
                continue

            adj_key = (td, ts_code)
            adj_f = adj_by_symbol.get(adj_key, last_factor)

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
            new_values['factor'].append(float(adj_f))
            new_values['close'].append(adj_close * scale)
            new_values['high'].append(adj_high * scale)
            new_values['low'].append(adj_low * scale)
            new_values['open'].append(adj_open * scale)
            # qlib normalize: bin_volume = tushare_vol / bin_factor
            new_values['volume'].append(vol / float(adj_f) if adj_f != 0 else vol)

            # vwap = raw_vwap * adj_f * scale, raw_vwap = amt/vol*10 (千元/手→元/股)
            if vol > 0:
                raw_vwap = amt / vol * 10
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

    # 7. 更新 calendar
    new_cal_entries = [d[:4] + '-' + d[4:6] + '-' + d[6:8] for d in sorted(new_trade_dates)]
    updated_calendar = calendar + new_cal_entries
    cal_path.write_text('\n'.join(updated_calendar) + '\n')
    print(f"\nCalendar 更新: {calendar[-1]} -> {updated_calendar[-1]} ({len(updated_calendar)} 天)")

    # 8. 更新 instruments/all.txt (添加新股票)
    all_txt_path = Path(qlib_dir) / 'instruments' / 'all.txt'
    if all_txt_path.exists():
        existing_instruments = set(all_txt_path.read_text().strip().split('\n'))
        # 需要格式：sh600000 这样的
        for ts_code in all_ts_codes:
            qlib_sym = tushare_to_qlib(ts_code)
            if qlib_sym and qlib_sym not in existing_instruments:
                existing_instruments.add(qlib_sym)
        all_txt_path.write_text('\n'.join(sorted(existing_instruments)) + '\n')

    print(f"\n=== 更新完成 ===")
    print(f"  新增交易日: {len(new_trade_dates)}")
    print(f"  更新股票数: {updated_symbols}")
    print(f"  新建股票数: {new_symbols_created}")
    print(f"  Calendar: {updated_calendar[0]} ~ {updated_calendar[-1]}")


if __name__ == '__main__':
    fire.Fire(incremental_update)
