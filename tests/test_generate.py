import json
import tempfile
import unittest
from pathlib import Path

import generate


RSS = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry><title>Older video</title><published>2025-01-01T00:00:00+00:00</published><yt:videoId>old</yt:videoId></entry>
  <entry><title>Newest video</title><published>2025-01-03T00:00:00+00:00</published><yt:videoId>new</yt:videoId></entry>
</feed>'''


class GenerateTests(unittest.TestCase):
    def write_config(self, root, channels, count=1):
        path = root / "channels.json"
        path.write_text(json.dumps({"videos_per_channel": count, "channels": channels}), encoding="utf-8")
        return path

    def test_generates_individual_and_aggregate_playlists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.write_config(root, [{"slug": "demo", "name": "Demo", "channel_id": "UCabcdefghijklmnopqrstuv"}])
            generate.generate(config, root / "playlists", 1, lambda url, timeout: RSS)
            text = (root / "playlists" / "demo.m3u8").read_text()
            self.assertIn("Newest video", text)
            self.assertNotIn("Older video", text)
            self.assertEqual(text, (root / "playlists" / "all.m3u8").read_text())

    def test_keeps_previous_channel_playlist_after_fetch_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "playlists"
            output.mkdir()
            prior = '#EXTM3U\n#EXTINF:-1,Previous\nhttps://www.youtube.com/watch?v=prior\n'
            (output / "demo.m3u8").write_text(prior)
            config = self.write_config(root, [{"slug": "demo", "name": "Demo", "channel_id": "UCabcdefghijklmnopqrstuv"}])
            warnings = generate.generate(config, output, 1, lambda url, timeout: (_ for _ in ()).throw(TimeoutError("offline")))
            self.assertTrue(warnings)
            self.assertEqual(prior, (output / "demo.m3u8").read_text())
            self.assertIn("Previous", (output / "all.m3u8").read_text())

    def test_rejects_ambiguous_channel_identifier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.write_config(root, [{"slug": "demo", "name": "Demo", "channel_id": "UCabcdefghijklmnopqrstuv", "url": "https://www.youtube.com/@demo"}])
            with self.assertRaises(generate.ConfigError):
                generate.load_config(config)


if __name__ == "__main__":
    unittest.main()
