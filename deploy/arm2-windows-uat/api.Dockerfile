FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1 UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH"
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv
WORKDIR /service
COPY backend/pyproject.toml backend/uv.lock ./
# Keep the approved dependency lock; do not fetch OCR weights or start trainers.
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
COPY deploy/arm2-windows-uat/uat_policy.py deploy/arm2-windows-uat/uat_entrypoint.py deploy/arm2-windows-uat/uat_control.py deploy/arm2-windows-uat/smoke.py /uat/
RUN find app alembic /uat -type d -exec chmod 0755 {} + \
    && find app alembic /uat -type f -exec chmod 0644 {} + \
    && chmod 0644 alembic.ini \
    && useradd --uid 10001 --create-home service \
    && mkdir /data && chown service:service /data
USER service
LABEL io.arm2.uat.package="ARM2-WIN-UAT-R2-001" io.arm2.uat.source="1f8c06d1f9269146ce7c07508b02dd09c89ff763"
