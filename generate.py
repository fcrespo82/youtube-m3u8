#!/usr/bin/env python3
"""Build M3U8 playlists from public YouTube channel RSS feeds."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
MEDIA = "{http://search.yahoo.com/mrss/}"
CHANNEL_ID_RE = re.compile(r"^UC[\w-]{20,}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
USER_AGENT = "youtube-m3u8-playlist-generator/1.0"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Channel:
    slug: str
    name: str
    channel_id: str | None
    url: str | None
    videos_per_channel: int


@dataclass(frozen=True)
class Video:
    video_id: str
    title: str
    published: datetime
    description: str
    thumbnail: str | None
    stream_url: str | None = None


@dataclass(frozen=True)
class ResolvedStream:
    url: str | None = None
    video_url: str | None = None
    audio_url: str | None = None
    video_format: dict | None = None
    audio_format: dict | None = None


def fetch_bytes(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def load_config_document(path: Path) -> tuple[list[Channel], dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigError(f"Configuration file not found: {path}") from error
    except json.JSONDecodeError as error:
        raise ConfigError(f"Invalid JSON in {path}: {error}") from error

    if not isinstance(data, dict):
        raise ConfigError("Configuration root must be an object")
    default_count = data.get("videos_per_channel", 3)
    if not isinstance(default_count, int) or default_count < 1:
        raise ConfigError("videos_per_channel must be a positive integer")
    raw_channels = data.get("channels")
    if not isinstance(raw_channels, list):
        raise ConfigError("channels must be an array")

    channels: list[Channel] = []
    seen_slugs: set[str] = set()
    for index, raw in enumerate(raw_channels, start=1):
        if not isinstance(raw, dict):
            raise ConfigError(f"Channel #{index} must be an object")
        slug, name = raw.get("slug"), raw.get("name")
        if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
            raise ConfigError(f"Channel #{index}: slug must use lowercase letters, numbers, and hyphens")
        if slug in seen_slugs:
            raise ConfigError(f"Duplicate channel slug: {slug}")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"Channel {slug}: name is required")
        channel_id, url = raw.get("channel_id"), raw.get("url")
        if (channel_id is None) == (url is None):
            raise ConfigError(f"Channel {slug}: set exactly one of channel_id or url")
        if channel_id is not None and (not isinstance(channel_id, str) or not CHANNEL_ID_RE.fullmatch(channel_id)):
            raise ConfigError(f"Channel {slug}: invalid YouTube channel_id")
        if url is not None and (not isinstance(url, str) or not url.strip()):
            raise ConfigError(f"Channel {slug}: url must be a non-empty string")
        count = raw.get("videos_per_channel", default_count)
        if not isinstance(count, int) or count < 1:
            raise ConfigError(f"Channel {slug}: videos_per_channel must be a positive integer")
        seen_slugs.add(slug)
        channels.append(Channel(slug, name.strip(), channel_id, url, count))
    return channels, data


def load_config(path: Path) -> list[Channel]:
    """Load the validated channel list without exposing the source document."""
    return load_config_document(path)[0]


def resolve_channel_id(channel: Channel, fetcher: Callable[[str, int], bytes], timeout: int) -> str:
    if channel.channel_id:
        return channel.channel_id
    assert channel.url is not None
    parsed = urllib.parse.urlparse(channel.url)
    host = parsed.hostname.lower() if parsed.hostname else ""
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        raise ValueError("URL must use youtube.com")
    direct = re.fullmatch(r"/channel/(UC[\w-]{20,})/?", parsed.path)
    if direct:
        return direct.group(1)
    if not re.fullmatch(r"/@[\w.-]+/?", parsed.path):
        raise ValueError("URL must be a /channel/UC... URL or a /@handle URL")
    html = fetcher(channel.url, timeout).decode("utf-8", errors="replace")
    match = re.search(r'"channelId":"(UC[\w-]{20,})"', html)
    if not match:
        match = re.search(r'https?://www\.youtube\.com/channel/(UC[\w-]{20,})', html)
    if not match:
        raise ValueError("Could not resolve a channel ID from URL")
    return match.group(1)


def fetch_videos(channel_id: str, limit: int, fetcher: Callable[[str, int], bytes], timeout: int) -> list[Video]:
    rss_url = "https://www.youtube.com/feeds/videos.xml?channel_id=" + urllib.parse.quote(channel_id)
    root = ET.fromstring(fetcher(rss_url, timeout))
    videos: list[Video] = []
    for entry in root.findall(ATOM + "entry"):
        video_id = entry.findtext(YT + "videoId")
        title = entry.findtext(ATOM + "title")
        published_text = entry.findtext(ATOM + "published") or entry.findtext(ATOM + "updated")
        if not video_id or not title or not published_text:
            continue
        try:
            published = datetime.fromisoformat(published_text.replace("Z", "+00:00"))
        except ValueError:
            continue
        media_group = entry.find(MEDIA + "group")
        description = ""
        thumbnail = None
        if media_group is not None:
            description = media_group.findtext(MEDIA + "description") or ""
            thumbnail_node = media_group.find(MEDIA + "thumbnail")
            if thumbnail_node is not None:
                thumbnail = thumbnail_node.get("url")
        videos.append(Video(video_id, title, published, description, thumbnail))
    videos.sort(key=lambda video: video.published, reverse=True)
    return videos[:limit]


def is_hls_format(item: dict) -> bool:
    return str(item.get("protocol", "")).startswith("m3u8") and bool(item.get("url"))


def best_hls_pair(formats: list[dict]) -> tuple[dict, dict] | None:
    videos = [item for item in formats if is_hls_format(item) and item.get("vcodec") not in {None, "none"} and item.get("acodec") in {None, "none"}]
    # yt-dlp may report HLS audio as acodec=null even though the selected
    # rendition is valid; absence of a video codec is the reliable signal.
    audios = [item for item in formats if is_hls_format(item) and item.get("vcodec") in {None, "none"}]
    if not videos or not audios:
        return None
    # Prefer AVC and the closest rendition to 360p. It is the lowest practical
    # iPhone target while avoiding the bandwidth cost of 720p/1080p/4K.
    target_height = 360
    video = max(videos, key=lambda item: (
        str(item.get("vcodec", "")).startswith("avc"),
        -abs((item.get("height") or 0) - target_height),
        -(item.get("height") or 0),
    ))
    audio = max(audios, key=lambda item: (str(item.get("acodec", "")).startswith("mp4a"), item.get("abr") or 0, item.get("tbr") or 0))
    return video, audio


def resolve_stream(video_id: str) -> ResolvedStream:
    """Get a current Google media URL, falling back to the official watch page."""
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        import yt_dlp
    except ImportError:
        return ResolvedStream(url=watch_url)

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # YouTube's HLS tracks are normally separate. Request the pair explicitly
        # so the generated master playlist always has independent video and audio.
        "format": "bestvideo[protocol^=m3u8]+bestaudio[protocol^=m3u8]",
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(watch_url, download=False)
    except Exception:
        return ResolvedStream(url=watch_url)
    if isinstance(info, dict):
        requested = info.get("requested_formats") or []
        # ``requested_formats`` reflects yt-dlp's best-quality selection. Choose
        # from the full format list first so our 360p policy takes precedence.
        pair = best_hls_pair(info.get("formats") or []) or best_hls_pair(requested)
        if pair:
            return ResolvedStream(video_url=pair[0]["url"], audio_url=pair[1]["url"], video_format=pair[0], audio_format=pair[1])
    return ResolvedStream(url=watch_url)


def render_hls_master(video: dict, audio: dict) -> str:
    video_bandwidth = int((video.get("tbr") or 0) * 1000)
    audio_bandwidth = int((audio.get("tbr") or audio.get("abr") or 0) * 1000)
    bandwidth = max(video_bandwidth + audio_bandwidth, 1)
    codecs = f'{video.get("vcodec", "avc1")},{audio.get("acodec", "mp4a.40.2")}'
    attributes = f'BANDWIDTH={bandwidth},CODECS="{codecs}",AUDIO="audio"'
    if video.get("width") and video.get("height"):
        attributes += f',RESOLUTION={video["width"]}x{video["height"]}'
    return "\n".join([
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Audio",DEFAULT=YES,AUTOSELECT=YES,URI="{audio["url"]}"',
        f"#EXT-X-STREAM-INF:{attributes}",
        video["url"],
        "",
    ])


def m3u_escape(value: str) -> str:
    return value.replace('"', "'").replace("\r", " ").replace("\n", " ").strip()


def render_playlist(channel: Channel, videos: list[Video]) -> str:
    lines = ["#EXTM3U"]
    for video in videos:
        title = m3u_escape(video.title)
        channel_name = m3u_escape(channel.name)
        attributes = f'tvg-name="{title}" group-title="{channel_name}"'
        if video.thumbnail:
            attributes += f' tvg-logo="{m3u_escape(video.thumbnail)}"'
        lines.append(f"#EXTINF:-1 {attributes},{channel_name} — {title}")
        if not video.stream_url:
            raise ValueError(f"Missing stream URL for {video.video_id}")
        lines.append(video.stream_url)
    return "\n".join(lines) + "\n"


def playlist_entries(text: str) -> list[str]:
    lines = text.splitlines()
    return [line for line in lines if not line.startswith("#EXTM3U")]


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        temp.write(text)
        temp_path = Path(temp.name)
    temp_path.replace(path)


def generate(config_path: Path, output_dir: Path, timeout: int,
             fetcher: Callable[[str, int], bytes] = fetch_bytes, public_base_url: str | None = None,
             stream_resolver: Callable[[str], ResolvedStream | str] = resolve_stream) -> list[str]:
    channels, config_data = load_config_document(config_path)
    successful: dict[str, tuple[Channel, list[Video], str]] = {}
    warnings: list[str] = []
    resolved_ids: dict[str, str] = {}
    for channel in channels:
        try:
            channel_id = resolve_channel_id(channel, fetcher, timeout)
            if channel.url:
                resolved_ids[channel.slug] = channel_id
            candidates = fetch_videos(channel_id, channel.videos_per_channel, fetcher, timeout)
            videos = []
            for video in candidates:
                resolved = stream_resolver(video.video_id)
                if isinstance(resolved, str):  # Simple injectable resolver used by tests/integrations.
                    videos.append(replace(video, stream_url=resolved))
                    continue
                if resolved.video_url and resolved.audio_url and resolved.video_format and resolved.audio_format:
                    master_name = f"{channel.slug}-{video.video_id}.m3u8"
                    write_atomic(output_dir / "masters" / master_name, render_hls_master(resolved.video_format, resolved.audio_format))
                    master_url = f"masters/{master_name}"
                    if public_base_url:
                        master_url = f"{public_base_url.rstrip('/')}/{master_url}"
                    videos.append(replace(video, stream_url=master_url))
                else:
                    videos.append(replace(video, stream_url=resolved.url or f"https://www.youtube.com/watch?v={video.video_id}"))
            successful[channel.slug] = (channel, videos, render_playlist(channel, videos))
        except (ValueError, ET.ParseError, urllib.error.URLError, TimeoutError) as error:
            warnings.append(f"{channel.slug}: {error}")

    # Replace handles/URLs with their canonical channel IDs. This makes future RSS
    # requests more reliable and avoids resolving the public channel page again.
    if resolved_ids:
        for raw_channel in config_data["channels"]:
            channel_id = resolved_ids.get(raw_channel.get("slug"))
            if channel_id:
                raw_channel["channel_id"] = channel_id
                raw_channel.pop("url", None)
        write_atomic(config_path, json.dumps(config_data, ensure_ascii=False, indent=2) + "\n")

    all_lines = ["#EXTM3U"]
    for channel in channels:
        path = output_dir / f"{channel.slug}.m3u8"
        generated = successful.get(channel.slug)
        if generated is not None:
            text = generated[2]
            write_atomic(path, text)
        elif path.exists():
            text = path.read_text(encoding="utf-8")
        else:
            warnings.append(f"{channel.slug}: no previous playlist exists; omitted from all.m3u8")
            continue
        all_lines.extend(playlist_entries(text))
    write_atomic(output_dir / "all.m3u8", "\n".join(all_lines) + "\n")
    return warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("channels.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("playlists"))
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--public-base-url", help="Public URL of the playlists directory")
    args = parser.parse_args()
    try:
        warnings = generate(args.config, args.output_dir, args.timeout, public_base_url=args.public_base_url)
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    print(f"Generated playlists in {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
