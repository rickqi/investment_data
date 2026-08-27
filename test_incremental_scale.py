"""R6 单测: incremental_update._compute_scale NaN 洞加固（2026-08-28）。

覆盖:
  - 正常末尾（有效 close/adjclose）→ scale = close/adjclose，行为与旧实现逐位一致
  - 末尾 last_close=NaN（adjclose 有效）→ 旧实现产生 NaN scale；现回溯最近有效对
  - 末尾 last_adjclose=NaN（2026-05-15 洞事故形态）→ 旧实现回退 1.0；现回溯最近有效对
  - 多个连续 NaN 条目 → 回溯
  - 全 0 占位（新股票）→ scale=1.0、tail_has_nan=False、不告警
"""

import numpy as np
import unittest

from incremental_update import _compute_scale


def bins(close, adjclose):
    return {"close": np.array(close, dtype=np.float32),
            "adjclose": np.array(adjclose, dtype=np.float32)}


class TestComputeScale(unittest.TestCase):
    def test_normal_tail_unchanged(self):
        # 旧逻辑: last_close/last_adjclose = 5.9/2.95 → scale=2.0
        b = bins(close=[5.0, 5.5, 5.9], adjclose=[2.5, 2.75, 2.95])
        scale, skipped, has_nan = _compute_scale(b)
        self.assertAlmostEqual(scale, 5.9 / 2.95, places=6)
        self.assertEqual(skipped, 0)
        self.assertFalse(has_nan)

    def test_last_close_nan_no_nan_scale(self):
        # 旧实现: last_adjclose=2.95 有效、last_close=NaN → scale=NaN（数据中毒）
        # 新实现: 回溯至 index1 有效对 5.5/2.75
        b = bins(close=[5.0, 5.5, np.nan], adjclose=[2.5, 2.75, 2.95])
        scale, skipped, has_nan = _compute_scale(b)
        self.assertFalse(np.isnan(scale), "scale 不得为 NaN")
        self.assertAlmostEqual(scale, 5.5 / 2.75, places=6)
        self.assertEqual(skipped, 1)
        self.assertTrue(has_nan)

    def test_last_adjclose_nan_no_fallback_1(self):
        # 旧实现（2026-05-15 洞事故形态）: last_adjclose=NaN → scale 回退 1.0（归一化断裂）
        # 新实现: 回溯至 index1 有效对 5.5/2.75
        b = bins(close=[5.0, 5.5, 5.9], adjclose=[2.5, 2.75, np.nan])
        scale, skipped, has_nan = _compute_scale(b)
        self.assertAlmostEqual(scale, 5.5 / 2.75, places=6)
        self.assertNotAlmostEqual(scale, 1.0)
        self.assertEqual(skipped, 1)
        self.assertTrue(has_nan)

    def test_multiple_nan_tail_backtracks(self):
        b = bins(close=[5.0, np.nan, np.nan, np.nan], adjclose=[2.5, np.nan, np.nan, np.nan])
        scale, skipped, has_nan = _compute_scale(b)
        self.assertAlmostEqual(scale, 5.0 / 2.5, places=6)
        self.assertEqual(skipped, 3)
        self.assertTrue(has_nan)

    def test_all_zero_new_symbol_no_warn(self):
        b = bins(close=[0.0, 0.0], adjclose=[0.0, 0.0])
        scale, skipped, has_nan = _compute_scale(b)
        self.assertEqual(scale, 1.0)
        self.assertFalse(has_nan, "全 0 占位不得触发 NaN 告警")
        self.assertGreater(skipped, 0)

    def test_all_nan_bin_warns(self):
        """repair-round-2（reviewer t10 边界缺陷）: 全 NaN bin 不再静默回退——
        has_nan 必须为 True（调用方据此告警，05-15 事故同形不得静默）。"""
        b = bins(close=[np.nan, np.nan], adjclose=[np.nan, np.nan])
        scale, skipped, has_nan = _compute_scale(b)
        self.assertEqual(scale, 1.0)
        self.assertEqual(skipped, 2)
        self.assertTrue(has_nan, "全 NaN bin 必须置 has_nan=True 触发告警")

    def test_all_nan_one_side_warns(self):
        b = bins(close=[np.nan, np.nan], adjclose=[1.0, np.nan])
        scale, skipped, has_nan = _compute_scale(b)
        self.assertEqual(scale, 1.0)
        self.assertTrue(has_nan, "任一侧含 NaN 即应置 has_nan=True")

    def test_mixed_nan_zero_warns(self):
        b = bins(close=[0.0, np.nan], adjclose=[0.0, np.nan])
        scale, skipped, has_nan = _compute_scale(b)
        self.assertEqual(scale, 1.0)
        self.assertTrue(has_nan, "混合 NaN/0 无效 bin 应告警")

    def test_zero_then_valid_tail(self):
        # 末尾为 0.0（异常但非 NaN）→ 回溯；tail 无 NaN → 不告警
        b = bins(close=[5.0, 0.0], adjclose=[2.5, 0.0])
        scale, skipped, has_nan = _compute_scale(b)
        self.assertAlmostEqual(scale, 5.0 / 2.5, places=6)
        self.assertEqual(skipped, 1)
        self.assertFalse(has_nan)


if __name__ == "__main__":
    unittest.main()
