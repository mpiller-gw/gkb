FROM python:3.12-slim

# OCR und Bibliotheken für Docling
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-deu libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY db ./db
RUN pip install --no-cache-dir -e ".[parse,llm,embed]"

# Optional: NER-Pseudonymisierung mit deutschem spaCy-Modell
ARG WITH_NER=false
RUN if [ "$WITH_NER" = "true" ]; then \
        pip install --no-cache-dir -e ".[parse,llm,embed,ner]" && python -m spacy download de_core_news_lg; \
    fi

ENTRYPOINT ["gkb"]
CMD ["--help"]
