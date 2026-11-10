# API 参考

> 由后端路由代码生成，共 224 个端点。路径为装饰器声明值，实际挂载以 `interface/api.py` 为准；完整 Schema 见运行中的 `/docs`。

## interface/routes/admin_models.py（11）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/admin/models` | `list_models` |  |
| POST | `/api/admin/models` | `create_model` |  |
| POST | `/api/admin/models/batch-create` | `batch_create_models` | Batch import models for a provider. |
| GET | `/api/admin/models/providers` | `list_providers` |  |
| POST | `/api/admin/models/providers` | `create_provider` |  |
| DELETE | `/api/admin/models/providers/{provider_id}` | `delete_provider` |  |
| PUT | `/api/admin/models/providers/{provider_id}` | `update_provider` |  |
| POST | `/api/admin/models/providers/{provider_id}/fetch-models` | `fetch_remote_models` | Fetch available models from the provider's /v1/models endpoint. |
| DELETE | `/api/admin/models/{model_id}` | `delete_model` |  |
| PUT | `/api/admin/models/{model_id}` | `update_model` |  |
| PUT | `/api/admin/models/{model_id}/default` | `set_default_model` |  |

## interface/routes/agent_api.py（9）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/agents/{agent_id}/api-doc` | `get_api_doc` | Get API documentation metadata for an agent. |
| POST | `/api/agents/{agent_id}/execute` | `execute_agent` | Synchronous one-shot execution of an agent. |
| POST | `/api/agents/{agent_id}/sessions` | `create_external_session` | Create a new session for external API calls (SSE streaming). |
| GET | `/api/agents/{agent_id}/versions` | `list_versions` | List all published versions of an agent (visible to the caller only). |
| POST | `/api/agents/{agent_id}/versions` | `publish_version` | Publish a new version of an agent (admin/owner only). |
| POST | `/api/sessions/{session_id}/chat` | `sse_chat` | SSE streaming chat for external API sessions. |
| POST | `/api/sessions/{session_id}/clear` | `clear_session` | Clear all messages in a session (keep the session). |
| GET | `/api/sessions/{session_id}/messages` | `get_session_messages` | Get conversation history for an external API session. |
| POST | `/api/sessions/{session_id}/messages/{message_id}/feedback` | `submit_message_feedback` | Submit feedback (up/down) for a message (session owner only). |

