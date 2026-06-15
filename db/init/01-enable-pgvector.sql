-- Runs automatically the first time the database container is created
-- (files in /docker-entrypoint-initdb.d are executed by the Postgres image
-- on an empty data directory). This enables the pgvector extension so the
-- application can use the `vector` type and similarity search without any
-- manual setup step.
--
-- Note: this only runs on a FRESH database. If you change it, recreate the
-- database with `docker compose down -v && docker compose up -d`.

CREATE EXTENSION IF NOT EXISTS vector;

-- Quick provenance marker so you can confirm the init script ran.
DO $$
BEGIN
    RAISE NOTICE 'LogLens init: pgvector extension ensured.';
END $$;
