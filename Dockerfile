# =============================================================================
# Dockerfile — сборка PostgreSQL REL_17_5 + расширение pg_deadlock_log
# PostgreSQL commit: REL_17_5
# =============================================================================
FROM ubuntu:24.04

ARG PG_GIT_REF=REL_17_5
ARG TARGET_DB=postgres
ARG EXT_REPO=https://github.com/khlyupinivan/pg_deadlock_log.git

ENV DEBIAN_FRONTEND=noninteractive
ENV PG_INSTALL_DIR=/usr/local/pgsql
ENV PG_DATA=/var/lib/postgresql/data
ENV PATH="${PG_INSTALL_DIR}/bin:${PATH}"

# -----------------------------------------------------------------------------
# 1. Системные зависимости для сборки PostgreSQL
# -----------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    libreadline-dev \
    zlib1g-dev \
    libssl-dev \
    libxml2-dev \
    libxslt1-dev \
    libicu-dev \
    pkg-config \
    bison \
    flex \
    ca-certificates \
    sudo \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# 2. Создаём пользователя postgres
# -----------------------------------------------------------------------------
RUN useradd -m -s /bin/bash postgres \
    && mkdir -p /usr/local/pgsql \
    && chown postgres:postgres /usr/local/pgsql

# -----------------------------------------------------------------------------
# 3. Клонируем исходники PostgreSQL
# -----------------------------------------------------------------------------
RUN git clone --filter=blob:none https://github.com/postgres/postgres.git /pg_src \
    && cd /pg_src \
    && git checkout ${PG_GIT_REF} \
    && chown -R postgres:postgres /pg_src


# -----------------------------------------------------------------------------
# 4. Собираем и устанавливаем PostgreSQL
# -----------------------------------------------------------------------------
RUN gosu postgres bash -c " \
    cd /pg_src && \
    ./configure \
        --prefix=${PG_INSTALL_DIR} \
        --with-readline \
        --with-zlib \
        --with-openssl \
        --with-libxml \
        --with-icu \
        --enable-debug \
    && make -j\$(nproc) \
    && make install \
"

# -----------------------------------------------------------------------------
# 5. Клонируем расширение pg_deadlock_log
# -----------------------------------------------------------------------------
RUN git clone ${EXT_REPO} /pg_deadlock_log \
    && chown -R postgres:postgres /pg_deadlock_log

# -----------------------------------------------------------------------------
# 6. Запускаем install.sh и entrypoint.sh
# -----------------------------------------------------------------------------
RUN chmod +x /pg_deadlock_log/install.sh /pg_deadlock_log/entrypoint.sh

# -----------------------------------------------------------------------------
# 7. Инициализируем кластер
# -----------------------------------------------------------------------------
RUN mkdir -p ${PG_DATA} && chown postgres:postgres ${PG_DATA} \
    && gosu postgres ${PG_INSTALL_DIR}/bin/initdb \
        --pgdata=${PG_DATA} \
        --encoding=UTF8 \
        --locale=C

# -----------------------------------------------------------------------------
# 8. Запускаем install.sh, перезапускаем PG, создаём расширение
# -----------------------------------------------------------------------------
RUN gosu postgres bash -c " \
    export PATH=${PG_INSTALL_DIR}/bin:\$PATH && \
    pg_ctl -D ${PG_DATA} -l ${PG_DATA}/pg_startup.log start && \
    until pg_isready -q; do sleep 1; done && \
    /pg_deadlock_log/install.sh \
        --pg-src /pg_src \
        --ext-dir /pg_deadlock_log \
        --pg-data ${PG_DATA} \
        --db ${TARGET_DB} && \
    pg_ctl -D ${PG_DATA} restart -w && \
    until pg_isready -q; do sleep 1; done && \
    psql -d ${TARGET_DB} -c 'CREATE EXTENSION IF NOT EXISTS pg_deadlock_log;' && \
    pg_ctl -D ${PG_DATA} stop \
"

# -----------------------------------------------------------------------------
# 9. Entrypoint
# -----------------------------------------------------------------------------
EXPOSE 5432
USER postgres
ENTRYPOINT ["/pg_deadlock_log/entrypoint.sh"]