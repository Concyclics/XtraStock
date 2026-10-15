FROM pgvector/pgvector:pg17

ARG DEBIAN_FRONTEND=noninteractive
ARG TIMESCALEDB_REPO_CODENAME=bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -d /usr/share/keyrings \
    && curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey \
      | gpg --dearmor -o /usr/share/keyrings/timescaledb.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/timescaledb.gpg] https://packagecloud.io/timescale/timescaledb/debian/ ${TIMESCALEDB_REPO_CODENAME} main" \
      > /etc/apt/sources.list.d/timescaledb.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      timescaledb-2-postgresql-17 \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

COPY database/postgres/init/ /docker-entrypoint-initdb.d/

RUN chmod 755 /docker-entrypoint-initdb.d \
    && chmod 644 /docker-entrypoint-initdb.d/*.sql \
    && chmod 755 /docker-entrypoint-initdb.d/*.sh
