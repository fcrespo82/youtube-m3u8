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
            generate.generate(
                config,
                root / "playlists",
                1,
                lambda url, timeout: RSS,
                stream_resolver=lambda video_id: f"https://media.example.test/{video_id}.m3u8",
            )
            text = (root / "playlists" / "demo.m3u8").read_text()
            self.assertIn("Newest video", text)
            self.assertIn("https://media.example.test/new.m3u8", text)
            self.assertNotIn("youtube.com/watch", text)
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

    def test_playlist_accepts_official_youtube_fallback_url(self):
        channel = generate.Channel("demo", "Demo", "UCabcdefghijklmnopqrstuv", None, 1)
        video = generate.Video("fallback", "Fallback", generate.datetime(2025, 1, 1), "", None,
                               "https://www.youtube.com/watch?v=fallback")
        playlist = generate.render_playlist(channel, [video])
        self.assertIn("https://www.youtube.com/watch?v=fallback", playlist)

    def test_resolved_handle_is_replaced_by_channel_id_in_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.write_config(root, [{"slug": "demo", "name": "Demo", "url": "https://www.youtube.com/@demo"}])

            def fetcher(url, timeout):
                if "@demo" in url:
                    return b'<script>{"channelId":"UCabcdefghijklmnopqrstuv"}</script>'
                return RSS

            generate.generate(
                config,
                root / "playlists",
                1,
                fetcher,
                stream_resolver=lambda video_id: f"https://media.example.test/{video_id}.m3u8",
            )
            saved = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual("UCabcdefghijklmnopqrstuv", saved["channels"][0]["channel_id"])
            self.assertNotIn("url", saved["channels"][0])

    def test_hls_master_combines_video_and_audio_playlists(self):
        master = generate.render_hls_master(
            {"url": "https://video.example/v.m3u8", "vcodec": "avc1.4D401F", "tbr": 700, "width": 854, "height": 480},
            {"url": "https://audio.example/a.m3u8", "acodec": "mp4a.40.2", "tbr": 128},
        )
        self.assertIn('TYPE=AUDIO,GROUP-ID="audio"', master)
        self.assertIn('AUDIO="audio"', master)
        self.assertIn("https://video.example/v.m3u8", master)
        self.assertIn("https://audio.example/a.m3u8", master)

    def test_generate_writes_relative_hls_master_without_public_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.write_config(root, [{"slug": "demo", "name": "Demo", "channel_id": "UCabcdefghijklmnopqrstuv"}])
            resolved = generate.ResolvedStream(
                video_url="https://video.example/v.m3u8", audio_url="https://audio.example/a.m3u8",
                video_format={"url": "https://video.example/v.m3u8", "vcodec": "avc1", "tbr": 100},
                audio_format={"url": "https://audio.example/a.m3u8", "acodec": "mp4a", "tbr": 20},
            )
            generate.generate(config, root / "playlists", 1, lambda url, timeout: RSS,
                              stream_resolver=lambda video_id: resolved)
            self.assertTrue((root / "playlists" / "masters" / "demo-new.m3u8").exists())
            self.assertIn("masters/demo-new.m3u8", (root / "playlists" / "demo.m3u8").read_text())


if __name__ == "__main__":
    unittest.main()
