-- The HITL data flywheel (decision #14): the original/edited pair per field
-- is exactly the supervision signal a future fine-tune would need.
CREATE TABLE review_edits (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    review_id       uuid NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    field_key       text NOT NULL CHECK (
        field_key IN (
            'chief_complaint', 'hopi', 'past_medical_history', 'medications',
            'allergies', 'examination_findings', 'provisional_diagnosis',
            'investigations_advised', 'treatment_plan'
        )
    ),
    original_value  jsonb NOT NULL,
    edited_value    jsonb NOT NULL,
    edit_type       text NOT NULL CHECK (
        edit_type IN ('correction', 'addition', 'deletion', 'regenerated')
    ),
    edited_at       timestamptz NOT NULL DEFAULT now(),
    editor_user_id  uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT
);

CREATE INDEX idx_review_edits_review_id ON review_edits (review_id);
