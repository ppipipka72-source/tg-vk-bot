"""Распознавание речи голосовых сообщений (offline, faster-whisper).

Голосовое (ogg/opus из VK или TG) декодируется через ffmpeg в PCM 16 кГц моно,
переводится в float32 [-1, 1] и распознаётся моделью faster-whisper (бэкенд
CTranslate2, int8 на CPU — без torch, заметно точнее на русском, чем Vosk).
Модель грузится лениво один раз и кэшируется в памяти. Если faster-whisper не
установлен или нет ffmpeg — фича просто отключается (transcribe_voice вернёт
None), мост продолжает работать.

Конфиг (переменные окружения):
  WHISPER_MODEL   — размер модели или путь к локальной CT2-папке. По умолчанию
                    "small". Имя размера (tiny/base/small/medium) скачается с
                    HuggingFace и закэшируется (~/.cache/huggingface). Для офлайна
                    укажи путь к уже распакованной CT2-модели.
  WHISPER_COMPUTE — тип вычислений CTranslate2 (по умолч. "int8" — самый лёгкий).
  WHISPER_LANG    — язык распознавания (по умолч. "ru"). Пусто/"auto" — автоопределение.
  FFMPEG_BIN      — путь к ffmpeg, если его нет в PATH.
"""

import asyncio
import logging
import os
import shutil
import subprocess
import threading

log = logging.getLogger(__name__)

_SAMPLE_RATE = 16000

# _state: None — ещё не пробовали загрузить; False — отключено (нет либы/модели);
# иначе — загруженный faster_whisper.WhisperModel. Защищено _lock от гонки потоков.
_state = None
_lock = threading.Lock()


def _get_model():
    """Лениво загрузить модель faster-whisper. None — фича недоступна (тихо выкл.)."""
    global _state
    if _state is not None:
        return _state or None
    with _lock:
        if _state is not None:
            return _state or None
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            log.warning("STT: библиотека faster-whisper не установлена — расшифровка "
                        "голосовых отключена (pip install faster-whisper)")
            _state = False
            return None
        name = os.getenv("WHISPER_MODEL", "").strip() or "small"
        compute = os.getenv("WHISPER_COMPUTE", "").strip() or "int8"
        try:
            _state = WhisperModel(name, device="cpu", compute_type=compute)
            log.info("STT: модель faster-whisper загружена (%s, %s)", name, compute)
        except Exception:  # noqa: BLE001
            log.exception("STT: не удалось загрузить модель faster-whisper (%s)", name)
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
    import numpy as np

    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    lang = os.getenv("WHISPER_LANG", "").strip() or "ru"
    if lang.lower() == "auto":
        lang = None
    # beam_size=1 — быстрее на CPU; vad_filter режет тишину; condition_on_previous
    # _text=False гасит зацикленные «галлюцинации» на коротких голосовых.
    segments, _info = model.transcribe(
        audio, language=lang, beam_size=1, vad_filter=True,
        condition_on_previous_text=False)
    text = " ".join(s.text.strip() for s in segments).strip()
    return text or None


async def transcribe_voice(data: bytes | None) -> str | None:
    """Распознать речь в голосовом (байты ogg/mp3). None — нет текста/фича выкл.

    Тяжёлая часть (ffmpeg + faster-whisper) блокирующая — выносим в пул потоков,
    чтобы не тормозить мост.
    """
    if not data:
        return None
    try:
        return await asyncio.to_thread(_transcribe_sync, data)
    except Exception:  # noqa: BLE001 — расшифровка не должна ронять мост
        log.exception("STT: ошибка распознавания голосового")
        return None
