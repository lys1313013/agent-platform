"""Dedicated single-owner BrowserSkill gateway (run with one Uvicorn worker)."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from agent_platform.browser.gateway import BrowserGateway
from agent_platform.browser.security import enabled
from agent_platform.browser.service import exchange
from agent_platform.db.connection import get_engine, get_session_factory

LOCK_ID = 1_091_575_365


@asynccontextmanager
async def lifespan(app: FastAPI):
    enabled()
    engine = get_engine()
    async with engine.connect() as conn:
        if not await conn.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": LOCK_ID}):
            raise RuntimeError("Browser Gateway 已有实例运行；只允许一个连接所有者")
        await conn.commit()
        gateway = BrowserGateway(get_session_factory())
        app.state.gateway = gateway
        app.state.ready = True
        await gateway.recover()

        async def monitor():
            try:
                while True:
                    await asyncio.sleep(5)
                    # Do not reconnect a lost lock connection and continue serving.
                    await conn.execute(text("SELECT 1"))
                    await conn.commit()
                    await gateway.expire_content()
            except asyncio.CancelledError:
                raise
            except Exception:
                app.state.ready = False
                await gateway.shutdown()
        task = asyncio.create_task(monitor())
        try:
            yield
        finally:
            app.state.ready = False
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await gateway.shutdown()
            with suppress(Exception):
                await conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": LOCK_ID})
                await conn.commit()


class ExchangeRequest(BaseModel):
    action: Literal["pair", "renew"]
    next_token: str = Field(min_length=43, max_length=43)
    label: str = Field(default="", max_length=100)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origin_regex=r"chrome-extension://[a-p]{32}",
                   allow_methods=["POST"], allow_headers=["Authorization", "Content-Type"])


@app.post("/extension/authorize")
async def authorize(body: ExchangeRequest, request: Request):
    if not getattr(app.state, "ready", False):
        raise HTTPException(503, "Gateway unavailable")
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(401, "Device authorization required")
    try:
        async with app.state.gateway.factory() as db:
            receipt = await exchange(db, header[7:], body.action, body.next_token, body.label)
            await db.commit()
        return JSONResponse(receipt, headers={"Cache-Control": "no-store"})
    except IntegrityError:
        raise HTTPException(409, "设备凭证冲突，请重新配对") from None


@app.websocket("/extension")
async def extension(socket: WebSocket):
    if not getattr(app.state, "ready", False):
        await socket.close(code=1013)
        return
    await app.state.gateway.websocket(socket)


@app.get("/health")
async def health():
    return JSONResponse({"ready": getattr(app.state, "ready", False)},
                        status_code=200 if getattr(app.state, "ready", False) else 503)
