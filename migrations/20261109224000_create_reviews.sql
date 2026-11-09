CREATE TABLE public.document_versions (
    created_at timestamptz NOT NULL DEFAULT now(),
    content_hash text NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    document_type text NOT NULL CHECK (document_type IN ('report', 'summary')),
    storage_ref text NOT NULL CHECK (
        length(storage_ref) BETWEEN 1 AND 512 AND
        storage_ref ~ '^[A-Za-z0-9][A-Za-z0-9._/-]*$'
    ),
    id uuid PRIMARY KEY
);

CREATE TABLE public.reviews (
    created_at timestamptz NOT NULL DEFAULT now(),
    check_profile_version text NOT NULL CHECK (
        length(check_profile_version) BETWEEN 1 AND 100 AND
        check_profile_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]*$'
    ),
    idempotency_key text NOT NULL UNIQUE CHECK (
        length(idempotency_key) BETWEEN 1 AND 128 AND
        idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._-]*$'
    ),
    request_fingerprint text NOT NULL CHECK (request_fingerprint ~ '^[a-f0-9]{64}$'),
    id uuid PRIMARY KEY,
    report_version_id uuid NOT NULL REFERENCES public.document_versions(id),
    summary_version_id uuid NOT NULL REFERENCES public.document_versions(id),
    CHECK (report_version_id <> summary_version_id)
);

CREATE INDEX reviews_report_version_idx ON public.reviews (report_version_id);
CREATE INDEX reviews_summary_version_idx ON public.reviews (summary_version_id);
