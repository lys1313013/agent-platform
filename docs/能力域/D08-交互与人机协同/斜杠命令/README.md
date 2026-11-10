# 斜杠命令

> 合并说明：本文由 2 份原始文档合并，内容保持原样，仅调整标题层级。

## 斜杠命令功能清单

> 参考来源：OpenClaw 斜杠命令、Hermes Agent 命令体系、飞书生态机器人命令实践。状态图例：✅ 已实现 · ⏳ 待实现。实现方案见 `02-设计.md`。

### 通用约定

- 消息以 `/` 开头即视为命令，服务端拦截解析，**不进入 Agent 对话**；未知命令 Web 端返回提示、飞书降级为普通消息
- 命令结果以 `role=system` 消息落库，不进 LLM 上下文、不污染对话
- Web 端输入 `/` 弹出候选菜单自动补全；飞书无原生命令菜单，以 `/help` 文本列表替代（交互式卡片待实现）
- 除标注「管理员」外所有命令普通用户可用；命令效果默认只作用于当前会话

---

### 已实现命令

#### 1. 帮助与发现

| 命令 | 功能 | 状态 |
|---|---|---|
| `/help` | 显示命令帮助摘要，按分组列出（Web 与飞书均可用，飞书为纯文本列表） | ✅ |
| `/commands` | 列出全部可用命令（含动态生成的技能命令） | ✅ |
| `/whoami` | 显示当前用户 ID、租户、角色、会话数与记忆数 | ✅ |

#### 2. 会话管理

| 命令 | 功能 | 状态 |
|---|---|---|
| `/new`（别名 `/reset`、`/clear`） | 清空当前上下文，开启新会话（Web 端自动切换新会话） | ✅ |
| `/sessions` | 列出我的历史会话（最近 20 条） | ✅ |
| `/rename <标题>` | 重命名当前会话 | ✅ |
| `/delete <id>` | 删除指定会话 | ✅ |
| `/switch <id>` / `/resume [id]` | 切换到指定 / 恢复上次会话（Web 端自动切换会话） | ✅ |

#### 3. 上下文与用量

| 命令 | 功能 | 状态 |
|---|---|---|
| `/context`（别名 `/ctx`） | 显示上下文占用：token 估算、预算占比、压缩触发线 | ✅ |
| `/compact` | 立即压缩上下文，生成摘要释放 token | ✅ |
| `/usage` | 显示会话消息统计、历史 token 估算与近 7 天 LLM 用量 | ✅ |

#### 4. 运行控制

| 命令 | 功能 | 状态 |
|---|---|---|
| `/stop` | 中断当前正在生成的回复（Web 端中断 SSE 流并返回确认） | ✅ |

#### 5. 会话模型

| 命令 | 功能 | 状态 |
|---|---|---|
| `/model` | 查看/切换当前会话使用的模型与可用模型列表 | ✅ |

#### 6. 智能体与委派

| 命令 | 功能 | 状态 |
|---|---|---|
| `/agents` | 列出我可用的智能体（标注当前绑定） | ✅ |
| `/agent <名称>` | 切换当前会话绑定的智能体 | ✅ |
| `/delegate <角色> <任务>` | 显式将任务委派给指定角色子智能体 | ✅ |

#### 7. 工具与技能

| 命令 | 功能 | 状态 |
|---|---|---|
| `/skills` | 列出已安装技能 | ✅ |
| `/skill <名称> [输入]` | 展开指定技能内容供直接使用 | ✅ |
| `/<技能名>` | 已安装技能自动注册为动态斜杠命令（内容同 `/skill`） | ✅ |
| `/tools` | 列出当前 Agent 可用工具（内置 / MCP / 远程工具） | ✅ |
| `/mcp` / `/reload-mcp`（管理员） | 查看 MCP 状态 / 热重载配置 | ✅ |
| `/reload-skills`（管理员） | 清除技能仓库元数据缓存 | ✅ |

#### 8. 记忆与知识库

| 命令 | 功能 | 状态 |
|---|---|---|
| `/memory` | 查看当前用户长期记忆列表 | ✅ |
| `/remember <内容>` | 手动写入一条长期记忆（L2 层） | ✅ |
| `/forget <id或关键词>` | 删除指定记忆（按 ID 或关键词检索删除） | ✅ |
| `/knowledge <问题>` | 直接检索当前会话智能体绑定的知识库并返回结果 | ✅ |
| `/portrait` | 查看 Agent 对当前用户的画像摘要 | ✅ |

#### 9. 工作区

| 命令 | 功能 | 状态 |
|---|---|---|
| `/ws list` | 列出我的工作区（标注当前） | ✅ |
| `/ws use <slug>` | 切换当前会话工作区 | ✅ |
| `/files` | 列出当前工作区文件 | ⏳ |

#### 10. 定时任务

