-- Phase 3A: LLM usage tracking for cost monitoring
CREATE TABLE IF NOT EXISTS llm_usage (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID REFERENCES users(id),
    org_id UUID REFERENCES organizations(id),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    feature TEXT NOT NULL,
    tokens_input INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0,
    cost_usd NUMERIC(10,6) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_org ON llm_usage(org_id);
CREATE INDEX IF NOT EXISTS idx_llm_usage_feature ON llm_usage(feature);
CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage(created_at);
