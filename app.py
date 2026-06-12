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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, render_template, request


BASE_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

STREAM_URL = os.getenv(
    "RADIO_STREAM_URL",
    "https://29053.live.streamtheworld.com/RADIO_RENASCENCA_SC?dist=onlineradiobox",
).strip()
STATION_NAME = os.getenv("RADIO_NAME", "Rádio Renascença").strip()
STATION_SUBTITLE = os.getenv("RADIO_SUBTITLE", "Sempre nunca igual").strip()
IDENTIFY_SECONDS = max(6, min(int(os.getenv("IDENTIFY_SECONDS", "8")), 14))
IDENTIFY_CACHE_SECONDS = max(20, int(os.getenv("IDENTIFY_CACHE_SECONDS", "70")))

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
app.config["JSON_AS_ASCII"] = False

_identify_lock = threading.Lock()
_identify_cache: dict[str, Any] = {"timestamp": 0.0, "payload": None}


class RadioError(RuntimeError):
    """Erro controlado da aplicação de rádio."""


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
            "FFmpeg não foi encontrado. Executa 'pip install -r requirements.txt'."
        ) from exc

    raise RadioError("FFmpeg não foi encontrado neste ambiente.")


def _record_stream_sample(output_path: Path) -> None:
    ffmpeg = _ffmpeg_executable()

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-user_agent",
        "Mozilla/5.0 (Radio Renascenca Super Deus)",
        "-rw_timeout",
        "15000000",
        "-i",
        STREAM_URL,
        "-t",
        str(IDENTIFY_SECONDS),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "44100",
        "-acodec",
        "pcm_s16le",
        "-y",
        str(output_path),
    ]

    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=IDENTIFY_SECONDS + 18,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RadioError("O stream demorou demasiado tempo a fornecer a amostra.") from exc

    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="ignore").strip()
        detail = re.sub(r"\s+", " ", detail)[-320:]
        raise RadioError(
            f"Não foi possível gravar a amostra do stream. {detail or 'Erro FFmpeg.'}"
        )

    if not output_path.exists() or output_path.stat().st_size < 30_000:
        raise RadioError("A amostra recebida é demasiado pequena para o Shazam.")


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


def _clean_text(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    return re.sub(r"\s+", " ", text) or fallback


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
            headers={"User-Agent": "RadioRenascencaSuperDeus/1.0"},
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
        candidate_title = set(
            re.findall(r"[a-z0-9]+", str(item.get("trackName", "")).lower())
        )
        candidate_artist = set(
            re.findall(r"[a-z0-9]+", str(item.get("artistName", "")).lower())
        )
        return 3 * len(title_words & candidate_title) + 2 * len(
            artist_words & candidate_artist
        )

    best = max(results, key=score)
    return {
        "cover": _upscale_apple_artwork(_clean_text(best.get("artworkUrl100"))),
        "album": _clean_text(best.get("collectionName")),
        "preview": _clean_text(best.get("previewUrl")),
        "apple_url": _clean_text(best.get("trackViewUrl")),
    }


def _extract_track_payload(track: dict[str, Any]) -> dict[str, Any]:
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

    return {
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
        "identified_at": datetime.now(timezone.utc).isoformat(),
        "sample_seconds": IDENTIFY_SECONDS,
    }


def _identify_now(force: bool = False) -> dict[str, Any]:
    now = time.time()
    cached = _identify_cache.get("payload")
    cached_at = float(_identify_cache.get("timestamp") or 0)
    if not force and cached and now - cached_at < IDENTIFY_CACHE_SECONDS:
        return {**cached, "cached": True}

    if not _identify_lock.acquire(blocking=False):
        if cached:
            return {**cached, "cached": True, "busy": True}
        raise RadioError("Já existe uma identificação em curso. Tenta novamente dentro de instantes.")

    sample_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f"rr_{uuid.uuid4().hex[:8]}_",
            suffix=".wav",
            dir=tempfile.gettempdir(),
            delete=False,
        ) as temp_file:
            sample_path = Path(temp_file.name)

        _record_stream_sample(sample_path)
        track = _recognize_with_shazam(sample_path)

        if not track:
            payload = {
                "ok": True,
                "identified": False,
                "message": "O Shazam não reconheceu música nesta amostra. Pode estar a passar voz, publicidade ou notícias.",
                "identified_at": datetime.now(timezone.utc).isoformat(),
                "sample_seconds": IDENTIFY_SECONDS,
            }
        else:
            payload = _extract_track_payload(track)

        _identify_cache["timestamp"] = time.time()
        _identify_cache["payload"] = payload
        return {**payload, "cached": False}
    finally:
        _identify_lock.release()
        if sample_path:
            try:
                sample_path.unlink(missing_ok=True)
            except OSError:
                pass


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

    return jsonify(
        {
            "ok": True,
            "app": "Rádio Renascença — Modo Super Deus",
            "station": STATION_NAME,
            "stream_configured": bool(STREAM_URL),
            "ffmpeg_ready": ffmpeg_ok,
            "ffmpeg_error": ffmpeg_error,
            "identify_seconds": IDENTIFY_SECONDS,
            "server_time": datetime.now(timezone.utc).isoformat(),
            "platform": "vercel" if os.getenv("VERCEL") else "local",
        }
    )


@app.route("/api/identify", methods=["GET", "POST"])
def identify():
    force = request.args.get("force", "0") == "1"
    try:
        return jsonify(_identify_now(force=force))
    except RadioError as exc:
        return jsonify({"ok": False, "identified": False, "error": str(exc)}), 503
    except Exception as exc:  # devolve mensagem segura, mas deixa detalhe no terminal
        app.logger.exception("Erro inesperado na identificação: %s", exc)
        return (
            jsonify(
                {
                    "ok": False,
                    "identified": False,
                    "error": "Ocorreu um erro inesperado durante a identificação.",
                }
            ),
            500,
        )


@app.get("/health")
def health():
    return jsonify({"ok": True, "station": STATION_NAME})


if __name__ == "__main__":
    print("\n✨ Rádio Renascença — Modo Super Deus")
    print("🌐 Abre: http://127.0.0.1:5000\n")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
