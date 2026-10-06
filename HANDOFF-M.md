# M：MaxKB 请求级实际用量归集交接

本轮完成可证实的主回复、问题优化和 ROUND 长期记忆调用归集及隔离验证。未修改任何应用配置，未关闭长期记忆，未访问真实模型、数据库、登录或运行服务；真实应用和 DPin 消费闭环由协调者后续验收。

## 工作区与提交

- 工作区：`/Users/a1/orca/workspaces/MaxKB-2/maxkb-complete-usage`。
- 分支：`Danieldongsongyang/maxkb-complete-usage`。
- 基线：`68b81ec`，本工作区开工时无既存修改。
- 首个可消费契约提交：`625fa18`，包含请求级累加器及最初 6 项测试。
- 完整实现与 20 项测试提交：`d89b8df9e06e09f6ebcdfb5c0773c2c628a77307`。
- 本文件单独提交；最终交付 SHA 见协调者收到的 `worker_done`，合入时须包含以上契约、实现和交接提交。

已完整读取批准的并行计划、需求文档、技术实现路径、进度清单、本工作区 AGENTS.md 及 DPin `.agents/rules/billing.md`。DPin 集成目录只读参考；未修改其他仓库、桌面工程或总进度。

## 修改文件

| 文件 | 作用 |
| --- | --- |
| `apps/common/handle/dpin_actual_usage.py` | 逐调用严格校验、请求级登记和冻结、来源一致性、重复及溢出拒绝 |
| `apps/application/chat_pipeline/pipeline_manage.py` | 每次流水线实例创建独立累加器，前置步骤失败转为固定 502 |
| `apps/application/chat_pipeline/I_base_chat_pipeline.py` | 严格步骤异常不持久化错误文本、不投递记忆任务、不使用零用量补位 |
| `apps/application/chat_pipeline/step/reset_problem_step/i_reset_problem_step.py` | 将本次 manage 显式传给问题优化实现 |
| `apps/application/chat_pipeline/step/reset_problem_step/impl/base_reset_problem_step.py` | 问题优化 invoke 登记真实用量，严格路径不调用估算器 |
| `apps/application/chat_pipeline/step/chat_step/impl/base_chat_step.py` | 主回复登记、记忆占位符副本替换、同步记忆与汇总、成功前冻结、严格详情不重复投递 |
| `apps/application/chat_pipeline/step/search_dataset_step/impl/base_search_dataset_step.py` | 严格检索路径在 embed_query 前拒绝无实际用量的向量调用 |
| `apps/application/long_term_memory/__init__.py` | 冻结实时 ROUND 配置、按轮数判断、将本轮对话传给同步提取、校验后保存记忆 |
| `apps/chat/serializers/chat.py` | 放行已归集的优化和 ROUND 记忆；拒绝未知消费者；注入本次记忆配置副本 |
| `apps/common/handle/impl/response/openai_to_response.py` | 成功响应核验请求票据与总量；提供安全的逐调用数量明细 |
| `tests/test_dpin_actual_usage.py` | 7 项纯标准库契约测试 |
| `tests/test_dpin_pipeline_usage.py` | 13 项源码函数与真实响应对象的隔离测试 |
| `HANDOFF-M.md` | 本交接记录 |

## 公开接口与实际接线

`actual_token_usage(message) -> (prompt, completion)` 保留既有调用方式。来源仅限**当前返回消息**的 `usage_metadata` 和 `response_metadata.token_usage`；支持 `input_tokens/output_tokens` 与 `prompt_tokens/completion_tokens`，两组同时存在须合法且一致。整数使用精确 `type(value) is int` 校验，拒绝布尔、字符串、浮点、负数和越界；提供 `total_tokens` 时必须等于两项之和。整个调用及整个请求的总量均不得超过 `2**31 - 1`，不截断、不估算。显式空或错误类型的 usage 来源不会被另一组合法数据掩盖。

`RequestActualUsage()` 每次请求新建，公开方法如下：

| 方法 | 契约 |
| --- | --- |
| `begin(purpose) -> call_id` | 调用前登记；用途只接受 `reply/problem_optimization/long_term_memory` |
| `complete(call_id, message) -> (prompt, completion)` | 校验当前响应并完成一次票据；重复票据、相同非空响应 ID 或总量溢出使整个请求失败 |
| `invoke(purpose, model, messages) -> message` | 上述登记、真实 invoke、完成校验的一次封装；上游异常转换为固定错误，不附带正文或凭据 |
| `fail(call_id)` | 将请求标记为不可恢复的失败；内部也使用 `None` 标记非单调用的整个请求失败 |
| `finalize() -> (prompt, completion)` | 空请求、失败或未完成票据均拒绝；成功后冻结，后续不能开始／完成新调用；重复读取冻结结果允许 |
| `details() -> list[dict]` | 冻结后输出 `call_id/purpose/prompt_tokens/completion_tokens`；不输出消息、凭据、方案或上游响应正文 |

使用锁只保护**本次实例**的登记与求和，没有全局状态、模型实例状态或跨请求锁。非空上游响应 ID 的去重范围仅为本次请求；无 ID 时仅依赖票据单次完成，不推断多个消息是否对应同一物理网络尝试。

