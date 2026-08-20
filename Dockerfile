FROM harbor.app.com/devops-infra/generic-python312:1.0.0

# App version shown in the Swagger UI; pass --build-arg APP_VERSION=<git-tag> in CI.
ARG APP_VERSION=v1.0.0
ENV APP_VERSION=${APP_VERSION}

ENV UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

# Deleting a values file is the one repo operation Bitbucket Server has no REST endpoint
# for, so the service shells out to git for it (app/clients/git.py). Fail the build here
# rather than the first DELETE request in production: a missing binary would otherwise
# surface as a 502 long after deploy. If this fails, install git in the base image — or add
# the appropriate `apt-get install -y git` / `apk add git` for whatever it is built on.
RUN git --version || ( \
      echo "ERROR: git is required for DELETE (see app/clients/git.py) and is missing." \
      && exit 1 )

# Lock + manifest only first, so the dependency layer caches unless they change.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

COPY app ./app

# --no-sync: run from the .venv built above, no network at container start.
CMD ["uv","run","--no-sync","-m","app.main","--host","0.0.0.0","--port","5000"]
