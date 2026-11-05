FROM python:3.12-slim

# LibreOffice converts the generated .docx/.xlsx to PDF. Without it, report generation
# returns a 503 (see recalc_service.py / word_builder.py). fonts-liberation gives Word docs
# metric-compatible Arial/Times substitutes so pagination matches what LibreOffice measured
# during development (see report-polish-toc-swot memory: page numbers were measured off a
# real LibreOffice render). The reports are set in Calibri: fonts-crosextra-carlito is its
# metric-compatible substitute. Without it LibreOffice fell back to the wider DejaVu Sans,
# pages ran longer than in Word, and the contents page numbers it measured came out wrong.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    fonts-liberation \
    fonts-crosextra-carlito \
    fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

ENV LIBREOFFICE_PATH=/usr/bin/soffice \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# App Platform injects $PORT and routes traffic to it; it is not always 8080.
EXPOSE 8080
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
