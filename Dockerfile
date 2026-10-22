# Local finance dashboard. Data and secrets are mounted, never baked into the image.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY app ./app
COPY .streamlit ./.streamlit
RUN uv sync --frozen --no-dev

ENV FIN_DATA_DIR=/app/data
VOLUME ["/app/data", "/app/secrets"]
EXPOSE 8501
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')"
CMD ["uv", "run", "--no-dev", "streamlit", "run", "app/streamlit_app.py", "--server.address=0.0.0.0"]
