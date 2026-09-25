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
from dataclasses import dataclass
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


def fetch_bytes(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def load_config(path: Path) -> list[Channel]:
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
    return channels


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


def m3u_escape(value: str) -> str:
    return value.replace('"', "'").replace("\r", " ").replace("\n", " ").strip()


def epg_id(channel: Channel, video: Video) -> str:
    return f"youtube.{channel.slug}.{video.video_id}"


def m3u_header(epg_url: str | None) -> str:
    header = "#EXTM3U"
    if epg_url:
        header += f' x-tvg-url="{m3u_escape(epg_url)}" url-tvg="{m3u_escape(epg_url)}"'
    return header


def render_playlist(channel: Channel, videos: list[Video], epg_url: str | None) -> str:
    lines = [m3u_header(epg_url)]
    for video in videos:
        title = m3u_escape(video.title)
        channel_name = m3u_escape(channel.name)
        attributes = (
            f'tvg-id="{epg_id(channel, video)}" '
            f'tvg-name="{title}" group-title="{channel_name}"'
        )
        if video.thumbnail:
            attributes += f' tvg-logo="{m3u_escape(video.thumbnail)}"'
        lines.append(f"#EXTINF:-1 {attributes},{channel_name} — {title}")
        lines.append(f"https://www.youtube.com/watch?v={video.video_id}")
    return "\n".join(lines) + "\n"


def playlist_entries(text: str) -> list[str]:
    lines = text.splitlines()
    return [line for line in lines if not line.startswith("#EXTM3U")]


def render_epg(playlists: list[tuple[Channel, list[Video]]], configured_slugs: set[str],
               previous_epg: Path | None = None) -> str:
    root = ET.Element("tv", {"generator-info-name": "youtube-m3u8"})
    included_slugs = {channel.slug for channel, _ in playlists}
    for channel, videos in playlists:
        for video in videos:
            identifier = epg_id(channel, video)
            channel_node = ET.SubElement(root, "channel", {"id": identifier})
            ET.SubElement(channel_node, "display-name").text = f"{channel.name} — {video.title}"
            if video.thumbnail:
                ET.SubElement(channel_node, "icon", {"src": video.thumbnail})
            programme = ET.SubElement(root, "programme", {
                "channel": identifier,
                "start": video.published.strftime("%Y%m%d%H%M%S %z"),
                "stop": "20991231235959 +0000",
            })
            ET.SubElement(programme, "title", {"lang": "pt"}).text = video.title
            ET.SubElement(programme, "sub-title", {"lang": "pt"}).text = channel.name
            ET.SubElement(programme, "desc", {"lang": "pt"}).text = video.description or "Vídeo disponível sob demanda no YouTube."
            ET.SubElement(programme, "category", {"lang": "pt"}).text = "YouTube"

    # Preserve guide entries for channels whose network update failed this run.
    if previous_epg and previous_epg.exists():
        try:
            old_root = ET.parse(previous_epg).getroot()
            for node in old_root:
                identifier = node.get("id") if node.tag == "channel" else node.get("channel")
                if identifier and identifier.startswith("youtube."):
                    parts = identifier.split(".", 2)
                    if len(parts) == 3 and parts[1] in configured_slugs and parts[1] not in included_slugs:
                        root.append(node)
        except ET.ParseError:
            pass
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        temp.write(text)
        temp_path = Path(temp.name)
    temp_path.replace(path)


def generate(config_path: Path, output_dir: Path, timeout: int,
             fetcher: Callable[[str, int], bytes] = fetch_bytes, epg_url: str | None = None) -> list[str]:
    channels = load_config(config_path)
    successful: dict[str, tuple[Channel, list[Video], str]] = {}
    warnings: list[str] = []
    for channel in channels:
        try:
            channel_id = resolve_channel_id(channel, fetcher, timeout)
            videos = fetch_videos(channel_id, channel.videos_per_channel, fetcher, timeout)
            successful[channel.slug] = (channel, videos, render_playlist(channel, videos, epg_url))
        except (ValueError, ET.ParseError, urllib.error.URLError, TimeoutError) as error:
            warnings.append(f"{channel.slug}: {error}")

    all_lines = [m3u_header(epg_url)]
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
    write_atomic(
        output_dir / "epg.xml",
        render_epg(
            [(item[0], item[1]) for item in successful.values()],
            {channel.slug for channel in channels},
            output_dir / "epg.xml",
        ),
    )
    return warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("channels.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("playlists"))
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--epg-url", help="Public URL of the generated XMLTV guide")
    args = parser.parse_args()
    try:
        warnings = generate(args.config, args.output_dir, args.timeout, epg_url=args.epg_url)
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    print(f"Generated playlists in {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
