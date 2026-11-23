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

    def test_reference_encoding_gets_no_audio(self):
        src = self.src
        self.assertIn("ref_video_audios=[]", src)
        self.assertIn("ref_audios=[]", src)

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
        self.assertIn('use_model_audio = (str(gp.get("audio_mode") or "使用原声") == "生成音频")', self.src)

    def test_audio_mode_has_two_choices(self):
        cfg = self.mod._parse_swap_settings("")
        self.assertEqual(cfg["gen_params"]["audio_mode"], "使用原声")
        cfg2 = self.mod._parse_swap_settings(
            '{"gen_params": {"audio_mode": "生成音频"}}')
        self.assertEqual(cfg2["gen_params"]["audio_mode"], "生成音频")
        cfg3 = self.mod._parse_swap_settings(
            '{"gen_params": {"audio_mode": "乱写"}}')
        self.assertEqual(cfg3["gen_params"]["audio_mode"], "使用原声")

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
