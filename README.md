# Rádio Renascença — Modo Super Deus

Aplicação Flask com:

- stream em direto;
- player fixo no rodapé;
- equalizador visual e Web Audio quando o stream permite CORS;
- identificação manual e automática de músicas com ShazamIO;
- amostra WAV curta criada pelo FFmpeg incluído pelo `imageio-ffmpeg`;
- capa do Shazam com alternativa no iTunes;
- histórico local das últimas 10 músicas;
- design responsivo para computador e telemóvel;
- estrutura compatível com Vercel.

## Executar no Windows

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python app.py
```

Abre `http://127.0.0.1:5000`.

## Publicar no Vercel

1. Coloca todos os ficheiros no GitHub, mantendo as pastas `api`, `templates` e `static`.
2. Importa o repositório no Vercel.
3. Não cries uma configuração `functions` para `app.py`; a função correta é `api/index.py`.
4. Publica. Não são necessárias variáveis de ambiente para o stream fornecido.

## Variáveis opcionais

Copia `.env.example` para `.env` quando quiseres alterar o stream, o nome ou a duração da amostra. A aplicação não necessita de chave de API do Shazam nem do iTunes.

## Nota sobre a identificação

A Renascença também transmite notícias, publicidade e programas falados. Nesses momentos, é normal o Shazam não devolver uma música. A aplicação usa uma amostra de 8 segundos para evitar funções demasiado longas no Vercel.