`request_memory_config(application, chat_user_id) -> config | None`：对现有实时 Application 的记忆配置取副本；保持原异步任务读取实时应用的语义。启用记忆时仅支持 SIMPLE／ROUND、正整数轮数和有效模型引用，定时配置返回严格失败，配置本身不改写。

`extract_long_term_memory_for_request(workspace_id, application_id, chat_user_id, config, actual_usage, current_conversation)`：读取已有记录数加本轮的轮数；到达阈值时用前 `rounds - 1` 条历史加显式本轮对话提取一次，归集本次 invoke 的真实数量后保存记忆。未到阈值时没有记忆调用，不伪造零用量票据。

当前源码已完成以下接线，不需要协调者再给 MaxKB 添加入口：

1. `ChatSerializers.chat` 拒绝流式、工作流、知识检索、工具／MCP／子应用／技能和 SCHEDULED 配置。
2. `ChatSerializers.chat_simple` 在运行前读取实时应用、冻结记忆配置并传给本次流水线。
3. `PipelineManage.__init__` 为 OpenAI 响应创建 `actual_usage`；`IResetProblemStep._run` 显式传入 manage。
4. 问题优化与 `BaseChatStep.get_block_result` 使用 `actual_usage.invoke`。主回复只在本次消息副本上替换 `{memory}`，不会改共享历史消息。
5. `BaseChatStep.execute_block` 先取得主回复，再同步执行必要的 ROUND 记忆，随后 finalize；请求上下文总量和逐调用明细更新完成后才调用 post handler、返回成功响应。
6. `BaseChatStep.get_details` 在严格路径下不再 `apply_async`，重复读取详情不会重复调用模型。非严格 System 响应保留既有异步任务及记忆 stream 路径。
7. `OpenaiToResponse.to_block_response` 成功前检查请求票据、准确整数与汇总的一致性；仅传 `usage_source=upstream` 无法伪造成功。

成功响应继续提供 DPin 现有 `prompt_tokens/completion_tokens/total_tokens/usage_source=upstream`，额外提供 `usage.calls` 明细。只读核对 DPin `relay/channel/maxkb/adaptor.go`：当前结构只读取既有四项用量字段，新增 calls 不改变它的数量校验和现有结算算法；本轮没有执行 DPin 端消费测试。

## 模型消费者与用量证据

| 消费点 | 本轮状态／证据 |
| --- | --- |
| 简单应用主回复 `get_block_result` 的 invoke | 已归集当前 AIMessage 两个 metadata 来源；非模型静态回复拒绝，不能冒充实际零用量 |
| `BaseResetProblemStep.execute` 的 invoke | 已归集；优化失败／缺失数量时，流水线在主回复前失败 |
| ROUND `long_term_memory._run_extract` | 严格请求内使用一次 invoke，读取当前返回消息；等待其完成才冻结和响应；原 Celery 投递被该请求抑制 |
| `BaseChatOpenAI` | `_convert_chunk_to_generation_chunk` 将原始 `usage` 转为消息 usage_metadata；invoke 返回消息中 token_usage／usage_metadata。本轮只读定位该来源，没有使用它写在模型实例上的共享 usage_metadata |
| Azure、Wenxin、XF、Hunyuan、Xinference、vLLM 等 provider | 存在实例级缓存／厂商字段来源差异；严格接口只信返回消息的标准字段，未实测每个厂商；缺失／非法标准用量即失败 |
| SCHEDULED `_execute_scheduled_extract` 与既有后台任务 | 独立后台调用没有本次请求归属，严格聊天接口拒绝相应配置；后台消费者本身未改造 |
| `BaseSearchDatasetStep.embed_query` 与 workflow search_knowledge | 返回向量，未找到本次调用数量；严格接口在调用前拒绝知识配置，并在检索步骤增加第二道拒绝 |
| `flow/tools.py` 的 create_deep_agent／agent.astream、MCP、技能、工具和子应用 | 可能迭代／再调用模型；无每次调用的完整数量证据，严格入口拒绝；未修改这些执行器 |
| workflow AI、question、intent、parameter_extraction、image_understand、video_understand | 有 invoke／stream 消费点，未逐节点归集，严格接口拒绝整个 WORK_FLOW |
| workflow reranker、loop、tool_workflow_lib、application_node | compress_documents、嵌套 stream／应用调用，数量及传播未证明，仍属于 WORK_FLOW 拒绝范围 |
| workflow 图像、图生视频、文生视频、STT、TTS | generate_image、generate_video、split_and_transcribe、text_to_speech 等消费；返回 URL／字节／文本，未建立实际数量归集，WORK_FLOW 拒绝 |
| 独立提示词生成及语音接口 | `DebugChatSerializers.generate_prompt`、`ApplicationOperateSerializer.speech_to_text/text_to_speech` 是其他 API，未接入本次聊天累加器，不宣称全站模型用量已完成 |
| 知识导入／问题更新后台嵌入 | `common/event/listener_manage.py`、knowledge 任务等，属于其他操作；未接入聊天请求归集 |

