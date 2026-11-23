import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

try:
    import torch  # noqa: F401  h3_windows 需要 torch
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
class DecodeChunkTest(unittest.TestCase):
    """分组解码：短片必须单块（行为不变），长片必须分块，且帧/音频区间连续不重不漏。"""

    @classmethod
    def setUpClass(cls):
        cls.W = _load("h3_windows")

    def test_chunks_tile_the_film(self):
        W = self.W
        for src in (124, 240, 332, 1000, 4000, 7200, 12000):
            total = _align(src)
            for budget in (1200, 600):
                chunks = W.plan_decode_chunks(total, budget)
                covered = 0
                for i, ch in enumerate(chunks):
                    v0, v1 = ch["video"]
                    self.assertTrue(0 <= v0 < v1 <= W.video_tokens(total))
                    dec_frames = W.frames_of((v1 - v0 - 2) // W.GROUP_TOKENS)
                    self.assertLessEqual(ch["drop_frames"] + ch["keep_frames"], dec_frames)
                    self.assertEqual(ch["frame_start"], covered)
                    a0, a1 = ch["audio"]
                    self.assertTrue(0 <= a0 < a1 <= W.audio_tokens(total))
                    covered += ch["keep_frames"]
                self.assertEqual(covered, total)
                if total <= budget:
                    self.assertEqual(len(chunks), 1, src)
                else:
                    self.assertGreaterEqual(len(chunks), 2, src)

    def test_short_film_is_single_chunk(self):
        W = self.W
        total = _align(240)
        ch = W.plan_decode_chunks(total, 1200)[0]
        self.assertEqual(ch["video"], (0, W.video_tokens(total)))
        self.assertEqual(ch["drop_frames"], 0)
        self.assertEqual(ch["keep_frames"], total)


@unittest.skipIf(torch is None, "需要 torch 才能加载 h3_windows")
class WindowProgressTest(unittest.TestCase):
    """进度条 1 格 = 1 次前向：总数 = 步数 × 窗口数。"""

    @classmethod
    def setUpClass(cls):
        cls.W = _load("h3_windows")

    def test_total_is_steps_times_windows(self):
        bar = self.W._WindowProgress(4, 3, tag="测试")
        seen = []
        for _i in bar:
            for k in range(1, 4):
                bar.tick(k)
            seen.append(bar.done)
        bar.close()
        self.assertEqual(seen, [3, 6, 9, 12])
        self.assertEqual(bar.bar.total, 12)

    def test_missing_ticks_are_filled_by_step(self):
        bar = self.W._WindowProgress(2, 3, tag="测试")
        for _i in bar:
            bar.tick(1)
        bar.close()
        self.assertEqual(bar.done, 6)
        self.assertEqual(bar.bar.total, 6)


if __name__ == "__main__":
    unittest.main()
