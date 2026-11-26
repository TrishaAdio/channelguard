FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PADDLE_PDX_CACHE_HOME=/models \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True

WORKDIR /app
COPY ocrapi/requirements.txt ocrapi/requirements.txt
RUN pip install --no-cache-dir -r ocrapi/requirements.txt

RUN python -c "from paddleocr import PaddleOCR; PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False)"

COPY ocrapi ocrapi

RUN useradd --system --uid 10001 ocr && chown -R ocr /models
USER ocr

EXPOSE 8000
CMD ["uvicorn", "ocrapi.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
