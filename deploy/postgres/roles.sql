-- ASAS PostgreSQL roles: least privilege around an append-only schema.
--
--   asas_owner   owns the schema; runs `python -m asas migrate` only (never the service)
--   asas_app     the service: SELECT + INSERT only. It does not own the tables, so it can't
--                drop or disable the append-only triggers, and it has no UPDATE, DELETE or
--                TRUNCATE privilege at all
--   asas_reader  read-only connections for agents' tools (ASAS_DATABASE_READER_URL)
--
-- Run as a superuser / DBA once per database, replacing the passwords (or use IAM auth).
-- psql -v ON_ERROR_STOP=1 -d asas -f deploy/postgres/roles.sql

CREATE ROLE asas_owner  LOGIN PASSWORD :'owner_password';
CREATE ROLE asas_app    LOGIN PASSWORD :'app_password';
CREATE ROLE asas_reader LOGIN PASSWORD :'reader_password';

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO asas_owner;
GRANT USAGE ON SCHEMA public TO asas_app, asas_reader;

-- privileges on tables the owner creates now and later
ALTER DEFAULT PRIVILEGES FOR ROLE asas_owner IN SCHEMA public
  GRANT SELECT, INSERT ON TABLES TO asas_app;
ALTER DEFAULT PRIVILEGES FOR ROLE asas_owner IN SCHEMA public
  GRANT SELECT ON TABLES TO asas_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE asas_owner IN SCHEMA public
  GRANT USAGE ON SEQUENCES TO asas_app;

-- belt and braces for tables that already exist
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA public TO asas_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO asas_reader;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO asas_app;
REVOKE UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public FROM asas_app, asas_reader;
