# Agent Platform

多租户 AI 智能体服务器，支持 LLM 调用、沙箱执行、持久记忆和多智能体委派。

## 功能特性

- 基于 PostgreSQL RLS 的多租户架构
- LLM 支持（OpenAI / Anthropic / 兼容 API）
- Docker 沙箱代码执行
- JWT 身份认证
- 持久记忆与技能系统
- 多智能体委派（父子智能体树）
- MCP 服务器集成
- CLI 和 Web UI 双界面

## 技术栈

- **后端**: Python 3.13, FastAPI, SQLAlchemy, Alembic
- **前端**: React + TypeScript, Vite, Ant Design
- **数据库**: PostgreSQL 16
- **沙箱**: Docker
- **包管理**: uv（后端）, pnpm（前端）

## 环境要求

- Python 3.13
- [uv](https://docs.astral.sh/uv/)（Python 包管理器）
- Node.js 24.22.1 LTS + pnpm 11.1.3（版本分别固定在 `frontend/.nvmrc` 和 `frontend/package.json`）
- Docker & Docker Compose

## 项目结构

```
agent-platform/
├── backend/                # 后端
│   ├── src/agent_platform/ # Python 源码
│   │   ├── core/           # Agent 循环、配置、提示词
│   │   ├── delegation/     # 子智能体委派处理器
│   │   ├── interface/      # FastAPI 路由（对话、会话、智能体...）
│   │   ├── tools/          # 内置工具 + MCP 集成
│   │   ├── memory/         # 记忆服务（L1/L2/L3）
│   │   └── skills/         # 技能服务
│   ├── alembic/            # 数据库迁移
│   ├── tests/              # 后端测试
│   ├── prompts/            # Jinja2 提示模板
│   ├── sandbox/            # 沙箱 Docker 镜像
│   ├── pyproject.toml      # 后端依赖
│   ├── uv.lock
│   └── .env.example        # 环境变量模板
├── frontend/                    # 前端 React 应用
│   ├── src/
│   │   ├── components/     # UI 组件
│   │   ├── pages/          # 页面组件
│   │   ├── stores/         # Zustand 状态管理
│   │   └── lib/            # API 客户端、类型定义、工具函数
│   └── vite.config.ts
├── docs/                   # 项目文档
├── docker-compose.yml      # 应用服务
├── docker-compose-base.yml # 独立管理的 PostgreSQL、Redis、MinIO
└── README.md
```

## 快速开始

```bash
# 1. 克隆并配置环境
git clone <repo-url> && cd agent-platform
cp backend/.env.example backend/.env

# 2. 启动基础设施 + 装依赖 + 数据库迁移
docker compose -f docker-compose-base.yml up -d
cd backend && uv sync && uv run alembic upgrade head && cd ..
cd frontend && corepack enable && pnpm install --frozen-lockfile && cd ..
```

### Docker 部署

基础服务和应用属于两个独立的 Compose 项目。先启动基础服务，再启动应用：

```bash
docker compose -f docker-compose-base.yml up -d --wait
# 在仓库根目录的 .env 中配置 DATABASE_URL、JWT_SECRET 和 STORAGE_* 等应用参数。
# 使用上述基础服务时，数据库主机为 postgres，REDIS_HOST=redis；
# STORAGE_ENDPOINT 应为浏览器和后端都可访问的对象存储地址。
docker compose pull
docker compose up -d --wait
```

两者通过 `agent-platform-infra` 网络连接；自定义网络时，在根目录 `.env` 中统一设置 `INFRA_NETWORK_NAME`。应用执行 `docker compose down` 只停止应用，不会停止 PostgreSQL、Redis、MinIO 或删除基础服务网络。

基础服务保留原默认数据卷名 `aio-agent-platform_pgdata` 和 `aio-agent-platform_minio_data`。已有部署若使用过自定义项目名，请先用 `docker volume ls` 核对，并通过 `POSTGRES_VOLUME_NAME`、`MINIO_VOLUME_NAME` 指定原数据卷，再切换基础服务项目；不要同时运行新旧基础服务容器。

### 启动后端（终端 1）

```bash
cd backend && uv run uvicorn agent_platform.interface.api:app --host 0.0.0.0 --port 8100 --reload
```

后端 API：**http://localhost:8100** ｜ API 文档：**http://localhost:8100/docs**

### 启动前端（终端 2）

```bash
cd frontend && pnpm run dev
```

前端：**http://localhost:1717**（`/api` 请求自动代理到后端）

### 其他命令

```bash
cd backend && uv run agent-cli          # CLI 对话
cd backend && uv run agent-manage       # 管理命令
cd backend && uv run alembic upgrade head  # 数据库迁移
cd frontend && pnpm run build         # 前端生产构建
cd frontend && pnpm run lint          # ESLint（JavaScript / TypeScript / React Hooks）
```

前端 lint 配置位于 `frontend/eslint.config.js`，CI 会自动执行检查。代码正确性和 Hooks 调用规则按错误检查；现有 `any` 类型、Hooks 依赖数组和 Fast Refresh 导出问题按警告报告，便于逐步清理。构建产物不参与检查。

前端 Docker 构建固定使用 Node.js 24.22.1 LTS，最终运行镜像固定为 Nginx 1.31.8 Alpine；CI 构建时强制拉取基础镜像。升级后需要重新构建、发布并替换前端容器，已有容器不会因基础镜像标签更新而自动升级。

## 配置说明

`backend/.env` 中的关键环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATABASE_URL` | `postgresql+asyncpg://agent_user:changeme@localhost:5435/agent_platform` | 数据库连接字符串 |
| `JWT_SECRET` | *（必填）* | JWT 签名密钥 |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | 访问令牌有效期（分钟） |
| `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | 刷新令牌有效期（天） |
| `HOST` / `PORT` | `0.0.0.0` / `8100` | API 服务绑定地址 |
| `CORS_ORIGINS` | `http://localhost:1717,http://localhost:3000` | 允许的跨域来源 |
| `AGENT_TRUST_LEVEL` | `auto_all` | 工具执行信任级别：`ask_always` / `ask_dangerous` / `auto_all` |
| `SANDBOX_IMAGE` | `crpi-yvha9coypdedaafa.cn-beijing.personal.cr.aliyuncs.com/lys1313013/agent-platform-sandbox:latest` | 沙箱 Docker 镜像 |

JWT 有效期兼容旧的无 `JWT_` 前缀变量；两者同时配置时以带前缀变量为准。修改有效期只影响新签发令牌，旧令牌保留原过期时间；需要立即使全部旧令牌失效时，由运维轮换 `JWT_SECRET`，用户须重新登录。密码至少 8 个字符，UTF-8 编码最多 72 字节（例如最多 24 个常见汉字），超过限制直接拒绝，不截断。API 中 bcrypt 使用最多 4 个并发工作线程，避免阻塞事件循环。

LLM 模型通过管理后台配置，不在 `.env` 中设置。

## 数据库迁移

```bash
cd backend

# 创建新迁移
alembic revision --autogenerate -m "描述"

# 执行迁移（新库和已有版本的数据库使用同一条迁移链）
uv run alembic upgrade head
```

API 启动也会检查并执行该迁移链，不再通过 `create_all` 建表后直接标记为最新版本。迁移 `c5e6f7a8b9d0` 补齐历史初始化遗漏的观测表、约束、索引与用户 RLS 策略；测试同时比较新库、旧版本升级和已标记版本的旧 bootstrap 数据库结构，并使用非 owner 数据库角色验证 RLS。

非空但没有迁移版本的数据库会拒绝启动和迁移，须先备份、核对实际结构并制定显式迁移方案，不能直接 `stamp head`。无法确定租户或存在不符合新约束的历史 NULL 记录时，迁移失败且不自动回填；`DATABASE_BOOTSTRAP_FORCE` 也不能绕过检查。历史归属修复需要独立授权。该结构修复迁移不提供自动 downgrade，回滚须使用经过验证的备份恢复方案。

## 文档

详细文档入口见 [`docs/README.md`](docs/README.md)。建议先读 [`docs/00-平台总览/README.md`](docs/00-平台总览/README.md) 了解企业级能力模型与本项目现状，再按能力域查阅 `docs/能力域/`。


### 执行租户与后台任务

后台任务和已有智能体会话固定使用执行对象的租户，界面切换不改变执行归属。成员资格和资源可见性仍独立校验；不能通过修改用户当前租户或取消过滤绕过权限。新增入口请遵循 [身份、组织与权限](docs/能力域/D01-身份组织与权限/README.md) 和 `AGENTS.md`，并覆盖切换租户、撤销权限、并发与取消回归。


定时任务星期输入采用标准 Cron（`0/7=周日，1=周一`），推荐使用 `sun`、`mon-fri` 等名称。存量数字表达式保留原 APScheduler 语义，编辑时转换为名称以避免无意改变时间。创建支持请求幂等和同名冲突提示；租户执行、权限与迁移约束见 [身份、组织与权限](docs/能力域/D01-身份组织与权限/README.md)。

定时任务绑定渠道后由执行器统一推送结果；智能体可通过 `cron_respond` 明确上报通知或静默决策，最终 `NO_REPLY` 也可抑制推送。后台分别保留执行摘要、通知状态及静默原因，见 [交互与人机协同](docs/能力域/D08-交互与人机协同/README.md)。
