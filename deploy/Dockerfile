# One image, two roles (ingest / assistant) selected by the compose command.
# Build natively on the target (ARM64 board, Graviton or Pi) so wheels and timings match it:
#   docker compose -f deploy/compose.yaml build
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY deploy/requirements-runtime.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY schema/ schema/
COPY sim/ sim/
COPY adapters/ adapters/
COPY memory/ memory/
COPY ingest/ ingest/
COPY diagnosis/ diagnosis/
COPY query/ query/
COPY ecd/ ecd/
COPY security/ security/
COPY ui/ ui/
COPY config/ config/

# unprivileged user; data and models are mounted at run time
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin drivetrace
USER 10001