| 命令 | 功能 | 状态 |
|---|---|---|
| `/cron list` | 列出我的定时任务 | ✅ |
| `/cron create <表达式> <任务>` | 创建定时任务（表达式支持引号，如 `/cron create '0 9 * * *' 日报`） | ✅ |
| `/cron pause <id>` / `/cron resume <id>` | 暂停 / 恢复定时任务 | ✅ |
| `/cron delete <id>` | 删除定时任务 | ✅ |
| `/loop <间隔> <任务>` | 在当前会话内循环执行某提示词 | ⏳ |

#### 11. 确认与权限

| 命令 | 功能 | 状态 |
|---|---|---|
| `/approve [id]` | 通过待确认操作；不带 id 时列出本会话待确认项 | ✅ |
| `/deny [id]` | 拒绝待确认操作；不带 id 时列出本会话待确认项 | ✅ |
| `/yolo on\|off` | 开关免确认模式 | ⏳ |

#### 12. 系统状态

| 命令 | 功能 | 状态 |
|---|---|---|
| `/status` | 系统状态：在线模型、MCP、渠道连接、队列概况 | ✅ |

#### 13. 飞书渠道特有

| 命令 | 功能 | 状态 |
|---|---|---|
| `/bind` | 绑定飞书账号与平台账号 | ✅ |
| `/new`（`/clear`、`/reset` 等效） | 开启新会话（走渠道专用逻辑，切换会话映射） | ✅ |
| 其他已实现命令 | `/memory`、`/cron`、`/skills`、`/agents` 等全部统一命令在飞书均可用，结果直接文本回复 | ✅ |
| `/btw <问题>` | 侧边提问，不污染当前会话上下文 | ⏳ |

#### 14. 交互增强（Web 端）

| 功能 | 说明 | 状态 |
|---|---|---|
| `/` 命令菜单 | 分组展示，前缀模糊搜索、上下键导航、Tab/Enter 补全与参数占位提示 | ✅ |
| 命令结果样式 | 以 `role=system` 浅色卡片展示（`系` 头像），与 Agent 回复区分 | ✅ |
| 动态命令与权限过滤 | 技能自动进候选菜单；菜单只显示当前用户可用命令 | ✅ |

---

### 待实现命令汇总

`/files` · `/loop` · `/yolo` · `/btw` · 飞书交互式命令卡片

> 说明：`/files` 需接入对象存储工作区文件列举；`/yolo` 涉及权限开关；飞书交互式卡片为渠道专项，均单独排期。

---

## 斜杠命令技术方案

> 对应功能清单：`01-功能清单.md`。本文描述斜杠命令系统的架构、拦截链路与扩展方式。

### 架构总览

后端 `interface/commands/` 包实现统一命令系统，Web 与飞书两条渠道共用同一份命令定义：

```
interface/commands/
├── models.py        # Command / CommandArg / CommandContext / CommandResult 数据模型
├── registry.py      # CommandRegistry 进程级单例 + @command 装饰器
├── parser.py        # shlex 分词 + 参数解析（支持引号、变参、类型强转、choices 校验）
├── dispatcher.py    # dispatch(ctx)：匹配命令 → 权限校验 → 参数解析 → 执行
├── dynamic.py       # 动态技能命令：用户技能自动注册为 /<技能名>
└── handlers/        # 各命令实现（按功能分组），模块导入即触发 @command 注册
    ├── system.py          # /help /commands /whoami /stop
    ├── session.py         # /new /sessions /rename /delete
    ├── skills_memory.py   # /skills /skill /memory /remember /forget /knowledge /portrait
    ├── schedule.py        # /cron list|create|pause|resume|delete
    └── agent.py           # /agents /agent /approve /deny /ws /model
```

命令元数据通过 `GET /api/commands` 下发，前端据此渲染候选菜单；`GET /api/models` 提供普通用户可读的模型列表。

### 拦截链路

#### Web 端（`interface/routes/chat.py`）

`POST /api/chat/stream` 的 `chat_stream` 在函数最顶部拦截（早于 agent 加载与模型解析）：

```python
if req.message.strip().startswith("/"):
    return await _handle_command_stream(req, request, user, db)
```

`_handle_command_stream` 流程：

1. 解析或创建会话（无 session_id 时新建）
2. 保存用户消息（`role="user"`，与普通消息同构）
3. 构造 `CommandContext`，调 `dispatch(ctx)` 执行命令
4. 将结果以 `role="system"` 消息写入会话历史（`load_conversation_history` 只收 `user/assistant`，system 不进 LLM 上下文）
5. 返回 SSE 流，依次推送两个事件：

```json
{"type": "command_result", "message_id": "...", "session_id": "...", "content": "...", "data": {...}}
{"type": "done", "command": true}
```

`data` 携带前端副作用：`new_session_id`（/new 切会话）、`deleted_session_id`（/delete 移除会话）、`session_id + title`（/rename 更新标题）、`agent_id`（/agent 刷新）、`workspace_id`（/ws use 切换）。

未知命令返回 `CommandResult(content="未知命令...")`，仍写入 system 消息；参数缺失返回 usage 提示；命令 handler 异常被 dispatcher 捕获返回错误文本，不中断连接。

