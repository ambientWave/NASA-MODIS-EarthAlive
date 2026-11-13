"""Distributed comparison fan-out: part planning, execution, assembly, cancel."""

from __future__ import annotations

import pytest

from app.jobs import queue as job_queue
from app.jobs import store, tasks
from app.jobs.store import read_status, read_result, write_status


@pytest.fixture
def jobs_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(tasks, "JOBS_DIR", tmp_path)
    return tmp_path


class _FakeRedis:
    def __init__(self) -> None:
        self.counters: dict[str, int] = {}

    def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def expire(self, key: str, seconds: int) -> bool:
        return True


class _FakeQueue:
    def __init__(self, *args, **kwargs) -> None:
        self.calls: list[tuple[str, str, tuple, dict]] = []

    def enqueue(self, func, *args, **kwargs):
        self.calls.append(("enqueue", func, args, kwargs))

    def enqueue_in(self, delta, func, *args, **kwargs):
        self.calls.append(("enqueue_in", func, args, kwargs))


def _variable_payload(variables: list[str]) -> dict:
    return {
        "aoi": {"west": 30.0, "south": 29.0, "east": 31.0, "north": 30.0},
        "start_date": "2023-01-01",
        "end_date": "2023-03-01",
        "temporal_resolution": "daily",
        "variables": variables,
        "analysis_methods": ["linear_regression"],
        "include_visualizations": False,
    }


def _region_payload(regions: list[dict]) -> dict:
    return {
        "variable": "ndvi",
        "regions": regions,
        "start_date": "2023-01-01",
        "end_date": "2023-03-01",
        "temporal_resolution": "daily",
        "analysis_methods": ["linear_regression"],
        "include_visualizations": False,
    }


def test_comparison_part_labels_split_variables_and_regions():
    assert job_queue.comparison_part_labels("variable_comparison", _variable_payload(["ndvi", "lst"])) == [
        "ndvi",
        "lst",
    ]
    assert job_queue.comparison_part_labels("region_comparison", _region_payload([{}, {}, {}])) == [
        "region_1",
        "region_2",
        "region_3",
    ]
    assert job_queue.comparison_part_labels("trend_analysis", {}) == []


def test_create_job_fans_out_comparison_parts(jobs_dir, monkeypatch):
    fake_queue = _FakeQueue()
    monkeypatch.setattr(job_queue, "QUEUE_MODE", "redis")
    monkeypatch.setattr(job_queue, "_redis_connection", lambda: object())
    monkeypatch.setattr("rq.Queue", lambda *args, **kwargs: fake_queue)

    job_id = job_queue.create_job("variable_comparison", _variable_payload(["ndvi", "lst", "gpm"]))

    enqueued = [call for call in fake_queue.calls if call[0] == "enqueue"]
    watchdogs = [call for call in fake_queue.calls if call[0] == "enqueue_in"]
    assert len(enqueued) == 3
    assert len(watchdogs) == 1
    assert all(call[1] == "app.jobs.tasks.run_comparison_part" for call in enqueued)
    assert [call[3]["job_id"] for call in enqueued] == [f"{job_id}-p0", f"{job_id}-p1", f"{job_id}-p2"]

    status = read_status(job_id)
    assert status["parts_total"] == 3
    assert [part["label"] for part in status["parts"]] == ["ndvi", "lst", "gpm"]
    assert status["status"] == "queued"


def test_create_job_keeps_single_part_comparison_on_normal_path(jobs_dir, monkeypatch):
    fake_queue = _FakeQueue()
    monkeypatch.setattr(job_queue, "QUEUE_MODE", "redis")
    monkeypatch.setattr(job_queue, "_redis_connection", lambda: object())
    monkeypatch.setattr("rq.Queue", lambda *args, **kwargs: fake_queue)

    job_id = job_queue.create_job("variable_comparison", _variable_payload(["ndvi"]))

    assert [call[1] for call in fake_queue.calls] == ["app.jobs.tasks.run_job"]
    status = read_status(job_id)
    assert "parts" not in status


def _seed_parent(jobs_dir, job_id: str, total: int) -> None:
    write_status(
        job_id,
        {
            "job_id": job_id,
            "kind": "variable_comparison",
            "status": "queued",
            "parts_total": total,
            "parts": [
                {"index": index, "label": f"part_{index}", "rq_job_id": f"{job_id}-p{index}"}
                for index in range(total)
            ],
        },
    )


