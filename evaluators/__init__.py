"""Evaluator registry. Built-ins are imported for self-registration."""
from .base import EvaluationContext, Evaluator, available, create_evaluator, evaluate_all, register
from . import efficiency as _efficiency  # noqa: F401
from . import outcome as _outcome  # noqa: F401
from . import reliability as _reliability  # noqa: F401
from . import trajectory as _trajectory  # noqa: F401

__all__ = [
    "EvaluationContext", "Evaluator", "available", "create_evaluator", "evaluate_all", "register",
]
