# 自定义 Evaluator

Evaluator 是只读评分插件：输入统一的 run / case / suite context，输出一组指标和可选明细。
它不能调用 runtime，也不应读取 OpenClaw 私有对象。已有事实必须来自 `RunResult`；需要模型
判断的语义项应使用独立 Judge 工作流，不要藏在 evaluator 里偷偷外发。

## 最小实现

在当前 Python 环境可 import 的模块中定义：

```python
from evaluators import register

@register("policy-compliance", version="policy-compliance-v1")
class PolicyComplianceEvaluator:
    def __init__(self, forbidden_tools=None):
        self.forbidden_tools = set(forbidden_tools or [])

    def evaluate(self, context):
        calls = {
            tool["name"]
            for run in context.runs
            for tool in (run.get("tool_calls") or [])
        }
        passed = not bool(calls & self.forbidden_tools)
        return {
            "metrics": {"policy_compliance": float(passed)},
            "observed_tools": sorted(calls),
        }
```

suite 中显式引用模块、注册名和参数：

```yaml
scoring:
  metrics: [policy_compliance]
  evaluators:
    - my_evals.policy:policy-compliance
  evaluator_options:
    my_evals.policy:policy-compliance:
      forbidden_tools: [shell, browser]
  gate:
    policy_compliance: ">= 1.00"
```

运行前会 import 并校验引用；模块不存在、注册名拼错、重复 evaluator、options 指向未启用
evaluator 或实现未声明 `version`，都会在执行前失败。无需修改 `score_full.py`、
`score_routing.py` 或运行编排。

## 输出契约

`evaluate(context)` 返回 JSON-compatible dict。约定：

- `metrics` 中的 `float / int / None` 会并入顶层 `scores`，因此可用于 gate；
- `dict / list / str / bool` 不会冒充连续指标，但仍完整保留在 `evaluation` 里供报告查看；
- 两个 evaluator 提供同名标量且值不同会拒绝评分，不按 evaluator 顺序覆盖；
- N/A 用 `None`，不要用 0；0 表示已有证据证明失败。

注册时可设 `expose_scalar_metrics=False`，让该 evaluator 的标量只留在 `evaluation`
展示层、不进入顶层 scores/gate。内置 `reliability` / `efficiency` 就采用这一模式；外部
evaluator 默认暴露标量。

`EvaluationContext` 只提供：

- `suite`：规范化 suite 配置；
- `snapshot`：本次 run 的完整配置快照；
- `cases`：case contract 映射；
- `runs`：归一化 `RunResult` JSON；
- `scores`：注册 evaluator 运行前已经算出的基础指标；
- `rows`：当前 scorer 的逐 run 确定性投影。

不要依赖 evaluator 调用顺序：所有 evaluator 收到同一个初始 context，不能消费另一个
evaluator 的临时输出。跨层组合应在独立 evaluator 内从基础事实重新计算。

## 可复现性

每份 `scores.json` 的 `evaluator_manifest` 记录引用、注册名、Python module/class、version、
源码完整 SHA-256 和 options。`pipeline rescore` 把同一份 manifest 纳入 `grading_hash`。
修改 evaluator 代码必须 bump version；即使忘记 bump，source SHA 仍会让两把量具可区分。

自定义 evaluator 模块必须在评分机器上可 import。归档 run 保存的是量具 fingerprint，
不是第三方源码副本；长期复现时应同时固定插件包版本或保留对应 git commit。
