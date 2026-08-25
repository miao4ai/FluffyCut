"""核心不变量的回归测试：python -m unittest discover -s tests

只测纯逻辑（不跑 ffmpeg），一秒内跑完。
"""

import unittest
from pathlib import Path

from core import fonts, layers, subtitle
from core.project import Clip, Project, ProjectError, estimate_duration
from core.render import RenderOptions, build_command
from core.tts import is_stale, text_sha

DEMO = Path(__file__).resolve().parent.parent / "projects" / "demo" / "project.json"


def demo() -> Project:
    return Project.load(DEMO)


class TestTimeline(unittest.TestCase):
    def test_duration_priority(self):
        """显式 duration > 音频时长 > 字数估算。"""
        from core.project import Audio

        c = Clip(id="c1", text="一二三四五六七八九十")
        self.assertAlmostEqual(c.seconds, estimate_duration(c.text))

        c.audio = Audio(path="a.aiff", duration=3.0)
        self.assertAlmostEqual(c.seconds, 3.35)     # 音频 + 换气

        c.duration = 1.25
        self.assertAlmostEqual(c.seconds, 1.25)

    def test_timeline_is_contiguous(self):
        """时间轴必须首尾相接、无缝无叠，否则字幕会漂。"""
        p = demo()
        tl = p.timeline()
        self.assertAlmostEqual(tl[0][1], 0.0)
        for (_c, _s, end), (_c2, start, _e) in zip(tl, tl[1:]):
            self.assertAlmostEqual(end, start, places=3)
        self.assertAlmostEqual(tl[-1][2], p.duration, places=3)

    def test_pace_labels(self):
        self.assertEqual(Clip(id="x", duration=1.0).pace, "fast")
        self.assertEqual(Clip(id="x", duration=2.2).pace, "ok")
        self.assertEqual(Clip(id="x", duration=5.0).pace, "slow")


class TestProjectIO(unittest.TestCase):
    def test_roundtrip_is_stable(self):
        """读 -> 写 -> 读，内容不变。project.json 要能安心进 git。"""
        p = demo()
        once = p.to_dict()
        twice = Project.from_dict(once, p.path).to_dict()
        self.assertEqual(once, twice)

    def test_unknown_fields_survive(self):
        p = Project.from_dict({"title": "t", "clips": [], "my_note": "别丢我"})
        self.assertEqual(p.to_dict()["my_note"], "别丢我")

    def test_duplicate_ids_get_fixed(self):
        p = Project.from_dict({"clips": [{"id": "a"}, {"id": "a"}]})
        self.assertNotEqual(p.clips[0].id, p.clips[1].id)

    def test_missing_asset_is_a_problem(self):
        p = demo()
        p.clips[0].visual.path = "assets/不存在.png"
        self.assertTrue(any("找不到" in x for x in p.validate()))

    def test_bad_visual_type_rejected(self):
        with self.assertRaises(ProjectError):
            Project.from_dict({"clips": [{"id": "a", "visual": {"type": "hologram"}}]})


class TestLayers(unittest.TestCase):
    def test_wrap_breaks_cjk_and_keeps_words(self):
        font = fonts.load(None, 40)
        lines = layers.wrap("hello world 这是一句很长很长很长很长的中文台词", font, 300)
        self.assertGreater(len(lines), 1)
        self.assertNotIn("hel lo", " ".join(lines))

    def test_wrap_ellipsizes(self):
        font = fonts.load(None, 40)
        lines = layers.wrap("很长" * 50, font, 300, max_lines=2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("…"))

    def test_kenburns_curve_matches_both_ends(self):
        kb = layers.kenburns_for(Clip(id="c1", kenburns="in"), 0.12)
        self.assertAlmostEqual(kb.zoom_at(0, 60), 1.0)
        self.assertAlmostEqual(kb.zoom_at(59, 60), 1.12)
        out = layers.kenburns_for(Clip(id="c1", kenburns="out"), 0.12)
        self.assertAlmostEqual(out.zoom_at(0, 60), 1.12)
        self.assertAlmostEqual(out.zoom_at(59, 60), 1.0)

    def test_kenburns_direction_is_deterministic(self):
        a = layers.kenburns_for(Clip(id="c7", kenburns=True))
        b = layers.kenburns_for(Clip(id="c7", kenburns=True))
        self.assertEqual(a.direction, b.direction)

    def test_frame_size_matches_project(self):
        p = demo()
        img = layers.compose_frame(p, p.clips[0], 0.0)
        self.assertEqual(img.size, (p.video.width, p.video.height))


