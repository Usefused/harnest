-- Keep Harnest-managed tables out of Hatchet's database and PostgreSQL's public schema.
CREATE SCHEMA IF NOT EXISTS harnest_runtime AUTHORIZATION harnest;

-- PostgresStore owns table creation and migrations; this fixture only selects its namespace.
ALTER ROLE harnest IN DATABASE harnest
    SET search_path TO harnest_runtime, public;
