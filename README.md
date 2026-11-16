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
- Login com Google (sessão em cookie `HttpOnly`) com rate limiting e log de tentativas negadas.
- Instalável como aplicativo web (PWA): "Adicionar à tela inicial" no celular.
- Notificações Web Push no celular quando a conversão termina ou falha.

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
| `MEDIA_CONVERTER_FFMPEG_THREADS` | `1` | Threads de codec por etapa do FFmpeg |
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
| `MEDIA_CONVERTER_YTDLP_COOKIES_FILE` | vazio | Arquivo `cookies.txt` no formato Netscape usado pelo yt-dlp |
| `MEDIA_CONVERTER_YTDLP_COOKIES_FROM_BROWSER` | vazio | Perfil de navegador de onde o yt-dlp extrairá cookies |
| `MEDIA_CONVERTER_YTDLP_JS_RUNTIMES` | vazio | Runtimes JavaScript usados pelo yt-dlp (ex.: `node:/usr/bin/node`, `deno`), separados por vírgula |
| `MEDIA_CONVERTER_AUTH_USER` | vazio | Usuário da autenticação básica obrigatória para expor o serviço |
| `MEDIA_CONVERTER_AUTH_PASSWORD` | vazio | Senha da autenticação básica |
| `MEDIA_CONVERTER_GOOGLE_CLIENT_ID` | vazio | Client ID do OAuth 2.0 usado no login com Google |
| `MEDIA_CONVERTER_GOOGLE_ALLOWED_DOMAINS` | vazio | Domínios (`gmail.com`) ou e-mails exatos (`cleberleonheart@gmail.com`) autorizados a usar o conversor, separados por vírgula |
| `MEDIA_CONVERTER_SESSION_SECRET` | vazio | Chave usada para assinar a sessão do login com Google |
| `MEDIA_CONVERTER_SESSION_TTL_SECONDS` | `86400` | Validade da sessão em segundos |
| `MEDIA_CONVERTER_LOGIN_MAX_FAILURES` | `20` | Tentativas de login com Google aceitas por origem antes do bloqueio temporário |
| `MEDIA_CONVERTER_LOGIN_FAILURE_WINDOW_SECONDS` | `600` | Janela, em segundos, usada para contar as falhas de login |
| `MEDIA_CONVERTER_VAPID_PUBLIC_KEY` | vazio | Chave pública VAPID usada nas notificações Web Push |
| `MEDIA_CONVERTER_VAPID_PRIVATE_KEY` | vazio | Chave privada VAPID usada para enviar as notificações |
| `MEDIA_CONVERTER_VAPID_SUBJECT` | `mailto:admin@localhost` | Contato enviado ao serviço de push (`mailto:`) |

Para permitir outra plataforma no yt-dlp, adicione o domínio à lista. Hosts públicos com extensão de mídia são aceitos como link direto; um host desconhecido sem extensão compatível é rejeitado.

Os limites de download e conversão são independentes: o primeiro controla cada operação de rede, o segundo controla o download completo e o terceiro controla o FFmpeg. Aumente `MEDIA_CONVERTER_DOWNLOAD_TOTAL_TIMEOUT_SECONDS` e `MEDIA_CONVERTER_CONVERSION_TIMEOUT_SECONDS` para mídias muito longas e reinicie o serviço.

`MEDIA_CONVERTER_ALLOW_PRIVATE_HOSTS=true` desativa a proteção contra endereços privados e locais. Use apenas em ambientes isolados e confiáveis.

Para vídeos com confirmação de idade, configure uma destas opções:

```bash
chmod 600 /caminho/cookies.txt
MEDIA_CONVERTER_YTDLP_COOKIES_FILE=/caminho/cookies.txt
```

O arquivo deve usar o formato Netscape `cookies.txt`. Alternativamente, use `MEDIA_CONVERTER_YTDLP_COOKIES_FROM_BROWSER=chromium` ou o perfil correspondente. As duas opções são mutuamente exclusivas. No Docker, monte o arquivo somente leitura dentro do container e aponte `MEDIA_CONVERTER_YTDLP_COOKIES_FILE` para o caminho montado.

