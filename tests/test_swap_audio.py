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

    def test_audio_mode_has_four_choices(self):
        cfg = self.mod._parse_swap_settings("")
        self.assertEqual(cfg["gen_params"]["audio_mode"], "使用原声")
        for mode in ("生成音频", "外部音频", "静音"):
            got = self.mod._parse_swap_settings(
                '{"gen_params": {"audio_mode": "%s"}}' % mode)["gen_params"]["audio_mode"]
            self.assertEqual(got, mode)
        cfg3 = self.mod._parse_swap_settings(
            '{"gen_params": {"audio_mode": "乱写"}}')
        self.assertEqual(cfg3["gen_params"]["audio_mode"], "使用原声")

    def test_audio_mode_prompt_rewrite(self):
        """四种模式都不许留下 `<Audio 1>`，且声音段按模式改写。"""
        reuse = self.mod._AUDIO_REUSE_LINE
        for mode, expect in (("使用原声", reuse),
                             ("生成音频", self.mod._AUDIO_GEN_LINE),
                             ("外部音频", self.mod._AUDIO_EXT_LINE),
                             ("静音", self.mod._AUDIO_MUTE_LINE)):
            out = self.mod._apply_audio_mode("<Audio 1> fully_copy\n" + reuse, mode)
            self.assertNotIn("<Audio 1>", out, mode)
            self.assertIn(expect, out, mode)
        for mode in ("使用原声", "生成音频", "外部音频", "静音"):
            self.assertIn(mode, self.mod._AUDIO_MODE_HINT)
            self.assertIn("<Audio 1>", self.mod._AUDIO_MODE_HINT[mode])

    def test_external_audio_is_the_slicing_source(self):
        """外部音频优先于源片原声作为切片来源；没给就退回源声。"""
        self.assertEqual(self.src.count("_slice_audio(cut_audio"), 2)
        # 落盘音轨四选一的标签必须齐全
        for label in ("生成音频（模型音轨）", "外部音频（整体替换）", "静音（无音轨）"):
            self.assertIn(label, self.src)

    def test_pick_output_audio_matrix(self):
        """「声音来源」四选一 → 成片音轨来源的正确性（纯函数，逐档验证）。"""
        import torch
        wf = torch.zeros(1, 2, 32000)
        src = {"waveform": wf, "sample_rate": 32000}
        ext = {"waveform": torch.ones(1, 2, 32000), "sample_rate": 32000}
        pick = self.mod._pick_output_audio

        # 使用原声：切源声，不用模型音轨
        use_model, cut, muted, got_ext, ref = pick("使用原声", src, ext, False)
        self.assertFalse(use_model)
        self.assertIs(cut["waveform"], wf)               # 内容来自源声
        self.assertEqual(cut["sample_rate"], 32000)
        self.assertFalse(muted)
        self.assertIsNone(got_ext)                       # 外部音频不该被误用
        self.assertIsNone(ref)                           # 也不会进参考编码

        # 生成音频（有音频VAE）：模型音轨；cut 由解码结果给
        self.assertEqual(pick("生成音频", src, ext, True), (True, None, False, None, None))

        # 生成音频（没接音频VAE）：回退源声
        use_model, cut, muted, _e, _r = pick("生成音频", src, ext, False)
        self.assertFalse(use_model)
        self.assertIs(cut["waveform"], wf)

        # 外部音频：优先用外部那条，源声不再使用
        use_model, cut, muted, got_ext, _r = pick("外部音频", src, ext, False)
        self.assertFalse(use_model)
        self.assertIs(cut["waveform"], ext["waveform"])  # 用的是外部那条
        self.assertFalse(muted)
        self.assertIsNotNone(got_ext)

        # 外部音频但没接替换音轨：退回源声
        use_model, cut, muted, got_ext, _r = pick("外部音频", src, None, False)
        self.assertIs(cut["waveform"], wf)               # 退回源声
        self.assertIsNone(got_ext)
        # 源声也没有 → 成片无声
        self.assertIsNone(pick("外部音频", None, None, False)[1])

        # 静音：一律无音轨，外部音频也不用
        self.assertEqual(pick("静音", src, ext, False)[0:3], (False, None, True))

        # 静音不受源声影响
        self.assertIsNone(pick("静音", None, None, False)[1])

    def test_pick_output_audio_reference_mode(self):
        """「参考音频」= 方案 B：音频进参考编码，同时解码模型生成的音轨。"""
        import torch
        wf = torch.zeros(1, 2, 32000)
        src = {"waveform": wf, "sample_rate": 32000}
        ref = {"waveform": torch.ones(1, 2, 32000), "sample_rate": 32000}
        pick = self.mod._pick_output_audio

        use_model, cut, muted, ext, got_ref = pick("参考音频", src, ref, True)
        self.assertTrue(use_model)                            # 模型生成音轨（按参考音色）
        self.assertIsNone(cut)
        self.assertFalse(muted)
        self.assertIsNone(ext)                                # 不走整体替换
        self.assertIs(got_ref["waveform"], ref["waveform"])    # 参考音频进编码

        # 没接参考音频 → 回退使用原声
        use_model, cut, muted, ext, got_ref = pick("参考音频", src, None, True)
        self.assertFalse(use_model)
        self.assertIsNone(got_ref)
        self.assertIs(cut["waveform"], wf)

        # 没接音频VAE → 无法编码参考音频，回退使用原声
        use_model, cut, muted, ext, got_ref = pick("参考音频", src, ref, False)
        self.assertFalse(use_model)
        self.assertIsNone(got_ref)
        self.assertIs(cut["waveform"], wf)

    def test_audio_duration_helper(self):
        import torch
        wf = torch.zeros(1, 2, 48000)
        self.assertAlmostEqual(self.mod._audio_duration({"waveform": wf, "sample_rate": 32000}), 1.5, 3)
        self.assertEqual(self.mod._audio_duration(None), 0.0)
        self.assertEqual(self.mod._audio_duration({"waveform": None}), 0.0)

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
