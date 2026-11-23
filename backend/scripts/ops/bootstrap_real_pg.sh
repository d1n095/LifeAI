#!/usr/bin/env bash
# Bootstrap a REAL userspace PostgreSQL (pgserver binaries) + pgvector for LifeAI verification.
# Replicates verify.yml's DATABASE_URL contract on localhost:5433.
set -euxo pipefail
PGROOT="$(python3 -c "import pgserver, os; print(os.path.join(pgserver.__path__[0], 'pginstall'))")"
PGBIN="$PGROOT/bin"
PGDATA=/tmp/pgdata-lifeai
PGLOG=/tmp/pgdata-lifeai.log
PGPORT=5433

echo "== pg_config sanity =="
"$PGBIN/pg_config" --version
"$PGBIN/pg_config" --pkglibdir
"$PGBIN/pg_config" --includedir-server

echo "== stop stale instance if any =="
"$PGBIN/pg_ctl" -D "$PGDATA" stop -m immediate 2>/dev/null || true
rm -rf "$PGDATA"

echo "== initdb =="
"$PGBIN/initdb" -D "$PGDATA" -U lifeai --auth=trust -E UTF8 >/dev/null

echo "== start =="
"$PGBIN/pg_ctl" -D "$PGDATA" -l "$PGLOG" -o "-p $PGPORT -k /tmp -c listen_addresses=127.0.0.1" start
sleep 1
"$PGBIN/pg_isready" -h localhost -p "$PGPORT"

echo "== create verify db =="
"$PGBIN/createdb" -h localhost -p "$PGPORT" -U lifeai lifeai_verify 2>/dev/null || true

echo "== build pgvector from source =="
rm -rf /tmp/pgvector-src
git clone -q --depth 1 --branch v0.8.0 https://github.com/pgvector/pgvector.git /tmp/pgvector-src
make -C /tmp/pgvector-src -s PG_CONFIG="$PGBIN/pg_config"
make -C /tmp/pgvector-src -s install PG_CONFIG="$PGBIN/pg_config"

echo "== CREATE EXTENSION vector =="
"$PGBIN/psql" -h localhost -p "$PGPORT" -U lifeai -d lifeai_verify -c "CREATE EXTENSION IF NOT EXISTS vector;"
"$PGBIN/psql" -h localhost -p "$PGPORT" -U lifeai -d lifeai_verify -t -c "SELECT extversion FROM pg_extension WHERE extname='vector';"

echo "== export contract (for subsequent steps) =="
cat > /tmp/pg-env.sh <<EOF
export DATABASE_URL="postgresql://lifeai:lifeai@localhost:$PGPORT/lifeai_verify"
export PGROOT="$PGROOT"
export PGDATA="$PGDATA"
EOF
echo "BOOTSTRAP OK"
