"""自動字幕比較工具（crawler/caption_compare.py）的單元測試，不連網。

執行（在專案根目錄）：python -m unittest tests.test_caption_compare -v
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crawler import caption_compare as cc


def cue(i, start, end, text):
    return {"id": f"c{i:03d}", "start": start, "end": end, "text": text}


class Basics(unittest.TestCase):
    def test_normalize_drops_punctuation_and_case(self):
        self.assertEqual(cc.normalize("Hello, 世界！ A_b"), "hello世界ab")

    def test_normalize_uses_converter(self):
        self.assertEqual(cc.normalize("甲乙", lambda s: s.replace("甲", "丙")), "丙乙")

    def test_auto_text_uses_cue_midpoint_and_order(self):
        cues = [cue(1, 0, 2, "甲"), cue(2, 2, 4, "乙"), cue(3, 10, 12, "丙")]
        self.assertEqual(cc.auto_text_for(cues, 0, 4), "甲 乙")
        self.assertEqual(cc.auto_text_for(cues, 9.8, 12.5), "丙")
        self.assertEqual(cc.auto_text_for(cues, 5, 8), "")

    def test_diff_spans(self):
        self.assertEqual(cc.diff_spans("水庫完工", "水庫完成"), [("工", "成")])
        self.assertEqual(cc.diff_spans("abc", "abc"), [])


class Compare(unittest.TestCase):
    chunks = [
        {"start": 0, "end": 4, "final": "石門水庫今天開始洩洪。"},
        {"start": 5, "end": 9, "final": "水庫就要做了"},
        {"start": 10, "end": 14, "final": "沒有對應的自動字幕"},
        {"start": 20, "end": 24, "final": ""},
    ]
    cues = [cue(1, 0, 4, "石門水庫今天開始洩洪"), cue(2, 5, 9, "水庫就要坐了啦"), cue(3, 20, 24, "有字幕但語音沒字")]

    def test_flags(self):
        rows = cc.compare(self.chunks, self.cues)
        self.assertEqual([r["flag"] for r in rows], ["agree", "differ", "auto_missing", "asr_missing"])
        self.assertEqual(rows[0]["ratio"], 1.0)
        self.assertTrue(rows[1]["spans"])

    def test_threshold_is_adjustable(self):
        rows = cc.compare(self.chunks, self.cues, agree_ratio=0.5)
        self.assertEqual(rows[1]["flag"], "agree")

    def test_markdown_lists_only_problem_rows(self):
        rows = cc.compare(self.chunks, self.cues)
        md = cc.render_markdown("標題", "VID", "zh-orig", rows, converted=False)
        self.assertIn("沒有安裝 opencc", md)
        self.assertIn("自動字幕沒有這一段", md)
        self.assertIn("FunASR 沒有文字", md)
        self.assertIn("水庫就要做了", md)
        self.assertNotIn("今天開始洩洪。", md)       # 一致的不列出

    def test_markdown_when_all_agree(self):
        rows = cc.compare(self.chunks[:1], self.cues)
        self.assertIn("沒有不一樣的段落", cc.render_markdown("標題", "VID", "zh", rows, converted=True))


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.folder = self.tmp / "output" / "videos" / "pageX"
        self.folder.mkdir(parents=True)
        (self.folder / "pageX_funasr_glossary_raw.json").write_text(
            json.dumps({"chunks": Compare.chunks}, ensure_ascii=False), encoding="utf-8")

    def run_cli(self):
        with mock.patch.object(cc, "ROOT", self.tmp), mock.patch.object(cc, "_read_meta", return_value=("標題", "")), \
                mock.patch.object(sys, "argv", ["caption_compare", "--id", "pageX"]):
            return cc.main()

    def test_end_to_end(self):
        (self.folder / "pageX_auto_captions.json").write_text(
            json.dumps({"video_id": "VID", "language": "zh-orig", "cues": Compare.cues}, ensure_ascii=False),
            encoding="utf-8")
        self.assertEqual(self.run_cli(), 0)
        md = (self.folder / "pageX_auto_vs_asr.md").read_text(encoding="utf-8")
        self.assertIn("自動字幕語言：zh-orig", md)

    def test_missing_auto_file_gives_clear_message(self):
        with self.assertRaisesRegex(SystemExit, "找不到自動字幕檔"):
            self.run_cli()


if __name__ == "__main__":
    unittest.main()
