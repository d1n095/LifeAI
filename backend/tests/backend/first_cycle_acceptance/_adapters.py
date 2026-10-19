"""Bind to production modules when they exist. Never invent a missing interface."""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass
from types import ModuleType


@dataclass(frozen=True)
class BoundModule:
    name: str
    module: ModuleType | None
    symbols: dict[str, object]

    @property
    def available(self) -> bool:
        return self.module is not None


def try_import(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        return None


def symbol(module: ModuleType | None, *names: str):
    if module is None:
        return None
    for name in names:
        if hasattr(module, name):
            return getattr(module, name)
    return None


def bind(name: str, *wanted: str) -> BoundModule:
    module = try_import(name)
    found = {item: symbol(module, item) for item in wanted}
    return BoundModule(name=name, module=module, symbols=found)


PRODUCTION = {
    "envelope_scope": bind(
        "app.mainai_execution.envelope_scope",
        "assert_changed_files_within_authority",
        "PathAEnvelopeError",
    ),
    "worktree": bind(
        "app.mainai_execution.worktree",
        "_authed_remote_url",
        "commit_worktree_changes",
        "push_worktree_branch",
        "git_changed_paths_no_renames",
        "create_task_worktree",
        "worktree_changed_files",
    ),
    "ci_wait": bind(
        "app.mainai_execution.ci_wait",
        "evaluate_check_runs",
        "evaluate_required_check_runs",
        "start_ci_wait",
        "poll_ci_wait",
    ),
    "required_checks": bind(
        "app.mainai_execution.required_checks",
        "ACTIVATION_REQUIRED_CHECKS",
        "evaluate_required_check_runs",
        "required_checks_payload",
    ),
    "approval": bind(
        "app.mainai_execution.approval",
        "grant_task_approval",
        "require_task_approval",
        "ApprovalRequiredError",
    ),
    "spend": bind(
        "app.provider_spend",
        "authorize_provider_spend",
        "reserve_provider_spend_call",
        "ProviderSpendError",
    ),
    "billed_chat": bind(
        "app.mainai_execution.billed_chat",
        "billed_execution_chat",
        "ExecutionSpendError",
    ),
    "pricing": bind(
        "app.providers.pricing",
        "estimate_cost",
        "token_rates_per_1k",
    ),
    "replan": bind(
        "app.mainai_execution.replan",
        "find_replan_trigger",
        "trigger_replan",
        "ReplanSuppressedError",
    ),
    "review_receipt": bind(
        "app.mainai_execution.review_receipt",
        "evaluate_first_cycle_review_receipt",
        "record_first_cycle_review_receipt",
        "PATH_A_BUILDER_IDENTITY",
        "STATE_STOP_BEFORE_MERGE",
        "STATE_INDEPENDENT_REVIEW_BLOCKED",
        "publication_truth",
    ),
    "first_cycle_stop": bind(
        "app.mainai_execution.first_cycle_stop",
        "stop_first_cycle",
        "fail_closed_external_ci",
        "should_suppress_paid_replan",
        "STATE_STOP_BEFORE_MERGE",
        "STATE_EXTERNAL_CI_FAILED",
    ),
    "push_grants": bind(
        "app.mainai_execution.push_grants",
        "issue_push_grant",
        "consume_push_grant",
        "require_remote_effect_authority",
    ),
    "activation_security": bind(
        "app.mainai_execution.activation_security",
        "issue_push_grant",
        "consume_push_grant",
        "require_remote_effect_authority",
    ),
    "remote_effect": bind(
        "app.mainai_execution.remote_effect",
        "require_remote_effect_authority",
        "issue_push_grant",
        "issue_pr_grant",
    ),
}


def production_grant_module() -> BoundModule | None:
    for key in ("push_grants", "activation_security", "remote_effect"):
        bound = PRODUCTION[key]
        if bound.available:
            return bound
    return None


def source_contains(fn, needle: str) -> bool | None:
    if fn is None:
        return None
    try:
        text = inspect.getsource(fn)
    except (OSError, TypeError):
        return None
    return needle in text
