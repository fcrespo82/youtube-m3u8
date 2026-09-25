# YouTube M3U8

Gera playlists M3U8 com os vídeos mais recentes de canais do YouTube e as mantém atualizadas pelo GitHub Actions. Cada execução obtém URLs de mídia diretas temporárias com `yt-dlp`, adequadas a players IPTV.

## Configuração

Edite `channels.json`. `videos_per_channel` define o padrão global e pode ser sobrescrito dentro de cada canal. Cada canal precisa de um `slug`, um nome e **um** identificador: `channel_id` ou `url`.

```json
{
  "videos_per_channel": 3,
  "channels": [
    {
      "slug": "meu-canal",
      "name": "Meu canal",
      "url": "https://www.youtube.com/@meucanal"
    },
    {
      "slug": "outro-canal",
      "name": "Outro canal",
      "channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx",
      "videos_per_channel": 5
    }
  ]
}
```

São aceitas URLs de handle (`/@handle`) e URLs de canal (`/channel/UC...`). O ID `UC...` é preferível por ser mais estável.

Na primeira execução bem-sucedida de uma URL/handle, o gerador resolve seu ID canônico `UC...`, substitui `url` por `channel_id` no `channels.json` e a Action faz commit dessa atualização. Assim, as execuções seguintes usam diretamente o feed RSS estável.

## Uso local

```bash
python generate.py
```

Os arquivos são criados em `playlists/`: um por canal, `all.m3u8` (agregada) e manifestos HLS em `masters/`. Se a atualização de um canal falhar, sua última versão é preservada e os outros canais continuam sendo publicados. Uma configuração inválida não altera as playlists.

As URLs diretas de mídia do YouTube expiram. O workflow as renova a cada 30 minutos; configure o rPlay TV para atualizar a playlist regularmente ou atualize-a manualmente antes de assistir. Quando o YouTube expõe HLS com áudio e vídeo separados, o gerador cria `playlists/masters/<canal>-<video>.m3u8`, um manifesto mestre que os associa. Caso não exista esse par, usa MP4 direto multiplexado; sem URL `googlevideo.com`, mantém `youtube.com/watch`. Executar `python generate.py` localmente também requer `pip install yt-dlp`.

## GitHub Actions e URLs Raw

O workflow em `.github/workflows/update-playlists.yml` roda a cada 30 minutos. Altere a expressão cron naquela linha para configurar a frequência (o GitHub aceita no mínimo cinco minutos e pode atrasar execuções agendadas).

Inicialize este diretório em Git, crie um repositório **público** no GitHub, conecte-o e envie o branch `main`:

```bash
git init -b main
git add .
git commit -m "feat: add YouTube playlist generator"
git remote add origin https://github.com/<usuario>/<repositorio>.git
git push -u origin main
```

Depois as URLs serão:

```
https://raw.githubusercontent.com/<usuario>/<repositorio>/main/playlists/all.m3u8
https://raw.githubusercontent.com/<usuario>/<repositorio>/main/playlists/<slug>.m3u8
```

No rPlay TV, importe a URL de `all.m3u8`.

Também é possível executar a atualização manualmente pela aba **Actions** usando `workflow_dispatch`.
