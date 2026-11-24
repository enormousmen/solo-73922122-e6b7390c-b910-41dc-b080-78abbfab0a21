FROM python:3.11-slim

# Run the service as a non-root user.  Defaults match the usual first host
# user (1000:1000); docker-compose forwards APP_UID/APP_GID so a bind-mounted
# OCI layout stays readable/writable by the host user.
ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    OCI_LAYOUT_DIR=/data/oci-layout

RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin app

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app app ./app

RUN mkdir -p /data/oci-layout && chown -R app:app /data /srv

USER app:app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
