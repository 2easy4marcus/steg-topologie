FROM python:3.13.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system app \
    && adduser --system --ingroup app app

# pyosmium ships a compiled extension that dynamically links libexpat, which
# python:*-slim does not carry. Without it `import osmium` raises ImportError
# and the topology reader test fails in the container but passes on a host
# that happens to have expat installed.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --requirement requirements.txt

COPY --chown=app:app app ./app
COPY --chown=app:app static ./static
COPY --chown=app:app migrations ./migrations

RUN mkdir /data \
    && chown app:app /data

USER app

EXPOSE 8010

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
