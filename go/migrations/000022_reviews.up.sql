CREATE TABLE reviews (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinical_note_id  uuid NOT NULL REFERENCES clinical_notes(id) ON DELETE CASCADE,
    reviewer_user_id  uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    started_at        timestamptz NOT NULL DEFAULT now(),
    completed_at      timestamptz,
    outcome           text CHECK (outcome IN ('approved', 'rejected', 'abandoned')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_reviews_clinical_note_id ON reviews (clinical_note_id);
CREATE INDEX idx_reviews_reviewer_user_id ON reviews (reviewer_user_id);

CREATE TRIGGER reviews_set_updated_at
    BEFORE UPDATE ON reviews
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
