# SPDX-License-Identifier: GPL-3.0-or-later
"""Rapid OBS overlay toggles must leave no listener behind."""
import asyncio
import socket

import aiohttp
import pytest
from aiohttp import web

from multichat.overlay.server import OverlayServer


@pytest.fixture
def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def assert_listener_closed(port):
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 2)
    except OSError:
        return
    writer.close()
    await writer.wait_closed()
    pytest.fail("Overlay still accepts connections after cleanup")


def test_stop_requested_during_start_leaves_overlay_stopped(monkeypatch, port):
    async def run():
        overlay = OverlayServer(asyncio.get_running_loop())
        entered, release = asyncio.Event(), asyncio.Event()
        start_site = web.TCPSite.start
        sites = []

        async def delayed_start(site):
            sites.append(site)
            await start_site(site)
            entered.set()
            await release.wait()

        monkeypatch.setattr(web.TCPSite, "start", delayed_start)
        starting = asyncio.create_task(overlay.start(port))
        stopping = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            stopping = asyncio.create_task(overlay.stop())
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(asyncio.gather(starting, stopping), 3)
            assert not overlay.running
            await assert_listener_closed(port)
        finally:
            release.set()
            await asyncio.gather(*[task for task in (starting, stopping) if task], return_exceptions=True)
            await overlay.stop()
            for site in sites:
                await site._runner.cleanup()

    asyncio.run(run())


def test_repeated_and_concurrent_start_stop(port):
    async def run():
        overlay = OverlayServer(asyncio.get_running_loop())
        try:
            for _ in range(3):
                await asyncio.gather(overlay.start(port), overlay.start(port))
                runner = overlay.runner
                await overlay.start(port)
                assert overlay.runner is runner
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"http://127.0.0.1:{port}/") as response:
                        assert response.status == 200
                await asyncio.gather(overlay.stop(), overlay.stop())
                assert not overlay.running and not overlay.clients
                await assert_listener_closed(port)
        finally:
            await overlay.stop()

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["error", "cancel"])
def test_failed_or_cancelled_start_releases_the_socket(monkeypatch, port, outcome):
    async def run():
        overlay = OverlayServer(asyncio.get_running_loop())
        entered = asyncio.Event()
        start_site = web.TCPSite.start
        sites = []

        async def interrupted_start(site):
            sites.append(site)
            await start_site(site)
            entered.set()
            if outcome == "error":
                raise RuntimeError("synthetic failure after binding")
            await asyncio.Event().wait()

        monkeypatch.setattr(web.TCPSite, "start", interrupted_start)
        starting = asyncio.create_task(overlay.start(port))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            if outcome == "cancel":
                starting.cancel()
            with pytest.raises(RuntimeError if outcome == "error" else asyncio.CancelledError):
                await starting
            assert not overlay.running
            await assert_listener_closed(port)
            monkeypatch.setattr(web.TCPSite, "start", start_site)
            await overlay.start(port)
            assert overlay.running
        finally:
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
            await overlay.stop()
            for site in sites:
                await site._runner.cleanup()

    asyncio.run(run())


def test_cancelled_stop_finishes_cleanup_before_releasing_ownership(port):
    async def run():
        overlay = OverlayServer(asyncio.get_running_loop())
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowClient:
            async def close(self):
                entered.set()
                await release.wait()

        await overlay.start(port)
        overlay.clients[SlowClient()] = asyncio.Queue()
        stopping = asyncio.create_task(overlay.stop())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            stopping.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await stopping
            assert not overlay.running and not overlay.clients
            await assert_listener_closed(port)
        finally:
            release.set()
            await asyncio.gather(stopping, return_exceptions=True)
            await overlay.stop()

    asyncio.run(run())


def test_stop_closes_connected_websockets(port):
    async def run():
        overlay = OverlayServer(asyncio.get_running_loop())
        await overlay.start(port)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
                    closing = asyncio.create_task(ws.receive())
                    await asyncio.wait_for(overlay.stop(), 3)
                    assert (await closing).type == aiohttp.WSMsgType.CLOSE
            assert not overlay.running and not overlay.clients
            await assert_listener_closed(port)
        finally:
            await overlay.stop()

    asyncio.run(run())


