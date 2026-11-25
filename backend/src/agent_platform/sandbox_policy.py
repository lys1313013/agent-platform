"""Deployment-owned sandbox specification, also used by the credential-free broker."""

import hashlib

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SandboxSettings(BaseSettings):
    """Sandbox execution configuration."""

    model_config = SettingsConfigDict(env_prefix="SANDBOX_")

    image: str = Field(
        default="crpi-yvha9coypdedaafa.cn-beijing.personal.cr.aliyuncs.com/lys1313013/agent-platform-sandbox:latest"
    )
    namespace: str = Field(default="agent-platform", min_length=1)
    lock_dir: str = Field(default="/tmp/aio-sandbox-locks", min_length=1)
    cpu_limit: float = Field(default=1.0, ge=0.1, le=32)
    memory_limit: str = Field(default="512m")
    tmpfs_size: str = Field(default="512m")
    network_disabled: bool = Field(default=True, description="Disable outbound network from sandbox")
    command_timeout: int = Field(default=60, ge=5, le=600)
    session_ttl: int = Field(default=3600, ge=60, le=604800, description="Idle timeout in seconds (up to 7 days)")
    workspace_quota_mb: int = Field(default=500, ge=10, le=10240)
    max_concurrent: int = Field(default=10, ge=1, le=100)


def container_name(namespace: str, user_id: str) -> str:
    digest = hashlib.sha256(f"{namespace}:{user_id}".encode()).hexdigest()[:32]
    return f"aio-sandbox-{digest}"


def container_options(config: SandboxSettings, labels: dict[str, str]) -> dict:
    return {
        "name": container_name(config.namespace, labels["aio.user_id"]),
        "user": "sandbox",
        "read_only": True,
        "init": True,
        "mounts": [],
        "tmpfs": {
            "/tmp": f"size={config.tmpfs_size},uid=1000,gid=1000",
            "/workspace": f"size={config.workspace_quota_mb}m,uid=1000,gid=1000",
        },
        "mem_limit": config.memory_limit,
        "cpu_quota": int(config.cpu_limit * 100_000),
        "network_disabled": config.network_disabled,
        "security_opt": ["no-new-privileges"],
        "cap_drop": ["ALL"],
        "labels": labels,
    }