## 实际验证命令与结果

测试依赖只安装在本 worktree 的 `.venv`，不改项目依赖声明、vendor 或生产环境。实际命令：

```sh
uv venv --python python3.11 .venv
uv pip install --python .venv/bin/python 'django==5.2.16' 'djangorestframework==3.17.2' 'openai' 'langchain-core==1.4.8' 'ruff==0.15.12'
.venv/bin/python -m unittest discover -s tests -p 'test_dpin*usage.py' -v
.venv/bin/python -m compileall -q apps/common/handle/dpin_actual_usage.py apps/application/chat_pipeline apps/application/long_term_memory apps/chat/serializers/chat.py apps/common/handle/impl/response/openai_to_response.py tests
.venv/bin/ruff check tests/test_dpin_actual_usage.py tests/test_dpin_pipeline_usage.py apps/common/handle/dpin_actual_usage.py apps/application/chat_pipeline/pipeline_manage.py --output-format concise
git diff --check
```

实际环境：Python 3.11.15、Django 5.2.16、DRF 3.17.2、OpenAI 3.24.0、langchain-core 1.4.8、Ruff 0.15.12。复现时可将安装命令中 `openai` 固定为 `openai==3.24.0`。

- 20 项测试通过；包含 64 次并发请求的独立累加器、32 次共享同一模型实例的并发回复、同请求并发完成、多调用汇总、来源冲突、重复、溢出、失败／缺失数量、静态回复拒绝、记忆完成前不响应、ROUND 历史与本轮各出现一次、配置不改写、未知入口拒绝及非严格路径保留。
- 编译检查与 `git diff --check` 通过；新增模块及两个测试 Ruff 通过。
- 对全部修改的现有 Python 文件运行 Ruff 并按规则代码／错误信息与 `68b81ec` 比较，新增问题为 0。现有文件仍有 7 项基线问题（未使用导入／异常变量、无占位符 f-string），本轮未扩大格式化修改范围。
- 真实响应对象使用原有 `.dict()` 产生 Pydantic 弃用提示；它不影响本次通过结果，未顺带修改原有序列化方式。
- 测试使用 AST 选择实际源码定义并执行，数据库／模型／空工具路径注入夹具，HTTP 响应、usage DTO 和 LangChain 消息使用真实依赖；**不是完整 Django 启动测试，也不是实际数据库／模型端到端验证**。

## 协调者需要接续的工作

1. 按顺序合入本分支三个提交到正确 MaxKB 集成基线；本轮不修改桌面原工程或真实服务。
2. 同步 DPin `tools/maxkb/dpin_actual_usage.py`、`apply-strict-usage.py` 和相关验证器：不能只保留旧四文件字符串补丁。上表十个生产源码文件都有必要改动，重放补丁必须包含它们；旧工具可能重写 BaseChatStep／serializer 并重新禁止 ROUND 或丢掉本次归集。
3. 验证器旧版对所有长期记忆／问题优化的预拒绝应改为本轮支持边界，继续拒绝 SCHEDULED、知识／工具／工作流／流式；不要通过关闭用户功能使验收通过。
4. 在协调者安排的隔离完整 Django 环境启动／导入验证；保持用户应用草稿、发布版和 Key 原样，以当前返回的逐调用明细验证实际 provider 数据来源。
5. 安排同会话两轮真实调用、触发 ROUND 的轮次、DPin 消费日志和余额变化闭环，并同步失败／缺失／拒绝和退款证据。真实运行前据用户原配置判断是否含本轮仍严格拒绝的消费者。

## 未验证范围与风险

- 未启动服务、未读取／写入真实 DB／env／应用 Key、未调用真实测试应用、未修改发布配置、未发送招聘消息；没有真实应用或全链路完成结论。
- ROUND 判断沿用“同应用同用户累计记录数”的计数方式，并把尚未持久化的本轮显式加一；**同一会话／同用户并发写入时的触发次数和记忆覆盖不是事务化恰好一次语义**。本轮证明的是请求归集不串用、每次实际请求内 invoke 单次计数，未实现跨请求轮次锁或去重账本。
- 同步记忆增加严格请求延迟；记忆数量非法时主模型已经完成，仍返回失败且不交付可发送回复。记忆保存与聊天保存沿用原有独立写入，没有新增数据库事务；后续聊天保存失败可能已经更新记忆，不能声称失败会回滚所有副作用。
- provider SDK 内部自动重试、缓存、网络超时后上游已处理但未返回的尝试，本轮没有可观察的逐尝试证据；返回消息证明的是该 invoke 的返回数量。未验证这些隐藏尝试的实际消费，不宣称全 provider／每次物理网络尝试已完整归集。
- 定时／非严格后台提取及其他独立 API 不归属于本次严格聊天请求；保留原功能但不宣称已统一收费。所有已明确未知的严格聊天消费者如上表拒绝，不能把配置保存成功解释为完整实际用量可用。
- 混合模型调用总量继续按 DPin 已选逻辑模型的既有计费路径传递，未新增价格、扣费公式或不同模型分项定价；定价与真实账务验收归协调者。
