#!/usr/bin/env bash
# shellcheck shell=bash
# Proxmox VE one-line installer for youtube-m3u8.
# Usage: var_os=debian bash -c "$(curl -fsSL https://raw.githubusercontent.com/fcrespo82/youtube-m3u8/main/ct/youtube-m3u8.sh)"

set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/fcrespo82/youtube-m3u8.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
var_os="${var_os:-debian}"
var_version="${var_version:-12}"
var_ctid="${var_ctid:-}"
var_hostname="${var_hostname:-youtube-m3u8}"
var_storage="${var_storage:-local-lvm}"
var_template_storage="${var_template_storage:-local}"
var_disk="${var_disk:-8}"
var_cpu="${var_cpu:-2}"
var_ram="${var_ram:-2048}"
var_bridge="${var_bridge:-vmbr0}"
var_ip="${var_ip:-dhcp}"
var_gw="${var_gw:-}"
var_unprivileged="${var_unprivileged:-1}"
PUBLIC_HOST="${PUBLIC_HOST:-stream.crespo.com.br}"

die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '\n==> %s\n' "$*"; }
require() { command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"; }

[[ $EUID -eq 0 ]] || die "Run this script as root on the Proxmox VE host."
require pct
require pveam
[[ "$var_os" == "debian" && "$var_version" == "12" ]] || die "Only Debian 12 is supported. Set var_os=debian and var_version=12."

if [[ -z "$var_ctid" ]]; then
  var_ctid="$(pvesh get /cluster/nextid)"
fi
[[ "$var_ctid" =~ ^[0-9]+$ ]] || die "var_ctid must be numeric."
if pct status "$var_ctid" >/dev/null 2>&1; then
  die "Container ID $var_ctid already exists. Choose another var_ctid."
fi
[[ "$var_disk" =~ ^[0-9]+$ && "$var_cpu" =~ ^[0-9]+$ && "$var_ram" =~ ^[0-9]+$ ]] || die "var_disk, var_cpu and var_ram must be positive integers."

if [[ "$var_ip" != "dhcp" ]]; then
  [[ "$var_ip" == */* ]] || die "For a static address use var_ip=192.168.x.y/24."
  [[ -n "$var_gw" ]] || die "var_gw is required with a static var_ip."
fi

info "Finding a Debian ${var_version} template"
pveam update >/dev/null
template="$(pveam available --section system | awk '/debian-12-standard_/ { print $NF }' | sort -V | tail -n1)"
[[ -n "$template" ]] || die "No Debian 12 template is available from the configured Proxmox repositories."

if ! pveam list "$var_template_storage" | awk '{print $1}' | grep -Fqx "$var_template_storage:vztmpl/$template"; then
  info "Downloading $template"
  pveam download "$var_template_storage" "$template"
fi

net0="name=eth0,bridge=${var_bridge},ip=${var_ip}"
[[ -n "$var_gw" ]] && net0+=",gw=${var_gw}"

info "Creating LXC ${var_ctid} (${var_hostname})"
pct create "$var_ctid" "$var_template_storage:vztmpl/$template" \
  --hostname "$var_hostname" \
  --cores "$var_cpu" \
  --memory "$var_ram" \
  --swap 512 \
  --rootfs "${var_storage}:${var_disk}" \
  --net0 "$net0" \
  --unprivileged "$var_unprivileged" \
  --features nesting=1 \
  --start 1

cleanup_on_error() {
  printf '\nInstallation failed. The LXC %s was left running for troubleshooting.\n' "$var_ctid" >&2
}
trap cleanup_on_error ERR

info "Waiting for the container"
for _ in {1..30}; do
  if pct exec "$var_ctid" -- true >/dev/null 2>&1; then break; fi
  sleep 1
done
pct exec "$var_ctid" -- true >/dev/null 2>&1 || die "The new LXC did not become ready."

info "Installing youtube-m3u8 and Caddy"
pct exec "$var_ctid" -- env REPO_URL="$REPO_URL" REPO_BRANCH="$REPO_BRANCH" PUBLIC_HOST="$PUBLIC_HOST" bash -s <<'CONTAINER_SETUP'
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates caddy git openssl python3 python3-venv
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
CLOUDFLARE_API_TOKEN=
CLOUDFLARE_ZONE_ID=
EOF
chmod 600 /etc/youtube-m3u8.env
chown -R youtube-m3u8:youtube-m3u8 /opt/youtube-m3u8 /var/lib/youtube-m3u8
install -m 644 /opt/youtube-m3u8/deploy/youtube-m3u8.service /opt/youtube-m3u8/deploy/youtube-m3u8-update.service /opt/youtube-m3u8/deploy/youtube-m3u8-update.timer /etc/systemd/system/
sed "s/stream\.crespo\.com\.br/${PUBLIC_HOST}/g" /opt/youtube-m3u8/deploy/Caddyfile >/etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable caddy youtube-m3u8 youtube-m3u8-update.timer
systemctl restart caddy
systemctl start youtube-m3u8 youtube-m3u8-update.timer
systemctl start youtube-m3u8-update.service
printf '\nPLAYLIST_URL=https://%s/p/%s/playlist.m3u8\n' "$PUBLIC_HOST" "$ACCESS_TOKEN"
CONTAINER_SETUP

trap - ERR
info "Installation complete"
printf 'Configure the DNS-only A record for %s and forward TCP ports 80/443 to this LXC.\n' "$PUBLIC_HOST"
printf 'Retrieve the playlist URL again with:\n  pct exec %s -- grep ^ACCESS_TOKEN= /etc/youtube-m3u8.env\n' "$var_ctid"
