from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.jobs import errors, queue, store
from app.jobs.tasks import run_job
from app.main import app


@pytest.fixture
def jobs_root(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "JOBS_DIR", tmp_path)
    return tmp_path


def _write_status(job_id: str, **fields) -> dict:
    payload = {
        "job_id": job_id,
        "kind": "trend_analysis",
        "status": "queued",
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
        "request": {"variable": "ndvi", "start_date": "2024-01-01", "end_date": "2024-01-31"},
    }
    payload.update(fields)
    store.write_status(job_id, payload)
    return payload


class _FakeRqJob:
    def __init__(self) -> None:
        self.canceled = False

    def cancel(self) -> None:
        self.canceled = True


def test_list_jobs_is_newest_first_and_filters(jobs_root):
    _write_status("older", created_at="2024-01-01T00:00:00+00:00")
    _write_status("newer", created_at="2024-03-01T00:00:00+00:00", status="finished")
    _write_status(
        "region",
        created_at="2024-02-01T00:00:00+00:00",
        kind="region_comparison",
        status="failed",
    )

    client = TestClient(app)
    body = client.get("/api/v1/analysis/jobs").json()
    assert [item["job_id"] for item in body["items"]] == ["newer", "region", "older"]
    assert body["total"] == 3

    filtered = client.get("/api/v1/analysis/jobs", params={"kind": "region_comparison"}).json()
    assert [item["job_id"] for item in filtered["items"]] == ["region"]

    by_status = client.get("/api/v1/analysis/jobs", params={"status": "finished,failed"}).json()
    assert [item["job_id"] for item in by_status["items"]] == ["newer", "region"]


def test_list_jobs_paginates(jobs_root):
    for index in range(3):
        _write_status(f"job{index}", created_at=f"2024-01-{index + 1:02d}T00:00:00+00:00")

    body = TestClient(app).get("/api/v1/analysis/jobs", params={"limit": 2, "offset": 0}).json()
    assert [item["job_id"] for item in body["items"]] == ["job2", "job1"]
    assert body["next"] == 2

    tail = TestClient(app).get("/api/v1/analysis/jobs", params={"limit": 2, "offset": 2}).json()
    assert [item["job_id"] for item in tail["items"]] == ["job0"]
    assert tail["next"] is None


def test_job_details_include_original_request(jobs_root):
    _write_status("job1", status="started", percent=40.0)

    body = TestClient(app).get("/api/v1/analysis/jobs/job1").json()
    assert body["status"] == "started"
    assert body["request"]["variable"] == "ndvi"


def test_unknown_job_returns_404_without_creating_directory(jobs_root):
    response = TestClient(app).get("/api/v1/analysis/jobs/does-not-exist")
    assert response.status_code == 404
    assert not (jobs_root / "does-not-exist").exists()


def test_cancel_queued_job_uses_rq_cancel(jobs_root, monkeypatch):
    monkeypatch.setattr(queue, "QUEUE_MODE", "redis")
    fake_job = _FakeRqJob()
    monkeypatch.setattr(queue, "_get_rq_job", lambda job_id: fake_job)
    _write_status("job1", status="queued")

    response = TestClient(app).post("/api/v1/analysis/jobs/job1/cancel")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "canceled"
    assert body["cancel_requested"] is True
    assert body["canceled_at"]
    assert fake_job.canceled is True


def test_cancel_running_job_is_cooperative_by_default(jobs_root, monkeypatch):
    monkeypatch.setattr(queue, "QUEUE_MODE", "redis")
    stops = []

    def fake_stop(job_id):
        stops.append(job_id)
        return True

    monkeypatch.setattr(queue, "_stop_running_rq_job", fake_stop)
    _write_status("job1", status="started")

    body = TestClient(app).post("/api/v1/analysis/jobs/job1/cancel").json()
    assert body["status"] == "canceling"
    assert body["cancel_requested"] is True
    assert stops == []


