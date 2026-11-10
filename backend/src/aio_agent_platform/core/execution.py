"""Transport-independent preparation for live and published chat execution."""
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aio_agent_platform.core import chat
from aio_agent_platform.core.agent import AgentLoop
from aio_agent_platform.core.execution_scope import bind_execution_scope
from aio_agent_platform.core.runtime_config import AgentRuntimeConfig
from aio_agent_platform.db.models import Agent, LLMModel, Session
from aio_agent_platform.llm import LLMMessage
from aio_agent_platform.tools.executor import ToolExecutor
from aio_agent_platform.tools.registry import Tool


@dataclass(frozen=True)
class PreparedExecution:
    user_id: UUID | None
    tenant_id: UUID
    config: AgentRuntimeConfig
    model: LLMModel

    @property
    def provider_type(self) -> str:
        return chat.resolve_provider_type(self.model.provider.provider_type)

    @property
    def allow_images(self) -> bool:
        return bool(self.model.is_multimodal)

    async def build_loop(self, executor: ToolExecutor, prompt: str, db: AsyncSession,
                         **capabilities: Any) -> AgentLoop:
        return await chat.build_agent_loop(
            executor, prompt, db, self.tenant_id,
            runtime_config=self.config, user_id=self.user_id,
            resolved_model=self.model, **capabilities,
        )

    async def load_history(self, db: AsyncSession, session_id: UUID,
                           **options: Any) -> tuple[list[LLMMessage], str | None]:
        return await chat.load_conversation_history(
            db, session_id, provider_type=self.provider_type,
            allow_images=self.allow_images, **options,
        )

    async def build_prompt(self, db: AsyncSession, message: str, tools: list[Tool], *,
                           agent: Agent | None = None, workspace_files: list[dict] | None = None,
                           prompt_config: dict | None = None) -> str:
        if self.user_id is None:
            raise ValueError("Prompt assembly requires an execution user")
        with bind_execution_scope(self.user_id, self.tenant_id, agent_id=agent.id if agent else None):
            return await chat.build_system_prompt_with_memories(
                db, self.user_id, message, tools, agent=agent,
                workspace_files=workspace_files, tenant_id=self.tenant_id, prompt_config=prompt_config,
            )


class ChatExecutionService:
    @staticmethod
    async def prepare(db: AsyncSession, *, tenant_id: UUID, user_id: UUID | None,
                      agent: Agent | None = None, session: Session | None = None,
                      config: AgentRuntimeConfig | None = None) -> PreparedExecution:
        """Freeze one model choice for formatting history and invoking the provider.

        Entrypoints authorize their object first. Published configurations stay
        immutable; only live sessions may override their agent's model choice.
        """
        if config is None:
            config = AgentRuntimeConfig.from_agent(agent) if agent else AgentRuntimeConfig()
            if session is not None and session.model_id:
                config = replace(config, model_id=session.model_id)
        model = await chat.resolve_model(db, tenant_id=tenant_id, model_id=config.model_id)
        return PreparedExecution(user_id, tenant_id, config, model)
