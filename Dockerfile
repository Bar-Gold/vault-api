FROM harbor.app.com/devops-infra/generic-python312:1.0.0

# App version shown in the Swagger UI; pass --build-arg APP_VERSION=<git-tag> in CI.
ARG APP_VERSION=v1.0.0
ENV APP_VERSION=${APP_VERSION}

ENV UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app
# Lock + manifest only first, so the dependency layer caches unless they change.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

COPY app ./app

# --no-sync: run from the .venv built above, no network at container start.
CMD ["uv","run","--no-sync","-m","app.main","--host","0.0.0.0","--port","5000"]
