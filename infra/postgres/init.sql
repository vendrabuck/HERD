-- HERD Database Schema Initialization
-- Creates separate schemas for each microservice (schema-per-service pattern)

-- Auth service schema
CREATE SCHEMA IF NOT EXISTS auth;

-- Inventory service schema
CREATE SCHEMA IF NOT EXISTS inventory;

-- Reservations service schema
CREATE SCHEMA IF NOT EXISTS reservations;

-- ACL service schema (stub, ready for future implementation)
CREATE SCHEMA IF NOT EXISTS acl;

-- User Profile service schema (stub, ready for future implementation)
CREATE SCHEMA IF NOT EXISTS user_profile;

-- Cabling service schema (stub, ready for future implementation)
CREATE SCHEMA IF NOT EXISTS cabling;

-- Execution service schema
CREATE SCHEMA IF NOT EXISTS execution;

-- Notifications service schema
CREATE SCHEMA IF NOT EXISTS notifications;

-- AI Orchestrator service schema (multi-turn assistant conversations)
CREATE SCHEMA IF NOT EXISTS ai_orchestrator;

-- Grant the herd user access to all schemas
GRANT ALL PRIVILEGES ON SCHEMA auth TO herd;
GRANT ALL PRIVILEGES ON SCHEMA inventory TO herd;
GRANT ALL PRIVILEGES ON SCHEMA reservations TO herd;
GRANT ALL PRIVILEGES ON SCHEMA acl TO herd;
GRANT ALL PRIVILEGES ON SCHEMA user_profile TO herd;
GRANT ALL PRIVILEGES ON SCHEMA cabling TO herd;
GRANT ALL PRIVILEGES ON SCHEMA execution TO herd;
GRANT ALL PRIVILEGES ON SCHEMA notifications TO herd;
GRANT ALL PRIVILEGES ON SCHEMA ai_orchestrator TO herd;

-- Set default search path
ALTER DATABASE herd SET search_path TO public, auth, inventory, reservations, acl, user_profile, cabling, execution, notifications, ai_orchestrator;
