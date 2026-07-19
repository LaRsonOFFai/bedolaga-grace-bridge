from __future__ import annotations

import asyncio

import pytest

from bedolaga_grace_bridge.controller import _finish_cleanup


@pytest.mark.asyncio
async def test_cleanup_finishes_before_cancellation_propagates() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def cleanup() -> None:
        started.set()
        await release.wait()
        finished.set()

    task = asyncio.create_task(_finish_cleanup(cleanup()))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)

    assert not task.done()
    assert not finished.is_set()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
