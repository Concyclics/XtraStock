-- Immutable before-images of precisely replaced rule outputs; not public data.
CREATE TABLE IF NOT EXISTS analytics.document_rule_revisions (
 revision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 document_id uuid NOT NULL,
 extraction_method text NOT NULL,
 object_type text NOT NULL CHECK(object_type IN ('event','keyword')),
 source_object_id text NOT NULL,
 reason text NOT NULL,
 input_signature text NOT NULL,
 previous_row jsonb NOT NULL,
 recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS document_rule_revisions_document_idx
 ON analytics.document_rule_revisions(document_id,recorded_at);
GRANT SELECT,INSERT ON analytics.document_rule_revisions TO finance_app;
GRANT SELECT ON analytics.document_rule_revisions TO finance_ai_reader;