#### 飞书渠道（`channels/pipeline.py`）

渠道消息在 `ChannelInboundPipeline` 中拦截：

```python
if text == "/bind":            # 渠道专用：绑定码流程
if text in ("/new", "/reset", "/clear"):  # 渠道专用：切换会话映射
if text == "/help":            # 渠道专用：从统一 registry 生成分组文本列表
if text.startswith("/") and await self._handle_unified_command(...):  # 其余已知命令走统一 dispatcher
```

`_handle_unified_command` 先按命令名匹配静态 registry 与动态技能命令，**未命中返回 False，降级为普通消息交给 Agent**（符合"未知命令降级"约定）；命中则执行 dispatcher 并 `adapter.send` 直接文本回复，同时 `db.commit()` 持久化命令写入。飞书渠道命令结果不落库 system 消息（渠道无会话历史概念）。

### CommandContext

```python
@dataclass
class CommandContext:
    user: Any             # User ORM（role 用于权限判断）
    user_id: str
    db: AsyncSession      # 与请求同 session，RLS 已由 get_db / pipeline 设置
    raw: str              # 原始命令文本（含 /）
    session_id: str | None
    session: Any | None   # Session ORM
    args: dict            # parser 解析后的参数
    tool_executor: Any | None
```

### 如何新增一个命令

1. 在 `handlers/` 下对应文件（或新建模块）用 `@command` 装饰器声明：

```python
from ..models import CommandArg, CommandResult, CommandContext
from ..registry import command

@command(
    "status",                       # 命令名
    aliases=["st"],                 # 别名（可选）
    group="系统状态",                # 分组（/help、菜单按此分组）
    desc="系统状态概览",             # 说明
    args=[CommandArg(name="detail", required=False, hint="详细模式")],  # 参数声明
)
async def cmd_status(ctx: CommandContext) -> CommandResult:
    return CommandResult(content="...", data={...})
```

2. 若新建模块，在 `handlers/__init__.py` 中 `from . import 模块名` 触发注册（模块导入即生效）。
3. 前端无需改动：`/api/commands` 自动下发元数据，命令菜单与飞书 `/help` 自动带出。

参数解析规则（`parser.py`）：
- 用 `shlex.split` 分词，支持引号包裹的空格参数（如 `/cron create '0 9 * * *' 任务`）
- `CommandArg(variadic=True)` 吞掉行尾全部剩余 token（如 `/remember <内容>`）
- `kind="int"` / `"uuid"` 做类型强转，失败返回用法提示
- `choices` 限制取值，超出返回提示

### 命令执行要点（复用现有服务）

| 命令 | 复用 |
|---|---|
| `/skills` `/skill` | `Skill` 表 + `dynamic.run_skill` |
| `/memory` `/remember` `/forget` | `MemoryService.list/create/delete` |
| `/knowledge` | `knowledge.handlers.handle_knowledge_retrieval`（临时设置 `current_agent_id` 为会话绑定 agent） |
| `/cron` | `CronJobService` + `scheduler.add_job/update_job/remove_job` |
| `/approve` `/deny` | `confirmation_manager.resolve_confirmation` |
| `/agents` `/agent` | `_agent_visible_to` 可见性查询 + 更新 `Session.agent_id` |
| `/ws` | `Workspace` 表（按 slug 查询） |
| `/model` | `LLMModel` 表（仅 active） |

### 前端

- `stores/commandStore.ts`：缓存 `/api/commands` 元数据，`search(query)` 按前缀/说明过滤并分组排序
- `components/chat/CommandMenu.tsx`：候选浮层，分组 + 键盘导航（↑↓/Tab/Enter/Esc）
- `ChatInput.tsx`：`/` 触发菜单；命令消息绕过消息队列直接发送（streaming 中 `/stop` 先中断流再发送）
- `ChatPage.tsx`：`command_result` 事件映射为 `role="system"` 消息 + 副作用；`done` 带命令标志时跳过 assistant 落库
- `ChatMessage.tsx`：`role="system"` 专用样式（浅色卡片、`系` 头像）

### 测试

`backend/tests/test_commands.py` 覆盖：参数解析（引号/变参/缺失/choices）、别名解析、未知命令降级、`/api/commands` 含动态技能项、`/api/models` 仅返回 active、`PATCH /sessions/{id}` 支持 agent_id、`/chat/stream` 端到端（command_result 事件 + system 消息写入且不进 LLM 历史）。

### 已知边界

- `/stop` 依赖前端 abort SSE 连接中断生成，服务端不做跨请求取消注册表；多 worker 部署下如需更强中断需引入任务注册中心（Redis）
- `/model` 当前只读：会话级模型切换需给 `Session` 增加 `model_id` 字段并处理与智能体默认模型的优先级
- 飞书 `/approve` `/deny` `/stop` 可返回文本但无对应执行上下文（飞书无 Web 确认卡片 / SSE 流）