class TestRenderCommand(unittest.TestCase):
    def test_command_shape(self):
        import tempfile

        p = demo()
        with tempfile.TemporaryDirectory() as tmp:
            cmd = build_command(p, Path(tmp) / "o.mp4", Path(tmp), RenderOptions())
        joined = " ".join(cmd)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn(f"concat=n={len(p.clips)}:v=1:a=0", graph)
        self.assertIn(f"concat=n={len(p.clips)}:v=0:a=1", graph)
        self.assertIn("format=yuv420p", graph)
        self.assertIn("-pix_fmt yuv420p", joined)

    def test_identical_text_reuses_one_subtitle_png(self):
        import tempfile

        from core.render import _subtitle_pngs

        p = Project.from_dict({"clips": [{"id": "a", "text": "同一句"}, {"id": "b", "text": "同一句"}]})
        with tempfile.TemporaryDirectory() as tmp:
            pngs = _subtitle_pngs(p, Path(tmp))
            self.assertEqual(pngs["a"], pngs["b"])


class TestSubtitle(unittest.TestCase):
    def test_ass_timing_matches_timeline(self):
        p = demo()
        lines = [l for l in subtitle.to_ass(p).splitlines() if l.startswith("Dialogue:")]
        self.assertEqual(len(lines), len([c for c in p.clips if c.text.strip()]))
        self.assertIn("0:00:00.00", lines[0])

    def test_ass_color_conversion(self):
        self.assertEqual(subtitle._ass_color("#FFE100"), "&H0000E1FF")


class TestTTSStaleness(unittest.TestCase):
    def test_stale_detection(self):
        from core.project import Audio

        c = Clip(id="c1", text="原来的台词")
        self.assertTrue(is_stale(c))                       # 压根没配音
        c.audio = Audio(path="a.aiff", duration=1.0, text_sha=text_sha(c.text))
        self.assertFalse(is_stale(c))
        c.text = "改过的台词"
        self.assertTrue(is_stale(c))                       # 改了字就过期


if __name__ == "__main__":
    unittest.main()


class TestShots(unittest.TestCase):
    """一句话切多个镜头。"""

    def _spans(self, visuals, duration=3.0, fps=30):
        """返回 (project, clip, 各镜头时长)。分镜边界由 project 算，它才知道 fps。"""
        p = Project.from_dict({
            "video": {"fps": fps},
            "clips": [{"id": "c1", "duration": duration, "visual": visuals}],
        })
        c = p.clips[0]
        return p, c, [round(d, 3) for _v, _s, d in p.shots_of(c)]

    def _clip(self, visuals, duration=3.0):
        return Clip.from_dict({"id": "c1", "duration": duration, "visual": visuals}, 0)

    def test_free_shots_split_evenly(self):
        _p, _c, spans = self._spans([{"type": "color"}] * 3, 3.0)
        self.assertEqual(spans, [1.0, 1.0, 1.0])

    def test_fixed_shot_takes_its_seconds(self):
        _p, _c, spans = self._spans([{"type": "color"}, {"type": "color", "seconds": 2.0}], 3.0)
        self.assertEqual(spans, [1.0, 2.0])

    def test_shots_always_sum_to_clip_duration(self):
        """镜头加起来必须正好是本句的帧数，差一帧时间轴就开始漂。"""
        for n in (1, 3, 7):
            p, c, spans = self._spans([{"type": "color"}] * n, 2.53)
            self.assertEqual(sum(round(d * 30) for d in spans), p.frames_of(c))

    def test_shots_get_at_least_one_frame(self):
        """镜头比帧还多也不能出现 0 帧的段，ffmpeg 接不住。"""
        p, c, spans = self._spans([{"type": "color"}] * 5, 0.1)
        self.assertTrue(all(d > 0 for d in spans))
        self.assertEqual(len(spans), 5)

    def test_single_shot_writes_back_as_object(self):
        one = self._clip([{"type": "color"}]).to_dict()["visual"]
        many = self._clip([{"type": "color"}, {"type": "color"}]).to_dict()["visual"]
        self.assertIsInstance(one, dict)
        self.assertIsInstance(many, list)

    def test_shot_at_picks_the_right_one(self):
        p, c, _spans = self._spans([{"type": "color", "color": "#111111"},
                                    {"type": "color", "color": "#222222"}], 2.0)
        self.assertEqual(layers.shot_at(p, c, 0.5)[0].color, "#111111")
        self.assertEqual(layers.shot_at(p, c, 1.5)[0].color, "#222222")

    def test_video_trim_validation(self):
        with self.assertRaises(ProjectError):
            Clip.from_dict({"id": "c", "visual": {"type": "video", "path": "a.mp4",
                                                  "in": 5, "out": 2}}, 0)


