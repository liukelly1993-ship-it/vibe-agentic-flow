# VAF Technical Design 评审报告

> 评审对象：`VAF-Technical-Design.md`
>
> 评审日期：2026-08-05
>
> 评审范围：技术方案、当前 M0 实现、测试和 CLI 契约；不包含真实 LLM、CI/CD、云资源和生产环境。

## 1. 结论与评分

**评分：84 / 100（B+，M0 条件通过）**

技术方案的核心方向正确：以 ArtifactVersion 和事件日志作为事实依据，以状态机约束阶段推进，以 ToolGateway 约束外部动作，以 worktree 隔离代码变更，以 TraceLink 和验证证据形成质量门。

当前可以继续推进单仓库、单变更、CLI、FakeAgent 的 M0；不能把当前实现描述为真实 LLM 到生产部署的企业级完整流水线。生产发布、云身份、CI/CD、并发运行、自动语义追踪和完整失败恢复仍属于后续版本。

| 维度 | 得分 | 结论 |
|---|---:|---|
| 范围与演进边界 | 9/10 | M0 已收窄到需求到可验证代码，不包含部署 |
| 领域模型与状态机 | 16/18 | 有明确转移表，当前工作流已接入关键门禁 |
| 外部动作与安全策略 | 16/18 | worktree、写文件、验证命令经过 ToolGateway |
| 证据、追踪与质量门 | 15/18 | 已有显式映射、哈希校验和覆盖率门 |
| 可靠性与恢复 | 13/18 | 有事件链和幂等恢复，完整重试/取消/失效传播未实现 |
| 测试与可验证性 | 9/10 | 29 个 unittest 覆盖主要 M0 风险 |
| 平台化与生产演进 | 6/8 | 演进方向清楚，但尚未有 CI/CD 和部署实现 |
| **合计** | **84/100** | **M0 条件通过** |

## 2. P0 必须修复项

以下 P0 已在当前代码中完成，并有回归验证。

### P0-1 审批必须绑定当前产物哈希

**风险：** 用户审阅旧文件后，文件被修改，系统仍可能接受旧审批并推进流程。

**修复：** `approve` 和 `reject` 强制接收 `target_hash`，同时校验事件投影中的当前哈希和磁盘文件的实际哈希；CLI 的 `--target-hash` 为必填参数。

**证据：** `src/vaf/application/local_workflow.py`、`src/vaf/cli.py`、`tests/integration/test_local_workflow.py::test_approve_rejects_stale_target_hash`。

### P0-2 验证结果必须绑定实际 workspace 状态

**风险：** 代码或测试文件改变后，旧的成功验证结果可能因幂等键不变而被复用。

**修复：** workspace 指纹纳入验证幂等键；验证事件持久化该指纹；Trace 重新计算当前 worktree 指纹，不匹配时不复用旧证据。

**证据：** `src/vaf/adapters/git_worktree.py::workspace_fingerprint`、`src/vaf/application/local_workflow.py::verify` 和 `trace`、`tests/integration/test_local_workflow.py::test_implementation_writes_only_declared_files_in_worktree`。

### P0-3 状态机和策略网关必须成为真实工作流门禁

**风险：** 仅定义状态和安全原则而不在应用路径执行，Agent 或应用代码仍可能绕过审批、worktree 或命令策略。

**修复：**

- 审批、驳回、重新生成和阶段推进调用领域状态转换校验。
- 未完成 implementation 阶段时，`verify` 直接返回 `VAF-STATE-001`。
- worktree 创建、文件写入和验证命令均通过 `ToolGateway`。
- 网络、Secret、生产部署和未白名单命令默认拒绝。

**边界：** M0 目前保留失败 worktree，尚未提供自动重试、取消、清理和上游失效传播；这些能力不能在当前 README 中宣称已经完成。

### P0-4 Trace 必须具备可执行质量门

**风险：** 只有 TraceLink 数据结构而没有覆盖率和验证条件，流程可能生成“看起来完整”的追踪报告但仍缺少需求、测试或证据。

**修复：** 实施计划支持显式 `requirement_ids`、`acceptance_ids`、`test_ids`；Trace 校验链接哈希、AC 覆盖率、代码可解释率和当前验证结果。全部通过后才返回 `status: passed`。

**边界：** M0 不自动从代码 diff 推断语义关系，采用显式声明；真实模型生成和语义推断属于后续版本。

## 3. P1 可选修复项

这些问题不阻塞当前 M0，可按产品验证结果安排。

1. **产物依赖失效传播未落地。** Technical Design 已定义 `StageInvalidated`，但当前实现尚未在上游新版本审批后自动阻止下游旧产物。
2. **验证命令尚未完全由 manifest 驱动。** 当前 `verify` 使用固定 unittest 命令，后续应读取 `.vaf/manifest.yaml` 中的命令 ID、超时和网络策略。
3. **事件与投影仍是本地文件实现。** 多进程并发、远程不可变存储、事件 schema migration 和跨机器恢复需要平台化设计。
4. **Trace 仍是显式映射。** 后续可引入 Agent 候选链接，但必须经过 Schema、哈希和人工/策略质量门确认。
5. **CI/CD 与生产治理未实现。** staging、生产人工审批、OIDC、健康检查和回滚应单独作为 v0.2/v0.3 设计与验收。
6. **当前测试主要覆盖 Python/unittest。** 需要增加 Golden Change 夹具、失败恢复、并发锁和跨进程进程级验收。

## 4. 建议的后续顺序

1. 保持当前 M0 不扩展真实模型和生产部署，继续固化 Golden Change 与失败证据。
2. 先实现 manifest 驱动验证、失败重试边界和上游产物失效传播。
3. 再接入一个真实 Agent Provider，并保留 FakeAgent 作为确定性回归实现。
4. 最后单独设计 staging，再评估生产审批、OIDC、健康检查和回滚。

## 5. 验证结果

执行命令：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t . -v
```

结果：**29 个测试全部通过**。

