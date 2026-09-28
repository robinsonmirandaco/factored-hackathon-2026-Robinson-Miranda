-- The language of a case (es or pt) is part of its conversational context: a "sí" or "sim"
-- that confirms the pending action is answered in the language of the case without reading the
-- message again. Cases opened before this migration keep null.
ALTER TABLE cases ADD COLUMN language text CHECK (language IN ('es', 'pt'));
