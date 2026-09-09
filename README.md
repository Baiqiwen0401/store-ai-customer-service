# 个体门店 AI 客服

面向美容院等个体门店的本地优先 AI 客服。当前版本只接入网页客服，支持意图识别、已发布知识直答、复杂问题模型辅助、预约意向待办、人工接管、客户长期记忆审核和模型诊断。

生产级方案与实施进度见 `个体门店AI客服生产级技术方案_v2.0.docx` 和 `PROJECT_PROGRESS.md`。新会话或新成员开始工作前，应先读取 `PROJECT_PROGRESS.md`，按照阶段状态继续，不重复已经完成的工作。

## 运行

```powershell
$env:LLM_API_KEY = "你的模型密钥"
$env:LLM_BASE_URL = "https://new.tianluo.ccwu.cc/v1"
$env:LLM_MODEL = "gpt-5.6-sol"
$env:LLM_TIMEOUT_SECONDS = "8"

python app.py
```

打开 <http://127.0.0.1:8000>。不配置模型密钥也可以使用知识库直答；复杂咨询会说明资料未覆盖并建立人工跟进，不会伪造答案。

生产环境还必须设置 `STORE_AI_ENV=production`、`STAFF_ACCESS_KEY` 和明确的 `STORE_AI_ALLOWED_ORIGINS`。当前 SQLite 仅用于本地试用，生产数据迁移到 PostgreSQL 前不得承载关键真实业务。

可选配置 `DIFY_BASE_URL` 与 `DIFY_API_KEY` 后，复杂问题会通过 Dify Workflow API 处理；门店项目、价格、地址和营业时间等结构化直答仍由业务后端优先处理。Dify 未配置时继续使用 `LLM_*` 的 OpenAI-compatible 模型调用。

数据默认保存在 `runtime/store-ai.sqlite3`，该目录已加入 `.gitignore`。Docker 启动：

```powershell
docker compose up --build
```

## 核心流程

```text
风险检测 -> 意图识别 -> 已发布知识是否充分 -> 知识直答 / 模型辅助 -> 预约待办或人工接管
```

客户勾选同意并提供手机号后，系统才保存长期记忆候选；门店员工在工作台确认后才会用于后续回答。人工接管后 AI 自动暂停，员工可认领、回复、恢复 AI 或结束会话。

## 接口

- `POST /api/chat`：网页会话、意图识别、回答、预约意向和人工接管。
- `GET /api/conversations`、`GET /api/conversations/{id}`：会话列表和完整消息。
- `POST /api/conversations/{id}/claim|reply|resume|close`：人工工作流。
- `GET /api/model-status`：最近模型调用状态、耗时和错误类别，不返回密钥。
- `GET /api/health`：服务和数据库健康检查，供反向代理和监控使用。
- `GET /api/tasks`、`POST /api/tasks/{id}/complete`：预约/人工待办。
- `GET /api/memories`、`POST /api/memories/{id}/approve|reject`：长期记忆审核。
- `GET /api/knowledge`、`POST /api/knowledge`、`PUT /api/knowledge/{id}/publish|archive`：知识草稿和发布。

设置 `STAFF_ACCESS_KEY` 后，除公开的 `/api/chat`、`/api/store`、`/api/health` 外，工作台读写接口都需要携带 `X-Staff-Key`。当前仍是本地试用版，生产部署还需要正式登录、HTTPS、备份、隐私协议和平台官方接口审核。

## 验证

```powershell
python -m unittest -v
```

当前自动化回归覆盖知识直答、项目总览、风险转人工、预约待办去重、记忆同意与审核、模型辅助安全话术和注意事项咨询。

## 开发续作

每完成一个阶段，必须同步更新 `PROJECT_PROGRESS.md` 的阶段状态、完成项、未完成项、阻塞项、验证记录和变更记录。生产化改造按“生产骨架 -> 数据与业务服务 -> Dify/RAG -> 人工协同 -> 前端工作台 -> 质量验收 -> 灰度上线”顺序执行。
