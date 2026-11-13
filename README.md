# Media Converter

Aplicação web para converter arquivos locais, links diretos ou vídeos online para MP4, WebM, MKV, MP3, M4A e WAV.

## Uso local

> A API não possui autenticação e os jobs são globais na instância. Use o aplicativo apenas em localhost ou em uma rede confiável. Não publique a porta diretamente na internet.

## Recursos

- Upload de vídeo ou áudio com limite configurável.
- Importação por link do YouTube, Vimeo, Dailymotion, Twitch e outras plataformas suportadas pelo `yt-dlp`.
- Links diretos públicos com extensão de mídia compatível.
- Conversão com FFmpeg e progresso real de download e conversão.
- Download, cancelamento e remoção dos resultados.
- Armazenamento local com limpeza automática após 24 horas.
- Limites de fila, duração, resolução, tamanho de entrada e tamanho de saída.

## Execução com Docker

```bash
docker compose up --build
```

Acesse `http://localhost:8000`. O Compose publica a porta somente em `127.0.0.1` e habilita sistema de arquivos somente leitura, capabilities reduzidas e limites de memória, CPU e processos.

## Execução local

Requisitos: Python 3.11+, FFmpeg e yt-dlp.

```bash
sudo apt install ffmpeg python3-venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Acesse `http://localhost:8000`. A documentação interativa da API fica em `http://localhost:8000/docs`.

## Configuração

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `MEDIA_CONVERTER_DATA_DIR` | `./data` | Diretório dos arquivos temporários |
| `MEDIA_CONVERTER_MAX_UPLOAD_BYTES` | `2147483648` | Limite por arquivo de entrada ou saída, em bytes |
| `MEDIA_CONVERTER_ALLOWED_HOSTS` | Lista em `app/config.py` | Plataformas autorizadas para o yt-dlp, separadas por vírgula |
| `MEDIA_CONVERTER_TRUSTED_HOSTS` | `localhost,127.0.0.1,[::1]` | Hosts aceitos no cabeçalho `Host` |
| `MEDIA_CONVERTER_ALLOW_PRIVATE_HOSTS` | `false` | Permite hosts privados; use somente em ambiente isolado |
| `MEDIA_CONVERTER_MAX_CONCURRENT_JOBS` | `2` | Conversões simultâneas |
| `MEDIA_CONVERTER_MAX_PENDING_JOBS` | `10` | Jobs simultâneos na fila ou em processamento |
| `MEDIA_CONVERTER_MAX_RETAINED_JOBS` | `100` | Jobs retidos antes da limpeza |
| `MEDIA_CONVERTER_MAX_DURATION_SECONDS` | `14400` | Duração máxima da mídia de origem |
| `MEDIA_CONVERTER_MAX_VIDEO_PIXELS` | `16777216` | Quantidade máxima de pixels por frame |
| `MEDIA_CONVERTER_DOWNLOAD_TIMEOUT_SECONDS` | `120` | Timeout de cada operação de rede do download |
| `MEDIA_CONVERTER_DOWNLOAD_TOTAL_TIMEOUT_SECONDS` | `43200` | Tempo máximo total para baixar um link |
| `MEDIA_CONVERTER_CONVERSION_TIMEOUT_SECONDS` | `43200` | Tempo máximo de conversão |
| `MEDIA_CONVERTER_PROBE_TIMEOUT_SECONDS` | `120` | Tempo máximo de análise da mídia |
| `MEDIA_CONVERTER_CLEANUP_AFTER_SECONDS` | `86400` | Tempo de retenção dos jobs |
| `FFMPEG_BINARY` | `ffmpeg` | Caminho do executável do FFmpeg |
| `FFPROBE_BINARY` | `ffprobe` | Caminho do executável do FFprobe |
| `YTDLP_BINARY` | `yt-dlp` | Caminho do executável do yt-dlp |
| `MEDIA_CONVERTER_YTDLP_PROXY` | vazio | Proxy de saída usado pelo yt-dlp (http, https, socks4 ou socks5) |

Para permitir outra plataforma no yt-dlp, adicione o domínio à lista. Hosts públicos com extensão de mídia são aceitos como link direto; um host desconhecido sem extensão compatível é rejeitado.

Os limites de download e conversão são independentes: o primeiro controla cada operação de rede, o segundo controla o download completo e o terceiro controla o FFmpeg. Aumente `MEDIA_CONVERTER_DOWNLOAD_TOTAL_TIMEOUT_SECONDS` e `MEDIA_CONVERTER_CONVERSION_TIMEOUT_SECONDS` para mídias muito longas e reinicie o serviço.

`MEDIA_CONVERTER_ALLOW_PRIVATE_HOSTS=true` desativa a proteção contra endereços privados e locais. Use apenas em ambientes isolados e confiáveis.

## Segurança e limites

- Requisições `/api` aceitam apenas os hosts definidos em `MEDIA_CONVERTER_TRUSTED_HOSTS`.
- O corpo das requisições é limitado mesmo quando não há cabeçalho `Content-Length`.
- A fila tem cota e os arquivos de entrada são removidos após sucesso ou falha.
- FFprobe e FFmpeg aceitam apenas uma lista de formatos de mídia; playlists e protocolos de rede são bloqueados.
- A conversão restringe duração, resolução, threads, tamanho de saída e tempo de execução.
- Links diretos validam DNS, redirects, HTTPS e o endereço efetivamente conectado.
- O download direto resolve o host uma única vez, valida os endereços e conecta ao IP fixado, mantendo Host/SNI originais.
- O yt-dlp realiza as próprias requisições e redirects. Para uso remoto, aplique uma política de egress que bloqueie redes internas e adicione autenticação e ownership por usuário. Defina `MEDIA_CONVERTER_YTDLP_PROXY` para encaminhar todo o tráfego do yt-dlp por um proxy de saída controlado.
- A API não implementa autenticação. Para acesso multiusuário, adicione autenticação,isolamento por proprietário, TLS, rate limiting e firewall de egress.

## Qualidade e formatos

A opção **Sem redimensionamento** mantém a resolução e, quando o codec de origem já é compatível com o destino, o arquivo é remuxado sem recodificação. Caso contrário, há recodificação. As demais opções limitam a altura a 1080p, 720p ou 480p.

| Formato | Vídeo | Áudio |
| --- | --- | --- |
| MP4 | H.264 | AAC |
| WebM | VP9 | Opus |
| MKV | H.264 | AAC |
| MP3 | — | MP3 |
| M4A | — | AAC |
| WAV | — | PCM 16-bit |

## Testes e qualidade

```bash
pytest
ruff check .
ruff format --check .
mypy app
```

## API

- `POST /api/jobs/upload`: cria uma conversão por upload.
- `POST /api/jobs/url`: cria uma conversão por link.
- `GET /api/jobs`: lista as conversões.
- `GET /api/jobs/{id}`: consulta uma conversão.
- `POST /api/jobs/{id}/cancel`: cancela uma conversão.
- `DELETE /api/jobs/{id}`: remove uma conversão concluída.
- `GET /api/jobs/{id}/download`: baixa o resultado.

Use apenas arquivos e links para os quais você tenha autorização.
