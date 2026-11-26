from __future__ import annotations

import os
from dataclasses import dataclass

ENGINES = ("paddle", "google", "consensus")


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    api_key: str
    engine: str
    google_api_key: str
    max_bytes: int
    max_pixels: int
    orientation: bool
    det_model: str
    rec_model: str
    google_timeout: float

    @classmethod
    def from_env(cls) -> "Settings":
        engine = os.getenv("OCR_ENGINE", "paddle").strip().lower() or "paddle"
        if engine not in ENGINES:
            raise ValueError(f"OCR_ENGINE must be one of {', '.join(ENGINES)} (got {engine!r})")
        google_api_key = os.getenv("GOOGLE_VISION_API_KEY", "").strip()
        if engine in ("google", "consensus") and not google_api_key:
            raise ValueError(f"OCR_ENGINE={engine} needs GOOGLE_VISION_API_KEY")
        return cls(
            api_key=os.getenv("OCR_API_KEY", "").strip(),
            engine=engine,
            google_api_key=google_api_key,
            max_bytes=_int("OCR_MAX_BYTES", 10 * 1024 * 1024),
            max_pixels=_int("OCR_MAX_PIXELS", 40_000_000),
            orientation=_flag("OCR_ORIENTATION", False),
            det_model=os.getenv("OCR_DET_MODEL", "").strip(),
            rec_model=os.getenv("OCR_REC_MODEL", "").strip(),
            google_timeout=float(os.getenv("GOOGLE_VISION_TIMEOUT", "20")),
        )
