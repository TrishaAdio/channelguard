from __future__ import annotations

import time
from datetime import datetime

from PIL import Image

from . import imaging, upi
from .engines import build_engines
from .models import Line
from .settings import Settings

RETRY_BELOW = 0.85


class OcrService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.engines = build_engines(settings)

    @property
    def engine_names(self) -> list[str]:
        return [engine.name for engine in self.engines]

    def read_lines(self, image: Image.Image) -> list[Line]:
        return self.engines[0].read(imaging.prepare(image))

    def _needs_retry(self, receipt: upi.Receipt) -> bool:
        return any(
            receipt.fields[name].value is None or receipt.fields[name].confidence < RETRY_BELOW
            for name in upi.KEY_FIELDS
        ) or any("ambiguous" in warning for warning in receipt.warnings)

    def parse_upi(self, image: Image.Image, thorough: bool = False, now: datetime | None = None) -> dict:
        started = time.perf_counter()
        prepared = imaging.prepare(image)
        passes = []
        receipts = []
        primary_lines: list[Line] = []
        for engine in self.engines:
            lines = engine.read(prepared)
            if not primary_lines:
                primary_lines = lines
            receipts.append(upi.parse(lines, now))
            passes.append(f"{engine.name}:original")
        if thorough or self._needs_retry(upi.merge(receipts)):
            lines = self.engines[0].read(imaging.enhanced(image))
            receipts.append(upi.parse(lines, now))
            passes.append(f"{self.engines[0].name}:enhanced")
        receipt = upi.merge(receipts)
        result_checks, warnings = upi.checks(receipt, now)
        return {
            "fields": {name: value.as_dict() for name, value in receipt.fields.items()},
            "checks": result_checks,
            "warnings": warnings,
            "text": "\n".join(line.text for line in primary_lines),
            "lines": [line.as_dict() for line in primary_lines],
            "passes": passes,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
