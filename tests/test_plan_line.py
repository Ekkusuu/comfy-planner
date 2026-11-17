"""The planning line (app/planning/runner.py): several plans wait in order and run back to back under one GPU lease."""
import asyncio
from types import SimpleNamespace

import pytest

from app.events import EventBus
from app.gpu import GpuLease
from app.planning.runner import PlanRunner
from tests.test_gpu import FakeFreeComfy, FakeServer, FakeWorker, Recorder


async def no_tag_data():
    """Stands in for PlanContext.load_tag_data: these tests plan nothing."""


@pytest.fixture
async def line_runner(config, db, store, comfy):
    """A PlanRunner with stubbed GPU (FakeWorker / FakeServer) and a trivial context: runs are plain callbacks."""
    rec = Recorder()
    lease = GpuLease(FakeWorker(rec), FakeFreeComfy(rec), FakeServer(rec))
    ctx = SimpleNamespace(oi=None, embedder=None, load_tag_data=no_tag_data)
    runner = PlanRunner(ctx, store, lease, comfy, EventBus(), config, db)
    yield runner, lease, rec
    await runner.close()


async def wait_status(runner, plan_id, timeout=10):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        plan = runner.get(plan_id)
        if plan["status"] in ("done", "failed", "cancelled"):
            return plan
        if loop.time() > deadline:
            raise AssertionError(f"plan {plan_id} stuck at {plan['status']}: {plan['message']}")
        await asyncio.sleep(0.02)


async def wait_started(order, timeout=5):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not order:
        if loop.time() > deadline:
            raise AssertionError("run never started")
        await asyncio.sleep(0.02)


async def test_two_plans_share_one_lease_and_run_in_order(line_runner):
    runner, lease, rec = line_runner
    order = []

    async def run_a(plan):
        order.append("a")

    async def run_b(plan):
        order.append("b")

    a = runner._start("plan", "a", run_a)
    b = runner._start("plan", "b", run_b)
    await wait_status(runner, a["id"])
    await wait_status(runner, b["id"])
    assert order == ["a", "b"]
    assert runner.get(a["id"])["status"] == "done" and runner.get(b["id"])["status"] == "done"
    assert rec.calls.count("server.start") == 1, rec.calls
    assert rec.calls == ["worker.hold", "comfy.free", "server.start", "server.stop", "worker.release"]
    assert not lease.active


async def test_plan_added_during_run_joins_same_lease(line_runner):
    runner, lease, rec = line_runner
    order = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def first(plan):
        order.append("first-start")
        started.set()
        await asyncio.wait_for(release.wait(), 10)
        order.append("first-end")

    async def second(plan):
        order.append("second")

    a = runner._start("plan", "a", first)
    await asyncio.wait_for(started.wait(), 5)
    assert lease.active
    b = runner._start("plan", "b", second)
    release.set()
    await wait_status(runner, a["id"])
    await wait_status(runner, b["id"])
    assert order == ["first-start", "first-end", "second"]
    assert rec.calls.count("server.start") == 1, rec.calls
    assert not lease.active


async def test_plan_cancelled_while_waiting_never_runs(line_runner):
    runner, lease, rec = line_runner
    started = asyncio.Event()
    release = asyncio.Event()
    ran = []

    async def first(plan):
        started.set()
        await asyncio.wait_for(release.wait(), 10)

    async def second(plan):
        ran.append(True)

    a = runner._start("plan", "a", first)
    await asyncio.wait_for(started.wait(), 5)
    b = runner._start("plan", "b", second)
    # waiting plan is next up
    assert runner.get(b["id"])["queue_position"] == 1
    cancelled = runner.cancel(b["id"])
    assert cancelled["status"] == "cancelled"
    assert runner.get(b["id"])["status"] == "cancelled"
    release.set()
    await wait_status(runner, a["id"])
    assert runner.get(a["id"])["status"] == "done"
    assert ran == []
    assert rec.calls.count("server.start") == 1, rec.calls
    assert not lease.active


async def test_failed_plan_does_not_stop_next(line_runner):
    runner, lease, rec = line_runner
    order = []

    async def boom(plan):
        raise ValueError("boom-test")

    async def ok(plan):
        order.append("ok")

    a = runner._start("plan", "a", boom)
    b = runner._start("plan", "b", ok)
    failed = await wait_status(runner, a["id"])
    done = await wait_status(runner, b["id"])
    assert failed["status"] == "failed" and "boom-test" in (failed["error"] or "")
    assert done["status"] == "done" and order == ["ok"]
    assert rec.calls.count("server.start") == 1, rec.calls
    assert not lease.active


async def test_queue_position_is_reported_correctly(line_runner):
    runner, lease, rec = line_runner
    started = asyncio.Event()
    release = asyncio.Event()

    async def block(plan):
        started.set()
        await asyncio.wait_for(release.wait(), 10)

    async def quick(plan):
        pass

    a = runner._start("plan", "a", block)
    await asyncio.wait_for(started.wait(), 5)
    b = runner._start("plan", "b", quick)
    c = runner._start("plan", "c", quick)
    assert runner.get(a["id"])["queue_position"] is None  # being worked on
    assert runner.get(b["id"])["queue_position"] == 1  # next up
    assert runner.get(c["id"])["queue_position"] == 2
    release.set()
    await wait_status(runner, a["id"])
    await wait_status(runner, b["id"])
    await wait_status(runner, c["id"])
    assert runner.get(a["id"])["queue_position"] is None
    assert runner.get(b["id"])["queue_position"] is None
    assert runner.get(c["id"])["queue_position"] is None
    assert not lease.active


async def test_no_worker_left_held_at_end(config, db, store, comfy, worker):
    """With the real queue worker: after the line drains it is not held and the lease is free."""
    rec = Recorder()
    lease = GpuLease(worker, comfy, FakeServer(rec))
    ctx = SimpleNamespace(oi=None, embedder=None, load_tag_data=no_tag_data)
    runner = PlanRunner(ctx, store, lease, comfy, EventBus(), config, db)
    try:
        order = []

        async def run(plan):
            order.append("ran")

        a = runner._start("plan", "a", run)
        b = runner._start("plan", "b", run)
        await wait_status(runner, a["id"])
        await wait_status(runner, b["id"])
        assert order == ["ran", "ran"]
        assert not worker.held and not lease.active
        assert worker.state()["held"] is False
    finally:
        await runner.close()
