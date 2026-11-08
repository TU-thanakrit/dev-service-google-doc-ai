FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# OCR toolchain (issue #7): poppler-utils (`pdfinfo`/`pdftotext`/`pdftoppm`),
# Tesseract with the Thai + English models, and ImageMagick (`identify`/`convert`)
# for decoding one frame of a multi-frame TIFF, which the other tools cannot do.
# Versions follow the Debian base image; pinning the engine per job is ticket #11
# and the container licence expression is still open (docs/ocr-v1-benchmark-report.md B7).
RUN apt-get update \
 && apt-get install --no-install-recommends -y \
      poppler-utils \
      tesseract-ocr \
      tesseract-ocr-eng \
      tesseract-ocr-tha \
      imagemagick \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY app ./app

# Run unprivileged: the worker writes only to its per-call temp directory under
# /tmp, so no root-owned path is needed at runtime.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin ocrworker
USER 10001

# One image, four roles (docs/ocr-v1-dispatch.md): this default CMD is the API
# service, and the worker, reconcile, and cleanup Cloud Run Jobs override it with
# `python -m app.ocr_worker` / `app.ocr_reconcile` / `app.ocr_cleanup`. The
# synchronous expense modules (`app/main.py`, `app/service.py`,
# `app/integrations.py`) are retained in the image as historical code only: no
# command here starts them, so no deployed role can reach the Gemini or Document
# AI expense path (issue #12).
CMD ["sh", "-c", "uvicorn app.ocr_api:app --host 0.0.0.0 --port ${PORT}"]
