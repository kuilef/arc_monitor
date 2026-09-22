FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home appuser && mkdir -p /app/data && chown appuser /app/data
USER appuser
ENV ARC_DB=/app/data/arc.db ARC_MODE=live
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "arc_monitor.app:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]
