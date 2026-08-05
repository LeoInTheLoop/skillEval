"""Evaluator registry and shared context.

Evaluator 只依赖归一化 RunResult/JSON、case contract 和 suite snapshot；不依赖
任何 runtime 私有对象。新增 evaluator 只需一个可 import 的注册模块与 suite 引用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import importlib
import inspect
from pathlib import Path
from typing import Any, Protocol


@dataclass
class EvaluationContext:
    suite: dict[str, Any]
    snapshot: dict[str, Any]
    cases: dict[str, Any]
    runs: list[dict[str, Any]]
    scores: dict[str, Any] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)


class Evaluator(Protocol):
    name: str
    version: str

    def evaluate(self, context: EvaluationContext) -> dict[str, Any]:
        ...


_REGISTRY: dict[str, type] = {}


def register(
    name: str,
    *,
    version: str | None = None,
    expose_scalar_metrics: bool = True,
):
    def decorator(cls):
        if name in _REGISTRY and _REGISTRY[name] is not cls:
            raise ValueError(f"evaluator 已注册：{name}")
        resolved_version = version or getattr(cls, "version", None)
        if not isinstance(resolved_version, str) or not resolved_version:
            raise ValueError(f"evaluator {name!r} 必须声明非空 version")
        cls.name = name
        cls.version = resolved_version
        cls.expose_scalar_metrics = expose_scalar_metrics
        _REGISTRY[name] = cls
        return cls
    return decorator


def _resolve_reference(reference: str) -> tuple[str, type]:
    if ":" in reference:
        module, name = reference.rsplit(":", 1)
        if not module or not name:
            raise ValueError(
                f"外部 evaluator 引用必须是 '<python.module>:<registered-name>'：{reference!r}"
            )
        importlib.import_module(module)
    else:
        name = reference
    try:
        cls = _REGISTRY[name]
    except KeyError as exc:
        hint = "；外部实现用 '<python.module>:<registered-name>'"
        raise ValueError(f"未知 evaluator {reference!r}；可用：{sorted(_REGISTRY)}{hint}") from exc
    return name, cls


def create_evaluator(reference: str, **options: Any) -> Evaluator:
    _name, cls = _resolve_reference(reference)
    return cls(**options)


def available() -> list[str]:
    return sorted(_REGISTRY)


def _source_sha256(cls: type) -> str | None:
    source = inspect.getsourcefile(cls)
    if not source:
        return None
    path = Path(source)
    if not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def evaluator_manifest(
    references: list[str],
    options: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve evaluators and fingerprint the scoring code used by this report."""
    configured = options or {}
    manifest = {}
    for reference in references:
        name, cls = _resolve_reference(reference)
        configured_options = configured.get(reference) or {}
        try:
            inspect.signature(cls).bind(**configured_options)
        except TypeError as error:
            raise ValueError(
                f"evaluator {reference!r} options 与构造器不匹配：{error}"
            ) from error
        manifest[reference] = {
            "name": name,
            "module": cls.__module__,
            "class": cls.__qualname__,
            "version": cls.version,
            "source_sha256": _source_sha256(cls),
            "options": configured_options,
            "expose_scalar_metrics": bool(cls.expose_scalar_metrics),
        }
    return manifest


def evaluate_all(
    names: list[str],
    context: EvaluationContext,
    options: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    configured = options or {}
    return {
        reference: create_evaluator(
            reference, **(configured.get(reference) or {})
        ).evaluate(context)
        for reference in names
    }


def scalar_metrics(
    layers: dict[str, dict[str, Any]],
    references: list[str] | None = None,
) -> dict[str, float | None]:
    """Expose scalar evaluator metrics to gates; reject ambiguous collisions."""
    merged: dict[str, float | None] = {}
    owners: dict[str, str] = {}
    exposed = None if references is None else {
        reference
        for reference in references
        if bool(_resolve_reference(reference)[1].expose_scalar_metrics)
    }
    for layer_name, layer in layers.items():
        if exposed is not None and layer_name not in exposed:
            continue
        for metric, value in (layer.get("metrics") or {}).items():
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                continue
            normalized = None if value is None else float(value)
            if metric in merged and merged[metric] != normalized:
                raise ValueError(
                    f"evaluator metric 冲突：{metric!r} 由 {owners[metric]!r}="
                    f"{merged[metric]!r} 与 {layer_name!r}={normalized!r} 同时提供"
                )
            merged[metric] = normalized
            owners[metric] = layer_name
    return merged
