-- HERD database schema initialization: one schema per database-backed service
-- (schema-per-service pattern).
--
-- The official postgres image runs this file once, on a fresh data volume only,
-- through psql connected as POSTGRES_USER to POSTGRES_DB. That role therefore owns
-- every schema created here and needs no GRANT, and current_database() below is
-- POSTGRES_DB. No statement names a role or a database (issue #1325): a literal
-- name stopped the first boot for any other POSTGRES_USER or POSTGRES_DB, and
-- tests/unit/test_postgres_init.py fails if one comes back.

-- Auth service schema
CREATE SCHEMA IF NOT EXISTS auth;

-- Inventory service schema
CREATE SCHEMA IF NOT EXISTS inventory;

-- Reservations service schema
CREATE SCHEMA IF NOT EXISTS reservations;

-- ACL service schema (resource-level grants)
CREATE SCHEMA IF NOT EXISTS acl;

-- User Profile service schema (preferences)
CREATE SCHEMA IF NOT EXISTS user_profile;

-- Cabling service schema (connections, topologies, reservation forks)
CREATE SCHEMA IF NOT EXISTS cabling;

-- Execution service schema
CREATE SCHEMA IF NOT EXISTS execution;

-- Notifications service schema
CREATE SCHEMA IF NOT EXISTS notifications;

-- AI Orchestrator service schema (multi-turn assistant conversations)
CREATE SCHEMA IF NOT EXISTS ai_orchestrator;

-- Integration service schema (external /api/v1 facade + outbound webhooks)
CREATE SCHEMA IF NOT EXISTS integration;

-- Secrets service schema (encrypted-at-rest credential store)
CREATE SCHEMA IF NOT EXISTS secrets;

-- Default search path for sessions in this database. The database is named
-- through current_database(), never literally.
DO $$
BEGIN
    EXECUTE format(
        'ALTER DATABASE %I SET search_path TO public, auth, inventory, reservations, '
        || 'acl, user_profile, cabling, execution, notifications, ai_orchestrator, '
        || 'integration, secrets',
        current_database()
    );
END
$$;