O YouTube exige que o yt-dlp resolva desafios JavaScript. Instale o pacote `yt-dlp-ejs` (já listado em `requirements.txt`) e informe um runtime compatível, por exemplo:

```bash
MEDIA_CONVERTER_YTDLP_JS_RUNTIMES=node:/usr/bin/node
```

São aceitos Node 22 ou superior e Deno 2.3 ou superior. Sem runtime, vídeos bloqueados por idade podem falhar mesmo com cookies válidos.

## Exposição do serviço

### Tailscale (recomendado)

O acesso fica restrito à tailnet, com HTTPS e certificado válido, sem expor portas na internet:

```bash
sudo tailscale up --hostname media-converter --ssh=false
sudo tailscale serve --bg --https=443 http://100.113.132.11:8000
```

O serviço precisa escutar no IP do Tailscale para ser acessível direto pela tailnet (por exemplo `http://100.113.132.11:8000`), sem depender de MagicDNS nem de TLS.

Adicione o nome e o IP do nó em `MEDIA_CONVERTER_TRUSTED_HOSTS` (por exemplo `media-converter.tail045fd4.ts.net,100.113.132.11`). Em containers LXC sem device TUN, habilite o modo túnel do tailscaled:

```bash
sudo systemctl edit tailscaled
```

```ini
[Service]
ExecStart=
ExecStart=/usr/sbin/tailscaled --state=/var/lib/tailscale/tailscaled.state --socket=/run/tailscale/tailscaled.sock --port=41641 --tun=tailscale
```

### Tailscale Funnel (acesso público com URL fixa)

O Funnel publica o mesmo endereço HTTPS (`https://media-converter.tail045fd4.ts.net`) também na internet, sem precisar de domínio próprio. O comando é idempotente e pode ser executado por uma unit de sistema:

```bash
sudo tailscale funnel --bg 8000
```

```ini
[Unit]
Description=Media Converter via Tailscale Funnel
After=network-online.target tailscaled.service
Requires=tailscaled.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/tailscale funnel --bg 8000
ExecStop=/usr/bin/tailscale funnel --https=443 off
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Como o Funnel deixa o serviço público, mantenha o login com Google ativo e restrito a contas específicas (`MEDIA_CONVERTER_GOOGLE_ALLOWED_DOMAINS`).

### Login com Google

Crie um cliente OAuth 2.0 do tipo **aplicativo web** no [Google Cloud Console](https://console.cloud.google.com/apis/credentials) e adicione as origens autorizadas (por exemplo `https://media-converter.tail045fd4.ts.net` e `http://localhost:8000`). Depois configure:

```bash
MEDIA_CONVERTER_GOOGLE_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
MEDIA_CONVERTER_SESSION_SECRET=uma-chave-aleatoria-longa
MEDIA_CONVERTER_GOOGLE_ALLOWED_DOMAINS=gmail.com
```

O frontend usa o Google Identity Services, envia o ID token ao backend, que valida a credencial, cria uma sessão assinada (cookie `HttpOnly`) e libera a API. `MEDIA_CONVERTER_GOOGLE_ALLOWED_DOMAINS` é opcional e restringe o acesso a determinados domínios de e-mail. Aceita domínios (`gmail.com`) ou e-mails exatos (`cleberleonheart@gmail.com`), separados por vírgula.

### Notificações push

Com o aplicativo instalado (PWA), o botão **Notificações** no cabeçalho registra o dispositivo e habilita avisos quando a conversão termina ou falha, mesmo com a tela bloqueada.

As chaves VAPID podem ser geradas localmente:

```bash
python3 - <<'PY'
import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

key = ec.generate_private_key(ec.SECP256R1())
private = key.private_numbers().private_value.to_bytes(32, "big")
public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
print("MEDIA_CONVERTER_VAPID_PUBLIC_KEY=" + b64(public))
print("MEDIA_CONVERTER_VAPID_PRIVATE_KEY=" + b64(private))
PY
```

