"""
Sync incremental data from chenditc/investment_data (DoltHub) to local Dolt DB.
Generates SQL files and executes via `dolt sql --file` for bulk import speed.

Usage:
  python sync_from_dolthub.py                       # sync data only
  python sync_from_dolthub.py --push=True            # sync + push to remote
  python sync_from_dolthub.py --local_dolt_dir=D:/codes/stock/dolt_sync
"""
import json
import os
import subprocess
import tempfile
import time
import fire
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import quote

SOURCE_OWNER = "chenditc"
SOURCE_REPO = "investment_data"
INCREMENTAL_DATES = ["2026-05-11", "2026-05-12", "2026-05-13"]


def dolthub_query(sql, owner=SOURCE_OWNER, repo=SOURCE_REPO):
    """Query DoltHub API. Returns list of row dicts, or None on error."""
    url = f"https://www.dolthub.com/api/v1alpha1/{owner}/{repo}/master?q={quote(sql)}"
    req = Request(url)
    req.add_header("User-Agent", "investment_data_sync/1.0")
    try:
        with urlopen(req, timeout=120) as resp:
            raw = resp.read().decode()
        data = json.loads(raw)
        status = data.get("query_execution_status")
        if status not in ("Success", "RowLimit"):
            print(f"  API Error: {data.get('query_execution_message', '')}")
            return None
        return data.get("rows", [])
    except Exception as e:
        print(f"  Query error: {e}")
        return None