## interface/routes/agents.py（9）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/admin/agents` | `admin_list_agents` |  |
| POST | `/api/admin/agents` | `admin_create_agent` |  |
| DELETE | `/api/admin/agents/{agent_id}` | `admin_delete_agent` |  |
| PUT | `/api/admin/agents/{agent_id}` | `admin_update_agent` |  |
| GET | `/api/agents` | `list_agents` |  |
| GET | `/api/agents/{agent_id}` | `get_agent` |  |
| GET | `/api/agents/{agent_id}/children` | `get_agent_children` | Get all direct children of an agent. |
| GET | `/api/agents/{agent_id}/stats` | `get_agent_stats` | Get usage statistics for an agent (current user's sessions only). |
| GET | `/api/agents/{agent_id}/tree` | `get_agent_tree` | Get the full hierarchy tree rooted at the specified agent. |

## interface/routes/analytics.py（4）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/analytics/detail` | `get_detail` |  |
| GET | `/api/analytics/distribution` | `get_distribution` |  |
| GET | `/api/analytics/summary` | `get_summary` |  |
| GET | `/api/analytics/trend` | `get_trend` |  |

## interface/routes/channels.py（10）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/channel-bindings` | `list_my_bindings` | List the current user's channel bindings. |
| POST | `/api/channel-bindings/bind` | `bind_with_code` | Consume a bind code to link a channel identity to the current user. |
| DELETE | `/api/channel-bindings/{binding_id}` | `unbind` | Unlink an identity in the current tenant; admins may manage other users. |
| GET | `/api/channels` | `list_channels` | List channels in the current tenant for all signed-in users. |
| POST | `/api/channels` | `create_channel` | Validate, save, and start a new channel by default. |
| DELETE | `/api/channels/{channel_id}` | `delete_channel` | Delete a channel and its bindings, bind codes and session mappings. |
| PUT | `/api/channels/{channel_id}` | `update_channel` | Update channel configuration. Requires re-enable after mode/credential changes. |
| GET | `/api/channels/{channel_id}/bindings` | `list_channel_bindings` | Admins see channel bindings; other users only see their own. |
| POST | `/api/channels/{channel_id}/disable` | `disable_channel` | Disable a channel and release its connection resources. |
| POST | `/api/channels/{channel_id}/enable` | `enable_channel` | Enable a channel and start its transport. |

## interface/routes/chat.py（8）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| POST | `/api/chat` | `chat` | Non-streaming chat endpoint. |
| POST | `/api/chat/attachments` | `upload_chat_attachment` | Upload an image attachment for a future chat message. |
| POST | `/api/chat/files` | `upload_workspace_file` | Upload a file to the workspace for agent processing. |
| GET | `/api/chat/runs/{run_id}/events` | `watch_chat_run` |  |
| POST | `/api/chat/runs/{run_id}/resume` | `resume_chat_run` |  |
| POST | `/api/chat/runs/{run_id}/stop` | `stop_chat_run` |  |
| GET | `/api/chat/sessions/{session_id}/run` | `latest_chat_run` |  |
| POST | `/api/chat/stream` | `chat_stream` | Streaming chat via Server-Sent Events. |

## interface/routes/commands.py（1）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/commands` | `list_commands` | Return commands visible to the current user (including dynamic skills). |

## interface/routes/confirmations.py（2）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/confirmations/sessions/{session_id}/pending` | `get_pending_confirmations` | Get all pending confirmation requests for a session (for page refresh recovery). |
| POST | `/api/confirmations/{confirmation_id}/respond` | `respond_to_confirmation` | Submit a response to a pending confirmation request. |

## interface/routes/cron_jobs.py（8）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/cron-jobs` | `list_cron_jobs` | List the caller's cron jobs in the selected tenant. |
| POST | `/api/cron-jobs` | `create_cron_job` | Create a new cron job. |
| GET | `/api/cron-jobs/runs` | `list_all_cron_job_runs` | List the caller's run records in the selected tenant. |
| DELETE | `/api/cron-jobs/{job_id}` | `delete_cron_job` | Delete a cron job. |
| GET | `/api/cron-jobs/{job_id}` | `get_cron_job` | Get a single cron job. |
| PUT | `/api/cron-jobs/{job_id}` | `update_cron_job` | Update a cron job. |
| POST | `/api/cron-jobs/{job_id}/run` | `run_cron_job_now` | Manually trigger a single execution of a cron job. |
| GET | `/api/cron-jobs/{job_id}/runs` | `list_cron_job_runs` | List execution logs for a cron job. |

## interface/routes/daily_memories.py（3）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/memories/daily` | `list_daily_memories` | Query daily memories by exact date or date range (default: recent 30 days). |
| DELETE | `/api/memories/daily/{day}` | `delete_daily_memory` | Delete a day's record (L3 source data is untouched; can regenerate). |
| POST | `/api/memories/daily/{day}/regenerate` | `regenerate_daily_memory` | Re-run LLM consolidation for a specific day (uses its own DB session). |

## interface/routes/delegations.py（2）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/delegations/sessions/{session_id}` | `get_session_delegations` | Page through an authorized session's delegations (default 50, maximum 200). |
| GET | `/api/delegations/{delegation_id}` | `get_delegation` | Get a single delegation by ID (owner only). |

## interface/routes/knowledge.py（8）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/admin/knowledge-bases` | `list_knowledge_bases` |  |
| POST | `/api/admin/knowledge-bases` | `create_knowledge_base` |  |
| DELETE | `/api/admin/knowledge-bases/{kb_id}` | `delete_knowledge_base` |  |
| PUT | `/api/admin/knowledge-bases/{kb_id}` | `update_knowledge_base` |  |
| POST | `/api/admin/knowledge-bases/{kb_id}/retrieval` | `retrieval_knowledge_base` | Test retrieval by querying RAGFlow with a custom query. |
| POST | `/api/admin/knowledge-bases/{kb_id}/test` | `test_knowledge_base` | Test connection by querying RAGFlow with a dummy query. |
| GET | `/api/admin/settings/ragflow` | `get_ragflow_settings` |  |
| PUT | `/api/admin/settings/ragflow` | `update_ragflow_settings` |  |

## interface/routes/mcp_servers.py（7）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/admin/mcp-servers` | `list_mcp_servers` | List MCP Servers for the admin's tenant with their current status. |
| POST | `/api/admin/mcp-servers` | `create_mcp_server` | Create a new MCP Server and attempt to connect. |
| DELETE | `/api/admin/mcp-servers/{server_id}` | `delete_mcp_server` | Delete an MCP Server. |
| GET | `/api/admin/mcp-servers/{server_id}` | `get_mcp_server` | Get a specific MCP Server. |
| PUT | `/api/admin/mcp-servers/{server_id}` | `update_mcp_server` | Update an MCP Server configuration. |
| POST | `/api/admin/mcp-servers/{server_id}/refresh` | `refresh_mcp_server` | Refresh MCP Server connection and rediscover tools. |
| GET | `/api/admin/mcp-servers/{server_id}/tools` | `list_mcp_server_tools` | List tools provided by a specific MCP Server. |

## interface/routes/memories.py（14）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/memories` | `list_memories` | List memories, optionally filtered by layer. |
| POST | `/api/memories` | `create_memory` | Create a new memory. |
| POST | `/api/memories/batch-delete` | `batch_delete_memories` | Delete multiple memories at once. |
| GET | `/api/memories/changes` | `list_changes` |  |
| POST | `/api/memories/changes/{change_id}/undo` | `undo_change` |  |
| POST | `/api/memories/organize/preview` | `preview_organization` |  |
| POST | `/api/memories/organize/{plan_id}/apply` | `apply_organization` |  |
| GET | `/api/memories/search` | `search_memories` | Search memories by similarity. |
| GET | `/api/memories/stats` | `memory_stats` | Return memory counts per layer. |
| DELETE | `/api/memories/{memory_id}` | `delete_memory` | Delete a memory. |
| GET | `/api/memories/{memory_id}` | `get_memory` | Get a single memory by ID. |
| PUT | `/api/memories/{memory_id}` | `update_memory` | Update an existing memory. |
| GET | `/api/memories/{memory_id}/versions` | `memory_versions` |  |
| POST | `/api/memories/{memory_id}/versions/{version}/restore` | `restore_memory_version` |  |

## interface/routes/models.py（1）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/models` | `list_models` |  |

## interface/routes/observability.py（7）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/observability/overview` | `get_overview` |  |
| GET | `/api/observability/quality` | `get_quality` |  |
| GET | `/api/observability/stats` | `get_stats` |  |
| GET | `/api/observability/tool-ranking` | `get_tool_ranking` |  |
| GET | `/api/observability/tools/{tool}/trend` | `get_tool_trend` |  |
| GET | `/api/observability/traces` | `list_traces` |  |
| GET | `/api/observability/traces/{trace_id}` | `get_trace` |  |

## interface/routes/pets.py（25）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| PUT | `/api/admin/pet-packages/{package_id}/takedown` | `admin_takedown` |  |
| PUT | `/api/admin/pet-packages/{package_id}/visibility` | `admin_set_visibility` |  |
| GET | `/api/pets/active` | `get_active_pet` |  |
| GET | `/api/pets/active-tasks` | `list_active_tasks` | 当前用户的在跑任务（渠道触发，如飞书）。调试/回退用，前端已改走 SSE。 |
| POST | `/api/pets/deactivate` | `deactivate_pet` |  |
| GET | `/api/pets/market` | `list_market` |  |
| GET | `/api/pets/mine` | `list_my_pets` |  |
| POST | `/api/pets/packages` | `upload_package` | 上传宠物包 zip（Codex 格式）。row_mapping 为 JSON 字符串，如 {"idle":0,"work":1}。 |
| GET | `/api/pets/packages/mine` | `list_my_packages` |  |
| DELETE | `/api/pets/packages/{package_id}` | `delete_package` |  |
| PUT | `/api/pets/packages/{package_id}/actions` | `set_package_actions` | 上传者改包级动作名（仅创建人/管理员）。 |
| PUT | `/api/pets/packages/{package_id}/default-agent` | `set_default_agent` | 包级默认人设 Agent（仅创建人/管理员）。agent_id 为 null 表示清除。 |
| GET | `/api/pets/packages/{package_id}/download` | `download_package` | 导出原始 zip（与上传时逐字节一致，可放回 ~/.codex/pets 使用）。 |
| PUT | `/api/pets/packages/{package_id}/row-mapping` | `set_row_mapping` |  |
| GET | `/api/pets/packages/{package_id}/spritesheet` | `get_spritesheet` | 精灵图（经后端转发以执行可见性判定，不可见返回 404）。 |
| PUT | `/api/pets/packages/{package_id}/visibility` | `set_visibility` |  |
| GET | `/api/pets/tasks/events` | `task_events_stream` | 用户级 SSE：渠道任务生命周期实时推送（连接即快照，再收增量 + 心跳）。 |
| POST | `/api/pets/{package_id}/adopt` | `adopt_pet` |  |
| DELETE | `/api/pets/{user_pet_id}` | `remove_pet` |  |
| PUT | `/api/pets/{user_pet_id}/actions` | `set_pet_actions` | 领养者改实例级动作名覆盖 + 状态映射覆盖。aliases 空串 value 删除该条覆盖；传 {} 恢复包级。 |
| POST | `/api/pets/{user_pet_id}/activate` | `activate_pet` |  |
| PUT | `/api/pets/{user_pet_id}/agent` | `bind_agent` | 实例级绑定/解绑智能体。agent_id 为 null 表示解绑（回退包级默认）。 |
| POST | `/api/pets/{user_pet_id}/bubble` | `pet_bubble` | 智能气泡：绑定 Agent 时流式返回 pet_action + text_delta 事件；未绑定/失败/超限回退 JSON。 |
| POST | `/api/pets/{user_pet_id}/chat` | `pet_chat` | 开启/复用宠物闲聊会话（source='pet'）。之后走现有会话消息接口 + SSE。 |
| POST | `/api/pets/{user_pet_id}/interact` | `interact_pet` |  |

## interface/routes/portal.py（2）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/portal/agents` | `list_portal_agents` |  |
| GET | `/api/portal/agents/{agent_id}` | `get_portal_agent` |  |

## interface/routes/public.py（1）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/public/images/{key:path}` | `get_public_image` | Serve a chat attachment image to its owner. |

## interface/routes/remote_tools.py（7）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/admin/remote-tools` | `list_remote_tools` | List all remote tools. |
| POST | `/api/admin/remote-tools` | `create_remote_tool` | Create a new remote tool. |
| DELETE | `/api/admin/remote-tools/{tool_id}` | `delete_remote_tool` | Delete a remote tool. |
| GET | `/api/admin/remote-tools/{tool_id}` | `get_remote_tool` | Get a specific remote tool. |
| PUT | `/api/admin/remote-tools/{tool_id}` | `update_remote_tool` | Update a remote tool configuration. |
| POST | `/api/admin/remote-tools/{tool_id}/test` | `test_remote_tool` | Test a remote tool by sending an actual HTTP request. |
| PATCH | `/api/admin/remote-tools/{tool_id}/toggle` | `toggle_remote_tool` | Toggle a remote tool's active status. |

## interface/routes/rooms.py（11）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/rooms` | `list_rooms` |  |
| POST | `/api/rooms` | `create_room` |  |
| DELETE | `/api/rooms/{room_id}` | `delete_room` |  |
| GET | `/api/rooms/{room_id}` | `get_room` |  |
| PATCH | `/api/rooms/{room_id}` | `update_room` |  |
| GET | `/api/rooms/{room_id}/events` | `events` |  |
| GET | `/api/rooms/{room_id}/messages/{message_id}` | `get_message` |  |
| POST | `/api/rooms/{room_id}/runs` | `send_message` |  |
| POST | `/api/rooms/{room_id}/runs/{run_id}/stop` | `stop_run` |  |
| POST | `/api/rooms/{room_id}/tasks/{task_id}/respond` | `respond` |  |
| POST | `/api/rooms/{room_id}/tasks/{task_id}/retry` | `retry_member` |  |

## interface/routes/sessions.py（7）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/sessions` | `list_sessions` | List all sessions for the current user, optionally filtered by agent. |
| POST | `/api/sessions` | `create_session` | Create a new conversation session, auto-bound to user's default workspace. |
| DELETE | `/api/sessions/{session_id}` | `delete_session` | Delete a session and all its messages. |
| GET | `/api/sessions/{session_id}` | `get_session` | Get a session with its message history. |
| PATCH | `/api/sessions/{session_id}` | `update_session` | Update a session (rename title and/or rebind agent). |
| GET | `/api/sessions/{session_id}/events` | `session_events_stream` | SSE 回放渠道任务在跑期间的完整 Agent 事件（连接即回放 + 实时尾随）。 |
| GET | `/api/sessions/{session_id}/status` | `get_session_status` | 查询会话当前是否有 AgentLoop / 渠道任务在跑。 |

## interface/routes/settings.py（15）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| PUT | `/api/settings/active-tenant` | `switch_active_tenant` |  |
| GET | `/api/settings/memory` | `get_memory_config` |  |
| PUT | `/api/settings/memory` | `update_memory_config` |  |
| PUT | `/api/settings/password` | `update_password` |  |
| GET | `/api/settings/personal-portrait` | `get_personal_portrait` |  |
| PUT | `/api/settings/personal-portrait` | `update_personal_portrait` |  |
| GET | `/api/settings/personal-portrait/versions` | `list_portrait_versions` |  |
| DELETE | `/api/settings/personal-portrait/versions/{version_id}` | `delete_portrait_version` |  |
| GET | `/api/settings/personal-portrait/versions/{version_id}` | `get_portrait_version` |  |
| POST | `/api/settings/personal-portrait/versions/{version_id}/restore` | `restore_portrait_version` | Restore the portrait to a historical version (saves current as snapshot first). |
| GET | `/api/settings/profile` | `get_profile` |  |
| PUT | `/api/settings/profile` | `update_profile` |  |
| GET | `/api/settings/security` | `get_security_config` |  |
| PUT | `/api/settings/security` | `update_security_config` |  |
| GET | `/api/settings/tenants` | `list_my_tenants` |  |

## interface/routes/skills.py（18）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/skills` | `list_skills` | List skills with optional filters. |
| POST | `/api/skills` | `create_skill` | Create a new skill. |
| POST | `/api/skills/import` | `import_skill` | Import a skill from a zip file. |
| GET | `/api/skills/search` | `search_skills` | Search skills by similarity. |
| POST | `/api/skills/sh/import` | `skills_sh_import` | Import skills from skills.sh by fetching their GitHub folders. |
| GET | `/api/skills/sh/resolve` | `skills_sh_resolve` | Resolve a skills.sh URL (or source+skill_id) into a skill preview. |
| GET | `/api/skills/sh/search` | `skills_sh_search` | Search the skills.sh skill marketplace. |
| DELETE | `/api/skills/{skill_id}` | `delete_skill` | Delete a skill and all its MinIO zips. |
| GET | `/api/skills/{skill_id}` | `get_skill` | Get a single skill by ID. |
| PUT | `/api/skills/{skill_id}` | `update_skill` | Update a skill. Automatically archives current version. |
| GET | `/api/skills/{skill_id}/download` | `download_skill` | Download the current version's zip file. |
| GET | `/api/skills/{skill_id}/files` | `list_skill_files` | List all files in a skill. |
| POST | `/api/skills/{skill_id}/files` | `upload_skill_files` | Upload files to an existing skill. |
| DELETE | `/api/skills/{skill_id}/files/{file_path:path}` | `delete_skill_file` | Remove a specific file from a skill. |
| GET | `/api/skills/{skill_id}/files/{file_path:path}/download` | `download_skill_file` | Download a specific file from a skill's zip. |
| GET | `/api/skills/{skill_id}/versions` | `list_versions` | List version history for a skill. |
| GET | `/api/skills/{skill_id}/versions/{version}` | `get_version` | Get a specific version's content. |
| GET | `/api/skills/{skill_id}/versions/{version}/download` | `download_skill_version` |  |

## interface/routes/system_config.py（2）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/admin/system-config/auto-title` | `get_auto_title_config` |  |
| PUT | `/api/admin/system-config/auto-title` | `update_auto_title_config` |  |

## interface/routes/tenants.py（7）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/admin/tenants` | `list_tenants` |  |
| POST | `/api/admin/tenants` | `create_tenant` |  |
| DELETE | `/api/admin/tenants/{tenant_id}` | `delete_tenant` |  |
| PUT | `/api/admin/tenants/{tenant_id}` | `update_tenant` |  |
| GET | `/api/admin/tenants/{tenant_id}/users` | `list_tenant_users` |  |
| PUT | `/api/admin/tenants/{tenant_id}/users` | `assign_tenant_users` |  |
| DELETE | `/api/admin/tenants/{tenant_id}/users/{user_id}` | `remove_tenant_user` |  |

## interface/routes/tools.py（1）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/tools` | `list_tools` | List all registered tools available to agents (built-in + MCP). |

## interface/routes/ui_actions.py（3）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| POST | `/api/ui-actions/sessions/{session_id}/cancel_all` | `cancel_all_ui_actions` | Cancel all pending UI actions AND pending confirmations for a session. |
| GET | `/api/ui-actions/sessions/{session_id}/pending` | `get_pending_ui_actions` | List pending UI actions for a session. |
| POST | `/api/ui-actions/{action_id}/respond` | `respond_to_ui_action` | Submit the execution result of a pending frontend UI action. |

## interface/routes/users.py（3）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/admin/users` | `list_users` |  |
| POST | `/api/admin/users` | `create_user` |  |
| PUT | `/api/admin/users/{user_id}` | `update_user` |  |

## interface/routes/web_tools.py（2）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/admin/settings/web` | `get_web_tool_config` | Return the effective (DB-over-env) web tool config. Secrets are never returned. |
| PUT | `/api/admin/settings/web` | `update_web_tool_config` | Update web tool config. Omitted fields keep current values. |

## interface/routes/webpages.py（2）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/webpages/{page_id}` | `get_webpage` | Serve a generated webpage inline (text/html), authorized by HMAC token. |
| POST | `/api/webpages/{page_id}/access` | `mint_webpage_access` | Mint a fresh access URL for a page owned by the current user. |

## auth/routes.py（4）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| POST | `/api/auth/login` | `login` |  |
| POST | `/api/auth/logout` | `logout` | Revoke a refresh token. |
| POST | `/api/auth/refresh` | `refresh` |  |
| POST | `/api/auth/register` | `register` |  |

## browser/routes.py（10）

| 方法 | 路径 | 函数 | 说明 |
|---|---|---|---|
| GET | `/api/local-browser/chats/{session_id}` | `state` |  |
| POST | `/api/local-browser/commands/{command_id}/approval` | `approve` |  |
| GET | `/api/local-browser/devices` | `devices` |  |
| DELETE | `/api/local-browser/devices/{device_id}` | `revoke` |  |
| PUT | `/api/local-browser/devices/{device_id}/persistent-authorization` | `persistent_authorization` |  |
| POST | `/api/local-browser/grants` | `grant` |  |
| DELETE | `/api/local-browser/grants/{grant_id}` | `revoke_grant` |  |
| POST | `/api/local-browser/pairings` | `pair` |  |
| POST | `/api/local-browser/sessions/{session_id}/confirm-cleanup` | `confirm_cleanup` |  |
| POST | `/api/local-browser/sessions/{session_id}/stop` | `stop` |  |
