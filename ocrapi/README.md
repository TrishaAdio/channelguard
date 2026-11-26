# UPI payment proof OCR API

A small HTTP service that reads a UPI payment screenshot (PhonePe, Google Pay,
Paytm, BHIM, Amazon Pay, CRED, bank apps...) and returns the amount, status,
UTR, date/time, payee and payer as JSON, with a confidence score per field and
warnings for anything suspicious.

It runs fully on your own server by default (PaddleOCR, PP-OCRv6 models, CPU
is fine). Google Cloud Vision can be added as a second engine for a two-engine
consensus.

## Run

Docker (models are downloaded at build time, so the container starts offline):

```bash
docker build -f ocrapi/Dockerfile -t upi-ocr .
docker run -d -p 8000:8000 -e OCR_API_KEY=<long random string> upi-ocr
```

Without Docker (Python 3.10-3.12; Linux needs `libgl1 libglib2.0-0 libgomp1`):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r ocrapi/requirements.txt
OCR_API_KEY=<long random string> uvicorn ocrapi.app:app --host 0.0.0.0 --port 8000 --workers 1
```

The first start downloads about 200 MB of models into `~/.paddlex`
(`PADDLE_PDX_CACHE_HOME` changes the location). Expect 1.5-2 GB RAM and
3-5 seconds per screenshot on 4-8 CPU cores. One worker uses all cores; add
workers only if you have RAM for one model copy each.

## Endpoints

All `POST` endpoints need the header `X-API-Key: <OCR_API_KEY>`.

### `POST /v1/upi`

Multipart upload, field name `file`. Optional query `thorough=true` always runs
the second, enhanced pass.

```bash
curl -H "X-API-Key: $OCR_API_KEY" -F "file=@receipt.png" http://localhost:8000/v1/upi
```

```json
{
  "fields": {
    "amount":              {"value": "1499.00", "confidence": 0.99, "source": "₹1,499"},
    "status":              {"value": "success", "confidence": 0.984, "source": "Transaction Successful"},
    "utr":                 {"value": "633010876541", "confidence": 0.994, "source": "UTR: 633010876541"},
    "transaction_id":      {"value": "T2611261042389127456931", "confidence": 0.95, "source": "..."},
    "datetime":            {"value": "2026-11-26T10:42:00+05:30", "confidence": 1.0, "source": "10:42 am on 26 Nov 2026"},
    "payee_name":          {"value": "RAHUL KUMAR SHARMA", "confidence": 0.949, "source": "..."},
    "payee_vpa":           {"value": "rahulsharma@ybl", "confidence": 1.0, "source": "..."},
    "payer_name":          {"value": null, "confidence": 0.0, "source": ""},
    "payer_vpa":           {"value": null, "confidence": 0.0, "source": ""},
    "payer_bank":          {"value": null, "confidence": 0.0, "source": ""},
    "payer_account_last4": {"value": "4821", "confidence": 1.0, "source": "XXXXXX4821"},
    "app":                 {"value": "PhonePe", "confidence": 0.95, "source": "Powered by PhonePe"}
  },
  "checks": {
    "complete": true,
    "is_success": true,
    "in_future": false,
    "age_hours": 11.3,
    "utr_matches_date": true
  },
  "warnings": [],
  "text": "Transaction Successful\n10:42 am on 26 Nov 2026\n...",
  "lines": [{"text": "Transaction Successful", "score": 0.9998, "box": [56, 90, 604, 137]}],
  "passes": ["paddle:original"],
  "elapsed_ms": 4190
}
```

- `amount` is a decimal string in rupees (no float rounding).
- `status` is `success`, `pending` or `failed`. If a screenshot shows a failure
  or pending word anywhere in a short line, that wins over "success".
- `datetime` is ISO 8601 in IST. When only a date is visible it is `YYYY-MM-DD`.
  A date without a year gets the current year, or last year if that would be
  in the future.
- `checks.complete` is true when amount, status, UTR and date were all found
  with confidence 0.8 or higher. Treat anything else as "needs a human look".
- `checks.utr_matches_date`: most UPI UTRs (RRNs) start with the last digit of
  the year and the day of the year (`6330...` = 2026, day 330 = 26 Nov). A
  mismatch is a strong hint the screenshot was edited, but some banks use other
  formats, so it is reported as a warning, not a rejection.

### `POST /v1/ocr`

Same upload, returns only `text` and `lines` (plain OCR, no UPI parsing).

### `GET /health`

`{"status": "ok", "engines": ["paddle"]}`. No key needed.

## How the accuracy is achieved

1. **Image prep:** EXIF rotation is applied, transparency is flattened, and
   small screenshots are upscaled to at least 1000 px wide before OCR.
2. **Second pass when unsure:** if any key field is missing, below 0.85
   confidence, or the amount is ambiguous, the image is read again as a
   contrast-stretched, 1.5x upscaled grayscale (dark mode is inverted first).
   The two passes are merged field by field. Agreement raises confidence;
   disagreement lowers it and adds a warning.
3. **Amount scoring:** every money-looking value is a candidate. A `₹`/`Rs`/`INR`
   prefix, an "Amount" label, a larger font and repeat occurrences (most apps
   print the amount twice) raise the score. Cashback, reward, balance, fee and
   offer lines are pushed down. A `₹` misread by OCR as `?`, `z`, `F` or `7` is
   recovered, and `71,499` vs `1,499` is settled by the second occurrence.
   Indian (`1,25,000`) and western (`125,000`) digit grouping are validated.
4. **UTR:** labelled values first (UTR, UPI Ref No, UPI transaction ID, RRN,
   Bank Ref No; same line or the next two), then any standalone 12-digit number.
   Common OCR confusions inside digit runs (`O`->0, `I`/`l`->1, `S`->5, `B`->8)
   are fixed and spaced groups (`6330 1087 6541`) are joined.
5. **Engines:** `OCR_ENGINE=consensus` runs PaddleOCR and Google Cloud Vision
   on every image and merges them the same way. This is the most accurate mode.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `OCR_API_KEY` | (required) | clients send it as `X-API-Key`. The service refuses to start without it unless `OCR_ALLOW_ANONYMOUS=1` (local testing only). |
| `OCR_ENGINE` | `paddle` | `paddle`, `google`, or `consensus` (both). |
| `GOOGLE_VISION_API_KEY` | | needed for `google` and `consensus`. Images are then sent to Google. |
| `GOOGLE_VISION_TIMEOUT` | `20` | seconds |
| `OCR_MAX_BYTES` | `10485760` | max upload size (413 above it) |
| `OCR_MAX_PIXELS` | `40000000` | max image pixels (decompression-bomb guard) |
| `OCR_ORIENTATION` | `false` | `true` enables PaddleOCR's page and text-line rotation models, for photos of phones taken sideways. Slower. |
| `OCR_DET_MODEL` / `OCR_REC_MODEL` | PaddleOCR default | override the model names, e.g. a mobile model for low-RAM servers |

## Errors

| Status | When |
|--------|------|
| 400 | empty upload |
| 401 | missing or wrong `X-API-Key` |
| 413 | file larger than `OCR_MAX_BYTES` |
| 415 | not a readable PNG/JPEG/WEBP/BMP/TIFF/GIF image |
| 502 | Google Vision failed (only with `google` / `consensus`) |

## Limits

OCR can only read what is on the screenshot. A well-made fake image will read
"correctly". Use `utr_matches_date`, `in_future` and `age_hours` as signals,
and confirm the UTR against your bank statement or payment gateway for anything
that matters.
