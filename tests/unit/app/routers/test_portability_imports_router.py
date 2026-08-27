# -*- coding: utf-8 -*-
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from qwenpaw.app.routers import agent_scoped
from qwenpaw.app.routers import portability_imports as routes
from qwenpaw.portability.import_jobs import ImportJobSnapshot
from qwenpaw.portability.models import ImportSelection, SourceLocation


def _request(host: str = "127.0.0.1"):
    return SimpleNamespace(client=SimpleNamespace(host=host))


class _Jobs:
    def __init__(self):
        self.calls = []

    async def create(self, workspace, sources):
        self.calls.append(("create", workspace, sources))
        return ImportJobSnapshot(job_id="import-" + "a" * 32, agent_id="paw")

    async def snapshot(self, workspace, job_id):
        self.calls.append(("snapshot", workspace, job_id))
        return ImportJobSnapshot(
            job_id=job_id,
            agent_id="paw",
            state="completed",
        )

    async def start(self, workspace, job_id, selections):
        self.calls.append(("start", workspace, job_id, selections))
        return ImportJobSnapshot(
            job_id=job_id,
            agent_id="paw",
            state="running",
        )

    async def subscribe(self, workspace, job_id, after=0):
        self.calls.append(("subscribe", workspace, job_id, after))
        yield {"seq": 2, "snapshot": {"state": "completed"}}


@pytest.fixture(name="api")
def _api(monkeypatch):
    workspace = SimpleNamespace(agent_id="paw")
    jobs = _Jobs()

    async def get_workspace(_request):
        return workspace

    monkeypatch.setattr(routes, "get_agent_for_request", get_workspace)
    monkeypatch.setattr(routes, "PORTABILITY_IMPORT_JOBS", jobs)
    return workspace, jobs


@pytest.mark.asyncio
async def test_sources_probe_only_returns_detection(monkeypatch):
    def locate(source):
        return SourceLocation(
            provider_id=source,
            data_home=f"/secret/{source}",
            data_home_exists=source == "codex",
        )

    monkeypatch.setattr(routes, "resolve_source_location", locate)
    result = await routes.list_import_sources(_request())

    assert result == [
        {"source": "codex", "name": "Codex", "detected": True},
        {"source": "qoder", "name": "Qoder", "detected": False},
    ]
    assert "/secret" not in str(result)


@pytest.mark.asyncio
async def test_job_routes_pin_workspace_and_submit_selection(api):
    workspace, jobs = api
    created = await routes.create_import_job(
        routes.CreateImportJobRequest(sources=["codex"]),
        _request(),
    )
    job_id = created.job_id
    await routes.get_import_job(job_id, _request())
    await routes.start_import_job(
        job_id,
        routes.StartImportJobRequest(
            selections={"codex": ImportSelection(skills=["skill-1"])},
        ),
        _request(),
    )

    assert jobs.calls == [
        ("create", workspace, ["codex"]),
        ("snapshot", workspace, job_id),
        (
            "start",
            workspace,
            job_id,
            {"codex": ImportSelection(skills=["skill-1"])},
        ),
    ]


@pytest.mark.asyncio
async def test_events_are_sse_and_replay_sequence(api):
    _workspace, jobs = api
    job_id = "import-" + "a" * 32
    response = await routes.stream_import_events(job_id, _request(), after=1)
    body = "".join([chunk async for chunk in response.body_iterator])

    assert response.media_type == "text/event-stream"
    assert '"seq":2' in body
    assert jobs.calls[-1][-1] == 1


@pytest.mark.asyncio
async def test_router_is_agent_scoped_and_localhost_only():
    scoped = agent_scoped.create_agent_scoped_router()
    mounted = [item.original_router for item in scoped.routes]
    assert routes.portability_import_router in mounted
    assert routes.portability_import_router.prefix == "/portability/imports"

    with pytest.raises(HTTPException) as error:
        await routes.list_import_sources(_request("203.0.113.9"))
    assert error.value.status_code == 403
