# =============================================================================
# SupplyLink
#
# Multi-stage: dependencies are installed in a builder and the result copied
# into a slim runtime, so compilers and build headers never ship. The runtime
# image runs as a non-root user.
# =============================================================================

FROM python:3.12-slim-bookworm AS builder

# PIP_NO_CACHE_DIR is deliberately not set: the install below uses a BuildKit
# cache mount, which that would disable.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# pyproject.toml is the single source of dependency truth. Listing the packages
# again here to win a cache layer would mean two lists that silently drift, and
# an image whose dependencies differ from the ones the tests ran against. A
# BuildKit cache mount gives most of the caching benefit without the duplication.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip
RUN --mount=type=cache,target=/root/.cache/pip \
    /opt/venv/bin/pip install .


# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# A non-root user with no login shell. If the process is ever compromised it
# should not also own the filesystem.
RUN groupadd --system --gid 1001 supplylink \
 && useradd --system --uid 1001 --gid supplylink --create-home --shell /usr/sbin/nologin supplylink

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Runtime inputs that live outside the installed package.
COPY --chown=supplylink:supplylink alembic.ini ./
COPY --chown=supplylink:supplylink migrations/ ./migrations/
COPY --chown=supplylink:supplylink contracts/ ./contracts/
COPY --chown=supplylink:supplylink sql/ ./sql/
COPY --chown=supplylink:supplylink scripts/ ./scripts/
COPY --chown=supplylink:supplylink tools/ ./tools/
COPY --chown=supplylink:supplylink data/ ./data/

# Absolute paths: the settings defaults are computed relative to the installed
# package, which is in the virtualenv rather than in /app.
ENV SUPPLYLINK_CONTRACTS_DIR=/app/contracts \
    SUPPLYLINK_DATA_DIR=/app/data \
    SUPPLYLINK_SQL_DIR=/app/sql \
    PYTHONPATH=/app

USER supplylink

EXPOSE 8000

# Checks the database and contracts, not just that the process is alive — a
# process that cannot reach its warehouse is not healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["supplylink", "health"]

CMD ["uvicorn", "supplylink.api:app", "--host", "0.0.0.0", "--port", "8000"]