Configure as duas chaves e o contato (`MEDIA_CONVERTER_VAPID_SUBJECT=mailto:seu-email@exemplo.com`). As assinaturas dos dispositivos ficam em `data/push-subscriptions.json` (permissão `600`) e assinaturas expiradas são removidas automaticamente.

### Autenticação básica

Defina `MEDIA_CONVERTER_AUTH_USER` e `MEDIA_CONVERTER_AUTH_PASSWORD` para exigir autenticação básica em todas as rotas, exceto `/api/health`:

```bash
MEDIA_CONVERTER_AUTH_USER=admin
MEDIA_CONVERTER_AUTH_PASSWORD=uma-senha-forte
```

Use-a sempre que o serviço estiver exposto fora da tailnet.

### Túnel público (Cloudflare)

Em máquinas sem IP público direto, use um túnel HTTPS do Cloudflare, que só precisa de conexão de saída:

```bash
cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8000
```

Adicione o domínio do túnel em `MEDIA_CONVERTER_TRUSTED_HOSTS` (por exemplo `*.trycloudflare.com`). O endereço do túnel rápido muda a cada reinício; para um endereço fixo, use um túnel nomeado com domínio próprio. Com túnel público, a autenticação básica é obrigatória.

## Segurança e limites

- Requisições `/api` aceitam apenas os hosts definidos em `MEDIA_CONVERTER_TRUSTED_HOSTS`.
- O corpo das requisições é limitado mesmo quando não há cabeçalho `Content-Length`.
- A fila tem cota e os arquivos de entrada são removidos após sucesso ou falha.
- FFprobe e FFmpeg aceitam apenas uma lista de formatos de mídia; playlists e protocolos de rede são bloqueados.
- A conversão restringe duração, resolução, tamanho de saída e tempo de execução, com threads de codec limitadas por etapa.
- A conversão seleciona apenas a primeira faixa de vídeo e a primeira de áudio, evitando faixas extras incompatíveis.
- A fila é local ao processo; não execute réplicas compartilhando o mesmo volume de dados.
- Links diretos validam DNS, redirects, HTTPS e o endereço efetivamente conectado.
- O download direto valida e fixa o IP de cada destino de redirecionamento, mantendo Host/SNI originais.
- O yt-dlp realiza as próprias requisições e redirects. Para uso remoto, aplique uma política de egress que bloqueie redes internas e adicione autenticação e ownership por usuário. Defina `MEDIA_CONVERTER_YTDLP_PROXY` para encaminhar todo o tráfego do yt-dlp por um proxy de saída controlado.
- Cookies do yt-dlp são credenciais sensíveis: use arquivo com permissão `600`, não versione seu conteúdo e monte-o somente leitura no container.
- A API não implementa autenticação por usuário: a proteção disponível é autenticação básica única (`MEDIA_CONVERTER_AUTH_USER`/`MEDIA_CONVERTER_AUTH_PASSWORD`) ou o login com Google restrito a contas específicas. Para acesso multiusuário, adicione isolamento por proprietário, TLS, rate limiting e firewall de egress.
- O login com Google aplica rate limiting por origem (`MEDIA_CONVERTER_LOGIN_MAX_FAILURES` falhas em `MEDIA_CONVERTER_LOGIN_FAILURE_WINDOW_SECONDS`) e registra no log os logins negados, sem gravar credenciais.

## Qualidade e formatos

A opção **Sem redimensionamento** mantém a resolução e, quando o codec de origem já é compatível com o destino, o arquivo é remuxado sem recodificação. Caso contrário, há recodificação. As demais opções limitam a altura a 1080p, 720p ou 480p.

| Formato | Vídeo | Áudio |
| --- | --- | --- |
| MP4 | H.264 | AAC |
| WebM | VP8 | Opus |
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
