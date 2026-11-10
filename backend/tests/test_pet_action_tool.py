"""Exercise pet actions through the real tool dispatcher, including its context kwargs."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from aio_agent_platform.db.models import PetPackage, Session, UserPet
from aio_agent_platform.pets.smart import ensure_pet_tools_registered
from aio_agent_platform.tools.executor import ToolExecutor
from aio_agent_platform.tools.registry import ToolRegistry


@pytest.fixture
def pet_tool(monkeypatch):
    user_id, session_id, pet_id, package_id = (uuid4() for _ in range(4))
    records = {
        (Session, session_id): Session(id=session_id, user_id=user_id, source="pet", pet_id=pet_id),
        (UserPet, pet_id): UserPet(id=pet_id, user_id=user_id, package_id=package_id),
        (PetPackage, package_id): PetPackage(
            id=package_id, row_count=2, row_mapping={"idle": 0, "sad": 1},
            actions={"0": {"name": "待机"}, "1": {"name": "沮丧"}},
        ),
    }
    db = AsyncMock()
    db.get.side_effect = lambda model, key: records.get((model, key))
    db.__aenter__.return_value = db
    factory = Mock(return_value=db)
    monkeypatch.setattr("aio_agent_platform.pets.smart.get_session_factory", lambda: factory)
    executor = ToolExecutor(ToolRegistry(), SimpleNamespace())
    ensure_pet_tools_registered(executor)
    return executor, str(user_id), str(session_id), factory


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", [None, {"pet_action"}])
async def test_pet_action_dispatch_emits_animation(pet_tool, grant):
    executor, user_id, session_id, _ = pet_tool
    queue = asyncio.Queue()

    result = await executor.execute(
        "pet_action", {"name": "沮丧"}, "call", user_id, session_id,
        event_queue=queue, allowed_tools=grant,
    )

    assert result.success, result.error
    assert result.output == "已让宠物表演「沮丧」"
    assert queue.get_nowait() == {"type": "pet_action", "name": "沮丧", "row": 1}
    assert queue.empty()


@pytest.mark.asyncio
async def test_pet_action_without_grant_is_denied_before_handler(pet_tool):
    executor, user_id, session_id, factory = pet_tool
    queue = asyncio.Queue()

    result = await executor.execute(
        "pet_action", {"name": "沮丧"}, "call", user_id, session_id,
        event_queue=queue, allowed_tools=set(),
    )

    assert not result.success
    assert "Permission denied" in result.error
    factory.assert_not_called()
    assert queue.empty()
