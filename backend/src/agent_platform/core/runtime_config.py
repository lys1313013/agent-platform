"""Execution settings shared by live agents and immutable published snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


class AgentRuntimeSource(Protocol):
    """Runtime fields exposed by both stored agents and dynamic sub-agents."""

    @property
    def model_id(self) -> UUID | None: ...

    @property
    def temperature(self) -> float | None: ...

    @property
    def max_iterations(self) -> int | None: ...

    @property
    def enable_retry(self) -> bool | None: ...


@dataclass(frozen=True)
class PublishedBindings:
    """Resource identifiers fixed at publication. Visibility is checked at execution."""

    skill_ids: tuple[UUID, ...]
    child_ids: tuple[UUID, ...]
    knowledge_base_ids: tuple[UUID, ...]
    graph_knowledge_base_ids: tuple[UUID, ...] | None

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> PublishedBindings:
        def ids(key: str) -> tuple[UUID, ...]:
            return tuple(UUID(str(value)) for value in snapshot.get(key, []))
        # Versions published before graph bindings were recorded used the live graph relation.
        graphs = ids("graph_knowledge_base_ids") if "graph_knowledge_base_ids" in snapshot else None
        return cls(ids("skill_ids"), ids("child_ids"), ids("knowledge_base_ids"), graphs)


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """Model and loop settings; tenant authorization belongs to the execution entry."""

    model_id: UUID | None = None
    temperature: float | None = None
    max_iterations: int | None = None
    enable_retry: bool = True

    @classmethod
    def from_agent(cls, agent: AgentRuntimeSource) -> AgentRuntimeConfig:
        return cls(
            model_id=agent.model_id,
            temperature=agent.temperature,
            max_iterations=agent.max_iterations,
            enable_retry=agent.enable_retry if agent.enable_retry is not None else True,
        )

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> AgentRuntimeConfig:
        model_id = snapshot.get("model_id")
        try:
            model_id = UUID(str(model_id)) if model_id else None
        except (ValueError, AttributeError):
            # Legacy published versions already fell back to the tenant's model
            # when a model reference was malformed. Never resolve another tenant.
            model_id = None
        retry = snapshot.get("enable_retry")
        return cls(
            model_id=model_id,
            temperature=snapshot.get("temperature"),
            max_iterations=snapshot.get("max_iterations"),
            # Older snapshots did not store this field and always enabled retry.
            enable_retry=retry if retry is not None else True,
        )

    def to_snapshot(self) -> dict:
        return {
            "model_id": str(self.model_id) if self.model_id else None,
            "temperature": self.temperature,
            "max_iterations": self.max_iterations,
            "enable_retry": self.enable_retry,
        }