class TestTransitions(unittest.TestCase):
    def test_last_clip_never_has_one(self):
        p = Project.from_dict({"clips": [{"id": "a", "transition": "fade"},
                                         {"id": "b", "transition": "fade"}]})
        self.assertIsNone(p.clips[-1].transition)
        self.assertIsNone(p.transition_after(1))

    def test_duration_is_clamped_to_half_the_shorter_clip(self):
        p = Project.from_dict({"clips": [
            {"id": "a", "duration": 2.0, "transition": {"type": "fade", "duration": 9.0}},
            {"id": "b", "duration": 1.0},
        ]})
        self.assertEqual(p.transition_after(0).duration, 0.5)   # min(2,1)/2

    def test_unknown_transition_rejected(self):
        with self.assertRaises(ProjectError):
            Project.from_dict({"clips": [{"id": "a", "transition": "explode"},
                                         {"id": "b"}]})

    def test_timeline_unchanged_by_transitions(self):
        """加转场不能改变任何一句的起点 —— 这是字幕不漂的根本保证。"""
        base = {"clips": [{"id": "a", "duration": 2.0}, {"id": "b", "duration": 2.0},
                          {"id": "c", "duration": 2.0}]}
        plain = Project.from_dict(base)
        withtrans = Project.from_dict({"clips": [
            {"id": "a", "duration": 2.0, "transition": "fade"},
            {"id": "b", "duration": 2.0, "transition": {"type": "dissolve", "duration": 0.5}},
            {"id": "c", "duration": 2.0},
        ]})
        self.assertEqual([s for _c, s, _e in plain.timeline()],
                         [s for _c, s, _e in withtrans.timeline()])
        self.assertEqual(plain.duration, withtrans.duration)


