import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _load_mod():
    """在 ComfyUI 环境里按「伪包」方式加载 nodes_character_swap（跳过 __init__ 的服务器路由）。"""
    try:
        sys.path.insert(0, r"Z:\AI_work\ComfyUI")
        import folder_paths  # noqa: F401
        import types
        import importlib
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


@unittest.skipIf(_MOD is None, "需要 ComfyUI 环境（comfy 模块）才能加载 nodes_character_swap")
class AudioIsolationTest(unittest.TestCase):
    """音频链路必须被彻底摘出：源音频不进编码、latent 音频不解码、音频 VAE 不使用。"""

    @classmethod
    def setUpClass(cls):
        import inspect
        cls.src = inspect.getsource(_MOD)
        cls.mod = _MOD

    def test_reference_encoding_gets_no_source_audio(self):
        """源片原声永远不进参考编码；只有「参考音频」档才把用户给的那条音频送进去（方案 B）。"""
        src = self.src
        self.assertIn("ref_video_audios=[]", src)
        self.assertIn("ref_audios=([ref_audio_enc] if ref_audio_enc is not None else [])", src)
        self.assertIn("_warn_audio_ref_variant(model, True, TAG)", src)
        self.assertIn("_AUDIO_REF_LINE", src)

    def test_decode_audio_only_when_generated(self):
        # 两处解码都必须按「声音来源」决定是否解码音频，不能写死
        import re
        calls = [c for c in re.findall(r"_decode_av\([^)]*\)", self.src, re.S)
                 if "_decode_av(vae, audio_vae" in c]
        self.assertEqual(len(calls), 2, calls)
        for call in calls:
            # _decode_chunk 内部把形参透传；实际取值由调用方给 skip_model_audio
            self.assertTrue("skip_audio=skip_audio" in call or "skip_audio=skip_model_audio" in call,
                            "有一处解码没按声音来源决定: " + " ".join(call.split())[:80])
        self.assertGreaterEqual(self.src.count("skip_audio=skip_model_audio"), 2,
                                "没有把声音来源接到解码上")

    def test_audio_vae_only_used_for_generated_audio(self):
        # 只在「声音来源 = 生成音频」时把音频 VAE 传下去，其余恒 None
        self.assertIn("audio_vae_eff = audio_vae if use_model_audio else None", self.src)
        # 音轨来源统一由纯函数决定（只有「生成音频」且接了音频VAE 才会走模型音轨）
        self.assertIn("_pick_output_audio(", self.src)
        self.assertIn("audio_vae is not None)", self.src)

    def test_audio_mode_has_three_choices(self):
        cfg = self.mod._parse_swap_settings("")
        self.assertEqual(cfg["gen_params"]["audio_mode"], "使用原声")
        for mode in ("参考音频", "模型生成"):
            got = self.mod._parse_swap_settings(
                '{"gen_params": {"audio_mode": "%s"}}' % mode)["gen_params"]["audio_mode"]
            self.assertEqual(got, mode)
        # 老工作流里的旧名字要映射到新三档
        self.assertEqual(self.mod._parse_swap_settings(
            '{"gen_params": {"audio_mode": "生成音频"}}')["gen_params"]["audio_mode"], "模型生成")
        for legacy in ("外部音频", "静音", "乱写"):
            self.assertEqual(self.mod._parse_swap_settings(
                '{"gen_params": {"audio_mode": "%s"}}' % legacy)["gen_params"]["audio_mode"],
                "使用原声", legacy)

    def test_audio_mode_prompt_rewrite(self):
        """三档的声音段改写：使用原声/模型生成删 <Audio 1>，参考音频保留它并补 retention。"""
        reuse = self.mod._AUDIO_REUSE_LINE
        for mode, expect in (("使用原声", reuse), ("模型生成", self.mod._AUDIO_GEN_LINE)):
            out = self.mod._apply_audio_mode("<Audio 1> fully_copy\n" + reuse, mode)
            self.assertNotIn("<Audio 1>", out, mode)
            self.assertIn(expect, out, mode)
        ref = self.mod._apply_audio_mode("retention_analysis:\n<Subject 1>: fully_preserved - a\n"
                                         "overall_soundscape: " + reuse, "参考音频")
        self.assertIn("<Audio 1>", ref)
        self.assertIn(self.mod._AUDIO_REF_LINE, ref)
        self.assertIn("attribute_transfer", ref)       # 自动补进 retention_analysis
        for mode in ("使用原声", "参考音频", "模型生成"):
            self.assertIn(mode, self.mod._AUDIO_MODE_HINT)

    def test_external_audio_and_mute_are_gone(self):
        """方案 A（外部音频 / 静音）已移除：代码里不该再残留这两档的常量与逻辑。"""
        for gone in ("_AUDIO_EXT_LINE", "_AUDIO_MUTE_LINE", "_SOUNDSCAPE_FORCED",
                     "_force_soundscape"):
            self.assertFalse(hasattr(self.mod, gone), gone)

    def test_pick_output_audio_matrix(self):
        """三档 → 成片音轨来源 + 是否进参考编码（纯函数，逐档验证）。"""
        import torch
        wf = torch.zeros(1, 2, 32000)
        src = {"waveform": wf, "sample_rate": 32000}
        ref = {"waveform": torch.ones(1, 2, 32000), "sample_rate": 32000}
        pick = self.mod._pick_output_audio

        # 使用原声：切源声，不解码模型音轨，也不进参考编码
        use_model, cut, ref_enc = pick("使用原声", src, ref, True)
        self.assertFalse(use_model)
        self.assertIs(cut["waveform"], wf)
        self.assertIsNone(ref_enc)                      # 参考音频不该被误用

        # 参考音频：进参考编码 + 解码模型音轨
        use_model, cut, ref_enc = pick("参考音频", src, ref, True)
        self.assertTrue(use_model)
        self.assertIsNone(cut)
        self.assertIs(ref_enc["waveform"], ref["waveform"])

        # 参考音频但没接参考音频 → 回退使用原声
        use_model, cut, ref_enc = pick("参考音频", src, None, True)
        self.assertFalse(use_model)
        self.assertIs(cut["waveform"], wf)
        self.assertIsNone(ref_enc)

        # 参考音频但没接音频VAE → 无法编码，回退使用原声
        use_model, cut, ref_enc = pick("参考音频", src, ref, False)
        self.assertFalse(use_model)
        self.assertIsNone(ref_enc)

        # 模型生成：解析模型音轨，参考音频不参与
        self.assertEqual(pick("模型生成", src, ref, True), (True, None, None))
        # 模型生成但没音频VAE → 回退使用原声
        use_model, cut, ref_enc = pick("模型生成", src, ref, False)
        self.assertFalse(use_model)
        self.assertIs(cut["waveform"], wf)

    def test_audio_input_types(self):
        import torch
        wf = torch.zeros(1, 2, 4800)

        class _Obj:
            waveform = wf
            sample_rate = 44100

        self.assertIsNone(self.mod._as_audio_dict(None))
        self.assertEqual(self.mod._as_audio_dict({"waveform": wf, "sample_rate": 32000})["sample_rate"], 32000)
        self.assertEqual(self.mod._as_audio_dict(_Obj())["sample_rate"], 44100)
        self.assertEqual(self.mod._as_audio_dict((wf, 48000))["sample_rate"], 48000)
        self.assertEqual(self.mod._as_audio_dict(wf)["sample_rate"], 32000)

    def test_slice_audio_by_frames(self):
        import torch
        wf = torch.arange(24000).float().reshape(1, 1, -1).repeat(1, 2, 1)
        out = self.mod._slice_audio({"waveform": wf, "sample_rate": 24000}, 0, 12)
        self.assertEqual(int(out["waveform"].shape[-1]), 12000)


if __name__ == "__main__":
    unittest.main()
