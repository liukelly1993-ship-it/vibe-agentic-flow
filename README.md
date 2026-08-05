# VAF

VAF（暂定名）是一个以规格文档驱动、面向企业级软件研发全流程的 AI 自动化工作流项目。

当前已进入 M0 实现阶段，完整方案见同目录下的：

`/Users/wanjiaheng/Documents/工作/workspace/ai-agent/project/vaf/vaf-调研与架构规划.md`

架构评审、评分和改进建议见：

`/Users/wanjiaheng/Documents/工作/workspace/ai-agent/project/vaf/vaf-架构评审报告.md`

产品范围、功能需求与验收标准见：

`/Users/wanjiaheng/Documents/工作/workspace/ai-agent/project/vaf/VAF-PRD.md`

技术架构、领域模型、状态机与 M0 实现顺序见：

`/Users/wanjiaheng/Documents/工作/workspace/ai-agent/project/vaf/VAF-Technical-Design.md`

Technical Design 的评审结论、P0 修复状态和 P1 建议见：

`/Users/wanjiaheng/Documents/工作/workspace/ai-agent/project/vaf/VAF-Technical-Design-评审报告.md`

## 当前状态

- 文档：架构规划、架构评审、PRD、Technical Design 已完成。
- 已实现：领域状态机、事件哈希链、ArtifactVersion、TraceLink 校验、Policy Engine、ToolGateway、Git worktree 适配器、Fake Agent、AgentPort 和 CLI 闭环。
- 已验证：29 个 unittest 全部通过，并覆盖审批推进、旧哈希拒绝、事件链校验、跨进程幂等恢复、隔离 worktree、代码范围校验和回归验证。
- 已实现：事件日志投影与状态恢复、跨进程 ToolGateway 幂等复用、可注入 `AgentPort`、确定性 FakeAgent、`implement` 隔离代码写入和 worktree 内验证。
- 已实现：最小显式 TraceLink 与质量门；审批要求绑定当前 `artifact_hash`，验证证据绑定当前 worktree 指纹，工作区变化会使旧验证结果失效。
- 当前边界：真实 LLM、语义关系自动推断、自动回归选择、CI/CD staging/production 部署尚未接入；FakeAgent 只按显式 `implementation.changes` 文件计划生成候选代码。
- 下一步：接入真实 Agent Provider，增加更严格的产物依赖失效传播，再设计 CI/CD Adapter。

产品工作流图见：

`/Users/wanjiaheng/Documents/工作/workspace/ai-agent/project/vaf/diagram/vaf-product-workflow/vaf-product-workflow.svg`

计划覆盖：

- 可行性分析
- 需求分析
- BRD / PRD
- 技术方案与架构决策
- 测试策略、测试用例和回归用例
- 代码生成、验证和需求追踪
- staging / production 部署、审批、健康检查和回滚

代码按 `src/vaf`、`tests` 和适配器分层实现；第一版继续保持 CLI + 文件化产物 + Git worktree + 自动验证闭环。

## M0 CLI 验证

在 VAF 仓库目录执行：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

对一个已初始化的 Git 项目运行最小流程：

```bash
export PYTHONPATH=/Users/wanjiaheng/Documents/工作/workspace/ai-agent/project/vaf/src
python3 -m vaf.cli --path /path/to/your/repo run \
  --change CHG-001 --title "示例需求" --objective "验证一个可交付目标"

# 使用上一步输出的 RUN-... 继续操作
python3 -m vaf.cli --path /path/to/your/repo review --run RUN-...
python3 -m vaf.cli --path /path/to/your/repo status --run RUN-...  # 读取当前 artifact_hash
python3 -m vaf.cli --path /path/to/your/repo approve --run RUN-... --actor reviewer --target-hash sha256:...
python3 -m vaf.cli --path /path/to/your/repo resume --run RUN-...
python3 -m vaf.cli --path /path/to/your/repo implement --run RUN-...
python3 -m vaf.cli --path /path/to/your/repo verify --run RUN-...
python3 -m vaf.cli --path /path/to/your/repo trace --run RUN-...
```

`run`、`approve`、`reject`、`resume` 生成确定性的文档草稿。需要验证代码写入闭环时，`run` 可接收一个包含 `implementation.changes` 的 YAML 文件：

```yaml
implementation:
  changes:
    - task_id: TASK-001
      path: src/example.py
      requirement_ids: [REQ-001]
      acceptance_ids: [AC-001]
      test_ids: [TC-001]
      content: |
        VALUE = 1
```

例如：`run ... --implementation-spec implementation.yaml`。审批完实施计划后执行 `implement`，VAF 会在隔离 worktree 中通过 Policy Gateway 写入声明文件；`verify` 随后在该 worktree 中执行白名单命令，并把退出码、workspace 指纹和截断后的 stdout/stderr 写入运行事件日志。`trace` 只有在显式映射、链接哈希、覆盖率和当前验证证据都通过时才返回 `status: passed`。