class TestRenderGraph(unittest.TestCase):
    """检查滤镜图的关键结构，比跑一次 ffmpeg 快得多。"""

    def _graph(self, data, voice=None):
        import tempfile

        p = Project.from_dict(data, DEMO)
        with tempfile.TemporaryDirectory() as tmp:
            cmd = build_command(p, Path(tmp) / "o.mp4", Path(tmp), RenderOptions(), voice)
        return p, cmd, cmd[cmd.index("-filter_complex") + 1]

    def test_xfade_offset_is_the_next_clip_start(self):
        p, _cmd, graph = self._graph({"clips": [
            {"id": "a", "duration": 2.0, "transition": {"type": "fade", "duration": 0.4}},
            {"id": "b", "duration": 3.0, "transition": {"type": "dissolve", "duration": 0.5}},
            {"id": "c", "duration": 2.0},
        ]})
        self.assertIn("xfade=transition=fade:duration=0.400:offset=2.000", graph)
        self.assertIn("xfade=transition=dissolve:duration=0.500:offset=5.000", graph)

    def test_transition_extends_only_the_outgoing_clip(self):
        """上一句多渲一个转场的长度，字幕却只显示到本句结束。"""
        _p, _cmd, graph = self._graph({"clips": [
            {"id": "a", "duration": 2.0, "transition": {"type": "fade", "duration": 0.4}},
            {"id": "b", "duration": 2.0},
        ]})
        self.assertIn("trim=duration=2.400", graph)      # 画面多渲 0.4s
        self.assertIn("enable='lt(t,2.000)'", graph)     # 字幕只到 2.0s

    def test_no_transition_means_one_concat(self):
        _p, _cmd, graph = self._graph({"clips": [{"id": "a"}, {"id": "b"}, {"id": "c"}]})
        self.assertIn("concat=n=3:v=1:a=0", graph)
        self.assertNotIn("xfade", graph)

    def test_video_shot_seeks_and_limits_read(self):
        p, cmd, _graph = self._graph({"clips": [
            {"id": "a", "duration": 2.0,
             "visual": {"type": "video", "path": "assets/clip.mp4", "in": 4.0, "out": 6.0}},
        ]})
        self.assertIn("-ss", cmd)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "4.000")
        self.assertEqual(cmd[cmd.index("-t") + 1], "2.000")

    def test_speed_and_freeze_on_short_source(self):
        _p, _cmd, graph = self._graph({"clips": [
            {"id": "a", "duration": 4.0,
             "visual": {"type": "video", "path": "x.mp4", "in": 0, "out": 1.0, "speed": 2.0}},
        ]})
        self.assertIn("setpts=PTS/2.0", graph)
        self.assertIn("tpad=stop_mode=clone", graph)      # 素材只够 0.5s，其余定格

    def test_music_chain_and_ducking(self):
        _p, _cmd, graph = self._graph(
            {"music": {"path": "assets/bgm.m4a", "volume": 0.2},
             "clips": [{"id": "a", "duration": 2.0}]},
            voice=Path("/tmp/voice.wav"),
        )
        self.assertIn("volume=0.200", graph)
        self.assertIn("sidechaincompress", graph)
        self.assertIn("amix=inputs=2", graph)
        # 两路旁链都必须固定帧长，否则同一份工程渲两次音轨不一样
        self.assertEqual(graph.count("asetnsamples=n=1024"), 2)
        self.assertNotIn("asplit", graph)

    def test_ducking_without_prerendered_voice_is_refused(self):
        """宁可报错也不要悄悄退化成不确定的图。"""
        with self.assertRaises(ProjectError):
            self._graph({"music": {"path": "assets/bgm.m4a"},
                         "clips": [{"id": "a", "duration": 2.0}]})

    def test_voice_track_is_rendered_separately_for_ducking(self):
        from core.render import build_voice_command

        p = Project.from_dict({"clips": [{"id": "a", "duration": 2.0},
                                         {"id": "b", "duration": 1.0}]}, DEMO)
        cmd = build_voice_command(p, Path("/tmp/v.wav"))
        self.assertIn("concat=n=2:v=0:a=1[voice]", cmd[cmd.index("-filter_complex") + 1])
        self.assertIn("pcm_f32le", cmd)

    def test_music_without_ducking_is_a_plain_mix(self):
        _p, _cmd, graph = self._graph({
            "music": {"path": "assets/bgm.m4a", "duck": False},
            "clips": [{"id": "a", "duration": 2.0}],
        })
        self.assertNotIn("sidechaincompress", graph)
        self.assertIn("amix=inputs=2", graph)

    def test_per_clip_gain(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "v.aiff").write_bytes(b"")   # 只判断存在，内容无所谓
            project = Project.from_dict(
                {"clips": [{"id": "a", "duration": 2.0,
                            "audio": {"path": "assets/v.aiff", "gain": 0.5}}]},
                root / "project.json",
            )
            cmd = build_command(project, root / "o.mp4", root, RenderOptions())
        self.assertIn("volume=0.500", cmd[cmd.index("-filter_complex") + 1])
