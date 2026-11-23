import importlib
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

try:
    import torch
except Exception:  # pragma: no cover
    torch = None


def _load(name):
    """按「伪包」方式加载模块（跳过 __init__ 的服务器路由注册）。"""
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
        return importlib.import_module(f"jzlswap.{name}")
    except Exception:
        return None


_AM = _load("nodes_asset_manager")
_CS = _load("nodes_character_swap")


@unittest.skipIf(_AM is None or torch is None, "需要 ComfyUI 环境才能加载 nodes_asset_manager")
class UpscalerArchTest(unittest.TestCase):
    """2D（resizer.*）与 3D（Conv3d）两套放大权重都要能识别并分派到各自的网络。"""

    def test_safetensors_2d_and_3d(self):
        from safetensors.torch import save_file
        with tempfile.TemporaryDirectory() as d:
            p2 = os.path.join(d, "m_2d.safetensors")
            save_file({"resizer.conv_in.weight": torch.zeros(4, 24, 3, 3),
                       "temporal_blocks.0.dwconv.weight": torch.zeros(4, 1, 5)}, p2)
            p3 = os.path.join(d, "m_3d.safetensors")
            save_file({"conv_in.weight": torch.zeros(4, 24, 3, 3, 3)}, p3)
            self.assertEqual(_AM._upscaler_arch(p2), "2d")
            self.assertEqual(_AM._upscaler_arch(p3), "3d")

    def test_pth_falls_back_to_filename(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "minimax_h3_latent_upscaler_2d_conv_v1_fp32.pth")
            with open(p, "wb") as f:
                f.write(b"PK\x03\x04" + b"\0" * 32)
            self.assertEqual(_AM._upscaler_arch(p), "2d")

    def test_both_vendored_networks_resolve_inside_plugin(self):
        """两套网络都必须来自本插件内置副本（不依赖用户是否装了上游节点包）。"""
        c3 = _AM._import_upscaler_3d()
        c2 = _AM._import_upscaler_2d()
        self.assertEqual(c3.__module__, "jzlswap.jzl_h3_latent_upscaler_3d")
        self.assertEqual(c2.__module__, "jzlswap.jzl_h3_latent_upscaler_2d")

    def test_second_pass_dropdown_lists_2d_but_selflift_does_not(self):
        """二采支持 2D/3D；SelfLift 的放大模型只用 3D（它走 vendor_selflift 的 3D 网络）。"""
        opts2 = _AM._upscaler_model_options()
        if not any("2d_conv" in x for x in opts2):
            self.skipTest("磁盘上没有 2D 权重，跳过")
        self.assertTrue(any("2d_conv" in x for x in opts2))
        self.assertFalse(any("2d_conv" in x for x in _AM._selflift_upscaler_options()))


@unittest.skipIf(_CS is None or torch is None, "需要 ComfyUI 环境才能加载 nodes_character_swap")
class AvLatentSplitTest(unittest.TestCase):
    """「Separate/Concat AV Latent」的输出是 LATENT dict（不是裸张量）：
    切片必须先取 ["samples"]，二采再把 Concat 的结果原样交给采样器（不能二次包 {"samples": dict}）。"""

    def _nested(self, frames=5 + 17 * 20):
        import comfy.nested_tensor as nt
        v = torch.zeros(1, 24, 2 + 5 * 20, 4, 4)
        a = torch.zeros(1, 32, 2, int(round(frames * 40 / 24)))
        return nt.NestedTensor((v, a)), frames

    def test_split_returns_latent_dicts(self):
        samples, _ = self._nested()
        v_lat, a_lat = _CS._split_av_latent(samples)
        self.assertIsInstance(v_lat, dict)
        self.assertIsInstance(a_lat, dict)
        self.assertEqual(tuple(v_lat["samples"].shape), (1, 24, 2 + 5 * 20, 4, 4))
        self.assertEqual(a_lat["samples"].ndim, 4)

    def test_decode_chunk_slices_by_token_range(self):
        samples, frames = self._nested()
        chunks = _CS._win.plan_decode_chunks(frames, 100)
        self.assertGreater(len(chunks), 1)
        ch = chunks[1]
        seen = []

        def fake_decode(vae, audio_vae, part, sample_decode=None, tag="", skip_audio=True):
            seen.append(part)
            n = int(ch["drop_frames"]) + int(ch["keep_frames"])
            return torch.zeros(n, 2, 2, 3), None

        orig = _CS._decode_av
        _CS._decode_av = fake_decode
        try:
            image, audio = _CS._decode_chunk(None, None, samples, ch)
        finally:
            _CS._decode_av = orig
        self.assertIsNone(audio)
        self.assertEqual(image.shape[0], int(ch["keep_frames"]))
        v_part = seen[0]["samples"].tensors[0]
        a_part = seen[0]["samples"].tensors[1]
        v0, v1 = ch["video"]
        a0, a1 = ch["audio"]
        self.assertEqual(int(v_part.shape[2]), int(v1) - int(v0))
        self.assertEqual(int(a_part.shape[-1]), int(a1) - int(a0))


@unittest.skipIf(_CS is None or torch is None, "需要 ComfyUI 环境才能加载 nodes_character_swap")
class PlanLineTest(unittest.TestCase):
    """采样计划里的步数必须是「实际 sigmas 步数 × 窗口数」，不是面板上那个容易对不上的数字。"""

    def test_plan_line_uses_real_counts(self):
        import contextlib
        import io as _io
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            _CS._plan_line("二采", 3, 2)
        out = buf.getvalue()
        self.assertIn("3 步 × 2 个窗口 = 6 次模型前向", out)
        self.assertNotIn("8 次", out)

    def test_plan_line_single_window(self):
        import contextlib
        import io as _io
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            _CS._plan_line("一采", 4, 1)
        self.assertIn("4 步 × 1 个窗口 = 4 次模型前向", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
