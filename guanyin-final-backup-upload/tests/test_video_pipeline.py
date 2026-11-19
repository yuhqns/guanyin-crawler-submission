"""影片總入口（crawler/video_pipeline.py）的單元測試：只盤點檔案，不連網、不執行任何處理。

執行（在專案根目錄）：python -m unittest tests.test_video_pipeline -v
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from crawler import video_pipeline as vp
from crawler import video_triage as vt


def row(page_id, route, video_id="VID"):
    return {"id": page_id, "video_id": video_id, "type": vt.TYPE_VIDEO, "route": route,
            "subtype": "測試", "title": "標題", "reason": "理由"}


class Inspect(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def touch(self, page_id, name, text="x"):
        folder = self.root / "output" / "videos" / page_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def labels(self, result):
        return [(s["label"], s["done"]) for s in result["stages"]]

    def test_subtitled_nothing_done(self):
        result = vp.inspect(row("p1", vt.ROUTE_SUBTITLED), self.root)
        self.assertEqual(result["next"]["label"], "字幕")
        self.assertIn("fetch --id p1", result["next"]["command"])
        self.assertTrue(all(not done for _, done in self.labels(result)))

    def test_subtitled_progress_moves_next_step(self):
        self.touch("p1", "p1_subtitles.json")
        self.assertEqual(vp.inspect(row("p1", vt.ROUTE_SUBTITLED), self.root)["next"]["label"], "重點")
        self.touch("p1", "p1_highlights.json")
        self.assertEqual(vp.inspect(row("p1", vt.ROUTE_SUBTITLED), self.root)["next"]["label"], "重點輸出")
        self.touch("p1", "p1_highlights.md")
        nxt = vp.inspect(row("p1", vt.ROUTE_SUBTITLED), self.root)["next"]
        self.assertEqual(nxt["label"], "截圖")
        self.assertIn("需要影片檔", nxt["command"])   # 沒有影片檔就提醒，不假裝能跑

    def test_subtitled_complete(self):
        for name in ("p1_subtitles.json", "p1_highlights.json", "p1_highlights.md", "highlights/hl_01_00-00-01.jpg"):
            self.touch("p1", name)
        result = vp.inspect(row("p1", vt.ROUTE_SUBTITLED), self.root)
        self.assertIsNone(result["next"])
        self.assertEqual(result["stages"][-1]["detail"], "1 張")

    def test_screenshot_count_is_checked_against_highlights(self):
        self.touch("p1", "p1_subtitles.json")
        self.touch("p1", "p1_highlights.json", '{"highlights": [{"cue": "c001"}, {"cue": "c002"}]}')
        self.touch("p1", "p1_highlights.md")
        self.touch("p1", "highlights/hl_01_00-00-01.jpg")
        result = vp.inspect(row("p1", vt.ROUTE_SUBTITLED), self.root)
        self.assertEqual(result["next"]["label"], "截圖")              # 只有 1/2 張，不算完成
        self.assertEqual(result["stages"][-1]["detail"], "1/2 張")
        self.touch("p1", "highlights/hl_02_00-00-02.jpg")
        self.assertIsNone(vp.inspect(row("p1", vt.ROUTE_SUBTITLED), self.root)["next"])

    def test_broken_highlights_file_does_not_crash(self):
        self.touch("p1", "p1_highlights.json", "not json")
        self.assertEqual(vp._highlight_count(self.root / "output/videos/p1/p1_highlights.json"), 0)
        self.assertEqual(vp._highlight_count(self.root / "nope.json"), 0)

    def test_highlight_output_command_includes_video_when_present(self):
        (self.root / "local_videos").mkdir()
        (self.root / "local_videos" / "VID.mp4").write_bytes(b"x")
        self.touch("p1", "p1_subtitles.json")
        self.touch("p1", "p1_highlights.json", '{"highlights": [{"cue": "c001"}]}')
        nxt = vp.inspect(row("p1", vt.ROUTE_SUBTITLED), self.root)["next"]
        self.assertEqual(nxt["label"], "重點輸出")
        self.assertIn("--video", nxt["command"])

    def test_video_present_changes_hint(self):
        (self.root / "local_videos").mkdir()
        (self.root / "local_videos" / "VID.mp4").write_bytes(b"x")
        for name in ("p1_subtitles.json", "p1_highlights.json", "p1_highlights.md"):
            self.touch("p1", name)
        nxt = vp.inspect(row("p1", vt.ROUTE_SUBTITLED), self.root)["next"]
        self.assertNotIn("需要影片檔", nxt["command"])

    def test_no_subtitle_route_order(self):
        result = vp.inspect(row("p2", vt.ROUTE_NO_SUBTITLES), self.root)
        self.assertEqual([s["label"] for s in result["stages"]],
                         ["畫面字幕 OCR", "FunASR 語音辨識", "人工確認網頁", "確認版逐字稿"])
        self.assertIn("subtitle_ocr --video-id VID", result["next"]["command"])
        self.touch("p2", "p2_ocr.md")
        self.assertEqual(vp.inspect(row("p2", vt.ROUTE_NO_SUBTITLES), self.root)["next"]["label"], "FunASR 語音辨識")
        self.touch("p2", "p2_funasr_raw.json")           # 沒有詞庫版時也算
        self.assertEqual(vp.inspect(row("p2", vt.ROUTE_NO_SUBTITLES), self.root)["next"]["label"], "人工確認網頁")
        self.touch("p2", "p2_v2_review.html")
        self.assertEqual(vp.inspect(row("p2", vt.ROUTE_NO_SUBTITLES), self.root)["next"]["label"], "確認版逐字稿")

    def test_confirmed_makes_review_page_optional_and_reports_open_questions(self):
        self.touch("p2", "p2_ocr.md")
        self.touch("p2", "p2_funasr_glossary_raw.json")
        self.touch("p2", "p2_confirmed.md", "# 標題\n\n- 採用順序：人工 8 個、AI 建議 121 個、沒有答案 30 個（保留原本判定）\n")
        result = vp.inspect(row("p2", vt.ROUTE_NO_SUBTITLES), self.root)
        self.assertIsNone(result["next"])
        self.assertIn("還有 30 處沒人決定", result["stages"][-1]["detail"])

    def test_no_open_questions_when_zero(self):
        confirmed = self.touch("p2", "p2_confirmed.md", "- 沒有答案 0 個\n")
        self.assertEqual(vp._open_questions(confirmed), "")

    def test_auto_route_adds_two_stages(self):
        result = vp.inspect(row("p3", vt.ROUTE_AUTO_COMPARE), self.root)
        self.assertEqual([s["label"] for s in result["stages"]][-2:], ["自動字幕", "自動字幕比較"])

    def test_unknown_route_is_not_guessed(self):
        result = vp.inspect(row("p4", vt.ROUTE_RETRY), self.root)
        self.assertEqual(result["stages"], [])
        self.assertEqual(result["next"]["label"], "無法自動判斷")


class ReadAndRender(unittest.TestCase):
    def test_missing_triage_file(self):
        with self.assertRaisesRegex(SystemExit, "video_triage"):
            vp.read_triage(Path("/nonexistent/video_triage.csv"))

    def test_only_video_rows_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.csv"
            vt.write_rows(path, [{**{k: "" for k in vt.FIELDS}, "id": "page001", "type": vt.TYPE_WEB},
                                 {**{k: "" for k in vt.FIELDS}, "id": "page002", "type": vt.TYPE_VIDEO}])
            self.assertEqual([r["id"] for r in vp.read_triage(path)], ["page002"])

    def test_render_marks_done_and_next(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = vp.inspect(row("p1", vt.ROUTE_SUBTITLED), Path(tmp))
            text = vp.render([result])
        self.assertIn("✘ 字幕", text)
        self.assertIn("**下一步：字幕**", text)


if __name__ == "__main__":
    unittest.main()
