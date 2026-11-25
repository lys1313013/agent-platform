"""Tool / MCP / skills / system status commands."""

from __future__ import annotations

from sqlalchemy import func, select

from agent_platform.channels.connection_manager import get_global_channel_manager
from agent_platform.core.chat import filter_tools_by_agent, load_agent, refresh_mcp_tools_for_agent
from agent_platform.cron_jobs.scheduler import get_global_scheduler
from agent_platform.db.models import CronJob, LLMModel, MCPServer, Skill
from agent_platform.interface.routes.mcp_servers import _build_config
from agent_platform.skills.sh_client import clear_repo_cache

from ..models import CommandContext, CommandResult
from ..registry import command


@command("tools", group="工具", desc="列出当前 Agent 可用工具（内置 / MCP / 远程）")
async def cmd_tools(ctx: CommandContext) -> CommandResult:
    if ctx.tool_executor is None:
        return CommandResult(content="工具执行器不可用。")

    agent = None
    mcp_schema = []
    if ctx.session is not None and ctx.session.agent_id:
        agent = await load_agent(ctx.db, ctx.session.agent_id, user=ctx.user, tenant_id=ctx.tenant_id)
        if agent is None:
            return CommandResult(content="当前会话绑定的智能体不可用。")

    if agent is not None:
        await refresh_mcp_tools_for_agent(ctx.tool_executor, agent)
        tools_list, schema = filter_tools_by_agent(ctx.tool_executor, agent)
        registered = {tool.name for tool in tools_list}
        mcp_schema = [entry["function"] for entry in schema if entry["function"]["name"] not in registered]
    else:
        tools_list = ctx.tool_executor.visible_tools(ctx.tenant_id)

    if not tools_list and not mcp_schema:
        return CommandResult(content="暂无可用工具。")

    lines = [f"**可用工具（{len(tools_list) + len(mcp_schema)}）：**", ""]
    for t in tools_list:
        kind = " [direct]" if getattr(t, "direct", False) else ""
        level = getattr(t, "permission_level", None) or "read"
        lines.append(f"- `{t.name}` — {t.description or '无描述'}（{level}）{kind}")
    for tool in mcp_schema:
        lines.append(f"- `{tool['name']}` — {tool['description'] or '无描述'}（dangerous） [MCP]")
    if agent is not None:
        lines.append("")
        lines.append(f"范围：智能体「{agent.name}」生效工具。")
    else:
        lines.append("")
        lines.append("当前会话未绑定智能体，展示当前租户可用的注册工具。")
    return CommandResult(content="\n".join(lines))


@command("mcp", group="工具", desc="查看 MCP 服务器连接状态", permission="admin")
async def cmd_mcp(ctx: CommandContext) -> CommandResult:
    mcp = ctx.tool_executor.mcp_manager if ctx.tool_executor else None
    if mcp is None:
        return CommandResult(content="MCP 管理器未初始化。")
    status = mcp.get_status(ctx.tenant_id)
    if not status:
        return CommandResult(content="未配置 MCP 服务器。")
    lines = ["**MCP 服务器状态：**", ""]
    for server_id, info in status.items():
        conn = "✅ 已连接" if info.get("connected") else "❌ 未连接"
        lines.append(
            f"- `{info.get('name', server_id)}` — {conn} · "
            f"传输 {info.get('transport', '?')} · 工具 {info.get('tools_count', 0)}"
        )
    return CommandResult(content="\n".join(lines))


@command("reload-mcp", group="工具", desc="热重载所有已配置的 MCP 服务器", permission="admin")
async def cmd_reload_mcp(ctx: CommandContext) -> CommandResult:
    if ctx.tool_executor is None or ctx.tool_executor.mcp_manager is None:
        return CommandResult(content="MCP 管理器未初始化。")
    servers = (
        await ctx.db.execute(select(MCPServer).where(MCPServer.is_active, MCPServer.tenant_id == ctx.tenant_id))
    ).scalars().all()
    if not servers:
        return CommandResult(content="没有已启用的 MCP 服务器。")
    lines = ["**MCP 重载结果：**", ""]
    ok = 0
    for server in servers:
        try:
            config = _build_config(server)
            await ctx.tool_executor.mcp_manager.add_server(server.id, config)
            ok += 1
            lines.append(f"- ✅ `{server.name}` 已重连")
        except Exception as exc:
            lines.append(f"- ❌ `{server.name}` 失败：{exc}")
    lines.append("")
    lines.append(f"共 {len(servers)} 个服务器，成功 {ok} 个。")
    return CommandResult(content="\n".join(lines))


@command("reload-skills", group="工具", desc="清除技能仓库元数据缓存", permission="admin")
async def cmd_reload_skills(ctx: CommandContext) -> CommandResult:
    clear_repo_cache()
    count = await ctx.db.scalar(select(func.count(Skill.id)).where(Skill.is_active))
    return CommandResult(content=f"✅ 已清除技能仓库缓存。当前启用技能 {count or 0} 个。")


@command("status", group="系统", desc="系统状态：在线模型、MCP、渠道、队列概况", permission="admin")
async def cmd_status(ctx: CommandContext) -> CommandResult:
    lines = ["**系统状态：**", ""]

    models = (
        await ctx.db.execute(
            select(LLMModel).where(LLMModel.is_active, LLMModel.tenant_id == ctx.tenant_id).order_by(LLMModel.is_default.desc())
        )
    ).scalars().all()
    lines.append(f"- **在线模型**：{len(models)} 个")
    for m in models[:5]:
        default = "（默认）" if m.is_default else ""
        lines.append(f"  - `{m.name}`{default}")
    if len(models) > 5:
        lines.append(f"  - …等共 {len(models)} 个")

    mcp = ctx.tool_executor.mcp_manager if ctx.tool_executor else None
    mcp_status = mcp.get_status(ctx.tenant_id) if mcp else {}
    connected = sum(1 for i in mcp_status.values() if i.get("connected"))
    lines.append(f"- **MCP**：{connected}/{len(mcp_status)} 已连接")

    active_job_ids = (await ctx.db.scalars(
        select(CronJob.id).where(CronJob.tenant_id == ctx.tenant_id, CronJob.is_active)
    )).all()
    scheduler = get_global_scheduler()
    scheduled = scheduler.scheduled_count(active_job_ids) if scheduler else 0
    lines.append(f"- **调度器**：{scheduled} 个定时任务")
    lines.append(f"  - 活动中 cron：{len(active_job_ids)}")

    sandbox = ctx.tool_executor.sandbox_mgr if ctx.tool_executor else None
    active = sandbox is not None and sandbox.get_active_for_user(ctx.user_id) is not None
    lines.append(f"- **我的沙箱**：{'活跃' if active else '未运行'}")

    channel_mgr = get_global_channel_manager()
    channels = channel_mgr.get_status(ctx.tenant_id) if channel_mgr else []
    lines.append(f"- **渠道连接**：{len(channels)} 个")
    for ch in channels:
        lines.append(
            f"  - `{ch.get('name') or ch.get('channel_id')}` — {ch.get('channel_type')} · {ch.get('transport_state')}"
        )
    return CommandResult(content="\n".join(lines))
