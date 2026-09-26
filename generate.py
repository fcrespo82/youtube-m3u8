#!/usr/bin/env python3
"""Build local M3U playlists from public YouTube channel RSS feeds.

Media is intentionally not resolved here. The companion proxy resolves HLS
URLs at playback time, so GoogleVideo URLs never leave the server that created
them.
"""

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
USER_AGENT = "youtube-m3u8-playlist-generator/2.0"


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
        description, thumbnail = "", None
        if media_group is not None:
            description = media_group.findtext(MEDIA + "description") or ""
            thumbnail_node = media_group.find(MEDIA + "thumbnail")
            thumbnail = thumbnail_node.get("url") if thumbnail_node is not None else None
        videos.append(Video(video_id, title, published, description, thumbnail))
    return sorted(videos, key=lambda video: video.published, reverse=True)[:limit]


def m3u_escape(value: str) -> str:
    return value.replace('"', "'").replace("\r", " ").replace("\n", " ").strip()


def render_playlist(channel: Channel, videos: list[Video]) -> str:
    lines = ["#EXTM3U"]
    for video in videos:
        if not video.stream_url:
            raise ValueError(f"Missing stream URL for {video.video_id}")
        title, channel_name = m3u_escape(video.title), m3u_escape(channel.name)
        attributes = f'tvg-name="{title}" group-title="{channel_name}"'
        if video.thumbnail:
            attributes += f' tvg-logo="{m3u_escape(video.thumbnail)}"'
        lines.extend((f"#EXTINF:-1 {attributes},{channel_name} — {title}", video.stream_url))
    return "\n".join(lines) + "\n"


def playlist_entries(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.startswith("#EXTM3U")]


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        temp.write(text)
        temp_path = Path(temp.name)
    temp_path.replace(path)


def generate(config_path: Path, output_dir: Path, timeout: int, public_base_url: str,
             fetcher: Callable[[str, int], bytes] = fetch_bytes) -> list[str]:
    """Publish proxy URLs. Failed channels retain their previous playlist."""
    if not public_base_url.startswith(("http://", "https://")):
        raise ConfigError("public_base_url must be an http(s) URL")
    channels, config_data = load_config_document(config_path)
    successful: dict[str, str] = {}
    warnings: list[str] = []
    resolved_ids: dict[str, str] = {}
    base = public_base_url.rstrip("/")
    for channel in channels:
        try:
            channel_id = resolve_channel_id(channel, fetcher, timeout)
            if channel.url:
                resolved_ids[channel.slug] = channel_id
            videos = [replace(video, stream_url=f"{base}/{video.video_id}/master.m3u8")
                      for video in fetch_videos(channel_id, channel.videos_per_channel, fetcher, timeout)]
            successful[channel.slug] = render_playlist(channel, videos)
        except (ValueError, ET.ParseError, urllib.error.URLError, TimeoutError) as error:
            warnings.append(f"{channel.slug}: {error}")

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
        text = successful.get(channel.slug)
        if text is not None:
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
    parser.add_argument("--public-base-url", required=True, help="Base proxy URL ending in /p/<token>/hls")
    args = parser.parse_args()
    try:
        warnings = generate(args.config, args.output_dir, args.timeout, args.public_base_url)
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    print(f"Generated playlists in {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
