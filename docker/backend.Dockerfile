FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv/backend
COPY backend/requirements.lock ./requirements.lock
RUN --mount=type=cache,target=/root/.cache/pip pip install --timeout 60 --retries 5 -r requirements.lock
COPY backend/ ./
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /srv/backend
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
