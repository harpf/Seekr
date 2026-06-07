FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    antiword \
    catdoc \
    git \
    nano \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-deu \
    tesseract-ocr-eng \
    libmagic1 \
    vim-tiny \
  && rm -rf /var/lib/apt/lists/* \
  && ln -sf /usr/bin/vim.tiny /usr/local/bin/vim

# Create a dedicated, unprivileged user/group with a fixed UID/GID so that
# host bind-mount ownership is predictable across environments.
RUN groupadd --gid 10001 seekr \
  && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin seekr

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Ensure writable runtime dirs exist and are owned by the unprivileged user.
# This chown MUST run after `COPY . .` so /app contents are owned correctly.
RUN mkdir -p /data /documents \
  && chown -R seekr:seekr /data /documents /app

ENV DOCUMENT_SEARCH_DB=/data/document_index.db
EXPOSE 8080

# Drop privileges: run the application as the non-root seekr user.
USER seekr

CMD ["uvicorn", "document_search.app:app", "--host", "0.0.0.0", "--port", "8080"]
