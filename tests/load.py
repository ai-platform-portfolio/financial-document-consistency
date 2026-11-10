"""Owned local PostgreSQL/worker benchmark; accepts no external target or model URL."""

import json
import math
import statistics
import time
from uuid import uuid4

import psycopg

from integration import IntegrationTests as Fixture, docker
import resource


def percentiles(samples):
    samples = sorted(samples)
    return {
        "p50_ms": round(statistics.median(samples) * 1000, 3),
        "p95_ms": round(samples[math.ceil(len(samples) * 0.95) - 1] * 1000, 3),
    }


def main():
    results = []
    Fixture.setUpClass()
    try:
        fixture = Fixture()
        body = fixture.review_input()
        with psycopg.connect(Fixture.runtime_url) as c:
            c.execute("SET statement_timeout='15s'")
            for rows in [1000, 10000, 100000]:
                c.execute(
                    "DELETE FROM public.reviews WHERE idempotency_key LIKE 'load-seed-%'"
                )
                c.execute(
                    "INSERT INTO public.reviews (id,report_version_id,summary_version_id,check_profile_version,idempotency_key,request_fingerprint) "
                    "SELECT gen_random_uuid(),%s,%s,'financial-v1','load-seed-'||n,repeat('a',64) FROM generate_series(1,%s) n",
                    (body["report_version_id"], body["summary_version_id"], rows),
                )
                c.commit()
                identifier = c.execute(
                    "SELECT id FROM public.reviews WHERE idempotency_key='load-seed-1'"
                ).fetchone()[0]
                samples = []
                for _ in range(100):
                    start = time.perf_counter()
                    c.execute(
                        "SELECT * FROM public.reviews WHERE id=%s", (identifier,)
                    ).fetchone()
                    samples.append(time.perf_counter() - start)
                plan = c.execute(
                    "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT * FROM public.reviews WHERE id=%s",
                    (identifier,),
                ).fetchone()[0]
                for replicas in [1, 2]:
                    # Reuse a verified pair; each review still gets independently evaluated steps.
                    report = fixture.upload(
                        "report",
                        "Example Ltd 2025 statutory revenue 100 GBP. Margin declined.",
                    )
                    summary = fixture.upload(
                        "summary",
                        "Example Ltd 2025 statutory revenue 110 GBP. Margin improved.",
                    )
                    request = {
                        "report_version_id": report["id"],
                        "summary_version_id": summary["id"],
                        "check_profile_version": "financial-v1",
                    }
                    ids = [
                        fixture.request("/api/reviews", request, uuid4().hex)[1]["id"]
                        for _ in range(20)
                    ]
                    cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
                    workers = [
                        fixture.start_worker(delay="0.05", quiet=True)
                        for _ in range(replicas)
                    ]
                    start = time.monotonic()
                    deadline = start + 60
                    peak_connections = 0
                    max_queue_age = 0
                    cpu_sample = None
                    while time.monotonic() < deadline:
                        done = c.execute(
                            "SELECT count(*) FROM public.review_runs WHERE review_id=ANY(%s::uuid[]) AND status='completed'",
                            (ids,),
                        ).fetchone()[0]
                        peak_connections = max(
                            peak_connections,
                            c.execute(
                                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()"
                            ).fetchone()[0],
                        )
                        age = c.execute(
                            "SELECT max(extract(epoch FROM now()-created_at)) FROM public.review_runs WHERE review_id=ANY(%s::uuid[]) AND status='queued'",
                            (ids,),
                        ).fetchone()[0]
                        max_queue_age = max(max_queue_age, float(age or 0))
                        c.commit()
                        if cpu_sample is None and time.monotonic() - start > 1:
                            cpu_sample = docker(
                                "stats",
                                "--no-stream",
                                "--format",
                                "{{.CPUPerc}}",
                                Fixture.container,
                            )
                        if done == len(ids):
                            break
                        time.sleep(0.2)
                    assert done == len(ids), "Bounded benchmark failed to complete"
                    elapsed = time.monotonic() - start
                    for worker in workers:
                        worker.terminate()
                    for worker in workers:
                        worker.wait(timeout=15)
                    latencies = c.execute(
                        "SELECT extract(epoch FROM updated_at-created_at)::float FROM public.review_runs WHERE review_id=ANY(%s::uuid[])",
                        (ids,),
                    ).fetchall()
                    cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
                    connections = c.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()"
                    ).fetchone()[0]
                    results.append(
                        {
                            "seed_rows": rows,
                            "workers": replicas,
                            "reviews": len(ids),
                            "synthetic_model": True,
                            "reviews_per_second": round(len(ids) / elapsed, 3),
                            "read": percentiles(samples),
                            "completion": percentiles([x[0] for x in latencies]),
                            "observed_connections_after_run": connections,
                            "peak_connections": peak_connections,
                            "max_observed_queue_age_seconds": round(max_queue_age, 3),
                            "postgres_cpu_sample": cpu_sample,
                            "worker_cpu_seconds": round(
                                cpu_after.ru_utime
                                + cpu_after.ru_stime
                                - cpu_before.ru_utime
                                - cpu_before.ru_stime,
                                3,
                            ),
                            "query_plan": plan[0]["Plan"]["Node Type"],
                        }
                    )
                c.commit()
        print(json.dumps(results, indent=2))
    finally:
        Fixture.doClassCleanups()


if __name__ == "__main__":
    main()
