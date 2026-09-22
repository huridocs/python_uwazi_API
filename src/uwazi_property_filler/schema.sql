-- Uwazi Property Filler — idempotent DDL, applied once at startup.

CREATE TABLE IF NOT EXISTS extension (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    category TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    endpoint TEXT,
    entrypoint TEXT,
    config JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_status (
    instance_key TEXT NOT NULL,
    shared_id TEXT NOT NULL,
    language TEXT NOT NULL,
    status TEXT NOT NULL,
    template_name TEXT,
    title TEXT,
    filename TEXT,
    filled_metadata JSONB,
    validated_at TIMESTAMPTZ,
    PRIMARY KEY (instance_key, shared_id, language)
);

CREATE TABLE IF NOT EXISTS suggestion (
    id BIGSERIAL PRIMARY KEY,
    instance_key TEXT NOT NULL,
    shared_id TEXT NOT NULL,
    extension_id TEXT NOT NULL,
    property_name TEXT NOT NULL,
    value JSONB NOT NULL,
    confidence REAL,
    highlights JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fill_audit (
    id BIGSERIAL PRIMARY KEY,
    instance_key TEXT NOT NULL,
    shared_id TEXT NOT NULL,
    property_name TEXT NOT NULL,
    before_value JSONB,
    after_value JSONB,
    extension_ids JSONB,
    validated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
