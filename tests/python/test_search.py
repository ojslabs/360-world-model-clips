import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import media
from app import server


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.directory = Path(self.scratch.name)
        self.data = patch.object(server, "DATA", self.directory)
        self.data.start()
        self.addCleanup(self.data.stop)
        self.info = {"id": "8o40mSS05iE", "title": "A video", "channel": "Channel", "duration": 90}
        self.reply = (json.dumps(self.info) + "\n").encode()

    def test_repeated_query_reuses_results_until_expiry(self):
        with patch.object(media, "run", return_value=self.reply) as request, \
             patch.object(server.time, "time", return_value=1000) as now:
            first = server.search("F1 jump")
            now.return_value = 1599
            self.assertEqual(server.search(" F1 jump "), first)
            self.assertEqual(request.call_count, 1)
            now.return_value = 1600
            self.assertEqual(server.search("F1 jump"), first)
            self.assertEqual(request.call_count, 2)
            server.search("F1 onboard")
            self.assertEqual(request.call_count, 3)

    def test_exact_saved_video_link_uses_local_metadata_without_network_or_mutation(self):
        folder = self.directory / "sources" / self.info["id"]
        folder.mkdir(parents=True)
        source = folder / "source.mkv"
        source.write_bytes(b"preserved original")
        info_path = folder / "source.info.json"
        media.write_json(info_path, self.info)
        before = {p.name: p.read_bytes() for p in folder.iterdir()}
        with patch.object(media, "run", side_effect=AssertionError("No network or decoding")):
            results = server.search("https://youtu.be/8o40mSS05iE")
            self.assertEqual(results[0]["title"], self.info["title"])
            self.assertEqual(server.search(self.info["id"]), results)
        self.assertEqual({p.name: p.read_bytes() for p in folder.iterdir()}, before)

    def test_invalid_or_future_cache_is_refetched_and_empty_results_are_not_cached(self):
        with patch.object(media, "run", return_value=self.reply) as request, \
             patch.object(server.time, "time", return_value=1000):
            server.search("F1 jump")
            cached = next((self.directory / "search-cache").glob("*.json"))
            cached.write_text("broken json")
            server.search("F1 jump")
            self.assertEqual(request.call_count, 2)
            value = json.loads(cached.read_text())
            value["fetched_at"] = 1001
            media.write_json(cached, value)
            server.search("F1 jump")
            self.assertEqual(request.call_count, 3)
            request.return_value = b""
            self.assertEqual(server.search("No matches"), [])
            self.assertEqual(server.search("No matches"), [])
            self.assertEqual(request.call_count, 5)


if __name__ == "__main__":
    unittest.main()
