"""有字幕路線（crawler/subtitle_highlights.py）的單元測試，不連網。

執行（在專案根目錄）：python -m unittest tests.test_subtitle_highlights -v
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from crawler import subtitle_highlights as sh

VTT = """WEBVTT
Kind: captions
Language: zh-Hant

00:00:00.500 --> 00:00:02.000
我是&amp;雲

00:00:02.000 --> 00:00:04.000
<c.colorE5E5E5>第二句</c>
還有換行

1
00:00:04.100 --> 00:00:05.000
第二句 還有換行

00:00:10.000 --> 00:00:12.000

00:01:00.250 --> 00:01:02.000
一分鐘後
"""


def cue(i, start, end, text="句子"):
    return {"id": f"c{i:03d}", "start": start, "end": end, "text": text}


class ParseVtt(unittest.TestCase):
    def test_parse(self):
        cues = sh.parse_vtt(VTT)
        self.assertEqual([c["id"] for c in cues], ["c001", "c002", "c003"])
        self.assertEqual(cues[0], {"id": "c001", "start": 0.5, "end": 2.0, "text": "我是&雲"})
        self.assertEqual(cues[1]["text"], "第二句 還有換行")  # 標記去掉、換行合併
        self.assertEqual(cues[1]["end"], 5.0)               # 相鄰重複句合併，結束時間延長
        self.assertEqual(cues[2]["start"], 60.25)           # 空句被略過，分鐘換算正確

    def test_empty(self):
        self.assertEqual(sh.parse_vtt("WEBVTT\n\n"), [])

    def test_number_lines_inside_text_are_kept(self):
        # 字幕文字本身有一行是純數字（年份、金額）時，不能被當成 cue 編號吃掉
        vtt = "WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\n他說\n2024\n年很重要\n\n2\n00:00:02.000 --> 00:00:03.000\n100\n"
        cues = sh.parse_vtt(vtt)
        self.assertEqual([c["text"] for c in cues], ["他說 2024 年很重要", "100"])

    def test_real_cue_numbers_are_still_skipped(self):
        vtt = "WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\n甲\n\n2\n00:00:02.000 --> 00:00:03.000\n乙\n"
        self.assertEqual([c["text"] for c in sh.parse_vtt(vtt)], ["甲", "乙"])


class Safety(unittest.TestCase):
    def test_video_dir_rejects_path_tricks(self):
        for bad in ("../x", "a/b", "", "a b", ".."):
            with self.assertRaises(SystemExit, msg=bad):
                sh.video_dir(bad)
        self.assertEqual(sh.video_dir("page027").name, "page027")
        self.assertEqual(sh.video_dir("AWP_v5I0zYA-x").name, "AWP_v5I0zYA-x")

    def test_load_json_friendly_errors(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "bad.json").write_text("{not json", encoding="utf-8")
        (tmp / "list.json").write_text('["c001"]', encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "不是有效的 JSON"):
            sh.load_json(tmp / "bad.json")
        with self.assertRaisesRegex(SystemExit, "必須是物件"):
            sh.load_json(tmp / "list.json")
        with self.assertRaisesRegex(SystemExit, "找不到"):
            sh.load_json(tmp / "none.json")

    def test_non_dict_highlight_item(self):
        with self.assertRaisesRegex(ValueError, "格式不對"):
            sh.resolve_highlights(["c001"], [cue(1, 0, 2)])


# YouTube 自動字幕的真實格式：句子前有只有空白的行；每塊重複上一句，只有新句帶逐字時間標記
AUTO_VTT = (
    "WEBVTT\nKind: captions\nLanguage: zh-orig\n\n"
    "00:00:00.920 --> 00:00:02.813 align:start position:0%\n \n"
    "第一<00:00:01.235><c>句話</c><00:00:01.445><c>在這</c>\n\n"
    "00:00:02.813 --> 00:00:02.865 align:start position:0%\n第一句話在這\n \n\n"
    "00:00:02.865 --> 00:00:04.916 align:start position:0%\n第一句話在這\n"
    " 第二<00:00:03.338><c>句話</c>\n\n"
    "00:00:04.916 --> 00:00:04.968 align:start position:0%\n 第二句話\n \n"
)


class AutoCaptions(unittest.TestCase):
    def test_rolling_captions_are_deduplicated(self):
        cues = sh.parse_auto_vtt(AUTO_VTT)
        self.assertEqual([c["text"] for c in cues], ["第一句話在這", "第二句話"])
        self.assertEqual(cues[0]["start"], 0.92)          # 句子前的空白行沒有害第一句被吃掉
        self.assertEqual(cues[0]["end"], 2.865)           # 結束時間延到下一句開始
        self.assertEqual([c["id"] for c in cues], ["c001", "c002"])

    def test_without_inline_tags_falls_back_and_drops_repeats(self):
        vtt = ("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n甲\n\n"
               "00:00:02.000 --> 00:00:04.000\n甲\n\n00:00:04.000 --> 00:00:06.000\n乙\n")
        cues = sh.parse_auto_vtt(vtt)
        self.assertEqual([c["text"] for c in cues], ["甲", "乙"])

    def test_pick_auto_language(self):
        self.assertEqual(sh.pick_auto_language(["en", "zh-TW-orig", "ja-orig"]), "zh-TW-orig")
        self.assertEqual(sh.pick_auto_language(["en", "ja-orig"]), "ja-orig")
        self.assertEqual(sh.pick_auto_language(["en", "zh"], original="zh"), "zh")
        self.assertEqual(sh.pick_auto_language(["en", "zh"], wanted="en"), "en")

    def test_pick_auto_language_does_not_guess(self):
        with self.assertRaisesRegex(ValueError, "沒有 -orig"):
            sh.pick_auto_language(["en", "zh"])
        with self.assertRaises(ValueError):
            sh.pick_auto_language(["en"], wanted="ja")


class PickLanguage(unittest.TestCase):
    def test_prefers_traditional_chinese(self):
        self.assertEqual(sh.pick_language(["en", "zh-Hans", "zh-TW"]), "zh-TW")

    def test_explicit_language(self):
        self.assertEqual(sh.pick_language(["en", "zh-TW"], "en"), "en")

    def test_explicit_language_missing(self):
        with self.assertRaises(ValueError):
            sh.pick_language(["zh-TW"], "ja")

    def test_no_chinese_is_not_guessed(self):
        with self.assertRaisesRegex(ValueError, "沒有中文字幕"):
            sh.pick_language(["en", "ja"])

    def test_no_subtitles(self):
        with self.assertRaisesRegex(ValueError, "沒有上傳者"):
            sh.pick_language([])


class Chunks(unittest.TestCase):
    def test_gap_starts_new_chunk(self):
        chunks = sh.make_chunks([cue(1, 0, 2), cue(2, 2, 4), cue(3, 10, 12)])
        self.assertEqual([len(c) for c in chunks], [2, 1])

    def test_long_chunk_is_split(self):
        cues = [cue(i, i * 10, i * 10 + 9) for i in range(1, 11)]
        chunks = sh.make_chunks(cues, max_seconds=45)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(sum(len(c) for c in chunks), 10)

    def test_empty(self):
        self.assertEqual(sh.make_chunks([]), [])


class Highlights(unittest.TestCase):
    cues = [cue(1, 0, 2), cue(2, 5, 7), cue(3, 20, 22)]

    def test_resolve_sorted_by_time(self):
        items = sh.resolve_highlights([{"cue": "c003", "title": "後"}, {"cue": "c001", "title": "前"}], self.cues)
        self.assertEqual([i["cue"] for i in items], ["c001", "c003"])

    def test_unknown_cue(self):
        with self.assertRaisesRegex(ValueError, "不在字幕裡"):
            sh.resolve_highlights([{"cue": "c999", "title": "x"}], self.cues)

    def test_duplicate_cue(self):
        with self.assertRaisesRegex(ValueError, "重複"):
            sh.resolve_highlights([{"cue": "c001", "title": "a"}, {"cue": "c001", "title": "b"}], self.cues)

    def test_missing_title(self):
        with self.assertRaisesRegex(ValueError, "title"):
            sh.resolve_highlights([{"cue": "c001", "title": "  "}], self.cues)

    def test_frame_time_is_cue_midpoint(self):
        self.assertEqual(sh.frame_time(cue(1, 5, 7)), 6.0)

    def test_markdown_marks_cue_and_notes_missing_video(self):
        items = sh.resolve_highlights([{"cue": "c002", "title": "重點", "summary": "說明"}], self.cues)
        md = sh.render_highlights_md("標題", "VID", "http://x", "zh-TW", "測試", items, self.cues, {}, False)
        self.assertIn("沒有截圖", md)
        self.assertIn("## 1. 重點", md)
        self.assertIn("**句子**", md)
        with_frames = sh.render_highlights_md("標題", "VID", "", "zh-TW", "", items, self.cues,
                                              {"c002": "highlights/a.jpg"}, True)
        self.assertIn("![重點](highlights/a.jpg)", with_frames)
        self.assertNotIn("沒有截圖", with_frames)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg")
class RenderWithVideo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.video = self.tmp / "t.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=5",
                        "-pix_fmt", "yuv420p", str(self.video)], check=True)
        folder = self.tmp / "output" / "videos" / "pageT"
        folder.mkdir(parents=True)
        self.subs = {"video_id": "VID", "language": "zh-TW", "source": "uploader",
                     "cues": [cue(1, 0, 2, "第一句"), cue(2, 2, 4, "第二句")]}
        (folder / "pageT_subtitles.json").write_text(json.dumps(self.subs, ensure_ascii=False), encoding="utf-8")
        self.folder = folder

    def run_render(self, highlights, video=True):
        (self.folder / "pageT_highlights.json").write_text(json.dumps(highlights, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(sh, "ROOT", self.tmp), mock.patch.object(sh, "_read_meta", return_value=("影片標題", "http://x")):
            return sh.cmd_render(Namespace(id="pageT", video=self.video if video else None, highlights=None))

    def test_renders_frames_and_markdown(self):
        self.run_render({"note": "測試", "highlights": [{"cue": "c001", "title": "甲"}, {"cue": "c002", "title": "乙"}]})
        frames = sorted((self.folder / "highlights").glob("hl_*.jpg"))
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(f.stat().st_size > 1000 for f in frames))
        md = (self.folder / "pageT_highlights.md").read_text(encoding="utf-8")
        self.assertIn("![甲](highlights/", md)
        self.assertIn("影片標題", md)

    def test_rerun_replaces_old_frames(self):
        self.run_render({"highlights": [{"cue": "c001", "title": "甲"}, {"cue": "c002", "title": "乙"}]})
        self.run_render({"highlights": [{"cue": "c002", "title": "乙"}]})
        self.assertEqual(len(list((self.folder / "highlights").glob("hl_*.jpg"))), 1)

    def test_video_shorter_than_subtitles_is_rejected(self):
        self.subs["cues"].append(cue(3, 100, 102, "太晚"))
        (self.folder / "pageT_subtitles.json").write_text(json.dumps(self.subs, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "超過影片長度"):
            self.run_render({"highlights": [{"cue": "c003", "title": "太晚"}]})

    def test_failure_midway_keeps_old_frames(self):
        self.run_render({"highlights": [{"cue": "c001", "title": "甲"}, {"cue": "c002", "title": "乙"}]})
        before = sorted(p.name for p in (self.folder / "highlights").glob("hl_*.jpg"))
        real = sh.grab_frame
        calls = []

        def flaky(video, seconds, out):
            calls.append(seconds)
            if len(calls) == 2:
                raise RuntimeError("模擬 ffmpeg 失敗")
            return real(video, seconds, out)

        with mock.patch.object(sh, "grab_frame", flaky):
            with self.assertRaisesRegex(SystemExit, "模擬 ffmpeg 失敗"):
                self.run_render({"highlights": [{"cue": "c001", "title": "甲"}, {"cue": "c002", "title": "乙"}]})
        after = sorted(p.name for p in (self.folder / "highlights").glob("hl_*.jpg"))
        self.assertEqual(before, after)                               # 舊截圖還在
        self.assertEqual([p.name for p in self.folder.glob(".hl_tmp_*")], [])  # 暫存資料夾已清掉

    def test_video_id_mismatch_is_rejected(self):
        with self.assertRaisesRegex(SystemExit, "video_id"):
            self.run_render({"video_id": "OTHER", "highlights": [{"cue": "c001", "title": "甲"}]})

    def test_title_and_url_prefer_subtitle_file(self):
        self.subs.update(title="字幕檔裡的標題", url="http://from-subs")
        (self.folder / "pageT_subtitles.json").write_text(json.dumps(self.subs, ensure_ascii=False), encoding="utf-8")
        self.run_render({"highlights": [{"cue": "c001", "title": "甲"}]}, video=False)
        md = (self.folder / "pageT_highlights.md").read_text(encoding="utf-8")
        self.assertIn("# 字幕檔裡的標題｜重點", md)
        self.assertIn("http://from-subs", md)

    def test_without_video_still_writes_markdown(self):
        self.run_render({"highlights": [{"cue": "c001", "title": "甲"}]}, video=False)
        md = (self.folder / "pageT_highlights.md").read_text(encoding="utf-8")
        self.assertIn("沒有截圖", md)
        self.assertFalse((self.folder / "highlights").exists())


if __name__ == "__main__":
    unittest.main()
