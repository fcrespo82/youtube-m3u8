import json
import tempfile
import unittest
from pathlib import Path

import generate


RSS = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry><title>Older video</title><published>2025-01-01T00:00:00+00:00</published><yt:videoId>older_id</yt:videoId></entry>
  <entry><title>Newest video</title><published>2025-01-03T00:00:00+00:00</published><yt:videoId>newest_id</yt:videoId></entry>
</feed>'''


class GenerateTests(unittest.TestCase):
    def write_config(self, root, channels, count=1):
        path = root / "channels.json"
        path.write_text(json.dumps({"videos_per_channel": count, "channels": channels}), encoding="utf-8")
        return path

    def test_generates_proxy_urls_and_aggregate_playlist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.write_config(root, [{"slug": "demo", "name": "Demo", "channel_id": "UCabcdefghijklmnopqrstuv"}])
            generate.generate(config, root / "playlists", 1, "https://stream.example/p/token/hls", lambda url, timeout: RSS)
            text = (root / "playlists" / "demo.m3u8").read_text()
            self.assertIn("Newest video", text)
            self.assertIn("https://stream.example/p/token/hls/newest_id/master.m3u8", text)
            self.assertNotIn("youtube.com/watch", text)
            self.assertEqual(text, (root / "playlists" / "all.m3u8").read_text())

    def test_keeps_previous_channel_playlist_after_fetch_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root, output = Path(directory), Path(directory) / "playlists"
            output.mkdir()
            prior = "#EXTM3U\n#EXTINF:-1,Previous\nhttps://stream.example/previous\n"
            (output / "demo.m3u8").write_text(prior)
            config = self.write_config(root, [{"slug": "demo", "name": "Demo", "channel_id": "UCabcdefghijklmnopqrstuv"}])
            warnings = generate.generate(config, output, 1, "https://stream.example/p/token/hls", lambda url, timeout: (_ for _ in ()).throw(TimeoutError("offline")))
            self.assertTrue(warnings)
            self.assertEqual(prior, (output / "demo.m3u8").read_text())
            self.assertIn("Previous", (output / "all.m3u8").read_text())

    def test_resolved_handle_is_replaced_by_channel_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.write_config(root, [{"slug": "demo", "name": "Demo", "url": "https://www.youtube.com/@demo"}])

            def fetcher(url, timeout):
                return b'<script>{"channelId":"UCabcdefghijklmnopqrstuv"}</script>' if "@demo" in url else RSS

            generate.generate(config, root / "playlists", 1, "https://stream.example/p/token/hls", fetcher)
            saved = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual("UCabcdefghijklmnopqrstuv", saved["channels"][0]["channel_id"])
            self.assertNotIn("url", saved["channels"][0])

if __name__ == "__main__":
    unittest.main()
