"""Transcription locale avec faster-whisper (chargement paresseux, 1 job à la fois)."""
import os
import threading

from . import db
from .youtube import fmt_ts, segments_to_text

_model = None
_model_name = None
_lock = threading.Lock()


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def transcribe_file(path: str, language: str = None):
    """-> (texte horodaté, langue détectée)."""
    global _model, _model_name
    from faster_whisper import WhisperModel

    name = db.get_setting("whisper_model", "small") or "small"
    with _lock:
        if _model is None or _model_name != name:
            _model = WhisperModel(name, device="cpu", compute_type="int8",
                                  download_root=os.path.join(db.DATA_DIR, "models"),
                                  cpu_threads=int(os.environ.get("WHISPER_THREADS", "3")))
            _model_name = name
        model = _model
    segments, info = model.transcribe(path, language=language or None, vad_filter=True, beam_size=1)
    segs = [(s.start, s.text.strip()) for s in segments if s.text.strip()]
    return segments_to_text(segs), info.language
