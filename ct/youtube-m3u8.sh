#!/usr/bin/env bash
# shellcheck shell=bash
# Proxmox VE installer. It deliberately uses Community Scripts' build.func for
# template discovery/download, storage selection, CT creation and safety checks.
#
# Usage:
# var_os=debian bash -c "$(curl -fsSL https://raw.githubusercontent.com/fcrespo82/youtube-m3u8/main/ct/youtube-m3u8.sh)"

_CS_DEFAULT_URL="https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main"
_cs_boot="${COMMUNITY_SCRIPTS_CORE_DIR:-$(dirname "${BASH_SOURCE[0]}")/../../core}/core/build.func"
source "$_cs_boot" 2>/dev/null || source <(curl -fsSL "${COMMUNITY_SCRIPTS_CORE_URL:-https://raw.githubusercontent.com/community-scripts/core/main}/core/build.func")

# Copyright (c) 2026
# License: MIT
# Community Scripts' core and Caddy installer retain their own licenses.

APP="YouTube M3U8"
REPO_URL="${REPO_URL:-https://github.com/fcrespo82/youtube-m3u8.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
PUBLIC_HOST="${PUBLIC_HOST:-stream.crespo.com.br}"

# Values understood by Community Scripts. User-provided var_* values win.
var_tags="${var_tags:-media;iptv}"
var_cpu="${var_cpu:-2}"
var_ram="${var_ram:-2048}"
var_disk="${var_disk:-8}"
var_os="${var_os:-debian}"
var_version="${var_version:-12}"
var_unprivileged="${var_unprivileged:-1}"

update_script() {
  msg_info "Updating ${APP} LXC"
  apt_update_safe
  $STD apt-get upgrade -y
  systemctl restart youtube-m3u8
  msg_ok "Updated ${APP} LXC"
}

install_youtube_m3u8() {
  msg_info "Installing ${APP}"
  pct exec "$CTID" -- env REPO_URL="$REPO_URL" REPO_BRANCH="$REPO_BRANCH" PUBLIC_HOST="$PUBLIC_HOST" bash -s <<'CONTAINER_SETUP'
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get install -y --no-install-recommends curl git openssl python3 python3-venv
install -d -m 0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared bookworm main' >/etc/apt/sources.list.d/cloudflared.list
apt-get update
apt-get install -y --no-install-recommends cloudflared
useradd --system --home /var/lib/youtube-m3u8 --shell /usr/sbin/nologin youtube-m3u8 2>/dev/null || true
rm -rf /opt/youtube-m3u8
git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" /opt/youtube-m3u8
python3 -m venv /opt/youtube-m3u8/.venv
/opt/youtube-m3u8/.venv/bin/pip install --disable-pip-version-check -r /opt/youtube-m3u8/requirements.txt
install -d -o youtube-m3u8 -g youtube-m3u8 /var/lib/youtube-m3u8/playlists
ACCESS_TOKEN="$(openssl rand -hex 32)"
cat >/etc/youtube-m3u8.env <<EOF
ACCESS_TOKEN=${ACCESS_TOKEN}
PUBLIC_BASE_URL=https://${PUBLIC_HOST}
CHANNELS_CONFIG=/opt/youtube-m3u8/channels.json
PLAYLIST_DIR=/var/lib/youtube-m3u8/playlists
PUBLIC_HOST=${PUBLIC_HOST}
CLOUDFLARE_TUNNEL_TOKEN=
EOF
chmod 600 /etc/youtube-m3u8.env
chown -R youtube-m3u8:youtube-m3u8 /opt/youtube-m3u8 /var/lib/youtube-m3u8
install -m 644 /opt/youtube-m3u8/deploy/youtube-m3u8.service /opt/youtube-m3u8/deploy/youtube-m3u8-update.service /opt/youtube-m3u8/deploy/youtube-m3u8-update.timer /opt/youtube-m3u8/deploy/youtube-m3u8-cloudflared.service /etc/systemd/system/
sed "s/stream\.crespo\.com\.br/${PUBLIC_HOST}/g" /opt/youtube-m3u8/deploy/Caddyfile >/etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable youtube-m3u8 youtube-m3u8-update.timer
systemctl restart caddy
systemctl start youtube-m3u8 youtube-m3u8-update.timer
systemctl start youtube-m3u8-update.service
printf 'PLAYLIST_URL=https://%s/p/%s/playlist.m3u8\n' "$PUBLIC_HOST" "$ACCESS_TOKEN"
CONTAINER_SETUP
  msg_ok "Installed ${APP}"
}

header_info "$APP"
variables
color
catch_errors

# build_container obtains the app installer from the Community Scripts project.
# Reusing its Caddy installer gives us its maintained package setup while its
# build helpers fetch/cache the Debian template whenever it is missing.
var_install="caddy-install"
start
build_container
install_youtube_m3u8
description
msg_ok "Completed successfully!"
echo -e "${INFO}${YW}Create a Cloudflare Tunnel public hostname for ${PUBLIC_HOST} pointing to http://localhost:80.${CL}"
echo -e "${INFO}${YW}Put its token in /etc/youtube-m3u8.env, then enable youtube-m3u8-cloudflared.${CL}"
echo -e "${INFO}${YW}Playlist token: pct exec ${CTID} -- grep ^ACCESS_TOKEN= /etc/youtube-m3u8.env${CL}"