def test_shutdown_cancelling_all_tasks_does_not_lose_stopping_runner(port):
    async def run():
        overlay = OverlayServer(asyncio.get_running_loop())
        await overlay.start(port)
        runner = overlay.runner
        try:
            asyncio.create_task(overlay.stop())
            await asyncio.sleep(0)
            # Match MainWindow.shutdown. This also cancels cleanup child tasks,
            # including any that have been created but have not started yet.
            tasks = asyncio.all_tasks() - {asyncio.current_task()}
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await overlay.stop()
            assert not overlay.running
            await assert_listener_closed(port)
        finally:
            await overlay.stop()
            await runner.cleanup()

    # On Windows Proactor loops, cancelling their socket-accept task can mask a
    # leaked runner. Selector loops reveal it and also cover the Linux behavior.
    loop = asyncio.SelectorEventLoop()
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()


def test_shutdown_during_failed_start_cleanup_releases_local_runner(monkeypatch, port):
    async def run():
        overlay = OverlayServer(asyncio.get_running_loop())
        entered, release = asyncio.Event(), asyncio.Event()
        start_site, cleanup_runner = web.TCPSite.start, web.AppRunner.cleanup
        runners = []

        async def failed_start(site):
            runners.append(site._runner)
            await start_site(site)
            raise RuntimeError("synthetic failure after binding")

        async def delayed_cleanup(runner):
            entered.set()
            await release.wait()
            await cleanup_runner(runner)

        monkeypatch.setattr(web.TCPSite, "start", failed_start)
        monkeypatch.setattr(web.AppRunner, "cleanup", delayed_cleanup)
        starting = asyncio.create_task(overlay.start(port))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            tasks = asyncio.all_tasks() - {asyncio.current_task()}
            for task in tasks:
                task.cancel()
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await overlay.stop()
            assert not overlay.running
            await assert_listener_closed(port)
        finally:
            release.set()
            await asyncio.gather(starting, return_exceptions=True)
            await overlay.stop()
            for runner in runners:
                await cleanup_runner(runner)

    loop = asyncio.SelectorEventLoop()
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()


def test_cancelling_one_websocket_close_still_closes_remaining_clients(monkeypatch, port):
    async def run():
        overlay = OverlayServer(asyncio.get_running_loop())
        entered, release = asyncio.Event(), asyncio.Event()
        await overlay.start(port)
        stopping = None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(f"http://127.0.0.1:{port}/ws") as first:
                    async with session.ws_connect(f"http://127.0.0.1:{port}/ws") as second:
                        server_first = next(iter(overlay.clients))
                        writer_type = type(server_first._writer)
                        writer_close = writer_type.close

                        async def delayed_close(writer, *args, **kwargs):
                            if writer is server_first._writer:
                                entered.set()
                                await release.wait()
                            return await writer_close(writer, *args, **kwargs)

                        monkeypatch.setattr(writer_type, "close", delayed_close)
                        readers = [asyncio.create_task(ws.receive()) for ws in (first, second)]
                        stopping = asyncio.create_task(overlay.stop())
                        try:
                            await asyncio.wait_for(entered.wait(), 3)
                            stopping.cancel()  # inside the first real WebSocketResponse.close
                            release.set()
                            with pytest.raises(asyncio.CancelledError):
                                await asyncio.wait_for(asyncio.shield(stopping), 2)
                            assert (await readers[1]).type == aiohttp.WSMsgType.CLOSE
                            assert not overlay.running and not overlay.clients
                        finally:
                            release.set()
                            await first.close()
                            await second.close()
                            await asyncio.gather(stopping, *readers, return_exceptions=True)
            await assert_listener_closed(port)
        finally:
            await overlay.stop()

    asyncio.run(run())