def dolt_sql_file(sql, dolt_dir, timeout=1800):
    """Execute SQL from a temp file via `dolt sql --file`. Handles large SQL efficiently."""
    fd, path = tempfile.mkstemp(suffix=".sql", prefix="dolt_import_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(sql)
        result = subprocess.run(
            ["dolt", "sql", "--file", path],
            cwd=dolt_dir, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            print(f"  dolt sql error: {result.stderr.strip()[:500]}")
            return False
        return True
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def dolt_cmd(args, dolt_dir, timeout=300):
    """Run a dolt CLI command."""
    return subprocess.run(
        ["dolt"] + args,
        cwd=dolt_dir, capture_output=True, text=True, timeout=timeout
    )


def escape_val(v):
    """Escape a value for SQL INSERT."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("'", "''")
    return f"'{s}'"


def generate_insert_sql(table, rows, batch_size=1000):
    """Generate INSERT IGNORE SQL statements for all rows, batched."""
    if not rows:
        return ""
    columns = list(rows[0].keys())
    col_list = ", ".join(f"`{c}`" for c in columns)
    statements = []
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        values = []
        for row in batch:
            vals = ", ".join(escape_val(row.get(c)) for c in columns)
            values.append(f"({vals})")
        statements.append(
            f"INSERT IGNORE INTO `{table}` ({col_list}) VALUES\n"
            + ",\n".join(values) + ";"
        )
    return "\n\n".join(statements)


def paginate_query(sql_template, date=None, max_offset=6000):
    """Paginate through DoltHub API results (1000 rows per page)."""
    all_rows = []
    for offset in range(0, max_offset, 1000):
        if date is not None:
            sql = sql_template.format(date=date, offset=offset)
        else:
            sql = sql_template.format(offset=offset)
        rows = dolthub_query(sql)
        if rows:
            all_rows.extend(rows)
        if not rows or len(rows) < 1000:
            break
        time.sleep(0.5)
    return all_rows


def sync_from_dolthub(local_dolt_dir="D:/codes/stock/dolt_sync", push=False):
    dolt_dir = str(Path(local_dolt_dir).resolve())
    print(f"Local Dolt: {dolt_dir}")
    total = 0

    # 1. ts_a_stock_eod_price (~5500 rows/day, 9 columns)
    print("\n=== ts_a_stock_eod_price ===")
    all_sql_parts = []
    for d in INCREMENTAL_DATES:
        rows = paginate_query(
            "SELECT * FROM ts_a_stock_eod_price WHERE tradedate='{date}' LIMIT 1000 OFFSET {offset}",
            date=d
        )
        if rows:
            sql = generate_insert_sql("ts_a_stock_eod_price", rows, batch_size=1000)
            all_sql_parts.append(sql)
            print(f"  {d}: {len(rows)} rows -> SQL generated")
            total += len(rows)

    if all_sql_parts:
        combined = "\n\n".join(all_sql_parts)
        print(f"  Executing SQL ({len(combined)} chars, timeout=1800s)...")
        ok = dolt_sql_file(combined, dolt_dir, timeout=1800)
        if not ok:
            total -= sum(r.count("INSERT") for r in all_sql_parts)
        print(f"  Result: {'OK' if ok else 'FAILED'}")

    # 2. final_a_stock_eod_price (~5500 rows/day, 9 columns)
    print("\n=== final_a_stock_eod_price ===")
    all_sql_parts = []
    for d in INCREMENTAL_DATES:
        rows = paginate_query(
            "SELECT * FROM final_a_stock_eod_price WHERE tradedate='{date}' LIMIT 1000 OFFSET {offset}",
            date=d
        )
        if rows:
            sql = generate_insert_sql("final_a_stock_eod_price", rows, batch_size=1000)
            all_sql_parts.append(sql)
            print(f"  {d}: {len(rows)} rows -> SQL generated")
            total += len(rows)

    if all_sql_parts:
        combined = "\n\n".join(all_sql_parts)
        print(f"  Executing SQL ({len(combined)} chars, timeout=1800s)...")
        ok = dolt_sql_file(combined, dolt_dir, timeout=1800)
        if not ok:
            total -= sum(r.count("INSERT") for r in all_sql_parts)
        print(f"  Result: {'OK' if ok else 'FAILED'}")

    # 3. ts_index_weight
    print("\n=== ts_index_weight ===")
    all_sql_parts = []
    for d in INCREMENTAL_DATES:
        rows = paginate_query(
            "SELECT * FROM ts_index_weight WHERE trade_date='{date}' LIMIT 1000 OFFSET {offset}",
            date=d, max_offset=10000
        )
        if rows:
            sql = generate_insert_sql("ts_index_weight", rows, batch_size=1000)
            all_sql_parts.append(sql)
            print(f"  {d}: {len(rows)} rows -> SQL generated")
            total += len(rows)
        else:
            print(f"  {d}: no data")

    if all_sql_parts:
        combined = "\n\n".join(all_sql_parts)
        print(f"  Executing SQL ({len(combined)} chars, timeout=1800s)...")
        ok = dolt_sql_file(combined, dolt_dir, timeout=1800)
        if not ok:
            total -= sum(r.count("INSERT") for r in all_sql_parts)
        print(f"  Result: {'OK' if ok else 'FAILED'}")

    # 4. ts_link_table (~6000 rows total)
    print("\n=== ts_link_table ===")
    rows = paginate_query(
        "SELECT * FROM ts_link_table LIMIT 1000 OFFSET {offset}",
        max_offset=10000
    )
    if rows:
        sql = generate_insert_sql("ts_link_table", rows, batch_size=1000)
        print(f"  {len(rows)} rows -> SQL generated, executing...")
        ok = dolt_sql_file(sql, dolt_dir, timeout=1800)
        if ok:
            total += len(rows)
        print(f"  Result: {'OK' if ok else 'FAILED'}")

    # 5. max_index_date (tiny, ~1 row)
    print("\n=== max_index_date ===")
    rows = dolthub_query("SELECT * FROM max_index_date")
    if rows:
        for r in rows:
            if "MIN(index_max_date)" in r:
                r["index_max_date"] = r.pop("MIN(index_max_date)")
        sql = "DELETE FROM max_index_date;\n" + generate_insert_sql("max_index_date", rows)
        ok = dolt_sql_file(sql, dolt_dir, timeout=300)
        if ok:
            total += len(rows)
        print(f"  {len(rows)} rows, result: {'OK' if ok else 'FAILED'}")

    # 6. Commit
    print(f"\n=== Commit ({total} total rows) ===")
    dolt_cmd(["add", "-A"], dolt_dir)
    status = dolt_cmd(["status"], dolt_dir)
    if "nothing to commit" in status.stdout:
        print("  No changes to commit")
    else:
        commit_msg = f"Sync from chenditc: {INCREMENTAL_DATES[0]}~{INCREMENTAL_DATES[-1]}"
        result = dolt_cmd(["commit", "-m", commit_msg], dolt_dir, timeout=600)
        if result.returncode == 0:
            print("  Committed")
        else:
            print(f"  Commit error: {result.stderr.strip()[:300]}")

    # 7. Push
    if push:
        print("\n=== Push to rickqi/investment_data ===")
        dolt_cmd(["remote", "remove", "origin"], dolt_dir)
        dolt_cmd(["remote", "add", "origin", "rickqi/investment_data"], dolt_dir)
        result = dolt_cmd(["push", "--force", "origin", "main"], dolt_dir, timeout=600)
        if result.returncode == 0:
            print("  Push OK")
        else:
            print(f"  Push FAILED: {result.stderr.strip()[:500]}")
    else:
        print("\n  Use --push=True to push to remote")

    return True


if __name__ == "__main__":
    fire.Fire(sync_from_dolthub)
