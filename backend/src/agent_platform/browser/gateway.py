"""BrowserSkill's extension-side protocol, with durable at-most-once dispatch.

One gateway process owns the sockets. API workers only enqueue database commands.
No client-supplied browser/session identifier is used to choose a socket.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID, uuid4

import anyio
import structlog
from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import delete, select, update

from agent_platform.browser.models import (
    BrowserCommand,
    BrowserDevice,
    BrowserObservation,
    BrowserPairing,
    BrowserSession,
)
from agent_platform.browser.security import aware, enabled, now, origin, seal, unseal
from agent_platform.browser.service import (
    allows_url,
    authenticate_device,
    execution_session,
    observe_record,
    require_execution_user,
)
from agent_platform.core.config import settings

logger = structlog.get_logger()


class PeerError(Exception):
    """Only stable codes escape the gateway; page/extension messages are private."""

    def __init__(self, code: str, effect_state: str = "unknown", reason: str | None = None):
        super().__init__(code + (":" + reason if reason else ""))
        self.effect_state = effect_state



@dataclass
class Peer:
    socket: WebSocket
    device_id: UUID
    epoch: int
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    last_seen: float = field(default_factory=time.monotonic)
    work: asyncio.Task | None = None
    closed: bool = False

    async def call(self, method: str, params: dict, *, timeout: float | None = None) -> dict:
        if self.closed:
            raise PeerError("disconnected")
        rpc_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[rpc_id] = future
        try:
            await self.socket.send_json({"id": rpc_id, "method": method, "params": params})
            response = await asyncio.wait_for(asyncio.shield(future), timeout or settings.browser.rpc_timeout)
            if "error" in response:
                code = response["error"].get("code", "protocol_error")
                data = response["error"].get("data") or {}
                effect = data.get("effect_state", "unknown") if isinstance(data, dict) else "unknown"
                reason = data.get("reason") if isinstance(data, dict) else None
                reason = reason if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,80}", reason) else None
                raise PeerError(code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,60}", code) else "protocol_error",
                                effect if effect in {"none", "unknown", "committed"} else "unknown", reason)
            result = response.get("result")
            if not isinstance(result, dict):
                raise PeerError("invalid_result")
            return result
        except (asyncio.CancelledError, TimeoutError):
            if not self.closed:
                with suppress(Exception):
                    await asyncio.wait_for(self.socket.send_json({"id": uuid4().hex, "method": "cancel",
                                                                  "params": {"rpc_id": rpc_id}}), 1)
            raise
        finally:
            self.pending.pop(rpc_id, None)
            if not future.done():
                future.cancel()


class BrowserGateway:
    def __init__(self, factory):
        self.factory = factory
        self.peers: dict[UUID, Peer] = {}
        self._admission = asyncio.Lock()

    async def websocket(self, socket: WebSocket) -> None:
        peer = None
        supervisor = None
        try:
            enabled()
            protocols = socket.headers.get("sec-websocket-protocol", "").split(",")
            if len(protocols) != 1 or not protocols[0].strip().startswith("bsk-auth."):
                raise HTTPException(401, "invalid_auth")
            subprotocol = protocols[0].strip()
            token = subprotocol.removeprefix("bsk-auth.")
            if not re.fullmatch(r"chrome-extension://[a-p]{32}", socket.headers.get("origin", "")):
                raise HTTPException(403, "invalid_origin")
            async with self._admission:
                async with self.factory() as db:
                    device = await authenticate_device(db, token)
                    if device.id not in self.peers and len(self.peers) >= settings.browser.max_connections:
                        raise HTTPException(429, "capacity")
                    previous = self.peers.get(device.id)
                    if previous:
                        await self.close_peer(previous)
                    await socket.accept(subprotocol=subprotocol)
                    frame = await asyncio.wait_for(socket.receive_text(), 10)
                    if len(frame) > 16_000:
                        raise PeerError("invalid_handshake")
                    handshake = json.loads(frame)
                    params = handshake.get("params", {})
                    if not isinstance(handshake.get("id"), str) or not handshake["id"] or handshake.get("method") != "system.handshake":
                        raise PeerError("invalid_handshake")
                    # Advertise only the protocol version actually implemented/tested.
                    if params.get("protocol_version") != "1.7" or params.get("min_compatible_protocol", "1.6") not in {"1.6", "1.7"}:
                        await socket.send_json({"id": handshake["id"], "error": {
                            "code": "protocol_error", "message": "Gateway requires BrowserSkill protocol 1.7"}})
                        raise PeerError("incompatible_protocol")
                    await db.refresh(device)
                    device.connection_epoch += 1
                    device.connected = True
                    device.last_seen_at = now()
                    device.extension_version = str(params.get("version", ""))[:40]
                    device.protocol_version = "1.7"
                    await db.commit()
                    peer = Peer(socket, device.id, device.connection_epoch)
                    self.peers[device.id] = peer
                await socket.send_json({"id": handshake["id"], "result": {
                    "server": "aio-browser-gateway", "version": "0.1.0", "protocol_version": "1.7",
                    "min_compatible_protocol": "1.7", "min_compatible_peer": "0.0.0"}})
            supervisor = asyncio.create_task(self.supervise(peer))
            while not peer.closed:
                raw = await asyncio.wait_for(socket.receive_text(), settings.browser.heartbeat_timeout)
                if len(raw.encode()) > settings.browser.max_frame_bytes:
                    raise PeerError("frame_too_large")
                frame = json.loads(raw)
                if not isinstance(frame, dict):
                    raise PeerError("invalid_frame")
                peer.last_seen = time.monotonic()
                if "event" in frame:
                    await self.event(peer, frame)
                elif isinstance(frame.get("id"), str):
                    future = peer.pending.get(frame["id"])
                    if future and not future.done() and (("result" in frame) != ("error" in frame)):
                        future.set_result(frame)
                    # Unknown/old IDs and unsolicited client requests are discarded.
        except (HTTPException, PeerError, WebSocketDisconnect, TimeoutError, ValueError, TypeError):
            pass
        except Exception:
            # Never log frames, credential headers or exception strings from a peer.
            logger.error("browser_connection_failed")
        finally:
            # ASGI disconnect/shutdown cancellation must not skip durable fencing.
            with anyio.CancelScope(shield=True):
                async with asyncio.timeout(10):
                    if supervisor:
                        supervisor.cancel()
                        with suppress(asyncio.CancelledError, Exception):
                            await supervisor
                    if peer:
                        await self.close_peer(peer)
                        if self.peers.get(peer.device_id) is peer:
                            self.peers.pop(peer.device_id, None)
                        with suppress(Exception):
                            async with self.factory() as db:
                                await db.execute(update(BrowserDevice).where(
                                    BrowserDevice.id == peer.device_id, BrowserDevice.connection_epoch == peer.epoch,
                                ).values(connected=False))
                                for session in (await db.scalars(select(BrowserSession).where(
                                        BrowserSession.device_id == peer.device_id,
                                        BrowserSession.connection_epoch == peer.epoch, BrowserSession.occupied))).all():
                                    session.state = "interrupted"
                                    session.stop_requested = True
                                    session.cleanup_state = "pending"
                                await db.commit()
                    with suppress(Exception):
                        await socket.close(code=1008)
    async def close_peer(self, peer: Peer):
        peer.closed = True
        if peer.work and not peer.work.done():
            peer.work.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await peer.work
        for future in list(peer.pending.values()):
            if not future.done():
                future.set_exception(PeerError("disconnected"))
        with suppress(Exception):
            await peer.socket.close(code=1008)

    async def event(self, peer: Peer, frame: dict):
        name = frame.get("event")
        if name not in {"session.user_interrupt", "session.interaction_changed", "session.ended", "session.window_closed"}:
            return
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            return
        try:
            session_id = UUID(payload.get("session_id", ""))
        except (ValueError, TypeError, AttributeError):
            return
        async with self.factory() as db:
            session = await db.scalar(select(BrowserSession).where(
                BrowserSession.id == session_id, BrowserSession.device_id == peer.device_id,
                BrowserSession.connection_epoch == peer.epoch, BrowserSession.occupied,
            ).with_for_update())
            if session is None:
                return
            logger.info("browser_session_event", session_id=str(session.id), event=name)
            await db.execute(update(BrowserObservation).where(
                BrowserObservation.browser_session_id == session.id).values(expires_at=now()))
            if name in {"session.ended", "session.window_closed", "session.user_interrupt"}:
                session.stop_requested = True
            else:
                session.state = "waiting_user"
            await db.commit()

    async def supervise(self, peer: Peer):
        try:
            while not peer.closed:
                async with self.factory() as db:
                    enabled()
                    device = await db.get(BrowserDevice, peer.device_id)
                    if (device is None or device.revoked_at or aware(device.expires_at) <= now()
                            or device.connection_epoch != peer.epoch
                            or time.monotonic() - peer.last_seen > settings.browser.heartbeat_timeout):
                        break
                    await require_execution_user(db, device.user_id, device.tenant_id)
                    device.last_seen_at = now()
                    # Production sessions disable autoflush. Authorization below
                    # reloads this device with populate_existing=True; persist
                    # the heartbeat first so that reload cannot discard it.
                    await db.flush()
                    sessions = (await db.scalars(select(BrowserSession).where(
                        BrowserSession.device_id == peer.device_id, BrowserSession.connection_epoch == peer.epoch,
                        BrowserSession.occupied))).all()
                    cleanup_ids = []
                    for session in sessions:
                        if not session.stop_requested:
                            try:
                                await execution_session(db, session.id, session.user_id)
                            except (HTTPException, ValueError) as error:
                                logger.warning("browser_session_authorization_lost", session_id=str(session.id),
                                               status_code=error.status_code if isinstance(error, HTTPException) else None)
                                session.stop_requested = True
                        if session.stop_requested:
                            cleanup_ids.append(session.id)
                    # Approval expiry never counts as consent.
                    await db.execute(update(BrowserCommand).where(
                        BrowserCommand.browser_session_id.in_([s.id for s in sessions]),
                        BrowserCommand.status.in_(["pending", "queued"]), BrowserCommand.expires_at <= now(),
                    ).values(status="cancelled", error_code="expired", completed_at=now()))
                    await db.commit()
                if cleanup_ids:
                    if peer.work and not peer.work.done():
                        peer.work.cancel()
                        with suppress(asyncio.CancelledError):
                            await peer.work
                    for session_id in cleanup_ids:
                        await self.cleanup(peer, session_id)
                elif peer.work is None or peer.work.done():
                    if peer.work:
                        # Retrieve exceptions; failures never cause the command to be replayed.
                        with suppress(asyncio.CancelledError, Exception):
                            peer.work.result()
                    command_id = await self.claim(peer)
                    peer.work = asyncio.create_task(self.execute(peer, command_id)) if command_id else None
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("browser_gateway_authority_lost", device_id=str(peer.device_id))
        finally:
            await self.close_peer(peer)

    async def claim(self, peer: Peer) -> UUID | None:
        async with self.factory() as db:
            command = await db.scalar(select(BrowserCommand).join(
                BrowserSession, BrowserSession.id == BrowserCommand.browser_session_id,
            ).where(BrowserSession.device_id == peer.device_id, BrowserSession.connection_epoch == peer.epoch,
                    BrowserSession.occupied, BrowserCommand.status == "queued",
                    BrowserCommand.expires_at > now()).order_by(BrowserCommand.created_at, BrowserCommand.id)
                .with_for_update(skip_locked=True))
            if not command:
                return None
            try:
                await execution_session(db, command.browser_session_id, command.user_id)
            except (HTTPException, ValueError):
                command.status, command.error_code = "cancelled", "access_revoked"
                command.completed_at = now()
                await db.commit()
                return None
            command.status = "dispatching"
            command.effect_state = "unknown"
            await db.commit()  # Write before any socket side effect.
            return command.id

    async def execute(self, peer: Peer, command_id: UUID):
        from agent_platform.browser.tools import OP_TO_TOOL
        status, error_code, result = "unknown", "execution_unknown", None
        effect_state = "unknown"
        operation = "unknown"
        try:
            async with self.factory() as db:
                command = await db.get(BrowserCommand, command_id)
                session, grant, _ = await execution_session(db, command.browser_session_id, command.user_id, tool=OP_TO_TOOL.get(command.operation, "unsupported"))
                if session.device_id != peer.device_id or session.connection_epoch != peer.epoch:
                    raise PeerError("connection_changed")
                payload = unseal(command.payload_ciphertext)
                operation = command.operation
                params: dict = {"session_id": session.id.hex}
                if operation not in {"open", "navigate", "close"} and session.active_tab:
                    params["tab_id"] = session.tabs[session.active_tab]
                method = {"open": "tool.session_start", "navigate": "tool.tab_create",
                          "observe": "tool.cooperate", "screenshot": "tool.screenshot",
                          "tabs": "tool.tab_list", "close": "tool.session_stop",
                          "help": "tool.request_help", "preview": "ui.task_preview"}.get(operation)
                if operation == "open":
                    params["focused"] = False
                elif operation == "navigate":
                    if not allows_url(grant, payload["url"]):
                        raise PeerError("origin_not_granted")
                    params.update(url=payload["url"], active=True)
                elif operation == "observe":
                    if not session.active_tab:
                        raise PeerError("navigate_first")
                    params.update(action=payload.get("mode", "sync"), max_tokens=4000, timeout_ms=15000)
                elif operation == "tabs":
                    params["scope"] = "agent"
                elif operation == "help":
                    params.update(prompt=payload["reason"], timeout_ms=300000)
                elif operation == "interact":
                    observation = await db.get(BrowserObservation, UUID(payload["observation_handle"]))
                    if (not observation or observation.browser_session_id != session.id
                            or observation.connection_epoch != peer.epoch or observation.consumed_by != command.id
                            or aware(observation.expires_at) <= now()):
                        raise PeerError("stale_observation")
                    action = payload["action"]
                    method = "tool.wheel" if action == "scroll" else f"tool.{action}"
                    params.update(context_id=observation.context_id, tab_id=observation.tab_id)
                    if payload.get("ref"):
                        params["ref"] = payload["ref"]
                    if action == "fill":
                        params["value"] = payload["value"]
                    elif action == "select":
                        params["values"] = [payload["value"]]
                    elif action == "press":
                        params["key"] = payload["value"]
                    elif action == "scroll":
                        params.update(delta_x=0, delta_y=payload["delta_y"])
                if not method:
                    raise PeerError("unsupported")
                # The model never controls these keys/method strings.
            if operation in {"observe", "screenshot", "interact"}:
                tabs = await peer.call("tool.tab_list", {"session_id": session.id.hex, "scope": "agent"})
                target = next((t for t in tabs.get("tabs", []) if t.get("tab_id") == params.get("tab_id")), None)
                if not target or not allows_url(grant, target.get("url", "")):
                    raise PeerError("page_origin_not_granted")
            result = await peer.call(method, params, timeout=300 if operation == "help" else None)
            async with self.factory() as db:
                session = await db.get(BrowserSession, session.id)
                if session.connection_epoch != peer.epoch or peer.closed:
                    raise PeerError("connection_changed")
                if operation == "open":
                    session.state = "active"
                    result = {"handle": str(session.id), "message": "浏览器任务已建立，请打开本次授权的网站"}
                elif operation == "navigate":
                    if not isinstance(result.get("tab_id"), int):
                        raise PeerError("invalid_result")
                    handle = uuid4().hex
                    session.tabs = {**session.tabs, handle: result["tab_id"]}
                    session.active_tab = handle
                    result = {"tab_handle": handle, "url": payload["url"], "message": "页面已打开，请读取页面确认加载结果"}
                elif operation == "observe":
                    result = await observe_record(db, session, result)
                    session.state = "active"
                elif operation == "tabs":
                    allowed = {v: k for k, v in session.tabs.items()}
                    visible = []
                    for tab in result.get("tabs", []):
                        if tab.get("tab_id") not in allowed:
                            continue
                        entry = {"tab_handle": allowed[tab["tab_id"]]}
                        try:
                            destination = origin(tab.get("url", ""))
                        except HTTPException:
                            destination = None
                        if destination and allows_url(grant, destination):
                            entry.update(title=tab.get("title"), url=tab.get("url"))
                        else:
                            entry.update(origin=destination, needs_authorization=True)
                        visible.append(entry)
                    result = {"tabs": visible}
                elif operation == "close":
                    if result.get("return_failures"):
                        raise PeerError("cleanup_failed")
                    session.state, session.cleanup_state, session.occupied = "closed", "confirmed", False
                    result = {"closed": True}
                if operation in {"interact", "help"}:
                    result = {k: v for k, v in result.items() if k not in {"tab_id", "session_id", "window_id", "context_id"}}
                # Encrypt every body before persisting, including screenshots and page text.
                # Commit the success receipt with the session transition so a
                # waiter cannot see a closed session with an unfinished command.
                command.status, command.error_code = "succeeded", None
                command.result_ciphertext = seal(result)
                command.effect_state = "none" if operation in {"observe", "screenshot", "tabs"} else "committed"
                command.completed_at = now()
                await db.commit()
            status, error_code = "succeeded", None
            effect_state = "none" if operation in {"observe", "screenshot", "tabs"} else "committed"
        except asyncio.CancelledError:
            error_code = "cancelled_in_flight"
            raise
        except HTTPException:
            error_code = "access_revoked"
        except PeerError as error:
            error_code = str(error)[:60]
            effect_state = "none" if operation in {"observe", "screenshot", "tabs"} else error.effect_state
            status = "failed" if effect_state == "none" else "unknown"
        except TimeoutError:
            error_code = "timeout_unknown"
        except Exception:
            logger.error("browser_command_failed", command_id=str(command_id))
        finally:
            # Cancellation cannot silently drop the unknown receipt.
            async with self.factory() as db:
                command = await db.get(BrowserCommand, command_id)
                if command and command.status == "dispatching":
                    command.status, command.error_code = status, error_code
                    command.result_ciphertext = seal(result) if status == "succeeded" else None
                    command.effect_state = effect_state
                    command.completed_at = now()
                    if status == "unknown" or (error_code and error_code.startswith("user_aborted")):
                        session = await db.get(BrowserSession, command.browser_session_id)
                        session.stop_requested = True
                    await db.commit()

    async def cleanup(self, peer: Peer, session_id: UUID):
        confirmed = False
        try:
            result = await peer.call("tool.session_stop", {"session_id": session_id.hex}, timeout=10)
            confirmed = not result.get("return_failures")
        except (PeerError, TimeoutError):
            pass
        async with self.factory() as db:
            session = await db.get(BrowserSession, session_id)
            if session and session.device_id == peer.device_id and session.connection_epoch == peer.epoch:
                session.state = "closed" if confirmed else "interrupted"
                session.cleanup_state = "confirmed" if confirmed else "failed"
                session.occupied = not confirmed
                await db.execute(update(BrowserCommand).where(
                    BrowserCommand.browser_session_id == session.id,
                    BrowserCommand.status.in_(["queued", "pending"]),
                ).values(status="cancelled", error_code="stopped", completed_at=now()))
                await db.commit()
        if not confirmed:
            # Bounded cleanup: disconnect and ask the owner to verify local cleanup.
            await self.close_peer(peer)

    async def recover(self):
        async with self.factory() as db:
            await db.execute(update(BrowserDevice).values(connected=False))
            await db.execute(update(BrowserCommand).where(BrowserCommand.status == "dispatching").values(
                status="unknown", effect_state="unknown", error_code="gateway_restarted", completed_at=now()))
            await db.execute(update(BrowserCommand).where(BrowserCommand.status.in_(["pending", "queued"])).values(
                status="cancelled", error_code="gateway_restarted", completed_at=now()))
            await db.execute(update(BrowserSession).where(BrowserSession.occupied).values(
                state="interrupted", cleanup_state="pending", stop_requested=True))
            # Private payloads expire; retain only non-content receipts.
            await db.execute(update(BrowserCommand).where(BrowserCommand.created_at < now() - timedelta(days=1)).values(
                result_ciphertext=None, payload_ciphertext=seal({})))
            await db.commit()

    async def expire_content(self):
        async with self.factory() as db:
            await db.execute(update(BrowserPairing).where(
                BrowserPairing.expires_at < now(), BrowserPairing.secret_ciphertext.is_not(None),
            ).values(secret_ciphertext=None))
            await db.execute(update(BrowserDevice).where(
                BrowserDevice.retry_until < now(), BrowserDevice.previous_digest.is_not(None),
            ).values(previous_digest=None, renewal_receipt=None))
            await db.execute(delete(BrowserObservation).where(BrowserObservation.expires_at < now() - timedelta(days=1)))
            await db.execute(update(BrowserCommand).where(
                BrowserCommand.created_at < now() - timedelta(days=1),
                BrowserCommand.status.in_(["succeeded", "failed", "unknown", "cancelled", "rejected"]),
                BrowserCommand.payload_ciphertext != "",
            ).values(result_ciphertext=None, payload_ciphertext=""))
            await db.commit()

    async def shutdown(self):
        for peer in list(self.peers.values()):
            await self.close_peer(peer)
