from __future__ import annotations

import base64
import threading
from statistics import mean

import httpx
from PIL import Image

from . import imaging
from .models import Line, reading_order
from .settings import Settings


class EngineError(RuntimeError):
    pass


class PaddleEngine:
    name = "paddle"

    def __init__(self, settings: Settings):
        from paddleocr import PaddleOCR

        options = {
            "use_doc_orientation_classify": settings.orientation,
            "use_doc_unwarping": False,
            "use_textline_orientation": settings.orientation,
        }
        if settings.det_model:
            options["text_detection_model_name"] = settings.det_model
        if settings.rec_model:
            options["text_recognition_model_name"] = settings.rec_model
        self._ocr = PaddleOCR(**options)
        self._lock = threading.Lock()

    def read(self, image: Image.Image) -> list[Line]:
        with self._lock:
            results = self._ocr.predict(imaging.to_bgr(image))
        lines = []
        for result in results:
            for text, score, box in zip(result["rec_texts"], result["rec_scores"], result["rec_boxes"]):
                if str(text).strip():
                    lines.append(Line(str(text).strip(), float(score), tuple(int(v) for v in box)))
        return reading_order(lines)


class GoogleVisionEngine:
    name = "google"
    url = "https://vision.googleapis.com/v1/images:annotate"
    line_breaks = {"EOL_SURE_SPACE", "LINE_BREAK"}
    space_breaks = {"SPACE", "SURE_SPACE"}

    def __init__(self, settings: Settings):
        self._key = settings.google_api_key
        self._client = httpx.Client(timeout=settings.google_timeout)

    def read(self, image: Image.Image) -> list[Line]:
        body = {
            "requests": [{
                "image": {"content": base64.b64encode(imaging.to_png(image)).decode("ascii")},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                "imageContext": {"languageHints": ["en", "hi"]},
            }]
        }
        try:
            response = self._client.post(self.url, headers={"X-goog-api-key": self._key}, json=body)
        except httpx.HTTPError as error:
            raise EngineError(f"Google Vision request failed ({type(error).__name__})") from error
        if response.status_code != 200:
            raise EngineError(f"Google Vision returned HTTP {response.status_code}")
        payload = response.json()["responses"][0]
        if "error" in payload:
            raise EngineError(f"Google Vision error: {payload['error'].get('message', 'unknown')}")
        return reading_order(self._lines(payload.get("fullTextAnnotation") or {}))

    def _lines(self, annotation: dict) -> list[Line]:
        lines: list[Line] = []
        words: list[tuple[str, float, list[tuple[int, int]], bool]] = []

        def flush():
            if not words:
                return
            text = "".join(word + (" " if spaced else "") for word, _, _, spaced in words).strip()
            points = [point for _, _, vertices, _ in words for point in vertices]
            box = (min(x for x, _ in points), min(y for _, y in points),
                   max(x for x, _ in points), max(y for _, y in points))
            lines.append(Line(text, mean(score for _, score, _, _ in words), box))
            words.clear()

        for page in annotation.get("pages", []):
            for block in page.get("blocks", []):
                for paragraph in block.get("paragraphs", []):
                    for word in paragraph.get("words", []):
                        symbols = word.get("symbols", [])
                        if not symbols:
                            continue
                        detected = symbols[-1].get("property", {}).get("detectedBreak", {}).get("type", "")
                        vertices = [(int(v.get("x", 0)), int(v.get("y", 0)))
                                    for v in word.get("boundingBox", {}).get("vertices", [])] or [(0, 0)]
                        text = "".join(symbol.get("text", "") for symbol in symbols)
                        words.append((text, float(word.get("confidence", 0.9)), vertices,
                                      detected in self.space_breaks))
                        if detected in self.line_breaks:
                            flush()
                    flush()
        return lines


def build_engines(settings: Settings) -> list:
    if settings.engine == "paddle":
        return [PaddleEngine(settings)]
    if settings.engine == "google":
        return [GoogleVisionEngine(settings)]
    return [PaddleEngine(settings), GoogleVisionEngine(settings)]
