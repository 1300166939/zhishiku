# AI 知识库

面向技术团队与 Agent 的单节点 Python 知识服务，支持公共库、API 专属库和 App 专属库，提供项目范围授权、版本化入库、混合检索及带引用的证据问答。

## 架构与代码结构

| 模块 | 主要文件 | 职责 |
|---|---|---|
| 服务入口 | `prototype/gateway.py`、`access.py` | HTTP 路由、请求校验、查询配额及响应前授权复核 |
| 知识底座 | `prototype/core.py`、`storage.py`、`parser.py` | 来源与版本注册表、SHA256 原文对象、解析、发布及撤回 |
| API 专属库 | `prototype/api_library.py` | 结构化 API 知识入库、稳定 ID、哈希幂等和项目筛选 |
| App 专属库 | `prototype/app_library.py`、`app_reports.py`、`app_maps.py`、`app_jobs.py` | HTML/ZIP 报告、Map、运行与步骤上下文；截图保留 URL 引用 |
| 混合检索 | `prototype/app_search.py`、`retrieval.py`、`rerank.py` | 词法、精确与向量召回，RRF 融合和可选重排 |
| 证据问答 | `prototype/evidence_qa.py` | 授权范围内 FTS 查询、关联章节、原文摘录、引用、冲突提示及拒答 |
| 业务治理 | `prototype/governance.py` | 有效期、权威依据、替代关系与审计；历史业务有效性默认未知 |
| 手动入库队列 | `prototype/connector_queue.py` | 持久任务、租约恢复、重试与检查点；支持公共/API/App 适配器 |
| 权限与开库 | `prototype/authorization.py`、`library_routes.py`、`library_operator.py` | 凭证策略、库目录发现和受控自助开库 |
| 运维 | `prototype/architecture_ops.py`、`backup.py`、`diagnostics.py` | 校验和备份、隔离恢复、文本向量重建、只读 GC 计划与追踪 |
| 模型接入 | `integration/` | 火山方舟 Embedding 与视觉模型适配器及验证 |
| 部署与客户端 | `deployment/` | 配置示例、手动 Worker 服务配置和 App 报告客户端 |

各库使用 SQLite WAL 注册表、文件系统原文对象、FTS 词法索引与回环地址 Qdrant 向量库，共享单节点物理资源。独立查询槽与 Worker 配额不代表独立基础设施隔离。

## 两条查询路径

- `POST /search`：保留词法、精确与向量混合召回，按配置启用重排，适合语义检索与证据查找。
- `POST /ask`：使用 FTS 和来源上下文返回带引用的原文摘录，不调用 Embedding、向量查询、重排或生成模型。

`/ask` 当前不覆盖图片派生问答、完整跨文档推理或生成式总结。找到证据不等于业务规则完整、当前版本有效或测试执行通过；消费 Agent 需要核验引用、版本与当前任务。

Web 连接器尚未实现。未部署本地 GPU 模型。

## 本地验证

要求 Python 3.11+，使用 Linux 或 macOS。部分服务依赖 `fcntl`，Linux 部署使用 systemd。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
PYTHONPATH=prototype:integration:deployment python -m unittest discover -s prototype -p 'test_*.py'
PYTHONPATH=prototype:integration:deployment python -m unittest discover -s integration -p 'test_*.py'
python tools/check_public_tree.py
```

测试使用合成数据及模拟模型/存储。依赖原始私有 HTML 报告的测试已在导出时剔除，并为图文步骤关联补充合成报告夹具；不要将真实报告复制到本仓库。

本地已通过 124 项服务测试与 5 项模型接入测试。示例名称进一步泛化后，39 项受影响测试再次通过；这部分与完整测试集重叠。模型配置示例已核对适配器字段，不触发真实网络调用。离线测试不代表生产性能或业务准确率验收。

`.github/workflows/check.yml` 会在 GitHub 上运行 Linux 离线检查，远端执行结果与本地验证分别记录。

`integration/smoke_ark.py` 是可选的真实供应商接口验证，需要自行配置凭证；它不属于自动离线测试。

## 部署方式

本仓库是可审查的实现导出，不是一键生产安装器。历史机器专用部署脚本、业务数据和运行快照均未包含。

详细配置见 [部署说明](deployment/README.md)。网关监听回环地址；需要部署自己的 HTTPS 反向代理、私密配置、Qdrant API Key、数据目录与授权策略。

App 报告入口仅允许占位来源 `https://reports.example.test/files/reports/`。接入真实报告服务时，应在 `prototype/app_jobs.py` 和 `deployment/app_kb_client.py` 中一致地替换精确白名单，不能改成允许任意 URL 的正则。

## 脱敏与运行边界

- 不包含公司文档、生产凭证、数据库快照、内部服务地址、真实报告附件或原项目 Git 历史。
- 配置示例仅保留字段名、通用路径和空凭证值。真实 `.env`、授权策略、凭证台账和运行对象不得提交。
- 示例网址使用保留的 `.test` 域名；官方模型供应商地址及回环地址按功能需要保留。
- 采集与队列均按人工触发使用，没有配置定时采集任务。
- GC 目前只生成计划，不执行删除；历史保留规则和离站恢复目标需要由部署方明确。
- 回滚到不支持 `qa_tokens` 的旧核心前，必须先移除 `qa_insert`、`qa_delete`、`qa_update` 触发器。
- 全量灾备、离站恢复、长期容量及独立业务评测仍需专项验收，不能由本仓库的测试结果代替。

本次导出未指定开源许可证。
