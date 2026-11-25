import importlib
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _load_mods():
    try:
        sys.path.insert(0, r"Z:\AI_work\ComfyUI")
        import folder_paths  # noqa: F401
    except Exception:
        return None, None
    import types
    if "jzlswap" not in sys.modules:
        pkg = types.ModuleType("jzlswap")
        pkg.__path__ = [_ROOT]
        sys.modules["jzlswap"] = pkg
    try:
        return (importlib.import_module("jzlswap.nodes_character_swap"),
                importlib.import_module("jzlswap.nodes_ref2va_bus"))
    except Exception:
        return None, None


_MOD, _BUS = _load_mods()


@unittest.skipIf(_MOD is None, "需要 ComfyUI 环境才能加载 nodes_character_swap")
class VideoEditSchemaTest(unittest.TestCase):
    """🎭 视频编辑：与人物替换 1:1，但素材只从一条总线进（不再有 image/video/audio 端口）。"""

    def _inputs(self):
        schema = _MOD.JZL_MiniMaxVideoEdit.define_schema()
        out = {}
        for inp in schema.inputs:
            # io.Xxx.Input(id, display_name=…)：id 是第一个位置参数
            out[getattr(inp, "id", None) or getattr(inp, "name", None)] = inp
        return out

    def test_no_media_ports(self):
        ins = self._inputs()
        for gone in ("source_video", "source_audio", "ref_images", "ref_image"):
            self.assertNotIn(gone, ins, f"视频编辑不应再有 {gone} 端口（素材走总线）")

    def test_bus_port_present(self):
        ins = self._inputs()
        self.assertIn("bus", ins)
        bus = ins["bus"]
        t = str(getattr(bus, "type", "") or getattr(bus, "io_type", ""))
        self.assertIn("JZL_REF2VA_BUS", t)

    def test_keeps_model_and_prompt_ports(self):
        ins = self._inputs()
        for kept in ("model", "clip", "vae", "audio_vae", "story_name",
                     "manager_settings", "video_desc", "image_desc"):
            self.assertIn(kept, ins, f"视频编辑应保留 {kept}")

    def test_same_engine_as_character_swap(self):
        # 1:1：视频编辑直接继承人物替换 —— 只允许重写「schema / execute（切日志标签）/ 落盘兜底名」，
        # 一旦有人在这里另起一套采样/落盘流程，这条断言会立刻失败。
        self.assertTrue(issubclass(_MOD.JZL_MiniMaxVideoEdit, _MOD.JZL_MiniMaxCharacterSwap))
        own = {k for k in _MOD.JZL_MiniMaxVideoEdit.__dict__ if not k.startswith("__")}
        self.assertLessEqual(own, {"OUT_FALLBACK", "define_schema", "execute"}, own)
        self.assertIsNot(_MOD.JZL_MiniMaxVideoEdit.execute, _MOD.JZL_MiniMaxCharacterSwap.execute)

    def test_default_prefix_is_video_edit(self):
        self.assertEqual(_MOD.JZL_MiniMaxVideoEdit.OUT_FALLBACK, "视频编辑")
        self.assertEqual(_MOD.DEFAULT_VIDEO_EDIT_OUT_PREFIX, "视频/视频编辑")


@unittest.skipIf(_MOD is None or _BUS is None, "需要 ComfyUI 环境才能加载节点模块")
class BusUnpackTest(unittest.TestCase):
    """内部集成「ref2va 参考总线（解包）」：总线拆成 源视频 / 源原声 / 定妆图。"""

    def test_bus_keys_are_in_sync(self):
        expected = (tuple(f"ref_image_{i}" for i in range(9))
                    + tuple(f"ref_video_{i}" for i in range(3))
                    + tuple(f"ref_video_audio_{i}" for i in range(3))
                    + tuple(f"ref_audio_{i}" for i in range(3)))
        self.assertEqual(tuple(_BUS.BUS_KEYS), expected)
        got = (_MOD._BUS_IMAGE_KEYS + _MOD._BUS_VIDEO_KEYS
               + _MOD._BUS_VIDEO_AUDIO_KEYS + _MOD._BUS_AUDIO_KEYS)
        self.assertEqual(got, expected)

    def test_pick_first_video_and_its_audio(self):
        bus = {
            "ref_image_0": "IMG0", "ref_image_1": "IMG1",
            "ref_video_0": None,
            "ref_video_1": "VIDEO1", "ref_video_audio_1": "AUDIO1",
            "ref_video_2": "VIDEO2", "ref_video_audio_2": "AUDIO2",
        }
        video, audio, imgs, slot = _MOD._bus_media(bus)
        self.assertEqual(video, "VIDEO1")
        self.assertEqual(audio, "AUDIO1")
        self.assertEqual(slot, 1)
        self.assertEqual(imgs["ref_image_0"], "IMG0")
        self.assertEqual(imgs["ref_image_1"], "IMG1")
        self.assertIsNone(imgs["ref_image_2"])

    def test_empty_bus_is_safe(self):
        for bad in (None, {}, [], "not-a-dict"):
            video, audio, imgs, slot = _MOD._bus_media(bad)
            self.assertIsNone(video)
            self.assertIsNone(audio)
            self.assertEqual(slot, -1)
            self.assertEqual(len(imgs), 9)
            self.assertTrue(all(v is None for v in imgs.values()))

    def test_video_without_audio(self):
        video, audio, _imgs, slot = _MOD._bus_media({"ref_video_0": "V0"})
        self.assertEqual(video, "V0")
        self.assertIsNone(audio)
        self.assertEqual(slot, 0)


@unittest.skipIf(_MOD is None, "需要 ComfyUI 环境才能加载 nodes_character_swap")
class OutPrefixFallbackTest(unittest.TestCase):
    """「分段输出」被清空时的兜底文件名按节点区分（人物替换 / 视频编辑）。"""

    def test_fallback_is_per_node(self):
        root = os.path.abspath(os.sep)
        sub, name = _MOD._resolve_out_prefix("", root, "视频编辑")
        self.assertEqual(name, "视频编辑")
        self.assertEqual(sub, root)
        _sub2, name2 = _MOD._resolve_out_prefix("", root)
        self.assertEqual(name2, "人物替换")

    def test_normal_prefix_still_works(self):
        root = os.path.abspath(os.sep)
        sub, name = _MOD._resolve_out_prefix("视频/视频编辑", root)
        self.assertEqual(name, "视频编辑")
        self.assertTrue(sub.endswith("视频"))

    def test_traversal_is_stripped(self):
        root = os.path.abspath(os.sep)
        sub, name = _MOD._resolve_out_prefix("../..\\C:/视频/剪辑", root)
        self.assertEqual(name, "剪辑")
        self.assertTrue(os.path.abspath(sub).startswith(root))


if __name__ == "__main__":
    unittest.main()
