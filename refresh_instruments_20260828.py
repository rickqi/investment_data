"""t8 v2: qlib_bin instruments 指数文件刷新 — 修复评估截断（2026-08-28）。

实证（v1 审计修正）:
  - all.txt: 当前成员以**裸代码**存在（5,570 行，incremental_update 历次追加），
    qlib 视为长期有效（D.list_instruments @ 08-26 = 5570）→ **无需改动**。
  - 指数文件 csi300/500/800/1000: 仅 3 列历史行，快照 end=2026-05-12，
    D.list_instruments @ 08-26 = **0 成员** → 截断根因，需刷新。
  - csiall: Tushare 无可用快照 → 回退：现有 max-end 成员集 end 扩展 2099-12-31。

方案（v2）:
  - 指数文件: Tushare index_weight 最新可用快照（2026-07-31 实测可用）——
      成分 ∈ 文件 max-end 集 → 该行 end→2099-12-31；
      成分 ∉ 文件 → 追加 "<sym> <快照日> 2099-12-31"；
      被剔除成分 → 保持原 end（调仓后退出）。
    快照不可用 → 回退：max-end 成员集 end→2099-12-31（保守假设成员延续，注明）。
  - 符号转换与 incremental_update.tushare_to_qlib 一致（SH/SZ/BJ 前缀）。
  写入前备份已由调用方完成（/tmp/instruments_backup_20260828）。
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from pathlib import Path

import tushare as ts

INSTR = Path("/home/ubuntu/stock/qlib/qlib_bin/instruments")
NEVER = "2099-12-31"
INDEX_MAP = {
    "csi300.txt": "399300.SZ",
    "csi500.txt": "000905.SH",
    "csi800.txt": "000906.SH",
    "csi1000.txt": "000852.SH",
    "csiall.txt": "000985.SH",
}
CANDIDATES = ["20260826", "20260820", "20260814", "20260807", "20260731", "20260630"]


def _token() -> str:
    env = Path("/home/ubuntu/stock/TradingAgents/.env").read_text(encoding="utf-8")
    m = re.search(r"(?:TUSHARE|tushare)[A-Z_]*\s*=\s*[\"']?([A-Za-z0-9]+)", env)
    return m.group(1) if m else ""


def _to_qlib(code: str) -> str | None:
    """600000.SH → SH600000（指数文件为大写约定，实测 csi300/500/800/1000/all 均 SH/SZ/BJ 前缀）。"""
    parts = code.split(".")
    if len(parts) != 2:
        return None
    exchange = parts[1].upper()
    if exchange not in ("SH", "SZ", "BJ"):
        return None
    return exchange + parts[0]


def _parse(text: str) -> list:
    rows = []
    for ln in text.splitlines():
        p = ln.split()
        if len(p) == 3:
            rows.append([p[0], p[1], p[2]])
        elif len(p) == 1:
            rows.append([p[0], "2000-01-01", NEVER])  # 裸代码 = 长期成员
    return rows


def _max_end(rows: list) -> str:
    return max((r[2] for r in rows), default="")


def refresh_index(pro, fname: str, idx_code: str) -> dict:
    f = INSTR / fname
    rows = _parse(f.read_text(encoding="utf-8"))
    before = len(rows)
    max_end = _max_end(rows)
    max_set = {r[0] for r in rows if r[2] == max_end}
    cur = defaultdict(list)
    for r in rows:
        cur[r[0]].append(r)

    # 取最新可用快照
    df, snap_date = None, None
    for cand in CANDIDATES:
        try:
            d = pro.index_weight(index_code=idx_code, start_date=cand, end_date=cand)
            time.sleep(0.15)
            if d is not None and len(d) > 0:
                df, snap_date = d, cand
                break
        except Exception as exc:  # noqa: BLE001
            print(f"  {fname} {cand} ERR {exc}")

    n_ext = n_add = 0
    if df is not None:
        cons = set()
        for _, r in df.iterrows():
            s = _to_qlib(str(r["con_code"]))
            if s:
                cons.add(s)
        for sym in sorted(cons):
            if sym in max_set:
                # 成员 ∈ 文件当前快照集（05-12 快照成员且仍在）→ 扩展其最大 end 行至 2099
                last = max(cur[sym], key=lambda x: x[1])
                if last[2] != NEVER:
                    last[2] = NEVER
                    n_ext += 1
            else:
                # 成员 ∉ 当前快照集（6 月调仓新加入/重新加入）→ 追加新行，start=快照日
                sd = f"{snap_date[:4]}-{snap_date[4:6]}-{snap_date[6:8]}"
                rows.append([sym, sd, NEVER])
                n_add += 1
        src = f"index_weight {snap_date} n={len(cons)}"
    else:
        # 回退: 现有 max-end 成员集 end→2099（保守假设成员延续）
        for r in rows:
            if r[2] == max_end:
                r[2] = NEVER
                n_ext += 1
        src = f"无快照 → 回退扩展 max-end({max_end}, n={len(max_set)}) → 2099"
    rows.sort(key=lambda r: (r[0], r[1]))
    f.write_text("\n".join("\t".join(r) for r in rows) + "\n", encoding="utf-8")
    return {"before_rows": before, "after_rows": len(rows),
            "extended_to_never": n_ext, "added": n_add, "snapshot": src}


if __name__ == "__main__":
    pro = ts.pro_api(_token())
    for fname, idx in INDEX_MAP.items():
        rep = refresh_index(pro, fname, idx)
        print(f"{fname}: {rep}")
