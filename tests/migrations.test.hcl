test "migrate" "review_constraints" {
  migrate {
    to = "20261109224000"
  }

  exec {
    sql = <<-SQL
      INSERT INTO public.document_versions (id, content_hash, document_type, storage_ref)
      VALUES ('00000000-0000-0000-0000-000000000001', repeat('a', 64), 'report', 'pack/report'),
             ('00000000-0000-0000-0000-000000000002', repeat('b', 64), 'summary', 'pack/summary');
      INSERT INTO public.reviews (id, report_version_id, summary_version_id,
        check_profile_version, idempotency_key, request_fingerprint)
      VALUES ('00000000-0000-0000-0000-000000000003',
        '00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000002',
        'financial-v1', 'request-1', repeat('c', 64));
    SQL
  }

  assert {
    sql = "SELECT count(*) = 1 AND bool_and(created_at IS NOT NULL) FROM public.reviews"
  }

  catch {
    sql = "INSERT INTO public.document_versions (id, content_hash, document_type, storage_ref) VALUES (gen_random_uuid(), 'bad', 'report', 'pack/report')"
    error = "violates check constraint"
  }

  catch {
    sql = "INSERT INTO public.document_versions (id, content_hash, document_type, storage_ref) VALUES (gen_random_uuid(), repeat('a', 64), 'other', 'pack/report')"
    error = "violates check constraint"
  }

  catch {
    sql = "INSERT INTO public.document_versions (id, content_hash, document_type, storage_ref) VALUES (gen_random_uuid(), repeat('a', 64), 'report', 'https://external.example/file')"
    error = "violates check constraint"
  }

  catch {
    sql = <<-SQL
      INSERT INTO public.reviews (id, report_version_id, summary_version_id,
        check_profile_version, idempotency_key, request_fingerprint) VALUES (gen_random_uuid(),
        '00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001',
        'financial-v1', 'same-document', repeat('c', 64))
    SQL
    error = "violates check constraint"
  }

  catch {
    sql = <<-SQL
      INSERT INTO public.reviews (id, report_version_id, summary_version_id,
        check_profile_version, idempotency_key, request_fingerprint) VALUES (gen_random_uuid(), gen_random_uuid(),
        '00000000-0000-0000-0000-000000000002', 'financial-v1', 'missing-document', repeat('c', 64))
    SQL
    error = "violates foreign key constraint"
  }

  catch {
    sql = <<-SQL
      INSERT INTO public.reviews (id, report_version_id, summary_version_id,
        check_profile_version, idempotency_key, request_fingerprint) VALUES (gen_random_uuid(),
        '00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000002',
        'financial-v1', 'request-1', repeat('c', 64))
    SQL
    error = "violates unique constraint"
  }
}
