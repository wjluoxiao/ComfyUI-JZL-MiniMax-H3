import importlib
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

try:
    import torch
except Exception:  # pragma: no cover
    torch = None


def _load():
    try:
        sys.path.insert(0, r"Z:\AI_work\ComfyUI")
        import folder_paths  # noqa: F401
    except Exception:
        return None
    import types
    if "jzlswap" not in sys.modules:
        pkg = types.ModuleType("jzlswap")
        pkg.__path__ = [_ROOT]
        sys.modules["jzlswap"] = pkg
    try:
        return importlib.import_module("jzlswap.nodes_asset_manager")
    except Exception:
        return None


_AM = _load()


class _FakeVAE:
    """只记录「送进 VAE 的画面尺寸」，返回形状合规的假 latent。"""

    def __init__(self):
        self.sizes = []

    def encode(self, x):
        self.sizes.append(tuple(x.shape))
        t, h, w = int(x.shape[0]), int(x.shape[1]), int(x.shape[2])
        tok = max(2, ((t - 5) // 17) * 5 + 2) if t >= 5 else 2
        return torch.zeros(1, 24, tok, max(1, h // 16), max(1, w // 16))


class _FakeClip:
    def tokenize(self, *a, **k):     # defer 模式不应走到文本编码
        raise AssertionError("defer 模式不应 tokenize")


@unittest.skipIf(_AM is None or torch is None, "需要 ComfyUI 环境才能加载 nodes_asset_manager")
class ShrinkSizeTest(unittest.TestCase):
    """「编码设置」的缩小倍率：1.0 原样返回，其余按 32 对齐缩小、下限 32。"""

    def test_ratio_one_is_identity(self):
        self.assertEqual(_AM._shrink_size(736, 416, 1.0), (736, 416))
        self.assertEqual(_AM._shrink_size(736, 416, None), (736, 416))

    def test_ratio_halves_and_aligns(self):
        w, h = _AM._shrink_size(1920, 1088, 0.5)
        self.assertAlmostEqual(w / h, 1920 / 1088, places=1)
        self.assertEqual(w % 32, 0)
        self.assertEqual(h % 32, 0)
        self.assertLess(w, 1920)

    def test_ratio_never_below_floor(self):
        self.assertEqual(_AM._shrink_size(128, 128, 0.2), (32, 32))
        self.assertEqual(_AM._shrink_size(64, 64, 0.05), (32, 32))


@unittest.skipIf(_AM is None or torch is None, "需要 ComfyUI 环境才能加载 nodes_asset_manager")
class EncodeRatioPlumbingTest(unittest.TestCase):
    """参考图放大只改参考图；输入视频缩小只改视频编码画布；文本视觉缩小只改文本编码的视觉帧。"""

    def _run(self, video_shrink=1.0, vision_shrink=1.0, ref_scale=1.0):
        vae = _FakeVAE()
        jobs = []
        _AM._encode_ref_to_video(
            _FakeClip(), vae, None, "p", 416, 736, 124,
            ref_images=[torch.zeros(1, 900, 600, 3)],
            ref_videos=[torch.zeros(124, 736, 416, 3)],
            ref_video_audios=[], ref_audios=[],
            ref_image_size="match", ref_scale=ref_scale,
            slot_names=None, probe=None, defer=jobs,
            video_shrink=video_shrink, vision_shrink=vision_shrink)
        return vae.sizes, jobs[0]["ref_blocks"], jobs[0]["ref_items"]

    def test_default_is_unchanged(self):
        sizes, blocks, items = self._run()
        self.assertEqual(len(sizes), 2)                       # 参考图 + 参考视频
        self.assertEqual([b["kind"] for b in blocks], ["image", "video"])
        vis = [tuple(it["data"].shape) for it in items]
        self.assertEqual(vis[0], sizes[0])                    # 视觉图 == 编码图（未降采样）
        self.assertEqual(sizes[1][1:3], (736, 416))           # 视频按生成画布编码
        self.assertEqual(vis[1][0], 11)                       # 每 0.5 秒 1 张
        self.assertEqual(tuple(vis[1][1:3]), (736, 416))

    def test_ref_scale_upscales_reference_image_only(self):
        base_sizes, _b, _i = self._run()
        sizes, _b2, _i2 = self._run(ref_scale=2.0)
        self.assertGreater(sizes[0][1], base_sizes[0][1])     # 参考图变大
        self.assertGreater(sizes[0][2], base_sizes[0][2])
        self.assertEqual(sizes[1], base_sizes[1])             # 参考视频不受影响

    def test_video_shrink_shrinks_video_encode_only(self):
        base_sizes, base_blocks, base_items = self._run()
        sizes, blocks, items = self._run(video_shrink=0.5)
        self.assertEqual(sizes[0], base_sizes[0])             # 参考图不变
        self.assertEqual(sizes[1][1:3], (384, 192))           # 736x416 ×0.5 并对齐 32
        self.assertLess(blocks[1]["latent_h"] * blocks[1]["latent_w"],
                        base_blocks[1]["latent_h"] * base_blocks[1]["latent_w"])
        # 视觉帧跟着视频编码画布走（本次未单独限制视觉）
        self.assertEqual(tuple(items[1]["data"].shape[1:3]), (384, 192))
        self.assertEqual(int(items[1]["data"].shape[0]), int(base_items[1]["data"].shape[0]))

    def test_vision_shrink_affects_text_frames_only(self):
        base_sizes, _b, base_items = self._run()
        sizes, _b2, items = self._run(vision_shrink=0.5)
        self.assertEqual(sizes, base_sizes)                   # VAE 输入不变
        self.assertLess(int(items[1]["data"].shape[1]), int(base_items[1]["data"].shape[1]))
        self.assertEqual(int(items[1]["data"].shape[1]) % 32, 0)
        self.assertEqual(int(items[1]["data"].shape[0]), 11)   # 帧数不变


if __name__ == "__main__":
    unittest.main()
