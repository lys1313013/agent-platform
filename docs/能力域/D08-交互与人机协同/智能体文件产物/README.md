# 智能体工作区文件变更

> 合并说明：本文由 2 份原始文档合并，内容保持原样，仅调整标题层级。

## 智能体工作区文件变更 — 需求文档

> 版本：1.0（简单版）
> 日期：2026-09-08

### 1. 目标

智能体在一轮对话中创建、修改或删除工作区文件后，平台自动记录真实文件变化，并在该轮助手消息中展示。用户无需理解沙箱路径，也不需要智能体额外执行“发布文件”。

### 2. 核心原则

- 工作区文件是唯一内容来源，不创建 Artifact 记录，不复制不可变快照。
- 文件变化由平台自动检测，不依赖模型文本或额外工具调用。
- 文件变化属于助手消息，刷新历史会话后仍可查看。
- 预览和下载继续使用工作区权限，不建立第二套文件权限。
- 文件被再次覆盖时读取当前内容；文件被删除或移动后，历史记录保留，但不能继续预览和下载。

### 3. 用户流程

1. 用户要求智能体创建或修改文件。
2. 智能体正常调用 `write_file`、`edit_file`、`run_code` 或 `run_shell`。
3. 平台比较工具执行前后的工作区文件清单。
4. 平台记录 `created`、`modified`、`deleted` 三类变化。
5. 对话中实时展示“本轮文件变更”。
6. “写入文件/编辑文件”的成功执行结果可直接点击；用户可以预览受支持格式，或下载当前文件。

### 4. 简单版范围

#### 支持

- 自动检测 `write_file`、`edit_file`、`run_code`、`run_shell` 引起的变化。
- 一轮内同一路径的多次变化自动合并。
- 排除 `.git`、`node_modules`、`.venv`、`__pycache__`、`.cache`、`dist`、`build`。
- 文件名是统一预览入口，不额外显示重复的“预览”按钮。
- Markdown 按标题、列表、代码块等语义直接渲染；其他文本显示原文，图片和 PDF 使用对应预览器。
- 当前文件下载。
- 成功的写入/编辑工具结果直接提供预览和下载入口；旧消息可从工具结果兼容解析文件路径。
- 管理端、Portal 和宠物对话共用消息展示结构。

#### 暂不支持

- 发布时快照和历史版本恢复。
- 将旧消息反向写回为结构化文件变化记录。
- 外部渠道原生文件推送。
- 文件内容级 diff。
- 大型工作区的增量文件监听优化。

### 5. 验收标准

- 智能体创建文件后，不调用额外工具也会显示“已创建”。
- 修改已有文件显示“已修改”，删除已有文件显示“已删除”。
- 同一轮创建后又修改仍显示“已创建”。
- 同一轮创建后删除不显示无意义记录。
- 刷新会话后文件变化仍附着在原助手消息中。
- 成功的写入/编辑结果中，`Written to` 等说明文字保持普通样式，只有文件名可点击预览；旧消息在文件仍存在时同样可用。
- 已删除文件不提供预览和下载按钮。
- 用户只能读取自己工作区中的文件。

---

## 智能体工作区文件变更 — 技术方案

> 版本：1.0（简单版）
> 日期：2026-09-08

### 1. 结论

不建立 Artifact 领域模型。`Message.file_changes` 直接记录该轮工作区变化，沙箱工具执行器负责自动生成结构化变化事件，聊天链路负责合并、推送和持久化，前端复用 Workspace 文件接口完成预览与下载。

```text
沙箱工具执行前 manifest
        ↓
write_file / edit_file / run_code / run_shell
        ↓
沙箱工具执行后 manifest
        ↓
created / modified / deleted
        ↓
ToolResult.file_changes
        ↓
SSE file_changes → Message.file_changes
        ↓
ToolCallCard / FileChangeList → Workspace 下载接口
```

### 2. 数据结构

`messages` 新增可空 JSONB 字段 `file_changes`：

```json
[
  {
    "action": "created",
    "workspace_id": "uuid",
    "path": "reports/result.md",
    "filename": "result.md",
    "mime_type": "text/markdown",
    "size": 1024
  }
]
```

简单版不单独建表，因为文件变化只随消息读取，不需要独立查询、分享、版本管理或单独授权。

### 3. 差异检测

执行以下工具前后扫描当前 workspace：

- `write_file`
- `edit_file`
- `run_code`
- `run_shell`

Manifest 保存相对路径、字节数和 `mtime_ns`。比较规则：

- 仅出现在执行后：`created`
- 前后都存在但大小或修改时间不同：`modified`
- 仅出现在执行前：`deleted`

扫描排除常见依赖、缓存、版本控制和构建目录。后续如果大型工作区扫描成本明显，再升级为文件系统事件监听或沙箱执行层变更日志。

### 4. 一轮内合并

同一路径多次变化按顺序合并：

| 已记录 | 新变化 | 最终结果 |
|---|---|---|
| created | modified | created |
| created | deleted | 移除记录 |
| modified | deleted | deleted |
| deleted | created | modified |

### 5. 消息与事件

AgentLoop 在工具结果后发送：

```text
file_changes:[...]
```

SSE 转为：

```json
{
  "type": "file_changes",
  "file_changes": []
}
```

流结束的 `done` 事件和助手消息均包含合并后的 `file_changes`。

### 6. 文件访问

前端使用现有接口：

```text
GET /api/workspaces/{workspace_id}/files/content?path={path}
```

接口已有用户工作区归属校验，并优先读取活跃沙箱中的最新文件，失败后回退对象存储。聊天结果、沙箱文件面板和工作区文件页统一复用 `WorkspaceFilePreview`：文件名负责打开预览，操作区只保留下载；Markdown 使用 `react-markdown` 和 `remark-gfm` 渲染，其他文本、图片和 PDF 使用各自的共享预览规则。

`ToolCallCard` 优先按路径关联 `Message.file_changes`。对于改造前的历史消息，如果成功的 `write_file` / `edit_file` 结果包含可解析路径，则前端临时构造访问描述，使执行结果文字也可点击；该兼容逻辑不修改历史数据，也不把推断结果用于审计。

### 7. 已知限制

- `mtime_ns + size` 是轻量检测，不计算内容哈希；极端情况下，同纳秒内同大小覆盖可能漏报。
- 每个写入型工具前后扫描一次工作区，大型目录可能产生额外开销。
- 历史记录表示“当时发生过什么”，预览和下载表示“该路径现在是什么”。
- 旧消息不自动回填；仅在展示时尝试解析成功的写入/编辑结果，文件已移动或删除时访问会失败。
