import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

try:
    import torch
except Exception:  # pragma: no cover
    torch = None


def _load_mod():
    """按「伪包」方式加载 nodes_character_swap（跳过 __init__ 的服务器路由）。"""
    try:
        import importlib
        import types
        sys.path.insert(0, r"Z:\AI_work\ComfyUI")
        import folder_paths  # noqa: F401
    except Exception:
        return None
    import importlib
    import types
    if "jzlswap" not in sys.modules:
        pkg = types.ModuleType("jzlswap")
        pkg.__path__ = [_ROOT]
        sys.modules["jzlswap"] = pkg
    try:
        return importlib.import_module("jzlswap.nodes_character_swap")
    except Exception:
        return None


_MOD = _load_mod()


@unittest.skipIf(_MOD is None or torch is None, "需要 ComfyUI 环境才能加载 nodes_character_swap")
class SourceFpsTest(unittest.TestCase):
    """源片不是 24fps 时，用原声真实时长反推帧率并升/降帧到 24fps（时长不变）。"""

    @staticmethod
    def _aud(sec, sr=32000):
        return {"waveform": torch.zeros(1, 2, int(sec * sr)), "sample_rate": sr}

    def test_detect_common_rates(self):
        A = _MOD._detect_source_fps
        for frames, sec, want in ((120, 10.0, 12.0), (240, 10.0, 24.0),
                                  (300, 10.0, 30.0), (240, 8.0, 30.0)):
            fps, dur = A(frames, self._aud(sec))
            self.assertAlmostEqual(fps, want, places=3, msg=f"{frames}帧/{sec}秒")
            self.assertAlmostEqual(dur, sec, places=3)

    def test_fallback_without_audio(self):
        fps, dur = _MOD._detect_source_fps(120, None)
        self.assertEqual(fps, 24.0)
        self.assertIsNone(dur)

    def test_unusual_rate_falls_back(self):
        fps, _ = _MOD._detect_source_fps(73, self._aud(10.0))
        self.assertEqual(fps, 24.0)

    def test_resample_keeps_duration(self):
        R = _MOD._resample_to_24
        src = torch.arange(120, dtype=torch.float32).reshape(120, 1, 1, 1)
        out = R(src, 12.0)
        self.assertEqual(int(out.shape[0]), 240)
        for i in (0, 1, 2, 3, 119, 239):
            self.assertEqual(int(out[i, 0, 0, 0]), min(119, int(i * 12.0 / 24.0)))
        # 30fps → 24fps 是降帧
        self.assertEqual(int(R(torch.zeros(300, 2, 2, 3), 30.0).shape[0]), 240)
        # uint8 缓存也能重采样
        u8 = R(torch.zeros(120, 8, 8, 3, dtype=torch.uint8), 12.0)
        self.assertEqual((u8.dtype, int(u8.shape[0])), (torch.uint8, 240))


if __name__ == "__main__":
    unittest.main()
