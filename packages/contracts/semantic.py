"""Cross-field semantic validation for C01~C12.

Structural JSON Schema cannot express "every required assertion must have an
Oracle binding" or "a CONFIRMED diagnosis must name a confirmation rule".
Those are the rules that actually protect the platform, so they are coded here,
next to the domain vocabulary they reference.

Every rule below mirrors a constraint that exists in the runtime code, or a
stated invariant from the frozen Verdict / Diagnosis / Repair semantics.
Rules are additive: a contract with no rule simply passes.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any

from packages.agents.diagnosis import (
    DIAGNOSIS_STATUSES,
    DISPOSITIONS,
    FAILURE_DOMAINS,
    FAILURE_MECHANISMS,
)
from packages.agents.probes import RISK_LEVELS
from packages.evidence.graph import AUTO_EDGE_TYPES, NODE_TYPES
from packages.execution.models import OracleVerdict
from packages.execution.repair import ALLOWED_KINDS, FORBIDDEN_KINDS, STATES
from packages.execution.runner import ExecutionStatus
from packages.execution.verdict import TestVerdict

TERMINAL_EXECUTION = {"COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"}
# Repair states where an approval record must already exist (promotion cannot
# happen without human approval — see repair.py promote()).
APPROVED_STATES = {"APPROVED", "PROMOTING", "PROMOTED", "ROLLED_BACK"}
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FLAKY_VALUES = {
    "NOT_DETERMINED",
    "NOT_OBSERVED",
    "REPEATED_DIVERGENCE_OBSERVED",
    "REPEATABLY_STABLE",
}
EXPECTED_VERDICTS = {str(e.value) for e in OracleVerdict}
TEST_VERDICTS = {str(e.value) for e in TestVerdict}
EXEC_STATUSES = {str(e.value) for e in ExecutionStatus}
ALL_EDGE_RELATIONS = set(AUTO_EDGE_TYPES) | {"direct_causal"}


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


def _blank(v: Any) -> bool:
    return not isinstance(v, str) or not v.strip()


def _nonempty(value: Any, path: str, code: str, issues: list[Issue]) -> None:
    if _blank(value):
        issues.append(Issue(code, path, "must be a non-empty string"))


def _enum(value: Any, allowed: Collection[str], path: str, code: str, issues: list[Issue]) -> None:
    if value not in allowed:
        issues.append(Issue(code, path, f"must be one of {sorted(allowed)}; got {value!r}"))


def _as_list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


# --------------------------------------------------------------------------- C01


def _c01(doc: dict[str, Any]) -> list[Issue]:
    out: list[Issue] = []
    _nonempty(doc.get("intent_id"), "intent_id", "C01_ID_EMPTY", out)
    _nonempty(doc.get("description"), "description", "C01_DESC_EMPTY", out)
    return out


# --------------------------------------------------------------------------- C02


def _c02(doc: dict[str, Any]) -> list[Issue]:
    out: list[Issue] = []
    if isinstance(doc.get("version"), int) and doc["version"] < 1:
        out.append(Issue("C02_VERSION_RANGE", "version", "version must be >= 1"))

    steps = _as_list(doc.get("steps"))
    if not steps:
        out.append(Issue("C02_NO_STEPS", "steps", "a test case needs at least one step"))

    step_ids = [s.get("step_id") for s in steps if isinstance(s, dict)]
    if len(step_ids) != len(set(step_ids)):
        out.append(Issue("C02_STEP_ID_DUP", "steps", "step_id must be unique within a case"))

    assertions = [a for a in _as_list(doc.get("assertions")) if isinstance(a, dict)]
    if not assertions:
        out.append(Issue("C02_NO_ASSERTIONS", "assertions", "at least one assertion is required"))

    assertion_ids = [a.get("assertion_id") for a in assertions]
    if len(assertion_ids) != len(set(assertion_ids)):
        out.append(Issue("C02_ASSERTION_ID_DUP", "assertions", "assertion_id must be unique"))

    if not any(a.get("required") is True for a in assertions) and assertions:
        out.append(
            Issue(
                "C02_NO_REQUIRED_ASSERTION",
                "assertions",
                "at least one required assertion is needed for a meaningful verdict",
            )
        )

    bindings = [b for b in _as_list(doc.get("oracle_bindings")) if isinstance(b, dict)]
    bound = {b.get("assertion_id") for b in bindings}
    missing = [a for a in assertion_ids if a not in bound]
    for m in missing:
        out.append(
            Issue(
                "C02_ORACLE_BINDING_MISSING",
                f"assertions[{m}]",
                "every assertion must have exactly one oracle binding",
            )
        )
    known = {str(a) for a in assertion_ids}
    orphan = sorted(str(b) for b in bound if b is not None and str(b) not in known)
    for o in orphan:
        out.append(
            Issue(
                "C02_ORACLE_BINDING_ORPHAN",
                f"oracle_bindings[{o}]",
                "binding references an assertion_id that does not exist",
            )
        )
    if len(bindings) > len(assertions):
        out.append(Issue("C02_ORACLE_BINDING_DUP", "oracle_bindings", "one binding per assertion"))

    step_refs = {s.get("target") for s in steps if isinstance(s, dict)}
    for a in assertions:
        ref = a.get("source_ref")
        if isinstance(ref, str) and ref.startswith("ui."):
            step_id = ref[3:].split(".")[0]
            if (
                step_id
                and step_id not in {str(s) for s in step_refs}
                and steps
                and not any(step_id == s.get("step_id") for s in steps)
            ):
                out.append(
                    Issue(
                        "C02_ASSERTION_DANGLING_STEP",
                        f"assertions[{a.get('assertion_id')}].source_ref",
                        f"source_ref points at unknown step {step_id!r}",
                    )
                )
    return out


# --------------------------------------------------------------------------- C03


def _oracle_spec(spec: Any, path: str, out: list[Issue]) -> None:
    if not isinstance(spec, dict):
        out.append(Issue("C03_SPEC_NOT_OBJECT", path, "oracle spec must be an object"))
        return
    _nonempty(spec.get("oracle_id"), f"{path}.oracle_id", "C03_ORACLE_ID_EMPTY", out)
    _nonempty(spec.get("source_type"), f"{path}.source_type", "C03_SOURCE_TYPE_EMPTY", out)
    _nonempty(spec.get("source_ref"), f"{path}.source_ref", "C03_SOURCE_REF_EMPTY", out)
    _enum(spec.get("expected"), EXPECTED_VERDICTS, f"{path}.expected", "C03_EXPECTED_INVALID", out)


def _c03(doc: dict[str, Any]) -> list[Issue]:
    out: list[Issue] = []
    _nonempty(doc.get("assertion_id"), "assertion_id", "C03_ASSERTION_ID_EMPTY", out)
    primary = doc.get("primary")
    _oracle_spec(primary, "primary", out)

    supporting = [s for s in _as_list(doc.get("supporting"))]
    for i, s in enumerate(supporting):
        _oracle_spec(s, f"supporting[{i}]", out)

    if isinstance(primary, dict):
        ptype = primary.get("source_type")
        for i, s in enumerate(supporting):
            if (
                isinstance(s, dict)
                and s.get("source_type") == ptype
                and s.get("source_ref") == primary.get("source_ref")
            ):
                out.append(
                    Issue(
                        "C03_SUPPORTING_SAME_SOURCE",
                        f"supporting[{i}]",
                        "supporting oracle duplicates the primary source; not independent",
                    )
                )
    _nonempty(doc.get("adjudication_rule"), "adjudication_rule", "C03_ADJ_RULE_EMPTY", out)
    return out


# --------------------------------------------------------------------------- C04


def _c04(doc: dict[str, Any]) -> list[Issue]:
    out: list[Issue] = []
    status = doc.get("execution_status")
    _enum(status, EXEC_STATUSES, "execution_status", "C04_STATUS_INVALID", out)

    verdict = doc.get("test_verdict")
    if verdict not in ("", None):
        _enum(verdict, TEST_VERDICTS, "test_verdict", "C04_VERDICT_INVALID", out)

    started, finished = doc.get("started_at"), doc.get("finished_at")
    if status in TERMINAL_EXECUTION and _blank(finished):
        out.append(
            Issue(
                "C04_TERMINAL_WITHOUT_FINISH",
                "finished_at",
                "terminal execution status requires finished_at",
            )
        )
    if not _blank(started) and not _blank(finished) and str(finished) < str(started):
        out.append(Issue("C04_TIME_ORDER", "finished_at", "finished_at precedes started_at"))

    if status == "COMPLETED" and not _as_list(doc.get("steps")):
        out.append(Issue("C04_COMPLETED_WITHOUT_STEPS", "steps", "a completed run must have steps"))

    cv = doc.get("case_version")
    if isinstance(cv, int) and cv < 1:
        out.append(Issue("C04_CASE_VERSION_RANGE", "case_version", "case_version must be >= 1"))
    return out


# --------------------------------------------------------------------------- C05


def _c05(doc: dict[str, Any]) -> list[Issue]:
    out: list[Issue] = []
    _nonempty(doc.get("event_id"), "event_id", "C05_EVENT_ID_EMPTY", out)
    _nonempty(doc.get("event_type"), "event_type", "C05_EVENT_TYPE_EMPTY", out)
    _nonempty(doc.get("schema_version"), "schema_version", "C05_SCHEMA_VERSION_EMPTY", out)
    _nonempty(doc.get("producer_id"), "producer_id", "C05_PRODUCER_EMPTY", out)
    _nonempty(
        doc.get("producer_instance_id"),
        "producer_instance_id",
        "C05_PRODUCER_INSTANCE_EMPTY",
        out,
    )
    _nonempty(doc.get("idempotency_key"), "idempotency_key", "C05_IDEMPOTENCY_EMPTY", out)

    seq = doc.get("sequence")
    if isinstance(seq, int) and seq < 1:
        out.append(Issue("C05_SEQUENCE_RANGE", "sequence", "sequence starts at 1"))

    occurred, recorded = doc.get("occurred_at"), doc.get("recorded_at")
    if not _blank(occurred) and not _blank(recorded) and str(recorded) < str(occurred):
        out.append(Issue("C05_TIME_ORDER", "recorded_at", "recorded_at precedes occurred_at"))
    return out


# --------------------------------------------------------------------------- C06


def _c06(doc: dict[str, Any]) -> list[Issue]:
    out: list[Issue] = []
    _nonempty(doc.get("evidence_id"), "evidence_id", "C06_EVIDENCE_ID_EMPTY", out)
    _nonempty(doc.get("evidence_type"), "evidence_type", "C06_TYPE_EMPTY", out)
    _nonempty(doc.get("source"), "source", "C06_SOURCE_EMPTY", out)
    _nonempty(doc.get("run_id"), "run_id", "C06_RUN_ID_EMPTY", out)
    _nonempty(doc.get("retention"), "retention", "C06_RETENTION_EMPTY", out)

    raw, derived = doc.get("raw"), _as_list(doc.get("derived_from"))
    if raw is True and derived:
        out.append(
            Issue("C06_RAW_HAS_PARENTS", "derived_from", "raw evidence cannot have derived_from")
        )
    if raw is False and not derived:
        out.append(
            Issue(
                "C06_DERIVED_WITHOUT_PARENTS", "derived_from", "derived evidence must cite sources"
            )
        )

    digest = doc.get("integrity_check")
    if not _blank(digest) and not SHA256_RE.match(str(digest)):
        out.append(Issue("C06_HASH_FORMAT", "integrity_check", "must be 'sha256:<64 hex chars>'"))
    return out


# --------------------------------------------------------------------------- C07


def _c07(doc: dict[str, Any]) -> list[Issue]:
    out: list[Issue] = []
    if "node_type" in doc:
        _enum(doc.get("node_type"), NODE_TYPES, "node_type", "C07_NODE_TYPE_INVALID", out)
        _nonempty(doc.get("node_id"), "node_id", "C07_NODE_ID_EMPTY", out)
        return out
    if "relation" in doc:
        relation = doc.get("relation")
        _enum(relation, ALL_EDGE_RELATIONS, "relation", "C07_RELATION_INVALID", out)
        _nonempty(doc.get("src"), "src", "C07_SRC_EMPTY", out)
        _nonempty(doc.get("dst"), "dst", "C07_DST_EMPTY", out)
        if doc.get("src") == doc.get("dst"):
            out.append(Issue("C07_SELF_EDGE", "dst", "an edge cannot point at itself"))
        if relation == "direct_causal" and _blank(doc.get("basis_fields")):
            out.append(
                Issue(
                    "C07_CAUSAL_NEEDS_BASIS",
                    "basis_fields",
                    "direct_causal requires explicit trusted basis",
                )
            )
        return out
    out.append(Issue("C07_SHAPE_UNKNOWN", "", "expected a node (node_type) or edge (relation)"))
    return out


# --------------------------------------------------------------------------- C08


def _c08(doc: dict[str, Any]) -> list[Issue]:
    out: list[Issue] = []
    _nonempty(doc.get("diagnosis_id"), "diagnosis_id", "C08_ID_EMPTY", out)
    _nonempty(doc.get("run_id"), "run_id", "C08_RUN_ID_EMPTY", out)
    _enum(doc.get("failure_domain"), FAILURE_DOMAINS, "failure_domain", "C08_DOMAIN_INVALID", out)
    _enum(
        doc.get("failure_mechanism"),
        FAILURE_MECHANISMS,
        "failure_mechanism",
        "C08_MECHANISM_INVALID",
        out,
    )
    status = doc.get("diagnosis_status")
    _enum(status, DIAGNOSIS_STATUSES, "diagnosis_status", "C08_STATUS_INVALID", out)
    _enum(doc.get("disposition"), DISPOSITIONS, "disposition", "C08_DISPOSITION_INVALID", out)
    _enum(
        doc.get("flaky_characteristic"),
        FLAKY_VALUES,
        "flaky_characteristic",
        "C08_FLAKY_INVALID",
        out,
    )

    hypothesis_list = [h for h in _as_list(doc.get("hypotheses")) if isinstance(h, dict)]
    for i, h in enumerate(hypothesis_list):
        _enum(
            h.get("failure_domain"),
            FAILURE_DOMAINS,
            f"hypotheses[{i}].failure_domain",
            "C08_HYP_DOMAIN_INVALID",
            out,
        )
        _enum(
            h.get("failure_mechanism"),
            FAILURE_MECHANISMS,
            f"hypotheses[{i}].failure_mechanism",
            "C08_HYP_MECHANISM_INVALID",
            out,
        )

    if status == "CONFIRMED":
        confirmed = [h for h in hypothesis_list if h.get("status") == "CONFIRMED"]
        if not confirmed:
            out.append(
                Issue(
                    "C08_CONFIRMED_WITHOUT_HYPOTHESIS",
                    "hypotheses",
                    "CONFIRMED requires at least one CONFIRMED hypothesis",
                )
            )
        for h in confirmed:
            if _blank(h.get("confirmation_rule")):
                out.append(
                    Issue(
                        "C08_CONFIRMED_WITHOUT_RULE",
                        f"hypotheses[{h.get('hypothesis_id')}].confirmation_rule",
                        "a CONFIRMED hypothesis must name its deterministic rule",
                    )
                )
        if not _as_list(doc.get("root_causes")):
            out.append(
                Issue(
                    "C08_CONFIRMED_WITHOUT_ROOT_CAUSE", "root_causes", "CONFIRMED needs root causes"
                )
            )
    if status == "INCONCLUSIVE" and doc.get("disposition") not in {
        "NEEDS_MORE_EVIDENCE",
        "MANUAL_REVIEW",
    }:
        out.append(
            Issue(
                "C08_INCONCLUSIVE_ROUTING",
                "disposition",
                "INCONCLUSIVE may only route to NEEDS_MORE_EVIDENCE or MANUAL_REVIEW",
            )
        )
    return out


# --------------------------------------------------------------------------- C09


def _c09(doc: dict[str, Any]) -> list[Issue]:
    out: list[Issue] = []
    if "executed" in doc:
        _nonempty(doc.get("probe_id"), "probe_id", "C09_PROBE_ID_EMPTY", out)
        if doc.get("executed") is True and not isinstance(doc.get("provenance"), dict):
            out.append(
                Issue("C09_PROVENANCE_MISSING", "provenance", "executed probe needs provenance")
            )
        return out
    if "workspace_id" in doc:
        _nonempty(doc.get("probe_id"), "probe_id", "C09_PROBE_ID_EMPTY", out)
        _nonempty(doc.get("workspace_id"), "workspace_id", "C09_SCOPE_EMPTY", out)
        if not isinstance(doc.get("params"), dict):
            out.append(Issue("C09_PARAMS_NOT_OBJECT", "params", "params must be an object"))
        return out
    if "read_only" in doc:
        _nonempty(doc.get("probe_id"), "probe_id", "C09_PROBE_ID_EMPTY", out)
        _enum(doc.get("risk_level"), RISK_LEVELS, "risk_level", "C09_RISK_INVALID", out)
        _nonempty(doc.get("permission_required"), "permission_required", "C09_PERM_EMPTY", out)
        if doc.get("read_only") is False:
            _nonempty(doc.get("side_effects"), "side_effects", "C09_SIDE_EFFECTS_UNDECLARED", out)
        if not isinstance(doc.get("input_schema"), dict):
            out.append(Issue("C09_INPUT_SCHEMA", "input_schema", "input_schema must be an object"))
        return out
    out.append(
        Issue("C09_SHAPE_UNKNOWN", "", "expected a ProbeSpec, ProbeRequest or ProbeResult shape")
    )
    return out


# --------------------------------------------------------------------------- C10


def _c10(doc: dict[str, Any]) -> list[Issue]:
    out: list[Issue] = []
    _enum(doc.get("disposition"), DISPOSITIONS, "disposition", "C10_DISPOSITION_INVALID", out)
    _enum(
        doc.get("diagnosis_status"),
        DIAGNOSIS_STATUSES,
        "diagnosis_status",
        "C10_STATUS_INVALID",
        out,
    )
    _nonempty(doc.get("diagnosis_id"), "diagnosis_id", "C10_DIAG_ID_EMPTY", out)
    if (
        doc.get("diagnosis_status") == "CONFIRMED"
        and doc.get("disposition") == "NEEDS_MORE_EVIDENCE"
    ):
        out.append(
            Issue(
                "C10_CONFIRMED_NEEDS_EVIDENCE",
                "disposition",
                "a CONFIRMED diagnosis cannot be routed as NEEDS_MORE_EVIDENCE",
            )
        )
    if doc.get("diagnosis_status") == "INCONCLUSIVE" and doc.get("disposition") in {
        "PRODUCT_DEFECT",
        "PLATFORM_ISSUE",
        "TEST_ASSET_ISSUE",
        "TEST_EXECUTION_ISSUE",
        "AGENT_ISSUE",
        "ENVIRONMENT_ISSUE",
    }:
        out.append(
            Issue(
                "C10_INCONCLUSIVE_BLAMES",
                "disposition",
                "INCONCLUSIVE must not be routed to a blame-bearing disposition",
            )
        )
    return out


# --------------------------------------------------------------------------- C11


def _c11(doc: dict[str, Any]) -> list[Issue]:
    out: list[Issue] = []
    state = doc.get("state")
    _enum(state, STATES, "state", "C11_STATE_INVALID", out)

    kinds = _as_list(doc.get("change_kinds"))
    forbidden = sorted(set(kinds) & FORBIDDEN_KINDS)
    for k in forbidden:
        out.append(Issue("C11_FORBIDDEN_CHANGE", "change_kinds", f"{k} is never an allowed repair"))
    unknown = sorted(set(kinds) - ALLOWED_KINDS - FORBIDDEN_KINDS)
    for k in unknown:
        out.append(
            Issue("C11_UNKNOWN_CHANGE", "change_kinds", f"{k} is not a recognized change kind")
        )

    for i, d in enumerate(_as_list(doc.get("diff"))):
        if isinstance(d, dict):
            _enum(d.get("kind"), ALLOWED_KINDS, f"diff[{i}].kind", "C11_DIFF_KIND_INVALID", out)

    base = doc.get("base_testcase_version")
    cand = doc.get("candidate_testcase_version")
    target = doc.get("promotion_target_version")
    if isinstance(base, int) and isinstance(cand, int) and cand <= base:
        out.append(
            Issue(
                "C11_CANDIDATE_NOT_NEWER",
                "candidate_testcase_version",
                "candidate must be a new version above base",
            )
        )
    if isinstance(cand, int) and isinstance(target, int) and target < cand:
        out.append(
            Issue(
                "C11_TARGET_BELOW_CANDIDATE",
                "promotion_target_version",
                "promotion target cannot be below the candidate version",
            )
        )

    if state in APPROVED_STATES:
        _nonempty(doc.get("approved_by"), "approved_by", "C11_APPROVAL_MISSING", out)
        _nonempty(doc.get("approval_record"), "approval_record", "C11_APPROVAL_MISSING", out)
    if state == "PROMOTED" and not isinstance(doc.get("promotion_record"), dict):
        out.append(
            Issue("C11_PROMOTION_RECORD_MISSING", "promotion_record", "PROMOTED needs a record")
        )
    if state in {"VALIDATION_FAILED", "PROMOTION_FAILED", "REJECTED"}:
        _nonempty(doc.get("failure_reason"), "failure_reason", "C11_FAILURE_REASON_MISSING", out)
    if state != "DRAFT" and not kinds:
        out.append(
            Issue(
                "C11_NO_CHANGE_KINDS", "change_kinds", "a non-draft proposal must declare changes"
            )
        )
    return out


# --------------------------------------------------------------------------- C12


def _c12(doc: dict[str, Any]) -> list[Issue]:
    out: list[Issue] = []
    if "expected_verdict" in doc:
        _nonempty(doc.get("scenario_id"), "scenario_id", "C12_SCENARIO_EMPTY", out)
        _nonempty(doc.get("primary_cause"), "primary_cause", "C12_CAUSE_EMPTY", out)
        _nonempty(doc.get("dataset_version"), "dataset_version", "C12_DATASET_VERSION_EMPTY", out)
        _enum(
            doc.get("expected_verdict"),
            TEST_VERDICTS,
            "expected_verdict",
            "C12_VERDICT_INVALID",
            out,
        )
        return out
    if "track" in doc:
        status = doc.get("status")
        if status not in {"COMPLETED", "NOT_RUN", "PARTIAL"}:
            out.append(
                Issue(
                    "C12_EXPERIMENT_STATUS",
                    "status",
                    "experiment status must be COMPLETED, PARTIAL or NOT_RUN",
                )
            )
        if status == "NOT_RUN" and doc.get("metrics"):
            out.append(
                Issue(
                    "C12_NOT_RUN_HAS_METRICS",
                    "metrics",
                    "a NOT_RUN experiment must not report metrics (no fabricated numbers)",
                )
            )
        count = doc.get("scenario_count")
        if isinstance(count, int) and count < 0:
            out.append(Issue("C12_SCENARIO_COUNT", "scenario_count", "cannot be negative"))
        _nonempty(doc.get("dataset_id"), "dataset_id", "C12_DATASET_ID_EMPTY", out)
        _nonempty(doc.get("method"), "method", "C12_METHOD_EMPTY", out)
        return out
    if "dataset_id" in doc:
        _nonempty(doc.get("dataset_id"), "dataset_id", "C12_DATASET_ID_EMPTY", out)
        _nonempty(doc.get("partition"), "partition", "C12_PARTITION_EMPTY", out)
        _nonempty(doc.get("version"), "version", "C12_VERSION_EMPTY", out)
        ids = _as_list(doc.get("scenario_ids"))
        if len(ids) != len(set(ids)):
            out.append(Issue("C12_DATASET_DUP", "scenario_ids", "scenario_ids must be unique"))
        return out
    if "numerator" in doc:
        for f in (
            "name",
            "unit",
            "numerator",
            "denominator",
            "exclusions",
            "missing_value",
            "multi_root",
        ):
            _nonempty(doc.get(f), f, "C12_METRIC_FIELD_EMPTY", out)
        return out
    out.append(Issue("C12_SHAPE_UNKNOWN", "", "unrecognized benchmark record shape"))
    return out


VALIDATORS: dict[str, Callable[[dict[str, Any]], list[Issue]]] = {
    "C01": _c01,
    "C02": _c02,
    "C03": _c03,
    "C04": _c04,
    "C05": _c05,
    "C06": _c06,
    "C07": _c07,
    "C08": _c08,
    "C09": _c09,
    "C10": _c10,
    "C11": _c11,
    "C12": _c12,
}


def validate_semantics(contract_id: str, instance: dict[str, Any]) -> list[Issue]:
    """Run cross-field rules. Empty list means no semantic violation found."""
    fn = VALIDATORS.get(contract_id)
    if fn is None:
        return [Issue("SEM_UNKNOWN_CONTRACT", "", f"no semantic rules for {contract_id}")]
    return fn(instance)


# Human-readable index of every rule above. Kept next to the implementation so a
# reviewer can enumerate what is actually enforced (and the report can cite it).
SEMANTIC_RULES: dict[str, tuple[tuple[str, str], ...]] = {
    "C01": (
        ("C01_ID_EMPTY", "intent_id 必填非空"),
        ("C01_DESC_EMPTY", "description 必填非空"),
    ),
    "C02": (
        ("C02_VERSION_RANGE", "version >= 1"),
        ("C02_NO_STEPS", "至少一个 step"),
        ("C02_STEP_ID_DUP", "step_id 用例内唯一"),
        ("C02_NO_ASSERTIONS", "至少一个 assertion"),
        ("C02_ASSERTION_ID_DUP", "assertion_id 唯一"),
        ("C02_NO_REQUIRED_ASSERTION", "至少一个 required 断言"),
        ("C02_ORACLE_BINDING_MISSING", "每个断言必须有且仅有一个 Oracle 绑定"),
        ("C02_ORACLE_BINDING_ORPHAN", "绑定不得引用不存在的断言"),
        ("C02_ORACLE_BINDING_DUP", "绑定数不得超过断言数"),
        ("C02_ASSERTION_DANGLING_STEP", "UI 断言的 source_ref 必须指向真实 step"),
    ),
    "C03": (
        ("C03_SPEC_NOT_OBJECT", "Oracle 规格必须是对象"),
        ("C03_ASSERTION_ID_EMPTY", "绑定必须指向非空 assertion_id"),
        ("C03_ORACLE_ID_EMPTY", "oracle_id 非空"),
        ("C03_SOURCE_TYPE_EMPTY", "source_type 非空"),
        ("C03_SOURCE_REF_EMPTY", "source_ref 非空"),
        ("C03_EXPECTED_INVALID", "expected 必须属于 OracleVerdict"),
        ("C03_SUPPORTING_SAME_SOURCE", "supporting 不得与 primary 同源（伪独立佐证）"),
        ("C03_ADJ_RULE_EMPTY", "冲突裁决规则必须显式声明"),
    ),
    "C04": (
        ("C04_STATUS_INVALID", "execution_status 必须属于 ExecutionStatus"),
        ("C04_VERDICT_INVALID", "test_verdict 必须属于四态 TestVerdict"),
        ("C04_TERMINAL_WITHOUT_FINISH", "终态必须有 finished_at"),
        ("C04_TIME_ORDER", "finished_at 不得早于 started_at"),
        ("C04_COMPLETED_WITHOUT_STEPS", "COMPLETED 必须有步骤记录"),
        ("C04_CASE_VERSION_RANGE", "case_version >= 1"),
    ),
    "C05": (
        ("C05_EVENT_ID_EMPTY", "event_id 非空"),
        ("C05_EVENT_TYPE_EMPTY", "event_type 非空"),
        ("C05_SCHEMA_VERSION_EMPTY", "schema_version 非空"),
        ("C05_PRODUCER_EMPTY", "producer_id 非空"),
        ("C05_PRODUCER_INSTANCE_EMPTY", "producer_instance_id 非空（重启换新）"),
        ("C05_IDEMPOTENCY_EMPTY", "幂等键必填"),
        ("C05_SEQUENCE_RANGE", "sequence 从 1 起"),
        ("C05_TIME_ORDER", "recorded_at 不得早于 occurred_at"),
    ),
    "C06": (
        ("C06_EVIDENCE_ID_EMPTY", "evidence_id 非空"),
        ("C06_TYPE_EMPTY", "evidence_type 非空"),
        ("C06_SOURCE_EMPTY", "source 非空"),
        ("C06_RUN_ID_EMPTY", "run_id 非空"),
        ("C06_RETENTION_EMPTY", "retention 必须显式声明"),
        ("C06_RAW_HAS_PARENTS", "原始证据不得有 derived_from"),
        ("C06_DERIVED_WITHOUT_PARENTS", "派生证据必须引用来源"),
        ("C06_HASH_FORMAT", "integrity_check 必须为 sha256:<64 hex>"),
    ),
    "C07": (
        ("C07_NODE_TYPE_INVALID", "node_type 必须属于 8 类节点"),
        ("C07_NODE_ID_EMPTY", "node_id 非空"),
        ("C07_RELATION_INVALID", "relation 必须属于 10 类自动边或 direct_causal"),
        ("C07_SRC_EMPTY", "边的 src 非空"),
        ("C07_DST_EMPTY", "边的 dst 非空"),
        ("C07_SELF_EDGE", "边不得自指"),
        ("C07_CAUSAL_NEEDS_BASIS", "direct_causal 必须有可信依据"),
        ("C07_SHAPE_UNKNOWN", "必须是节点或边之一"),
    ),
    "C08": (
        ("C08_ID_EMPTY", "diagnosis_id 非空"),
        ("C08_RUN_ID_EMPTY", "run_id 非空"),
        ("C08_DOMAIN_INVALID", "failure_domain 必须属于 7 类域"),
        ("C08_MECHANISM_INVALID", "failure_mechanism 必须属于 10 类机制"),
        ("C08_HYP_DOMAIN_INVALID", "假设的 failure_domain 必须受控"),
        ("C08_HYP_MECHANISM_INVALID", "假设的 failure_mechanism 必须受控"),
        ("C08_STATUS_INVALID", "diagnosis_status 三态"),
        ("C08_DISPOSITION_INVALID", "disposition 必须属于 8 类路由"),
        ("C08_FLAKY_INVALID", "flaky 特征取值受控"),
        ("C08_CONFIRMED_WITHOUT_HYPOTHESIS", "CONFIRMED 必须有已确认假设"),
        ("C08_CONFIRMED_WITHOUT_RULE", "已确认假设必须写明确定性规则"),
        ("C08_CONFIRMED_WITHOUT_ROOT_CAUSE", "CONFIRMED 必须有根因"),
        ("C08_INCONCLUSIVE_ROUTING", "INCONCLUSIVE 只能转补证或人工"),
    ),
    "C09": (
        ("C09_PROBE_ID_EMPTY", "probe_id 非空"),
        ("C09_RISK_INVALID", "risk_level 必须属于 LOW/MEDIUM/HIGH"),
        ("C09_PERM_EMPTY", "permission_required 非空"),
        ("C09_SIDE_EFFECTS_UNDECLARED", "非只读探针必须声明副作用"),
        ("C09_SCOPE_EMPTY", "ProbeRequest 必须带 workspace 作用域"),
        ("C09_PARAMS_NOT_OBJECT", "ProbeRequest.params 必须是对象"),
        ("C09_PROVENANCE_MISSING", "已执行探针必须带 provenance"),
        ("C09_INPUT_SCHEMA", "input_schema 必须是对象"),
        ("C09_SHAPE_UNKNOWN", "必须是 Spec/Request/Result 之一"),
    ),
    "C10": (
        ("C10_DISPOSITION_INVALID", "disposition 必须属于 8 类路由"),
        ("C10_STATUS_INVALID", "diagnosis_status 三态"),
        ("C10_DIAG_ID_EMPTY", "diagnosis_id 非空"),
        ("C10_CONFIRMED_NEEDS_EVIDENCE", "已确认不得路由为补证"),
        ("C10_INCONCLUSIVE_BLAMES", "未定论不得归责到具体主体"),
    ),
    "C11": (
        ("C11_STATE_INVALID", "state 必须属于 11 态"),
        ("C11_FORBIDDEN_CHANGE", "禁止改动 Oracle/删断言/降严格度/改产品代码"),
        ("C11_UNKNOWN_CHANGE", "变更类型必须受控"),
        ("C11_DIFF_KIND_INVALID", "diff 条目类型必须受控"),
        ("C11_CANDIDATE_NOT_NEWER", "候选版本必须高于基线"),
        ("C11_TARGET_BELOW_CANDIDATE", "晋升目标不得低于候选"),
        ("C11_APPROVAL_MISSING", "已批准态必须有批准人与批准记录"),
        ("C11_PROMOTION_RECORD_MISSING", "PROMOTED 必须有晋升记录"),
        ("C11_FAILURE_REASON_MISSING", "失败态必须写明原因"),
        ("C11_NO_CHANGE_KINDS", "非草稿提案必须声明变更"),
    ),
    "C12": (
        ("C12_SCENARIO_EMPTY", "scenario_id 非空"),
        ("C12_CAUSE_EMPTY", "primary_cause 非空"),
        ("C12_DATASET_VERSION_EMPTY", "GT 必须带数据集版本"),
        ("C12_VERDICT_INVALID", "expected_verdict 必须属于四态"),
        ("C12_EXPERIMENT_STATUS", "实验状态受控"),
        ("C12_NOT_RUN_HAS_METRICS", "NOT_RUN 不得带指标（禁止编造）"),
        ("C12_SCENARIO_COUNT", "场景数非负"),
        ("C12_DATASET_ID_EMPTY", "dataset_id 非空"),
        ("C12_METHOD_EMPTY", "method 非空"),
        ("C12_PARTITION_EMPTY", "partition 必填"),
        ("C12_VERSION_EMPTY", "数据集版本必填"),
        ("C12_DATASET_DUP", "scenario_ids 唯一"),
        ("C12_METRIC_FIELD_EMPTY", "指标口径字段齐全"),
        ("C12_SHAPE_UNKNOWN", "必须是 GT/实验/数据集/指标之一"),
    ),
}
