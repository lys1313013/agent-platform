"""Shared, fail-closed tool approval policy (independent of the transport)."""


def requires_approval(executor, name: str, trust_level: str) -> bool:
    if name == "AskUserQuestion":
        return False
    tool = executor.registry.get(name)
    if tool is not None and tool.execution_location == "local_browser":
        # The browser handler validates the owner's durable/one-run capability
        # and performs its own command-bound approvals before dispatch.
        return False
    if trust_level == "auto_all":
        return False
    if trust_level != "ask_dangerous":
        return True
    # External services may perform writes; unknown capabilities are dangerous.
    if executor.mcp_manager and executor.mcp_manager.is_mcp_tool(name):
        return True
    if executor.remote_manager and executor.remote_manager.is_remote_tool(name):
        config = executor.remote_manager.get_config(name)
        return config is None or config.method.upper() not in {"GET", "HEAD", "OPTIONS"}
    if tool is not None:
        return tool.permission_level not in {"read", "write"}
    return executor.direct_handler_permissions.get(name, "dangerous") not in {"read", "write"}