def test_cancel_running_job_with_force_stops_the_worker(jobs_root, monkeypatch):
    monkeypatch.setattr(queue, "QUEUE_MODE", "redis")
    stops = []
    monkeypatch.setattr(queue, "_stop_running_rq_job", lambda job_id: stops.append(job_id) or True)
    _write_status("job1", status="started")

    body = TestClient(app).post("/api/v1/analysis/jobs/job1/cancel", params={"force": True}).json()
    assert body["status"] == "canceled"
    assert stops == ["job1"]


def test_force_cancel_falls_back_to_cooperative_when_stop_fails(jobs_root, monkeypatch):
    monkeypatch.setattr(queue, "QUEUE_MODE", "redis")
    monkeypatch.setattr(queue, "_stop_running_rq_job", lambda job_id: False)
    _write_status("job1", status="started")

    body = TestClient(app).post("/api/v1/analysis/jobs/job1/cancel", params={"force": True}).json()
    assert body["status"] == "canceling"


def test_cancel_finished_job_returns_conflict(jobs_root, monkeypatch):
    monkeypatch.setattr(queue, "QUEUE_MODE", "redis")
    _write_status("job1", status="finished")

    response = TestClient(app).post("/api/v1/analysis/jobs/job1/cancel")
    assert response.status_code == 409
    assert "already finished" in response.json()["detail"]


def test_cancel_inline_job_returns_conflict(jobs_root, monkeypatch):
    monkeypatch.setattr(queue, "QUEUE_MODE", "inline")
    _write_status("job1", status="started")

    response = TestClient(app).post("/api/v1/analysis/jobs/job1/cancel")
    assert response.status_code == 409
    assert "inline" in response.json()["detail"].lower()


def test_cancel_unknown_job_returns_404(jobs_root, monkeypatch):
    monkeypatch.setattr(queue, "QUEUE_MODE", "redis")
    response = TestClient(app).post("/api/v1/analysis/jobs/nope/cancel")
    assert response.status_code == 404
    assert not (jobs_root / "nope").exists()


def test_worker_marks_canceled_when_service_stops(jobs_root, monkeypatch):
    _write_status("job1", status="queued")

    def fake_execute(payload, job_id, progress=None, should_cancel=None):
        assert should_cancel is not None
        status = store.read_status(job_id)
        status["cancel_requested"] = True
        store.write_status(job_id, status)
        raise errors.JobCanceled()

    monkeypatch.setattr("app.jobs.tasks.execute_trend_analysis", fake_execute)

    outcome = run_job("job1", "trend_analysis", {})
    assert outcome["canceled"] is True

    status = store.read_status("job1")
    assert status["status"] == "canceled"
    assert status["canceled_at"]
    assert "error" not in status
    assert not (jobs_root / "job1" / "result.json").exists()


def test_cancel_requested_before_finish_discards_result(jobs_root, monkeypatch):
    _write_status("job1", status="queued")

    def fake_execute(payload, job_id, progress=None, should_cancel=None):
        status = store.read_status(job_id)
        status["cancel_requested"] = True
        store.write_status(job_id, status)
        return {"success": True, "job_id": job_id}

    monkeypatch.setattr("app.jobs.tasks.execute_trend_analysis", fake_execute)

    outcome = run_job("job1", "trend_analysis", {})
    assert outcome["canceled"] is True
    assert store.read_status("job1")["status"] == "canceled"
    assert not (jobs_root / "job1" / "result.json").exists()


def test_create_job_persists_request(jobs_root, monkeypatch):
    monkeypatch.setattr(queue, "QUEUE_MODE", "inline")
    monkeypatch.setattr("app.jobs.tasks.run_job", lambda *args, **kwargs: None)

    job_id = queue.create_job("trend_analysis", {"variable": "ndvi", "start_date": "2024-01-01"})

    status = store.read_status(job_id)
    assert status["request"]["variable"] == "ndvi"
    assert status["status"] == "queued"