def _fake_part(key: str) -> dict:
    return {
        "key": key,
        "label": key,
        "unit": "unit",
        "status": "calculated",
        "entry": {"metadata": {"variable_id": key}, "series": [], "warnings": []},
        "series": [],
        "analysis": {},
        "warnings": [],
    }


def test_parts_run_on_separate_workers_and_last_one_assembles(jobs_dir, monkeypatch):
    job_id = "job123"
    payload = _variable_payload(["ndvi", "lst"])
    _seed_parent(jobs_dir, job_id, 2)

    redis = _FakeRedis()
    monkeypatch.setattr(tasks, "redis_connection", lambda: redis)
    monkeypatch.setattr(tasks, "variable_part", lambda payload, variable_id, job_id, **kwargs: _fake_part(variable_id))
    monkeypatch.setattr(
        tasks,
        "assemble_variable_comparison",
        lambda payload, parent_id, parts: {"success": True, "job_id": parent_id, "keys": [p["key"] for p in parts]},
    )

    tasks.run_comparison_part(job_id, "variable_comparison", payload, 0)

    status = read_status(job_id)
    assert status["status"] == "started"
    assert status["current"] == 1
    assert read_result(job_id) is None

    tasks.run_comparison_part(job_id, "variable_comparison", payload, 1)

    assert read_part_status(job_id, 0)["status"] == "finished"
    assert read_part_status(job_id, 1)["status"] == "finished"
    assert read_result(job_id)["keys"] == ["ndvi", "lst"]
    status = read_status(job_id)
    assert status["status"] == "finished"
    assert status["percent"] == 100.0
    assert status["result_url"].endswith(f"/{job_id}/result")


def read_part_status(job_id: str, index: int) -> dict:
    return tasks.read_part_status(job_id, index)


def test_watchdog_fails_parent_when_a_part_never_produced_a_result(jobs_dir):
    job_id = "jobwatch"
    _seed_parent(jobs_dir, job_id, 2)

    tasks.finalize_comparison_watchdog(job_id, "variable_comparison", _variable_payload(["ndvi", "lst"]))

    status = read_status(job_id)
    assert status["status"] == "failed"
    assert "part(s) [1, 2] produced no result" in status["error"]


def test_cancel_stops_queued_parts_and_settles_parent(jobs_dir, monkeypatch):
    job_id = "jobcancel"
    _seed_parent(jobs_dir, job_id, 2)
    monkeypatch.setattr(job_queue, "QUEUE_MODE", "redis")
    monkeypatch.setattr(job_queue, "mark_cancel_requested", lambda job_id: None)
    monkeypatch.setattr(job_queue, "_get_rq_job", lambda job_id: None)
    monkeypatch.setattr(job_queue, "_cancel_queued_rq_job", lambda job_id: True)

    status = job_queue.cancel_job(job_id)

    assert status["status"] == "canceled"
    assert tasks.read_part_status(job_id, 0)["status"] == "canceled"
    assert tasks.read_part_status(job_id, 1)["status"] == "canceled"


def test_refresh_parent_progress_is_monotonic(jobs_dir):
    job_id = "jobmono"
    _seed_parent(jobs_dir, job_id, 2)
    tasks._write_part_status(job_id, 0, {"label": "ndvi", "status": "started", "detail": "ndvi", "percent": 80.0})
    tasks._refresh_parent_progress(job_id, "variable_comparison")
    assert read_status(job_id)["percent"] == 40.0

    tasks._write_part_status(job_id, 0, {"label": "ndvi", "status": "finished", "percent": 100.0})
    tasks._refresh_parent_progress(job_id, "variable_comparison")
    assert read_status(job_id)["percent"] == 50.0

    # A late write from a slower part must not move the bar backwards.
    tasks._write_part_status(job_id, 1, {"label": "lst", "status": "started", "detail": "lst", "percent": 10.0})
    tasks._refresh_parent_progress(job_id, "variable_comparison")
    assert read_status(job_id)["percent"] == 55.0

    # A late write from a part that has already finished must not move the bar
    # backwards.
    tasks._write_part_status(job_id, 0, {"label": "ndvi", "status": "started", "detail": "ndvi", "percent": 40.0})
    tasks._refresh_parent_progress(job_id, "variable_comparison")
    assert read_status(job_id)["percent"] == 55.0
