# YouTube M3U8 para rPlayTV

Este projeto publica vídeos recentes de canais do YouTube como uma playlist IPTV e retransmite o HLS pelo seu servidor. O rPlayTV recebe somente URLs de `stream.crespo.com.br`; por isso não tenta acessar URLs temporários do GoogleVideo, que são vinculados ao IP do servidor que os obteve.

Não há EPG: os vídeos sob demanda são listados pelo título, grupo e miniatura da playlist.

## Como funciona

1. O timer atualiza os feeds RSS e grava a playlist local a cada 30 minutos.
2. O rPlayTV abre `playlist.m3u8` com um token secreto na URL.
3. Quando um vídeo é selecionado, o proxy usa `yt-dlp` naquele momento, escolhe H.264 próximo de 360p e áudio AAC, e devolve um master HLS padrão.
4. Manifests filhos e segmentos são retransmitidos pelo LXC. Assim, todo o tráfego para o YouTube sai do mesmo IP que resolveu os URLs.

## Configuração de canais

Edite `channels.json`. `videos_per_channel` é o padrão global e cada canal pode sobrescrevê-lo. Use `channel_id` quando possível; uma URL de handle é aceita e, na primeira atualização bem-sucedida, será trocada pelo ID canônico.

```json
{
  "videos_per_channel": 3,
  "channels": [
    {
      "slug": "meu-canal",
      "name": "Meu Canal",
      "url": "https://www.youtube.com/@meucanal"
    }
  ]
}
```

Se a leitura de um canal falhar, a última playlist dele permanece publicada. Um vídeo indisponível no YouTube retorna erro apenas quando for aberto, sem afetar os demais.

## Instalação no LXC Debian 12

No host Proxmox, crie e configure o LXC inteiro com um comando, no mesmo estilo dos scripts comunitários:

```bash
var_os='debian' bash -c "$(curl -fsSL https://raw.githubusercontent.com/fcrespo82/youtube-m3u8/main/ct/youtube-m3u8.sh)"
```

O padrão cria um CT Debian 12 com 2 vCPU, 2 GB RAM, disco de 8 GB e DHCP. O script usa `build.func` e o instalador de Caddy dos Community Scripts para escolher storage, baixar/cachear o template se necessário e criar o CT com as validações deles. Para IP fixo, ID e hostname específicos, use as variáveis oficiais `var_*`:

```bash
var_ctid=123 var_hostname=youtube-m3u8 var_net='192.168.1.50/24' var_gateway='192.168.1.1' PUBLIC_HOST='stream.crespo.com.br' bash -c "$(curl -fsSL https://raw.githubusercontent.com/fcrespo82/youtube-m3u8/main/ct/youtube-m3u8.sh)"
```

O instalador exibe a URL da playlist ao final. Encaminhe TCP 80 e 443 do roteador para o IP do CT e crie na Cloudflare o registro A `stream.crespo.com.br`, inicialmente em modo **DNS only** (nuvem cinza).

### Instalação manual

No LXC, copie este repositório para `/opt/youtube-m3u8` e execute como root:

```bash
apt update
apt install -y python3 python3-venv caddy
useradd --system --home /var/lib/youtube-m3u8 --shell /usr/sbin/nologin youtube-m3u8
mkdir -p /var/lib/youtube-m3u8/playlists
python3 -m venv /opt/youtube-m3u8/.venv
/opt/youtube-m3u8/.venv/bin/pip install -r /opt/youtube-m3u8/requirements.txt
cp /opt/youtube-m3u8/deploy/youtube-m3u8.env.example /etc/youtube-m3u8.env
chmod 600 /etc/youtube-m3u8.env
chown -R youtube-m3u8:youtube-m3u8 /opt/youtube-m3u8 /var/lib/youtube-m3u8
```

Edite `/etc/youtube-m3u8.env` antes de iniciar os serviços. Crie o token com `openssl rand -hex 32`; não o compartilhe. Para trocar o intervalo, edite `OnUnitActiveSec=30min` em `youtube-m3u8-update.timer` e recarregue o `systemd`.

Depois instale e inicie os serviços:

```bash
cp /opt/youtube-m3u8/deploy/youtube-m3u8.service /opt/youtube-m3u8/deploy/youtube-m3u8-update.service /opt/youtube-m3u8/deploy/youtube-m3u8-update.timer /etc/systemd/system/
cp /opt/youtube-m3u8/deploy/Caddyfile /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable caddy youtube-m3u8 youtube-m3u8-update.timer
systemctl restart caddy
systemctl start youtube-m3u8 youtube-m3u8-update.timer
systemctl start youtube-m3u8-update.service
```

Após DNS e TLS estarem ativos, importe no rPlayTV:

```
https://stream.crespo.com.br/p/SEU_ACCESS_TOKEN/playlist.m3u8
```

## DDNS Cloudflare

Se o IP PPPoE puder mudar, crie um API Token da Cloudflare com permissão `Zone / DNS / Edit` apenas para a zona `crespo.com.br`. Preencha `CLOUDFLARE_API_TOKEN` e `CLOUDFLARE_ZONE_ID` no arquivo de ambiente. Em seguida:

```bash
cp /opt/youtube-m3u8/deploy/cloudflare-ddns.service /opt/youtube-m3u8/deploy/cloudflare-ddns.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cloudflare-ddns.timer
systemctl start cloudflare-ddns.service
```

O script força o registro a permanecer sem proxy da Cloudflare. Não use Cloudflare Tunnel ou a nuvem laranja para o caminho de vídeo.

## Diagnóstico

```bash
systemctl status youtube-m3u8 youtube-m3u8-update.timer caddy
journalctl -u youtube-m3u8 -u youtube-m3u8-update.service -f
curl -fsS https://stream.crespo.com.br/healthz
```

Para testar o master fora da sua rede, use a URL de um item da playlist com `ffprobe` ou mpv. O proxy expira sessões HLS inativas após quatro horas; recarregar a playlist ou abrir o item novamente cria uma sessão nova.
