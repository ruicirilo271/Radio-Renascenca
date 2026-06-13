from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, render_template, request, send_file


BASE_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

IS_VERCEL = bool(os.getenv("VERCEL"))

# IMPORTANTE:
# - No PC voltamos à lógica da primeira versão que funcionou contigo:
#   FFmpeg grava diretamente do stream original com ?dist=onlineradiobox e o Shazam analisa WAV.
# - No Vercel evitamos depender dessa amostra, porque cada nova ligação ao StreamTheWorld
#   pode cair sempre no mesmo pré-roll/início repetido.
LOCAL_STREAM_URL = "https://29053.live.streamtheworld.com/RADIO_RENASCENCA_SC?dist=onlineradiobox"
VERCEL_STREAM_URL = "https://29053.live.streamtheworld.com/RADIO_RENASCENCA_SC"
STREAM_URL = os.getenv("RADIO_STREAM_URL", VERCEL_STREAM_URL if IS_VERCEL else LOCAL_STREAM_URL).strip()
STATION_NAME = os.getenv("RADIO_NAME", "Rádio Renascença").strip()
STATION_SUBTITLE = os.getenv("RADIO_SUBTITLE", "Sempre nunca igual").strip()

# No Vercel é melhor dividir a tarefa em 2 chamadas:
# 1) capturar MP3/AAC para /tmp; 2) enviar essa amostra ao Shazam.
RAW_CAPTURE_SECONDS = max(4.0, min(float(os.getenv("RAW_CAPTURE_SECONDS", "7.2" if IS_VERCEL else "9.5")), 14.0))
RAW_MIN_BYTES = max(25_000, int(os.getenv("RAW_MIN_BYTES", "52000" if IS_VERCEL else "65000")))
RAW_MAX_BYTES = max(70_000, int(os.getenv("RAW_MAX_BYTES", "220000" if IS_VERCEL else "360000")))

# Duração usada no PC pela lógica original com FFmpeg + WAV.
IDENTIFY_SECONDS = max(8, min(int(os.getenv("IDENTIFY_SECONDS", "12")), 18))
# StreamTheWorld pode entregar um pré-roll/início igual sempre que se abre uma ligação nova.
# No PC, a v7 mantém a mesma ligação aberta, descarta este início e só depois grava a amostra.
LOCAL_SKIP_START_SECONDS = max(0.0, min(float(os.getenv("LOCAL_SKIP_START_SECONDS", "45")), 90.0))
IDENTIFY_CACHE_SECONDS = max(15, int(os.getenv("IDENTIFY_CACHE_SECONDS", "65")))
SAMPLE_TTL_SECONDS = max(180, int(os.getenv("SAMPLE_TTL_SECONDS", "900")))
NOWPLAYING_CACHE_SECONDS = max(10, int(os.getenv("NOWPLAYING_CACHE_SECONDS", "45")))

# A Renascença usa StreamTheWorld/Triton. No Vercel, cada nova ligação ao stream
# pode começar pelo mesmo pré-roll/jingle, por isso a identificação principal passa
# a consultar os metadados oficiais Now Playing e só usa Shazam como plano B.
TRITON_MOUNTS: list[str] = []
for _mount in (
    os.getenv("TRITON_MOUNT", "RADIO_RENASCENCA_SC"),
    "RADIO_RENASCENCA_SC",
    "RADIO_RENASCENCA",
):
    _mount = (_mount or "").strip().upper()
    if _mount and _mount not in TRITON_MOUNTS:
        TRITON_MOUNTS.append(_mount)

SAMPLE_DIR = Path(tempfile.gettempdir()) / "radio_renascenca_super_deus"
SAMPLE_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
app.config["JSON_AS_ASCII"] = False

_identify_lock = threading.Lock()
_sample_lock = threading.Lock()
_identify_cache: dict[str, Any] = {"timestamp": 0.0, "payload": None}
_metadata_cache: dict[str, Any] = {"timestamp": 0.0, "payload": None, "error": ""}
_samples: dict[str, dict[str, Any]] = {}
_latest_sample_id: str | None = None


