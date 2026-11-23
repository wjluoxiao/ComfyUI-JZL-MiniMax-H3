import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
try:
    import torch  # noqa: F401  (h3_windows 需要 torch)
except Exception:  # pragma: no cover
    torch = None


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _align(n):
    for f in range(int(n), int(n) + 200):
        if f >= 5 and (f - 5) % 17 == 0:
            return f
    raise ValueError(n)


@unittest.skipIf(torch is None, "需要 torch 才能加载 h3_windows")
class SwapRetakeTest(unittest.TestCase):
    """「只重跑第 N 段」：把窗口 k−1/k/k+1 重定位成独立小段后，裁出的帧区间必须和整片一致。"""

    @classmethod
    def setUpClass(cls):
        cls.W = _load("h3_windows")

    def test_rebased_piece_matches_full_run(self):
        W = self.W
        for total_src in (124, 240, 243, 332, 345, 500, 1000):
            total = _align(total_src)
            wins = W.plan_windows(total, 124, 22)
            bounds = [w.frame_start for w in wins] + [total]
            for k in range(len(wins)):
                first, last = max(0, k - 1), min(len(wins) - 1, k + 1)
                base = wins[first].frame_start
                reb, rtot = W.sub_windows(wins, first, last)

                self.assertEqual(reb[0].frame_start, 0)
                self.assertEqual(reb[-1].frame_start + reb[-1].frames, rtot)
                # 重定位后的窗口集必须能独立成片（5+17N）
                self.assertIsNotNone(W.groups_of(rtot))
                for a, b in zip(reb, reb[1:]):
                    overlap = a.frame_start + a.frames - b.frame_start
                    self.assertGreaterEqual(overlap, 22)
                    self.assertEqual((b.frame_start - a.frame_start) % 17, 0)

                # 视频/音频 token 区间整体平移，切片内容不变
                for r, w in zip(reb, wins[first:last + 1]):
                    self.assertEqual(r.frames, w.frames)
                    self.assertEqual(r.video_len, w.video_len)
                    self.assertEqual(r.video_start, w.video_start - wins[first].video_start)
                    self.assertEqual(r.audio_start, w.audio_start - wins[first].audio_start)

                # 裁出的那一片 = 整片里该段的区间
                lo = 0 if k == 0 else reb[k - first].frame_start
                hi = rtot if k == len(wins) - 1 else reb[k - first + 1].frame_start
                self.assertEqual((lo, hi), (bounds[k] - base, bounds[k + 1] - base))
                self.assertGreater(hi, lo)
                self.assertLessEqual(hi, rtot)


if __name__ == "__main__":
    unittest.main()
