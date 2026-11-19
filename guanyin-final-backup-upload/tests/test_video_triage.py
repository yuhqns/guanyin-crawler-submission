"""影片分流器的單元測試（不連網，用假的 yt-dlp 資料）。

執行（在專案根目錄）：python -m unittest tests.test_video_triage -v
"""
import unittest

from crawler import video_triage as vt

TRACK = [{"ext": "vtt", "url": "x"}]


def info(**kw):
    base = {"id": "XXXXXXXXXXX", "title": "範例標題", "channel": "範例頻道", "duration": 120,
            "subtitles": {}, "automatic_captions": {}}
    base.update(kw)
    return base


class ClassifyInfo(unittest.TestCase):
    def test_uploader_subtitles(self):
        r = vt.classify_info(info(subtitles={"zh-TW": TRACK, "ja": TRACK}))
        self.assertEqual((r["subtype"], r["route"]), (vt.SUB_UPLOADER, vt.ROUTE_SUBTITLED))
        self.assertEqual(r["manual_languages"], "ja zh-TW")
        self.assertNotIn("沒有中文", r["reason"])

    def test_uploader_subtitles_without_chinese_is_flagged(self):
        r = vt.classify_info(info(subtitles={"en": TRACK}))
        self.assertEqual(r["subtype"], vt.SUB_UPLOADER)
        self.assertIn("沒有中文字幕", r["reason"])

    def test_auto_captions_only(self):
        r = vt.classify_info(info(automatic_captions={"zh-Hant": TRACK, "en": TRACK}))
        self.assertEqual((r["subtype"], r["route"]), (vt.SUB_AUTO, vt.ROUTE_AUTO_COMPARE))
        self.assertEqual(r["auto_languages"], "2 種")

    def test_auto_original_language_is_reported(self):
        r = vt.classify_info(info(automatic_captions={"zh-orig": TRACK, "en": TRACK}))
        self.assertEqual(r["auto_languages"], "zh-orig")

    def test_no_subtitles(self):
        r = vt.classify_info(info())
        self.assertEqual((r["subtype"], r["route"]), (vt.SUB_NONE, vt.ROUTE_NO_SUBTITLES))

    def test_live_chat_and_empty_tracks_do_not_count(self):
        r = vt.classify_info(info(subtitles={"live_chat": TRACK, "zh-TW": []}))
        self.assertEqual(r["subtype"], vt.SUB_NONE)

    def test_empty_data_is_unknown_not_no_subtitles(self):
        r = vt.classify_info({})
        self.assertEqual((r["subtype"], r["route"]), (vt.SUB_UNKNOWN, vt.ROUTE_RETRY))
        self.assertEqual(r["duration_seconds"], "")

    def test_playlist_or_channel_is_unknown(self):
        for kind in ("playlist", "channel"):
            r = vt.classify_info({"_type": kind, "id": "@x", "title": "頻道", "entries": []})
            self.assertEqual(r["subtype"], vt.SUB_UNKNOWN, kind)
            self.assertIn(kind, r["reason"])

    def test_missing_caption_fields_is_unknown(self):
        r = vt.classify_info({"id": "XXXXXXXXXXX", "title": "x"})
        self.assertEqual(r["subtype"], vt.SUB_UNKNOWN)
        self.assertIn("沒有字幕欄位", r["reason"])

    def test_explicit_video_type_is_fine(self):
        r = vt.classify_info(info(_type="video"))
        self.assertEqual(r["subtype"], vt.SUB_NONE)

    def test_missing_id_is_unknown(self):
        self.assertEqual(vt.classify_info(info(id=""))["subtype"], vt.SUB_UNKNOWN)


class TriageUrl(unittest.TestCase):
    def test_web_page_is_not_fetched(self):
        def boom(url):
            raise AssertionError("網頁不該去連 yt-dlp")
        r = vt.triage_url("page001", "https://zh.wikipedia.org/wiki/X", boom)
        self.assertEqual((r["type"], r["route"]), (vt.TYPE_WEB, vt.ROUTE_WEB))

    def test_youtube_hosts(self):
        for url in ("https://youtu.be/abc", "https://www.youtube.com/watch?v=abc",
                    "https://youtube.com/shorts/abc", "https://m.youtube.com/watch?v=abc"):
            self.assertTrue(vt.is_youtube(url), url)
        self.assertFalse(vt.is_youtube("https://notyoutube.com/x"))
        self.assertFalse(vt.is_youtube("https://example.com/?u=youtube.com"))

    def test_fetch_failure_is_unknown_not_no_subtitles(self):
        def fail(url):
            raise RuntimeError("HTTP Error 403")
        r = vt.triage_url("page002", "https://youtu.be/abc", fail)
        self.assertEqual((r["type"], r["subtype"], r["route"]), (vt.TYPE_VIDEO, vt.SUB_UNKNOWN, vt.ROUTE_RETRY))
        self.assertIn("403", r["reason"])

    def test_row_has_all_columns(self):
        r = vt.triage_url("page003", "https://youtu.be/abc", lambda u: info())
        self.assertEqual(list(r), vt.FIELDS)
        self.assertEqual(r["id"], "page003")


class AddExtraVideos(unittest.TestCase):
    """--add 編號=網址：加入不在 csv 裡的影片。"""

    def run_main(self, *extra_args):
        import csv
        import sys
        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "triage.csv"
            seen = []

            def fake(page_id, url, fetcher=None):
                seen.append((page_id, url))
                return {**{k: "" for k in vt.FIELDS}, "id": page_id, "url": url, "type": vt.TYPE_VIDEO,
                        "subtype": vt.SUB_NONE, "route": vt.ROUTE_NO_SUBTITLES}

            argv = ["video_triage", "--out", str(out), *extra_args]
            with mock.patch.object(vt, "read_urls", return_value=["https://youtu.be/aaa"]), \
                    mock.patch.object(vt, "triage_url", fake), mock.patch.object(sys, "argv", argv):
                code = vt.main()
            rows = list(csv.DictReader(out.open(encoding="utf-8-sig")))
        return code, seen, rows

    def test_extra_video_is_appended_after_csv_rows(self):
        code, seen, rows = self.run_main("--add", "XYZ=https://youtu.be/zzz")
        self.assertEqual(code, 0)
        self.assertEqual(seen, [("page001", "https://youtu.be/aaa"), ("XYZ", "https://youtu.be/zzz")])
        self.assertEqual([r["id"] for r in rows], ["page001", "XYZ"])

    def test_bad_add_format_is_rejected(self):
        with self.assertRaisesRegex(SystemExit, "編號=網址"):
            self.run_main("--add", "no-equals-sign")

    def test_duplicate_add_id_is_rejected(self):
        with self.assertRaisesRegex(SystemExit, "已經存在"):
            self.run_main("--add", "page001=https://youtu.be/zzz")

    def test_negative_limit_is_rejected(self):
        with self.assertRaisesRegex(SystemExit, "負數"):
            self.run_main("--limit", "-1")


if __name__ == "__main__":
    unittest.main()
