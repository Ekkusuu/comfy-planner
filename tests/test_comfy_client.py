import httpx
import pytest

from app.comfy.client import ComfyClient, ComfyUnreachable


def client_with(handler) -> ComfyClient:
    comfy = ComfyClient("http://127.0.0.1:8188")
    comfy._http = httpx.AsyncClient(base_url=comfy.base_url, transport=httpx.MockTransport(handler))
    return comfy


async def test_system_stats_answers_are_kept_for_a_few_seconds_unless_fresh():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"system": {"comfyui_version": "1"}})

    comfy = client_with(handler)
    await comfy.system_stats()
    await comfy.system_stats()
    assert await comfy.is_up()
    assert len(calls) == 1                         # the repeats came from the kept answer
    await comfy.system_stats(fresh=True)
    assert len(calls) == 2


async def test_an_unreachable_comfyui_is_asked_once_in_the_window_and_recovers_after_it():
    calls = []
    down = True

    def handler(request):
        calls.append(request.url.path)
        if down:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={})

    comfy = client_with(handler)
    for _ in range(3):
        with pytest.raises(ComfyUnreachable):
            await comfy.system_stats()
    assert not await comfy.is_up() and len(calls) == 1
    down = False
    assert not await comfy.is_up()                  # still inside the window
    assert await comfy.is_up(fresh=True)            # a fresh check sees it is back
    assert await comfy.is_up()


async def test_set_url_forgets_the_kept_answer():
    comfy = client_with(lambda request: httpx.Response(200, json={}))
    await comfy.system_stats()
    await comfy.set_url("http://127.0.0.1:9999")
    assert comfy._stats is None
    await comfy.close()
