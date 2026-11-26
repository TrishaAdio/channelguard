from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool

from . import imaging
from .engines import EngineError
from .service import OcrService
from .settings import Settings

log = logging.getLogger("ocrapi")
CHUNK = 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    if not settings.api_key and os.getenv("OCR_ALLOW_ANONYMOUS", "").lower() not in ("1", "true", "yes"):
        raise RuntimeError("Set OCR_API_KEY (or OCR_ALLOW_ANONYMOUS=1 for local testing only)")
    app.state.settings = settings
    app.state.service = await run_in_threadpool(OcrService, settings)
    log.info("OCR engines ready: %s", ", ".join(app.state.service.engine_names))
    yield


app = FastAPI(title="UPI payment proof OCR", version="1.0.0", lifespan=lifespan)


def require_key(request: Request) -> None:
    expected = request.app.state.settings.api_key
    if not expected:
        return
    supplied = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or wrong X-API-Key")


async def read_image(request: Request, upload: UploadFile):
    limit = request.app.state.settings.max_bytes
    data = bytearray()
    while chunk := await upload.read(CHUNK):
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(413, f"image is larger than {limit} bytes")
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty file")
    try:
        return imaging.load(bytes(data), request.app.state.settings.max_pixels)
    except imaging.ImageError as error:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(error)) from error


@app.get("/health")
async def health(request: Request) -> dict:
    return {"status": "ok", "engines": request.app.state.service.engine_names}


@app.post("/v1/upi", dependencies=[Depends(require_key)])
async def parse_upi(
    request: Request,
    file: UploadFile = File(..., description="UPI payment screenshot (PNG, JPEG, WEBP...)"),
    thorough: bool = Query(False, description="always run the extra enhanced pass"),
) -> dict:
    image = await read_image(request, file)
    try:
        return await run_in_threadpool(request.app.state.service.parse_upi, image, thorough)
    except EngineError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error


@app.post("/v1/ocr", dependencies=[Depends(require_key)])
async def plain_ocr(request: Request, file: UploadFile = File(...)) -> dict:
    image = await read_image(request, file)
    try:
        lines = await run_in_threadpool(request.app.state.service.read_lines, image)
    except EngineError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
    return {"text": "\n".join(line.text for line in lines), "lines": [line.as_dict() for line in lines]}
