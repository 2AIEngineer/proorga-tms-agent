import httpx
import pytest
from tms_mcp.connector import RestTmsConnector, TmsError, TmsNotFound, TmsUnavailable


def connector(handler, **kw) -> RestTmsConnector:
    return RestTmsConnector("http://tms/v1", transport=httpx.MockTransport(handler), max_retries=kw.pop("retries", 2), **kw)


async def test_pagination_follows_cursor():
    def handler(request: httpx.Request):
        cursor = request.url.params.get("cursor")
        assert request.method == "GET"
        if cursor is None:
            return httpx.Response(200, json={"data": [{"id": "a"}], "pagination": {"cursor": "c2", "has_more": True}})
        return httpx.Response(200, json={"data": [{"id": "b"}], "pagination": {"cursor": None, "has_more": False}})

    assert [m["id"] for m in await connector(handler).list_missions(status=["in_progress"])] == ["a", "b"]


async def test_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(503, json={"title": "Busy"}) if len(attempts) < 3 else httpx.Response(200, json={"time": "2025-11-04T09:00:00Z"})

    assert (await connector(handler).now()).hour == 9
    assert len(attempts) == 3


async def test_unavailable_after_retries(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(TmsUnavailable):
        await connector(handler).health()


async def test_problem_json_translated():
    def handler(request):
        return httpx.Response(404, json={"title": "Mission introuvable", "detail": "Aucune mission 'x'.", "status": 404})

    with pytest.raises(TmsNotFound, match="Mission introuvable : Aucune mission"):
        await connector(handler).get_mission("x")

    def bad(request):
        return httpx.Response(422, json={"title": "Requête invalide"})

    with pytest.raises(TmsError) as exc:
        await connector(bad).get_mission("x")
    assert exc.value.status == 422


async def test_role_users_cached():
    hits = []

    def handler(request):
        hits.append(1)
        return httpx.Response(200, json={"data": [{"id": "u"}], "pagination": {"has_more": False}})

    c = connector(handler)
    await c.list_role_users("ops_agent")
    await c.list_role_users("ops_agent")
    assert len(hits) == 1


async def _no_sleep(_):
    return None
