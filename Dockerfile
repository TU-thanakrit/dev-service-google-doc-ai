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

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
