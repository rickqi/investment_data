"""
从本地 qlib_bin 目录读取 .bin 文件，导入到 Dolt MySQL 数据库。
用法: python import_qlib_bin_to_dolt.py [--qlib_dir C:/codes/qlib/qlib_bin] [--batch_size 10000]
"""
import numpy as np
import os
import sys
from sqlalchemy import create_engine, text
from pathlib import Path
import fire
import time

FINAL_FIELDS = ['open', 'high', 'low', 'close', 'volume', 'adjclose', 'amount']


def import_qlib_bin(qlib_dir=r'C:\codes\qlib\qlib_bin', batch_size=10000):
    engine = create_engine('mysql+pymysql://root:@127.0.0.1/investment_data', pool_recycle=3600)

    # 1. 读取日历
    cal_path = Path(qlib_dir) / 'calendars' / 'day.txt'
    with open(cal_path, 'r') as f:
        calendar = [line.strip() for line in f if line.strip()]
    print(f"Calendar: {calendar[0]} ~ {calendar[-1]} ({len(calendar)} days)")

    # 2. 导入日历
    print("Importing calendar...")
    with engine.connect() as conn:
        for d in calendar:
            conn.execute(text(
                "INSERT INTO ts_trade_day_calendar (date, exchange, is_open) VALUES (:d, 'SSE', 1)"
            ), {'d': d})
        conn.commit()
    print(f"  Imported {len(calendar)} calendar days")

    # 3. 遍历 features 目录
    features_dir = Path(qlib_dir) / 'features'
    symbols = sorted([d.name for d in features_dir.iterdir() if d.is_dir()])
    print(f"Total symbols: {len(symbols)}")

    # 4. 批量导入 — 使用 raw SQL INSERT
    total_rows = 0
    start_time = time.time()
    batch_values = []

    insert_sql = text(
        "INSERT INTO final_a_stock_eod_price "
        "(tradedate, symbol, open, high, low, close, volume, adjclose, amount) "
        "VALUES (:tradedate, :symbol, :open, :high, :low, :close, :volume, :adjclose, :amount)"
    )

    with engine.connect() as conn:
        for i, symbol in enumerate(symbols):
            sym_dir = features_dir / symbol
            adjclose_path = sym_dir / 'adjclose.day.bin'
            if not adjclose_path.exists():
                continue

            adjclose = np.fromfile(str(adjclose_path), dtype='<f')

            # 读取各字段
            fields = {}
            for field in FINAL_FIELDS:
                fpath = sym_dir / f'{field}.day.bin'
                if fpath.exists():
                    fields[field] = np.fromfile(str(fpath), dtype='<f')
                else:
                    fields[field] = np.zeros(len(adjclose))

            # 转换 symbol: sh600000 -> 600000.SH
            if symbol.startswith('sh'):
                ts_symbol = symbol[2:] + '.SH'
            elif symbol.startswith('sz'):
                ts_symbol = symbol[2:] + '.SZ'
            elif symbol.startswith('bj'):
                ts_symbol = symbol[2:] + '.BJ'
            else:
                ts_symbol = symbol

            # 跳过第一天（通常为0）和 NaN
            for j in range(1, len(adjclose)):
                if j >= len(calendar):
                    break
                val = adjclose[j]
                if np.isnan(val) or val == 0:
                    continue

                def safe(arr, idx):
                    v = float(arr[idx])
                    return v if not np.isnan(v) else None

                batch_values.append({
                    'tradedate': calendar[j],
                    'symbol': ts_symbol,
                    'open': safe(fields['open'], j),
                    'high': safe(fields['high'], j),
                    'low': safe(fields['low'], j),
                    'close': safe(fields['close'], j),
                    'volume': safe(fields['volume'], j),
                    'adjclose': float(adjclose[j]),
                    'amount': safe(fields['amount'], j),
                })

            # 批量写入
            if len(batch_values) >= batch_size:
                conn.execute(insert_sql, batch_values)
                conn.commit()
                total_rows += len(batch_values)
                elapsed = time.time() - start_time
                speed = total_rows / elapsed if elapsed > 0 else 0
                print(f"  [{i+1}/{len(symbols)}] {symbol} -> {total_rows:,} rows ({speed:.0f} rows/s)")
                batch_values = []

        # 写入剩余
        if batch_values:
            conn.execute(insert_sql, batch_values)
            conn.commit()
            total_rows += len(batch_values)

    elapsed = time.time() - start_time
    print(f"\nDone! Total: {total_rows:,} rows in {elapsed:.1f}s ({total_rows/elapsed:.0f} rows/s)")

    # 验证
    with engine.connect() as conn:
        r = conn.execute(text("SELECT COUNT(*) FROM final_a_stock_eod_price"))
        count = r.scalar()
        r2 = conn.execute(text("SELECT COUNT(DISTINCT symbol) FROM final_a_stock_eod_price"))
        sym_count = r2.scalar()
        r3 = conn.execute(text("SELECT MAX(tradedate) FROM final_a_stock_eod_price"))
        max_date = r3.scalar()
        print(f"Verify: {count:,} rows, {sym_count} symbols, max_date={max_date}")

    engine.dispose()


if __name__ == '__main__':
    fire.Fire(import_qlib_bin)
