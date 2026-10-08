-- Align installations from the base schema with the index created by migration 0076.
-- Create the canonical unique index before dropping the redundant constraint,
-- so uniqueness remains enforced throughout the change.
CREATE UNIQUE INDEX IF NOT EXISTS assistant_kb_document_chunker_version
    ON public.assistant_kb_documents (path, content_sha256, version);
ALTER TABLE public.assistant_kb_documents
    DROP CONSTRAINT IF EXISTS assistant_kb_documents_path_content_sha256_version_key;
