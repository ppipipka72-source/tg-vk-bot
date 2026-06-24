"""Распознавание речи голосовых сообщений (offline, Vosk).

Голосовое (ogg/opus из VK или TG) декодируется через ffmpeg в PCM 16 кГц моно и
скармливается модели Vosk. Модель грузится лениво один раз и кэшируется в памяти
(~200 МБ). Если vosk не установлен, модель не найдена или нет ffmpeg — фича
просто отключается (transcribe_voice вернёт None), мост продолжает работать.

Путь к модели: переменная окружения VOSK_MODEL_PATH (по умолчанию папка
`vosk-model` в корне проекта). Модель: https://alphacephei.com/vosk/models
(для русского — vosk-model-small-ru, ~45 МБ).
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading

log = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_MODEL = os.path.join(_PROJECT_ROOT, "vosk-model")

_SAMPLE_RATE = 16000
# Декодируем и распознаём кусками — так Vosk отдаёт промежуточные результаты и
# не держит весь PCM длинного голосового в одном вызове.
_CHUNK = 32000  # байт PCM (~1 c при 16 кГц/16 бит моно)

# _state: None — ещё не пробовали загрузить; False — отключено (нет модели/либы);
# иначе — загруженный объект vosk.Model. Защищено _lock от гонки потоков.
_state = None
_lock = threading.Lock()


def _get_model():
    """Лениво загрузить модель Vosk. None — фича недоступна (тихо отключена)."""
    global _state
    if _state is not None:
        return _state or None
    with _lock:
        if _state is not None:
            return _state or None
        try:
            from vosk import Model, SetLogLevel
        except ImportError:
            log.warning("STT: библиотека vosk не установлена — расшифровка "
                        "голосовых отключена (pip install vosk)")
            _state = False
            return None
        path = os.getenv("VOSK_MODEL_PATH", "").strip() or _DEFAULT_MODEL
        if not os.path.isdir(path):
            log.warning("STT: модель Vosk не найдена (%s) — расшифровка отключена. "
                        "Скачай модель и укажи VOSK_MODEL_PATH.", path)
            _state = False
            return None
        try:
            SetLogLevel(-1)  # заглушить болтливый лог Kaldi
            _state = Model(path)
            log.info("STT: модель Vosk загружена (%s)", path)
        except Exception:  # noqa: BLE001
            log.exception("STT: не удалось загрузить модель Vosk")
            _state = False
            return None
    return _state or None


def _decode_to_pcm(data: bytes) -> bytes | None:
    """ogg/opus/mp3 -> PCM s16le 16 кГц моно через ffmpeg. None при ошибке."""
    ff = os.getenv("FFMPEG_BIN", "").strip() or shutil.which("ffmpeg")
    if not ff:
        log.warning("STT: ffmpeg не найден — не могу декодировать голосовое "
                    "(установи ffmpeg или задай FFMPEG_BIN)")
        return None
    try:
        proc = subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-ar", str(_SAMPLE_RATE), "-ac", "1", "-f", "s16le", "pipe:1"],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    except Exception:  # noqa: BLE001
        log.exception("STT: ffmpeg упал при декодировании голосового")
        return None
    if proc.returncode != 0 or not proc.stdout:
        log.warning("STT: ffmpeg не декодировал голосовое (rc=%s)", proc.returncode)
        return None
    return proc.stdout


def _transcribe_sync(data: bytes) -> str | None:
    model = _get_model()
    if model is None:
        return None
    pcm = _decode_to_pcm(data)
    if not pcm:
        return None
    from vosk import KaldiRecognizer

    rec = KaldiRecognizer(model, _SAMPLE_RATE)
    parts: list[str] = []
    for i in range(0, len(pcm), _CHUNK):
        if rec.AcceptWaveform(pcm[i:i + _CHUNK]):
            parts.append(json.loads(rec.Result()).get("text", ""))
    parts.append(json.loads(rec.FinalResult()).get("text", ""))
    text = " ".join(p for p in parts if p).strip()
    return text or None


async def transcribe_voice(data: bytes | None) -> str | None:
    """Распознать речь в голосовом (байты ogg/mp3). None — нет текста/фича выкл.

    Тяжёлая часть (ffmpeg + Vosk) блокирующая — выносим в пул потоков, чтобы не
    тормозить мост.
    """
    if not data:
        return None
    try:
        return await asyncio.to_thread(_transcribe_sync, data)
    except Exception:  # noqa: BLE001 — расшифровка не должна ронять мост
        log.exception("STT: ошибка распознавания голосового")
        return None