class RadioError(RuntimeError):
    """Erro controlado da aplicação de rádio."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    return re.sub(r"\s+", " ", text) or fallback


def _ffmpeg_executable() -> str:
    configured = os.getenv("FFMPEG_BINARY", "").strip()
    if configured and Path(configured).exists():
        return configured

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and Path(bundled).exists():
            return bundled
    except Exception as exc:  # pragma: no cover - depende do ambiente
        raise RadioError(
            "FFmpeg não foi encontrado. Confirma que imageio-ffmpeg está no requirements.txt."
        ) from exc

    raise RadioError("FFmpeg não foi encontrado neste ambiente.")


def _safe_error_detail(stderr: bytes | str, limit: int = 420) -> str:
    if isinstance(stderr, bytes):
        text = stderr.decode("utf-8", errors="ignore")
    else:
        text = stderr
    text = re.sub(r"\s+", " ", text).strip()
    return text[-limit:]


def _extension_from_content_type(content_type: str) -> str:
    lowered = (content_type or "").lower()
    if "mpeg" in lowered or "mp3" in lowered:
        return ".mp3"
    if "aac" in lowered or "aacp" in lowered:
        return ".aac"
    if "ogg" in lowered:
        return ".ogg"
    return ".mp3"


def _cleanup_old_samples() -> None:
    now = time.time()
    dead_ids: list[str] = []

    for sample_id, info in list(_samples.items()):
        created = float(info.get("created_ts") or 0)
        if now - created > SAMPLE_TTL_SECONDS:
            dead_ids.append(sample_id)

    for sample_id in dead_ids:
        info = _samples.pop(sample_id, {})
        for key in ("raw_path", "mp3_path"):
            path = info.get(key)
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass

    for path in SAMPLE_DIR.glob("rr_*"):
        try:
            if now - path.stat().st_mtime > SAMPLE_TTL_SECONDS:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def _capture_local_ffmpeg_wav_sample(skip_seconds: float | None = None, seconds: int | None = None) -> dict[str, Any]:
    """PC/local: FFmpeg fica ligado ao stream, ignora o pré-roll e grava WAV.

    A amostra antiga repetia porque cada identificação abria uma ligação nova ao
    StreamTheWorld e os primeiros segundos eram sempre iguais. Nesta versão o
    FFmpeg mantém essa ligação aberta, descarta LOCAL_SKIP_START_SECONDS e só
    depois guarda a parte que vai ao Shazam.
    """
    if not STREAM_URL:
        raise RadioError("O stream da rádio não está configurado.")

    _cleanup_old_samples()
    sample_id = uuid.uuid4().hex[:16]
    wav_path = SAMPLE_DIR / f"rr_{sample_id}_local_shazam.wav"
    ffmpeg = _ffmpeg_executable()
    skip_seconds = LOCAL_SKIP_START_SECONDS if skip_seconds is None else max(0.0, min(float(skip_seconds), 120.0))
    seconds = IDENTIFY_SECONDS if seconds is None else max(8, min(int(seconds), 18))

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-user_agent",
        "Mozilla/5.0 (Radio Renascenca Super Deus Local)",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "2",
        "-rw_timeout",
        "30000000",
        "-i",
        STREAM_URL,
        "-ss",
        f"{skip_seconds:.2f}",
        "-t",
        str(seconds),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "44100",
        "-acodec",
        "pcm_s16le",
        "-y",
        str(wav_path),
    ]

    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=int(skip_seconds + seconds + 28),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RadioError("O stream demorou demasiado tempo a fornecer a amostra depois de descartar o início repetido.") from exc

    if completed.returncode != 0:
        detail = _safe_error_detail(completed.stderr)
        raise RadioError(f"Não foi possível gravar a amostra WAV do stream. {detail or 'Erro FFmpeg.'}")

    if not wav_path.exists() or wav_path.stat().st_size < 30_000:
        raise RadioError("A amostra WAV recebida é demasiado pequena para o Shazam.")

    global _latest_sample_id
    info = {
        "sample_id": sample_id,
        "raw_path": str(wav_path),
        "mp3_path": "",
        "created_ts": time.time(),
        "created_at": _now_iso(),
        "content_type": "audio/wav",
        "response_url": STREAM_URL,
        "raw_bytes": wav_path.stat().st_size,
        "capture_seconds": round(time.monotonic() - started, 2),
        "recorded_seconds": seconds,
        "skipped_seconds": round(skip_seconds, 2),
        "download_url": f"/api/sample/{sample_id}",
        "raw_download_url": f"/api/sample/{sample_id}?kind=raw",
        "method": "local_ffmpeg_wav_skip_preroll",
    }
    _samples[sample_id] = info
    _latest_sample_id = sample_id
    return info


def _capture_raw_stream_sample() -> dict[str, Any]:
    """
    Lógica tipo M80 Ballads para Vercel:
    baixa um pedaço real do stream para /tmp em vez de pedir ao FFmpeg para gravar WAV.
    Isto evita amostras pesadas e torna o diagnóstico simples, porque a amostra pode ser descarregada.
    """
    if not STREAM_URL:
        raise RadioError("O stream da rádio não está configurado.")

    _cleanup_old_samples()
    sample_id = uuid.uuid4().hex[:16]
    started = time.monotonic()
    content_type = ""
    response_url = STREAM_URL
    byte_count = 0

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RadioRenascencaSuperDeus/2.0",
        "Accept": "audio/mpeg,audio/aac,audio/*,*/*;q=0.8",
        "Icy-MetaData": "0",
        "Connection": "close",
        "Cache-Control": "no-cache",
    }

    temp_raw = SAMPLE_DIR / f"rr_{sample_id}_raw.tmp"

    try:
        with requests.get(
            STREAM_URL,
            headers=headers,
            stream=True,
            allow_redirects=True,
            timeout=(8, max(12, int(RAW_CAPTURE_SECONDS + 8))),
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            response_url = response.url
            ext = _extension_from_content_type(content_type)
            raw_path = SAMPLE_DIR / f"rr_{sample_id}_sample{ext}"

            with temp_raw.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=16384):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    byte_count += len(chunk)

                    elapsed = time.monotonic() - started
                    if byte_count >= RAW_MAX_BYTES or elapsed >= RAW_CAPTURE_SECONDS:
                        break

        temp_raw.replace(raw_path)
    except requests.RequestException as exc:
        try:
            temp_raw.unlink(missing_ok=True)
        except OSError:
            pass
        raise RadioError(f"Não foi possível baixar a amostra do stream: {exc}") from exc

    elapsed = max(0.01, time.monotonic() - started)
    if not raw_path.exists() or raw_path.stat().st_size < RAW_MIN_BYTES:
        size = raw_path.stat().st_size if raw_path.exists() else 0
        raise RadioError(
            f"A amostra ficou pequena demais para o Shazam ({size} bytes). Tenta novamente quando estiver a tocar música."
        )

    global _latest_sample_id
    info = {
        "sample_id": sample_id,
        "raw_path": str(raw_path),
        "mp3_path": "",
        "created_ts": time.time(),
        "created_at": _now_iso(),
        "content_type": content_type or "audio/mpeg",
        "response_url": response_url,
        "raw_bytes": raw_path.stat().st_size,
        "capture_seconds": round(elapsed, 2),
        "download_url": f"/api/sample/{sample_id}",
        "raw_download_url": f"/api/sample/{sample_id}?kind=raw",
        "method": "tmp_raw_stream_chunk",
    }
    _samples[sample_id] = info
    _latest_sample_id = sample_id
    return info


def _normalize_sample_to_mp3(sample: dict[str, Any]) -> Path:
    existing = sample.get("mp3_path")
    if existing and Path(existing).exists() and Path(existing).stat().st_size > 20_000:
        return Path(existing)

    raw_path = Path(str(sample.get("raw_path") or ""))
    if not raw_path.exists():
        raise RadioError("A amostra já não existe em /tmp. Faz nova identificação.")

    # Se já for MP3, usamos diretamente. Assim é mais rápido no Vercel.
    if raw_path.suffix.lower() == ".mp3" and raw_path.stat().st_size >= RAW_MIN_BYTES:
        sample["mp3_path"] = str(raw_path)
        sample["download_url"] = f"/api/sample/{sample['sample_id']}"
        return raw_path

    ffmpeg = _ffmpeg_executable()
    mp3_path = SAMPLE_DIR / f"rr_{sample['sample_id']}_shazam.mp3"
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(raw_path),
        "-t",
        "11",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "44100",
        "-b:a",
        "128k",
        "-codec:a",
        "libmp3lame",
        str(mp3_path),
    ]

    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=12 if IS_VERCEL else 18,
        check=False,
    )
    if completed.returncode != 0 or not mp3_path.exists() or mp3_path.stat().st_size < 20_000:
        detail = _safe_error_detail(completed.stderr)
        raise RadioError(
            f"A amostra foi criada, mas a conversão para MP3 falhou. {detail or 'FFmpeg não conseguiu converter.'}"
        )

    sample["mp3_path"] = str(mp3_path)
    sample["download_url"] = f"/api/sample/{sample['sample_id']}"
    sample["mp3_bytes"] = mp3_path.stat().st_size
    return mp3_path


def _fallback_ffmpeg_direct_sample() -> dict[str, Any]:
    """Plano B: quando requests não consegue baixar chunk, o FFmpeg grava MP3 direto."""
    _cleanup_old_samples()
    sample_id = uuid.uuid4().hex[:16]
    mp3_path = SAMPLE_DIR / f"rr_{sample_id}_ffmpeg.mp3"
    ffmpeg = _ffmpeg_executable()
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-user_agent",
        "Mozilla/5.0 RadioRenascencaSuperDeus/2.0",
        "-rw_timeout",
        "9000000",
        "-i",
        STREAM_URL,
        "-t",
        str(min(RAW_CAPTURE_SECONDS, 8.5)),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "44100",
        "-b:a",
        "128k",
        "-codec:a",
        "libmp3lame",
        "-y",
        str(mp3_path),
    ]

    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=14 if IS_VERCEL else 20,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RadioError("O FFmpeg demorou demasiado tempo a gravar a amostra.") from exc

    if completed.returncode != 0 or not mp3_path.exists() or mp3_path.stat().st_size < 25_000:
        detail = _safe_error_detail(completed.stderr)
        raise RadioError(f"Não foi possível criar a amostra MP3. {detail or 'Erro FFmpeg.'}")

    global _latest_sample_id
    info = {
        "sample_id": sample_id,
        "raw_path": str(mp3_path),
        "mp3_path": str(mp3_path),
        "created_ts": time.time(),
        "created_at": _now_iso(),
        "content_type": "audio/mpeg",
        "response_url": STREAM_URL,
        "raw_bytes": mp3_path.stat().st_size,
        "mp3_bytes": mp3_path.stat().st_size,
        "capture_seconds": round(time.monotonic() - started, 2),
        "download_url": f"/api/sample/{sample_id}",
        "raw_download_url": f"/api/sample/{sample_id}?kind=raw",
        "method": "ffmpeg_direct_mp3_fallback",
    }
    _samples[sample_id] = info
    _latest_sample_id = sample_id
    return info


def _create_sample(skip_seconds: float | None = None, seconds: int | None = None) -> dict[str, Any]:
    if not _sample_lock.acquire(blocking=False):
        latest = _get_latest_sample(max_age=120)
        if latest:
            return {**_sample_public_payload(latest), "busy": True}
        raise RadioError("Já estou a gravar uma amostra. Tenta novamente dentro de instantes.")

    try:
        if not IS_VERCEL:
            # PC: volta à primeira versão que funcionava contigo.
            sample = _capture_local_ffmpeg_wav_sample(skip_seconds=skip_seconds, seconds=seconds)
            return _sample_public_payload(sample)

        # Vercel: mantém a lógica de chunk/diagnóstico como fallback, mas a identificação
        # principal deve vir de /api/nowplaying antes de qualquer amostra.
        try:
            sample = _capture_raw_stream_sample()
        except RadioError as first_error:
            # Plano B: em alguns streams, o requests recebe playlist/redirect estranho.
            try:
                sample = _fallback_ffmpeg_direct_sample()
                sample["first_method_error"] = str(first_error)
            except RadioError:
                raise first_error
        return _sample_public_payload(sample)
    finally:
        _sample_lock.release()


def _sample_public_payload(sample: dict[str, Any]) -> dict[str, Any]:
    raw_path = Path(str(sample.get("raw_path") or ""))
    mp3_path = Path(str(sample.get("mp3_path") or "")) if sample.get("mp3_path") else None
    return {
        "ok": True,
        "sample_id": sample.get("sample_id"),
        "created_at": sample.get("created_at"),
        "capture_seconds": sample.get("capture_seconds"),
        "recorded_seconds": sample.get("recorded_seconds"),
        "skipped_seconds": sample.get("skipped_seconds"),
        "raw_bytes": raw_path.stat().st_size if raw_path.exists() else sample.get("raw_bytes", 0),
        "mp3_bytes": mp3_path.stat().st_size if mp3_path and mp3_path.exists() else sample.get("mp3_bytes", 0),
        "content_type": sample.get("content_type", "audio/mpeg"),
        "download_url": sample.get("download_url"),
        "raw_download_url": sample.get("raw_download_url"),
        "method": sample.get("method"),
        "platform": "vercel" if IS_VERCEL else "local",
    }


def _get_latest_sample(max_age: int = SAMPLE_TTL_SECONDS) -> dict[str, Any] | None:
    if not _latest_sample_id:
        return None
    sample = _samples.get(_latest_sample_id)
    if not sample:
        return None
    if time.time() - float(sample.get("created_ts") or 0) > max_age:
        return None
    raw_path = Path(str(sample.get("raw_path") or ""))
    if not raw_path.exists():
        return None
    return sample


def _get_sample(sample_id: str | None) -> dict[str, Any]:
    _cleanup_old_samples()
    sample: dict[str, Any] | None = None
    if sample_id:
        sample = _samples.get(sample_id)
    else:
        sample = _get_latest_sample()

    if not sample:
        raise RadioError("A amostra não foi encontrada. Grava uma nova amostra.")

    raw_path = Path(str(sample.get("raw_path") or ""))
    if not raw_path.exists() or raw_path.stat().st_size < 10_000:
        raise RadioError("A amostra expirou no /tmp do Vercel. Grava uma nova amostra.")
    return sample


def _run_coroutine(coro: Any) -> Any:
    """Executa uma coroutine num contexto Flask síncrono."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover
            error["value"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "value" in error:
        raise error["value"]
    return result.get("value")


def _recognize_with_shazam(sample_path: Path) -> dict[str, Any] | None:
    try:
        from shazamio import Shazam
    except ImportError as exc:
        raise RadioError(
            "A biblioteca ShazamIO não está instalada. Executa 'pip install -r requirements.txt'."
        ) from exc

    async def recognize() -> dict[str, Any]:
        shazam = Shazam()
        return await shazam.recognize(str(sample_path))

    response = _run_coroutine(recognize())
    if not isinstance(response, dict):
        return None
    track = response.get("track")
    return track if isinstance(track, dict) else None


def _upscale_apple_artwork(url: str) -> str:
    if not url:
        return ""
    return re.sub(r"/\d+x\d+(?:bb)?\.(jpg|png)$", r"/1000x1000bb.\1", url)


def _itunes_metadata(artist: str, title: str) -> dict[str, str]:
    if not artist or not title:
        return {}

    try:
        response = requests.get(
            "https://itunes.apple.com/search",
            params={
                "term": f"{artist} {title}",
                "entity": "song",
                "limit": 8,
                "country": "PT",
            },
            headers={"User-Agent": "RadioRenascencaSuperDeus/2.0"},
            timeout=8,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
    except (requests.RequestException, ValueError):
        return {}

    if not results:
        return {}

    title_words = set(re.findall(r"[a-z0-9]+", title.lower()))
    artist_words = set(re.findall(r"[a-z0-9]+", artist.lower()))

    def score(item: dict[str, Any]) -> int:
        candidate_title = set(re.findall(r"[a-z0-9]+", str(item.get("trackName", "")).lower()))
        candidate_artist = set(re.findall(r"[a-z0-9]+", str(item.get("artistName", "")).lower()))
        return 3 * len(title_words & candidate_title) + 2 * len(artist_words & candidate_artist)

    best = max(results, key=score)
    return {
        "cover": _upscale_apple_artwork(_clean_text(best.get("artworkUrl100"))),
        "album": _clean_text(best.get("collectionName")),
        "preview": _clean_text(best.get("previewUrl")),
        "apple_url": _clean_text(best.get("trackViewUrl")),
    }


def _itunes_guess_from_text(text: str) -> dict[str, str]:
    """Tenta separar artista/título quando a playlist vem sem hífen.

    Exemplo comum no Online Radio Box: "Earth, Wind & Fire After the Love Is Gone".
    A pesquisa do iTunes devolve artista e faixa separados, evitando inventar a divisão.
    """
    text = _clean_text(text)
    if len(text) < 5:
        return {}

    try:
        response = requests.get(
            "https://itunes.apple.com/search",
            params={"term": text, "entity": "song", "limit": 10, "country": "PT"},
            headers={"User-Agent": "RadioRenascencaSuperDeus/7.0"},
            timeout=8,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
    except (requests.RequestException, ValueError):
        return {}

    query_words = set(re.findall(r"[a-z0-9]+", text.lower()))
    if not query_words:
        return {}

    def score(item: dict[str, Any]) -> int:
        title = str(item.get("trackName", ""))
        artist = str(item.get("artistName", ""))
        both_words = set(re.findall(r"[a-z0-9]+", f"{artist} {title}".lower()))
        title_words = set(re.findall(r"[a-z0-9]+", title.lower()))
        artist_words = set(re.findall(r"[a-z0-9]+", artist.lower()))
        return 4 * len(query_words & title_words) + 3 * len(query_words & artist_words) + len(query_words & both_words)

    candidates = [item for item in results if isinstance(item, dict) and item.get("trackName") and item.get("artistName")]
    if not candidates:
        return {}
    best = max(candidates, key=score)
    if score(best) < 4:
        return {}

    return {
        "artist": _clean_text(best.get("artistName")),
        "title": _clean_text(best.get("trackName")),
        "album": _clean_text(best.get("collectionName")),
        "cover": _upscale_apple_artwork(_clean_text(best.get("artworkUrl100"))),
        "preview": _clean_text(best.get("previewUrl")),
        "apple_url": _clean_text(best.get("trackViewUrl")),
    }


def _payload_from_loose_song_text(text: str, source: str) -> dict[str, Any] | None:
    text = _clean_text(text)
    split_artist, split_title = _split_artist_title(text)
    if split_artist and split_title:
        return _payload_from_artist_title(split_artist, split_title, source, text)

    guessed = _itunes_guess_from_text(text)
    if not guessed:
        return None
    artist = guessed.get("artist", "")
    title = guessed.get("title", "")
    if _looks_like_station_or_ad(artist, title, text):
        return None
    return {
        "ok": True,
        "identified": True,
        "title": title,
        "artist": artist,
        "album": guessed.get("album", ""),
        "cover": guessed.get("cover", ""),
        "background": "",
        "shazam_url": "",
        "apple_url": guessed.get("apple_url", ""),
        "preview": guessed.get("preview", ""),
        "identified_at": _now_iso(),
        "source": source,
        "source_detail": text[:400],
        "sample_seconds": 0,
    }


def _normalise_for_compare(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _first_prop(props: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = _clean_text(props.get(name, ""))
        if value:
            return value
    return ""


def _split_artist_title(text: str) -> tuple[str, str]:
    text = _clean_text(text)
    text = re.sub(r"\s+", " ", text).strip(" -–—|/")
    if not text:
        return "", ""

    for sep in (" - ", " – ", " — ", " | ", " / "):
        if sep in text:
            left, right = [part.strip() for part in text.split(sep, 1)]
            if left and right:
                return left, right

    return "", text


def _looks_like_station_or_ad(artist: str, title: str, raw: str = "") -> bool:
    """Rejeita nomes da estação/programas que NÃO são músicas.

    Na v3, a página Radioonline podia devolver o título HTML
    "Radio Renascença online — Radioonline.com.pt" e a app aceitava isso
    como se fosse "artista — música". Agora só validamos o artista/título
    e rejeitamos explicitamente nomes da rádio, páginas, programas e notícias.
    """
    artist_norm = _normalise_for_compare(artist)
    title_norm = _normalise_for_compare(title)
    raw_norm = _normalise_for_compare(raw)
    combined = f"{artist_norm} {title_norm}".strip()
    if not combined:
        return True

    blocked_anywhere = (
        "radio renascenca",
        "renascenca online",
        "radioonline",
        "myradioonline",
        "radioplayer",
        "sempre nunca igual",
        "emissao em direto",
        "ouvir online",
        "ao vivo",
        "sem som",
        "website",
        "portugal",
    )
    if any(word in combined or word in raw_norm for word in blocked_anywhere):
        return True

    # Rejeita conteúdos típicos de rádio falada/anúncios quando aparecem como título.
    blocked_title_words = (
        "publicidade",
        "advertisement",
        "commercial",
        "noticias",
        "informacao",
        "jornal",
        "bola branca",
        "programa",
        "podcast",
        "entrevista",
    )
    if any(word in artist_norm or word in title_norm for word in blocked_title_words):
        return True

    if artist_norm == title_norm:
        return True

    if len(artist_norm) < 2 or len(title_norm) < 2:
        return True

    return False


def _payload_from_artist_title(
    artist: str,
    title: str,
    source: str,
    raw: str = "",
    sample: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    artist = _clean_text(artist)
    title = _clean_text(title)

    # Alguns feeds devolvem tudo no título.
    if not artist and title:
        split_artist, split_title = _split_artist_title(title)
        if split_artist and split_title:
            artist, title = split_artist, split_title

    if not artist or not title or _looks_like_station_or_ad(artist, title, raw):
        return None

    itunes = _itunes_metadata(artist, title)
    payload = {
        "ok": True,
        "identified": True,
        "title": title,
        "artist": artist,
        "album": itunes.get("album", ""),
        "cover": itunes.get("cover", ""),
        "background": "",
        "shazam_url": "",
        "apple_url": itunes.get("apple_url", ""),
        "preview": itunes.get("preview", ""),
        "identified_at": _now_iso(),
        "source": source,
        "source_detail": raw[:400] if raw else "",
        "sample_seconds": 0,
    }
    if sample:
        raw_path = Path(str(sample.get("raw_path") or ""))
        payload.update(
            {
                "sample_id": sample.get("sample_id"),
                "sample_seconds": sample.get("capture_seconds"),
                "sample_bytes": raw_path.stat().st_size if raw_path.exists() else sample.get("raw_bytes", 0),
                "sample_url": sample.get("download_url"),
                "sample_method": sample.get("method"),
            }
        )
    return payload


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse_triton_xml(xml_text: str, mount: str) -> dict[str, Any] | None:
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return None

    for node in root.iter():
        if _local_name(node.tag) != "nowplaying-info":
            continue

        props: dict[str, str] = {}
        for child in list(node):
            if _local_name(child.tag) != "property":
                continue
            key = _clean_text(child.attrib.get("name", "")).lower()
            value = _clean_text(" ".join(child.itertext()))
            if key and value:
                props[key] = value

        artist = _first_prop(
            props,
            (
                "cue_artist",
                "artist",
                "artistname",
                "track_artist",
                "trackartist",
                "song_artist",
                "program_artist",
            ),
        )
        title = _first_prop(
            props,
            (
                "cue_title",
                "title",
                "track_title",
                "tracktitle",
                "song_title",
                "program_title",
                "cue_name",
            ),
        )

        raw_joined = " | ".join(f"{k}={v}" for k, v in props.items())
        payload = _payload_from_artist_title(
            artist=artist,
            title=title,
            source=f"triton_nowplaying:{mount}",
            raw=raw_joined,
        )
        if payload:
            return payload

        # Alguns mounts devolvem só uma propriedade com “Artista - Música”.
        for key in ("cue_title", "title", "track_title", "nowplaying", "song"):
            split_artist, split_title = _split_artist_title(props.get(key, ""))
            payload = _payload_from_artist_title(
                artist=split_artist,
                title=split_title,
                source=f"triton_nowplaying:{mount}",
                raw=raw_joined,
            )
            if payload:
                return payload

    return None


def _triton_nowplaying() -> dict[str, Any] | None:
    errors: list[str] = []
    for mount in TRITON_MOUNTS:
        try:
            response = requests.get(
                "https://np.tritondigital.com/public/nowplaying",
                params={
                    "mountName": mount,
                    "numberToFetch": 5,
                    "eventType": "track",
                    "rnd": str(int(time.time())),
                },
                headers={"User-Agent": "RadioRenascencaSuperDeus/5.0"},
                timeout=7,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            errors.append(f"{mount}: {exc}")
            continue

        payload = _parse_triton_xml(response.text, mount)
        if payload:
            return payload
        errors.append(f"{mount}: sem música nos metadados")

    if errors:
        _metadata_cache["error"] = "; ".join(errors)[-600:]
    return None


def _read_icy_stream_title() -> dict[str, Any] | None:
    """Plano B: tenta ler StreamTitle diretamente do stream."""
    headers = {
        "User-Agent": "Mozilla/5.0 RadioRenascencaSuperDeus/5.0",
        "Icy-MetaData": "1",
        "Accept": "audio/mpeg,audio/aac,audio/*,*/*;q=0.8",
        "Cache-Control": "no-cache",
    }
    try:
        with requests.get(
            STREAM_URL,
            headers=headers,
            stream=True,
            allow_redirects=True,
            timeout=(7, 13),
        ) as response:
            response.raise_for_status()
            metaint = int(response.headers.get("icy-metaint") or response.headers.get("Icy-MetaInt") or 0)
            if metaint <= 0 or metaint > 2_000_000:
                return None
            response.raw.read(metaint)
            size_byte = response.raw.read(1)
            if not size_byte:
                return None
            metadata_len = size_byte[0] * 16
            if metadata_len <= 0:
                return None
            metadata = response.raw.read(metadata_len).decode("utf-8", errors="ignore")
    except Exception:
        return None

    match = re.search(r"StreamTitle=['\"]([^'\"]+)['\"]", metadata, re.I)
    if not match:
        return None

    raw_title = _clean_text(match.group(1))
    artist, title = _split_artist_title(raw_title)
    return _payload_from_artist_title(artist, title, "icy_streamtitle", raw_title)


def _payload_from_dash_text(text: str, source: str) -> dict[str, Any] | None:
    """Converte texto tipo 'Artista - Música' num payload seguro."""
    artist, title = _split_artist_title(text)
    return _payload_from_artist_title(artist, title, source, text)



def _onlineradiobox_nowplaying() -> dict[str, Any] | None:
    """Playlist pública do Online Radio Box, com parse seguro.

    A página mostra uma secção "Ao vivo agora Radio Renascença" com a faixa atual.
    Não usamos o título da página e nunca aceitamos "Radio Renascença online" como música.
    """
    url = "https://onlineradiobox.com/pt/renascenca/"
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 RadioRenascencaSuperDeus/7.0",
                "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.6",
                "Cache-Control": "no-cache",
            },
            timeout=8,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        _metadata_cache["error"] = f"onlineradiobox: {exc}"
        return None

    text = re.sub(r"<script[\s\S]*?</script>", " ", response.text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    # Mantém o texto dos anchors, porque é onde aparece a música.
    text = re.sub(r"<[^>]+>", "\n", text)
    text = (
        text.replace("&nbsp;", " ")
            .replace("&#160;", " ")
            .replace("&amp;", "&")
            .replace("&quot;", '"')
            .replace("&#039;", "'")
    )
    lines = [_clean_text(line) for line in text.splitlines()]
    lines = [line for line in lines if line]

    for index, line in enumerate(lines):
        norm = _normalise_for_compare(line)
        if norm == "ao vivo agora radio renascenca":
            # Normalmente a linha seguinte começa por "Ao vivo <artista música>".
            for candidate in lines[index + 1:index + 8]:
                c = re.sub(r"^Ao\s+vivo\s+", "", candidate, flags=re.I).strip()
                c = re.sub(r"^Live\s+", "", c, flags=re.I).strip()
                if not c or re.match(r"^\d{1,2}:\d{2}\b", c):
                    continue
                payload = _payload_from_loose_song_text(c, "onlineradiobox_live")
                if payload:
                    return payload

    # Fallback por regex em texto colapsado.
    collapsed = re.sub(r"\s+", " ", " ".join(lines))
    match = re.search(
        r"Ao vivo agora Radio Renascença\s+Ao vivo\s+(?P<song>.+?)(?=\s+\d{1,2}:\d{2}\s+|\s+Playlist da Radio Renascença|\s+Principais músicas|$)",
        collapsed,
        flags=re.I,
    )
    if match:
        candidate = _clean_text(match.group("song"))
        payload = _payload_from_loose_song_text(candidate, "onlineradiobox_live")
        if payload:
            return payload

    return None


def _myradioonline_playlist_nowplaying() -> dict[str, Any] | None:
    """Fonte mais fiável no Vercel: playlist pública da MyRadioOnline.

    Esta página já lista a faixa LIVE da Renascença em texto simples, por exemplo:
    "LIVE - 13.06 01:35 - MARK AMBOR - BELONG TOGETHER".
    Como a amostra do StreamTheWorld no Vercel repete o mesmo pré-roll, esta fonte
    passa a ser a primeira escolha no Vercel.
    """
    url = "https://myradioonline.pt/renascenca/playlist"
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 RadioRenascencaSuperDeus/5.0",
                "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.6",
            },
            timeout=8,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        _metadata_cache["error"] = f"myradioonline: {exc}"
        return None

    # O HTML costuma conter texto do género:
    # LIVE - 13.06 01:35 - MARK AMBOR - BELONG TOGETHER
    html = response.text
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"&nbsp;|&#160;", " ", text, flags=re.I)
    lines = [_clean_text(line) for line in text.splitlines()]
    lines = [line for line in lines if line]

    live_patterns = [
        r"^LIVE\s*-\s*\d{1,2}\.\d{1,2}\s+\d{1,2}:\d{2}\s*-\s*(?P<artist>.+?)\s*-\s*(?P<title>.+?)\s*$",
        r"^\d{1,2}\.\d{1,2}\s+\d{1,2}:\d{2}\s*-\s*(?P<artist>.+?)\s*-\s*(?P<title>.+?)\s*$",
    ]

    # Primeiro tenta linha a linha para não juntar a música seguinte ao título atual.
    for line in lines[:80]:
        for pattern in live_patterns:
            match = re.search(pattern, line, flags=re.I)
            if not match:
                continue
            artist = _clean_text(match.group("artist"))
            title = _clean_text(match.group("title"))
            payload = _payload_from_artist_title(artist, title, "myradioonline_playlist_live", line)
            if payload:
                return payload

    # Fallback: texto colapsado, caso o HTML venha minificado sem quebras.
    collapsed = re.sub(r"\s+", " ", " ".join(lines))
    match = re.search(
        r"LIVE\s*-\s*\d{1,2}\.\d{1,2}\s+\d{1,2}:\d{2}\s*-\s*(?P<artist>[^\-–—]{2,120})\s*-\s*(?P<title>.+?)(?=\s+\d{1,2}\.\d{1,2}\s+\d{1,2}:\d{2}\s*-|\s+Próxima página|\s+©|$)",
        collapsed,
        flags=re.I,
    )
    if match:
        artist = _clean_text(match.group("artist"))
        title = _clean_text(match.group("title"))
        payload = _payload_from_artist_title(artist, title, "myradioonline_playlist_live", match.group(0))
        if payload:
            return payload

    return None


def _radioplayer_nowplaying() -> dict[str, Any] | None:
    """Fonte de metadados pública com Now Playing/Last played songs.

    Esta fonte costuma devolver o artista numa linha e o título na seguinte,
    por isso não inventamos separadores quando não existem.
    """
    url = "https://play.radioplayer.org/en/live/NjIwfjgwODM"
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 RadioRenascencaSuperDeus/5.0"},
            timeout=8,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        _metadata_cache["error"] = f"radioplayer: {exc}"
        return None

    text = re.sub(r"<script[\s\S]*?</script>", " ", response.text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    lines = [_clean_text(line) for line in text.splitlines()]
    lines = [line for line in lines if line]

    noise = {
        "home", "live", "podcasts", "search", "settings", "now playing",
        "last played songs", "related stations", "radioplayer.org", "renascença .pt",
        "música para sentir", "image", "pause", "play",
    }

    try:
        start = next(i for i, line in enumerate(lines) if _normalise_for_compare(line) == "now playing")
    except StopIteration:
        start = 0

    useful: list[str] = []
    for line in lines[start + 1:start + 18]:
        n = _normalise_for_compare(line)
        if not n or n in noise or n.startswith("image"):
            continue
        if n.startswith("related stations"):
            break
        useful.append(line)

    # Estrutura normal: Artista numa linha, título noutra.
    if len(useful) >= 2:
        artist, title = useful[0], useful[1]
        payload = _payload_from_artist_title(artist, title, "radioplayer_nowplaying", f"{artist} - {title}")
        if payload:
            return payload

    return None


def _radioonline_nowplaying() -> dict[str, Any] | None:
    """Fonte pública ACRCloud/Radioonline, mas com parse estrito.

    Não usamos o <title> da página. Só aceitamos alt de imagens no formato
    'Artista - Música', que é como a página apresenta a música atual.
    """
    url = "https://radioonline.com.pt/renascenca/"
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 RadioRenascencaSuperDeus/5.0"},
            timeout=8,
        )
        response.raise_for_status()
        html = response.text
    except requests.RequestException as exc:
        _metadata_cache["error"] = f"radioonline: {exc}"
        return None

    # Primeiro: procurar alt='Artista - Música'. É o formato mais seguro.
    alt_values = re.findall(r"<img[^>]+alt=[\"']([^\"']+)[\"']", html, flags=re.I)
    for alt in alt_values:
        candidate = _clean_text(alt)
        if " - " not in candidate and " – " not in candidate and " — " not in candidate:
            continue
        payload = _payload_from_dash_text(candidate, "radioonline_image_alt")
        if payload:
            return payload

    return None


def _identify_from_nowplaying(force: bool = False) -> dict[str, Any] | None:
    now = time.time()
    cached = _metadata_cache.get("payload")
    cached_at = float(_metadata_cache.get("timestamp") or 0)
    if not force and cached and now - cached_at < NOWPLAYING_CACHE_SECONDS:
        return {**cached, "cached": True}

    providers = (
        _onlineradiobox_nowplaying,
        _myradioonline_playlist_nowplaying,
        _radioplayer_nowplaying,
        _triton_nowplaying,
        _radioonline_nowplaying,
        _read_icy_stream_title,
    )
    for provider in providers:
        payload = provider()
        if payload:
            _metadata_cache["timestamp"] = time.time()
            _metadata_cache["payload"] = payload
            _metadata_cache["error"] = ""
            return {**payload, "cached": False}

    return None


def _extract_track_payload(track: dict[str, Any], sample: dict[str, Any] | None = None) -> dict[str, Any]:
    title = _clean_text(track.get("title"), "Música desconhecida")
    artist = _clean_text(track.get("subtitle"), "Artista desconhecido")
    images = track.get("images") if isinstance(track.get("images"), dict) else {}
    shazam_url = _clean_text(track.get("url"))

    cover = _clean_text(images.get("coverart") or images.get("coverarthq"))
    background = _clean_text(images.get("background"))
    album = ""

    sections = track.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            metadata = section.get("metadata")
            if not isinstance(metadata, list):
                continue
            for item in metadata:
                if not isinstance(item, dict):
                    continue
                label = _clean_text(item.get("title")).lower()
                if label in {"album", "álbum"}:
                    album = _clean_text(item.get("text"))

    itunes = _itunes_metadata(artist, title)
    cover = itunes.get("cover") or cover
    album = album or itunes.get("album", "")

    payload = {
        "ok": True,
        "identified": True,
        "title": title,
        "artist": artist,
        "album": album,
        "cover": cover,
        "background": background,
        "shazam_url": shazam_url,
        "apple_url": itunes.get("apple_url", ""),
        "preview": itunes.get("preview", ""),
        "identified_at": _now_iso(),
    }
    if sample:
        payload.update(
            {
                "sample_id": sample.get("sample_id"),
                "sample_seconds": sample.get("capture_seconds"),
                "sample_recorded_seconds": sample.get("recorded_seconds"),
                "sample_skipped_seconds": sample.get("skipped_seconds"),
                "sample_bytes": Path(str(sample.get("raw_path") or "")).stat().st_size,
                "sample_url": sample.get("download_url"),
                "sample_method": sample.get("method"),
            }
        )
    return payload


def _identify_sample(sample: dict[str, Any], force: bool = False) -> dict[str, Any]:
    now = time.time()
    cached = _identify_cache.get("payload")
    cached_at = float(_identify_cache.get("timestamp") or 0)
    if not force and cached and now - cached_at < IDENTIFY_CACHE_SECONDS:
        return {**cached, "cached": True}

    # No PC mantemos a lógica da primeira versão: amostra direta + Shazam.
    # No Vercel os metadados são tentados antes, na rota /api/identify,
    # para evitar abrir uma nova ligação que cai sempre no mesmo pré-roll.

    if not _identify_lock.acquire(blocking=False):
        if cached:
            return {**cached, "cached": True, "busy": True}
        raise RadioError("Já existe uma identificação em curso. Tenta novamente dentro de instantes.")

    try:
        # PC/local: usa o WAV bruto como a primeira versão, sem converter primeiro.
        # Vercel: usa MP3 normalizado para ser mais leve e compatível.
        if not IS_VERCEL and str(sample.get("method", "")).startswith("local_ffmpeg_wav"):
            sample_path = Path(str(sample.get("raw_path") or ""))
            if not sample_path.exists():
                raise RadioError("A amostra local já não existe. Faz nova identificação.")
        else:
            try:
                sample_path = _normalize_sample_to_mp3(sample)
            except RadioError:
                # Mesmo que a normalização falhe, tentamos o ficheiro bruto.
                sample_path = Path(str(sample.get("raw_path") or ""))
                if not sample_path.exists():
                    raise

        track = _recognize_with_shazam(sample_path)
        if not track and sample_path != Path(str(sample.get("raw_path") or "")):
            # Plano B: Shazam com o ficheiro bruto.
            raw_path = Path(str(sample.get("raw_path") or ""))
            if raw_path.exists():
                track = _recognize_with_shazam(raw_path)

        if not track:
            payload = {
                "ok": True,
                "identified": False,
                "message": "O Shazam não reconheceu esta amostra. Pode estar a passar voz, publicidade, notícia ou a amostra ficou com pouca música.",
                "identified_at": _now_iso(),
                "sample_id": sample.get("sample_id"),
                "sample_seconds": sample.get("capture_seconds"),
                "sample_recorded_seconds": sample.get("recorded_seconds"),
                "sample_skipped_seconds": sample.get("skipped_seconds"),
                "sample_bytes": Path(str(sample.get("raw_path") or "")).stat().st_size,
                "sample_url": sample.get("download_url"),
                "sample_method": sample.get("method"),
            }
        else:
            payload = _extract_track_payload(track, sample)

        _identify_cache["timestamp"] = time.time()
        _identify_cache["payload"] = payload
        return {**payload, "cached": False}
    finally:
        _identify_lock.release()


@app.after_request
def add_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/")
def index():
    return render_template(
        "index.html",
        station_name=STATION_NAME,
        station_subtitle=STATION_SUBTITLE,
        stream_url=STREAM_URL,
    )


@app.get("/api/status")
def status():
    ffmpeg_ok = True
    ffmpeg_error = ""
    try:
        _ffmpeg_executable()
    except RadioError as exc:
        ffmpeg_ok = False
        ffmpeg_error = str(exc)

    latest = _get_latest_sample(max_age=SAMPLE_TTL_SECONDS)
    return jsonify(
        {
            "ok": True,
            "app": "Rádio Renascença — Modo Super Deus v7",
            "station": STATION_NAME,
            "stream_configured": bool(STREAM_URL),
            "stream_url": STREAM_URL,
            "ffmpeg_ready": ffmpeg_ok,
            "ffmpeg_error": ffmpeg_error,
            "capture_seconds": RAW_CAPTURE_SECONDS,
            "identify_seconds_local": IDENTIFY_SECONDS,
            "raw_min_bytes": RAW_MIN_BYTES,
            "raw_max_bytes": RAW_MAX_BYTES,
            "identification_logic": "local=ffmpeg_skip_preroll_wav_shazam_then_playlist_fallback; vercel=playlist_live_then_shazam_sample",
            "sample_logic": "local=ffmpeg_waits_same_connection_discards_preroll_then_wav; vercel=diagnostic_only_because_stream_preroll_repeats",
            "local_skip_start_seconds": LOCAL_SKIP_START_SECONDS,
            "triton_mounts": TRITON_MOUNTS,
            "nowplaying_cache_seconds": NOWPLAYING_CACHE_SECONDS,
            "latest_metadata": _metadata_cache.get("payload"),
            "metadata_error": _metadata_cache.get("error", ""),
            "latest_sample": _sample_public_payload(latest) if latest else None,
            "server_time": _now_iso(),
            "platform": "vercel" if IS_VERCEL else "local",
        }
    )


def _request_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = request.args.get(name, "").strip()
    if not raw and request.is_json:
        body = request.get_json(silent=True) or {}
        raw = str(body.get(name, "")).strip()
    if not raw:
        return default
    try:
        return max(minimum, min(float(raw), maximum))
    except ValueError:
        return default


@app.route("/api/sample", methods=["GET", "POST"])
def sample():
    try:
        skip_seconds = _request_float("skip", LOCAL_SKIP_START_SECONDS, 0.0, 120.0)
        seconds = int(_request_float("seconds", float(IDENTIFY_SECONDS), 8.0, 18.0))
        payload = _create_sample(skip_seconds=skip_seconds, seconds=seconds)
        return jsonify(payload)
    except RadioError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    except Exception as exc:
        app.logger.exception("Erro inesperado ao criar amostra: %s", exc)
        return jsonify({"ok": False, "error": "Erro inesperado ao criar a amostra."}), 500


@app.route("/api/identify", methods=["GET", "POST"])
def identify():
    force = request.args.get("force", "0") == "1"
    json_body = request.get_json(silent=True) if request.is_json else {}
    sample_id = request.args.get("sample_id") or (json_body or {}).get("sample_id")

    try:
        # PC: mantém a primeira versão que funcionou — grava amostra e usa Shazam.
        # Vercel: tenta primeiro metadados fiáveis para fugir ao pré-roll repetido.
        if IS_VERCEL and not sample_id:
            metadata_payload = _identify_from_nowplaying(force=force)
            if metadata_payload:
                _identify_cache["timestamp"] = time.time()
                _identify_cache["payload"] = metadata_payload
                return jsonify({**metadata_payload, "cached": bool(metadata_payload.get("cached"))})

        if not sample_id:
            skip_seconds = _request_float("skip", LOCAL_SKIP_START_SECONDS, 0.0, 120.0)
            seconds = int(_request_float("seconds", float(IDENTIFY_SECONDS), 8.0, 18.0))
            sample_payload = _create_sample(skip_seconds=skip_seconds, seconds=seconds)
            sample_id = str(sample_payload.get("sample_id"))

        sample_info = _get_sample(sample_id)
        shazam_payload = _identify_sample(sample_info, force=force)
        if (not IS_VERCEL) and not shazam_payload.get("identified"):
            metadata_payload = _identify_from_nowplaying(force=True)
            if metadata_payload and metadata_payload.get("identified"):
                metadata_payload["sample_id"] = sample_info.get("sample_id")
                metadata_payload["sample_url"] = sample_info.get("download_url")
                metadata_payload["sample_method"] = sample_info.get("method")
                metadata_payload["sample_seconds"] = sample_info.get("capture_seconds")
                metadata_payload["sample_skipped_seconds"] = sample_info.get("skipped_seconds")
                metadata_payload["source_detail"] = "Fallback playlist/live depois de Shazam não reconhecer a amostra."
                return jsonify(metadata_payload)
        return jsonify(shazam_payload)
    except RadioError as exc:
        return jsonify({"ok": False, "identified": False, "error": str(exc)}), 503
    except Exception as exc:
        app.logger.exception("Erro inesperado na identificação: %s", exc)
        return jsonify({"ok": False, "identified": False, "error": "Ocorreu um erro inesperado durante a identificação."}), 500


@app.get("/api/nowplaying")
def nowplaying():
    force = request.args.get("force", "0") == "1"
    payload = _identify_from_nowplaying(force=force)
    if payload:
        return jsonify(payload)
    return jsonify({
        "ok": True,
        "identified": False,
        "message": "Não encontrei metadados Now Playing neste momento.",
        "metadata_error": _metadata_cache.get("error", ""),
        "mounts": TRITON_MOUNTS,
    })


@app.get("/api/sample/latest")
def latest_sample_download():
    sample_info = _get_latest_sample(max_age=SAMPLE_TTL_SECONDS)
    if not sample_info:
        return jsonify({"ok": False, "error": "Ainda não existe amostra ou ela expirou."}), 404
    return _send_sample(sample_info)


@app.get("/api/sample/<sample_id>")
def download_sample(sample_id: str):
    try:
        sample_info = _get_sample(sample_id)
        return _send_sample(sample_info)
    except RadioError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404


def _send_sample(sample_info: dict[str, Any]):
    kind = request.args.get("kind", "mp3").lower()
    path: Path
    mimetype = "audio/mpeg"

    if kind == "raw":
        path = Path(str(sample_info.get("raw_path") or ""))
        mimetype = sample_info.get("content_type") or "application/octet-stream"
    else:
        try:
            path = _normalize_sample_to_mp3(sample_info)
        except RadioError:
            path = Path(str(sample_info.get("raw_path") or ""))
            mimetype = sample_info.get("content_type") or "application/octet-stream"

    if not path.exists():
        return jsonify({"ok": False, "error": "A amostra já expirou em /tmp."}), 404

    download_name = f"radio-renascenca-amostra-{sample_info.get('sample_id')}{path.suffix or '.mp3'}"
    return send_file(
        path,
        mimetype=mimetype,
        as_attachment=True,
        download_name=download_name,
        max_age=0,
    )


@app.get("/health")
def health():
    return jsonify({"ok": True, "station": STATION_NAME, "platform": "vercel" if IS_VERCEL else "local"})


if __name__ == "__main__":
    print("\n✨ Rádio Renascença — Modo Super Deus v7")
    print("🌐 Abre: http://127.0.0.1:5000\n")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
