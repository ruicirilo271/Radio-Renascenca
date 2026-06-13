# Rádio Renascença — Modo Super Deus v7

Esta versão corrige o problema das amostras repetidas.

## O que estava a acontecer

A identificação no servidor abre uma ligação nova ao StreamTheWorld. Na Renascença, essa ligação nova pode receber sempre o mesmo início/pré-roll, mesmo que no browser já esteja uma música a meio. Por isso esperar no browser não muda a amostra gravada pelo servidor.

## Correção v7

No PC/local:

1. O FFmpeg abre o stream.
2. Mantém a mesma ligação aberta.
3. Descarta os primeiros segundos repetidos.
4. Só depois grava uma amostra WAV.
5. O Shazam analisa essa amostra.
6. Se o Shazam falhar, a app tenta playlist/live segura como fallback.

Por defeito:

- `LOCAL_SKIP_START_SECONDS=45`
- `IDENTIFY_SECONDS=12`

Podes ajustar no ficheiro `.env`:

```env
LOCAL_SKIP_START_SECONDS=60
IDENTIFY_SECONDS=12
```

No Vercel:

- A identificação principal continua por playlist/live.
- A amostra continua disponível apenas para diagnóstico, porque em serverless ela pode repetir o mesmo pré-roll.

## Testes rápidos

Depois de correr no PC:

```txt
http://127.0.0.1:5000/api/status
```

Gravar amostra com o padrão:

```txt
http://127.0.0.1:5000/api/sample
```

Gravar amostra descartando 60 segundos:

```txt
http://127.0.0.1:5000/api/sample?skip=60&seconds=12
```

Identificar com Shazam depois de descartar 60 segundos:

```txt
http://127.0.0.1:5000/api/identify?force=1&skip=60&seconds=12
```

## Executar no PC

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python app.py
```

Abrir:

```txt
http://127.0.0.1:5000
```

## Publicar no Vercel

Mantém a estrutura:

```txt
api/index.py
app.py
static/
templates/
requirements.txt
vercel.json
.python-version
```

Se usares variáveis no Vercel, podes definir:

```env
RADIO_STREAM_URL=https://29053.live.streamtheworld.com/RADIO_RENASCENCA_SC
```

No PC, podes deixar sem `RADIO_STREAM_URL`, porque a app usa por defeito:

```txt
https://29053.live.streamtheworld.com/RADIO_RENASCENCA_SC?dist=onlineradiobox
```
