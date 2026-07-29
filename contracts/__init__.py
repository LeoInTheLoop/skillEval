"""N0：Contract Registry（AGENTS.md §6）。

全部契约从这里统一导出，上层只 `from contracts import X`，不必知道分了几个模块。
"""
from __future__ import annotations

from .context import ContextMessage, ContextRole, ContextTool, RoutingContext, ToolSource
from .evalcase import (
    CaseSetValidationError,
    CaseType,
    FullEvalTurn,
    RoutingCase,
    Severity,
    dataset_review_status,
    diff_case_sets,
    load_cases,
    require_approved_dataset,
    validate_case_set,
)
from .runtime import (
    TEXT_EXCERPT_LIMIT,
    Artifact,
    ErrorKind,
    ErrorSubkind,
    InvocationRequest,
    NetworkMode,
    ResolvedEnvironment,
    RunResult,
    RuntimeCapabilities,
    RuntimeHealth,
    RunStatus,
    SkillMode,
    ToolCall,
)
from .skill import (
    VERSION_DIR,
    SkillMeta,
    build_catalog,
    discover_skills,
    load_skills,
    version_order,
)
from .suite import (
    PINNED_IMAGE,
    RoutingSuite,
    SuiteEnvironmentSpec,
    SuiteJudgeSpec,
    SuiteModelSpec,
    SuiteScoringSpec,
    SuiteRoutingInputSpec,
    SuiteSkillSpec,
    format_suite_validation_error,
    load_suite,
)

# 骨架期的名字，保留别名以免旧脚本/测试断掉
RoutingRun = RunResult

__all__ = [
    "PINNED_IMAGE",
    "TEXT_EXCERPT_LIMIT",
    "Artifact",
    "CaseSetValidationError",
    "CaseType",
    "ContextMessage",
    "ContextRole",
    "ContextTool",
    "ErrorKind",
    "ErrorSubkind",
    "FullEvalTurn",
    "InvocationRequest",
    "NetworkMode",
    "RoutingCase",
    "RoutingContext",
    "RoutingRun",
    "RunResult",
    "RunStatus",
    "RuntimeCapabilities",
    "RuntimeHealth",
    "RoutingSuite",
    "ResolvedEnvironment",
    "Severity",
    "SkillMeta",
    "SkillMode",
    "ToolCall",
    "ToolSource",
    "SuiteJudgeSpec",
    "SuiteEnvironmentSpec",
    "SuiteModelSpec",
    "SuiteScoringSpec",
    "SuiteRoutingInputSpec",
    "SuiteSkillSpec",
    "build_catalog",
    "dataset_review_status",
    "diff_case_sets",
    "format_suite_validation_error",
    "load_cases",
    "VERSION_DIR",
    "discover_skills",
    "load_skills",
    "version_order",
    "load_suite",
    "require_approved_dataset",
    "validate_case_set",
]
