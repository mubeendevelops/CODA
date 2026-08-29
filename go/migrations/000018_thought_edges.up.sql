CREATE TABLE thought_edges (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    consultation_id uuid NOT NULL REFERENCES consultations(id) ON DELETE CASCADE,
    run_config_id   uuid NOT NULL REFERENCES run_configs(id) ON DELETE RESTRICT,
    src_thought_id  uuid NOT NULL REFERENCES thoughts(id) ON DELETE CASCADE,
    dst_thought_id  uuid NOT NULL REFERENCES thoughts(id) ON DELETE CASCADE,
    edge_type       text NOT NULL CHECK (edge_type IN ('temporal', 'causal', 'logical')),
    weight          real NOT NULL, -- entity_overlap x edge_type_prior (ADR-0011)
    predicted_by    text NOT NULL CHECK (predicted_by IN ('rule', 'llm')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT thought_edges_no_self_loop CHECK (src_thought_id <> dst_thought_id)
);

-- Query pattern: fetch full pipeline artifacts for one consultation, plus
-- graph traversal from either endpoint.
CREATE INDEX idx_thought_edges_consultation_run_config
    ON thought_edges (consultation_id, run_config_id);
CREATE INDEX idx_thought_edges_src ON thought_edges (src_thought_id);
CREATE INDEX idx_thought_edges_dst ON thought_edges (dst_thought_id);
