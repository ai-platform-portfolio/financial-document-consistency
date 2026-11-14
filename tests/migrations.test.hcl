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

test "migrate" "pipeline_state" {
  migrate {
    to = "20261110210000"
  }

  exec {
    sql = <<-SQL
      INSERT INTO public.document_versions (id,content_hash,document_type,storage_ref,verified)
      VALUES ('00000000-0000-0000-0000-000000000011',repeat('a',64),'report','originals/report.pdf',true),
             ('00000000-0000-0000-0000-000000000012',repeat('b',64),'summary','originals/summary.pdf',true);
      INSERT INTO public.reviews (id,report_version_id,summary_version_id,check_profile_version,idempotency_key,request_fingerprint)
      VALUES ('00000000-0000-0000-0000-000000000013','00000000-0000-0000-0000-000000000011',
              '00000000-0000-0000-0000-000000000012','financial-v1','pipeline-request',repeat('c',64));
      INSERT INTO public.review_runs (review_id,config_hash)
      VALUES ('00000000-0000-0000-0000-000000000013',repeat('d',64));
    SQL
  }

  assert {
    sql = "SELECT status='queued' AND model_calls=0 FROM public.review_runs"
  }

  assert {
    sql = "SELECT EXISTS(SELECT 1 FROM pg_proc WHERE proname='procrastinate_defer_jobs_v1') AND EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='procrastinate_jobs_notify_queue_job_inserted_v1')"
  }

  catch {
    sql = "UPDATE public.review_runs SET status='completed'"
    error = "violates check constraint"
  }

  catch {
    sql = "UPDATE public.review_runs SET model_calls=13"
    error = "violates check constraint"
  }

  catch {
    sql = "INSERT INTO public.upload_intents (id,document_type,content_hash,size_bytes) VALUES (gen_random_uuid(),'report',repeat('a',64),5242881)"
    error = "violates check constraint"
  }
}
