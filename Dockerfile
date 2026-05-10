FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    antiword \
    catdoc \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-deu \
    tesseract-ocr-eng \
    libmagic1 \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DOCUMENT_SEARCH_DB=/data/document_index.db
EXPOSE 8080

CMD ["uvicorn", "document_search.app:app", "--host", "0.0.0.0", "--port", "8080"]
