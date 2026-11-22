"""Fault catalogue and injection engine for the five stage-1 fault classes.

Each fault is deterministic and reversible: enabling writes a FaultConfig row;
the business path checks for an active config at the point of execution;
disabling deletes/resets it. Every injection records an eval EventLog entry so
experiments can prove the fault actually fired (stage 1 acceptance: 可重复实验).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.core.errors import DomainError

from .eval_models import EventLog, FaultConfig

# The five faults mandated by v1.1 stage 1.
TF_API_001 = "TF-API-001"  # API 异常：创建接口返回 500
TF_NET_001 = "TF-NET-001"  # 网络超时：请求延迟或超时
TF_DB_001 = "TF-DB-001"  # 持久化失败：事务回滚/写入失败
TF_UI_001 = "TF-UI-001"  # UI 表现异常：成功提示缺失/错误显示
TF_TIME_001 = "TF-TIME-001"  # 异步时序异常：列表刷新延迟/竞态

ALL_FAULTS = (TF_API_001, TF_NET_001, TF_DB_001, TF_UI_001, TF_TIME_001)


@dataclass(frozen=True)
class FaultSpec:
    fault_id: str
    label: str
    default_params: dict[str, Any]


FAULT_SPECS: dict[str, FaultSpec] = {
    TF_API_001: FaultSpec(TF_API_001, "API 异常", {"http_status": 500}),
    TF_NET_001: FaultSpec(TF_NET_001, "网络超时", {"delay_ms": 3000}),
    TF_DB_001: FaultSpec(TF_DB_001, "持久化失败", {}),
    TF_UI_001: FaultSpec(TF_UI_001, "UI 表现异常", {"mode": "missing_success"}),
    TF_TIME_001: FaultSpec(TF_TIME_001, "异步时序异常", {"delay_ms": 2500}),
}


class InjectedApiError(DomainError):
    """Raised by the business layer when TF-API-001 is armed."""

    code = "TF_API_FAULT"
    status_code = 500

    def __init__(self, params: dict[str, Any]) -> None:
        super().__init__(
            "Injected API fault (TF-API-001)",
            details={"fault_id": TF_API_001, "http_status": params.get("http_status", 500)},
        )


def set_fault(
    session: Session, fault_id: str, enabled: bool, params: dict[str, Any] | None
) -> FaultConfig:
    if fault_id not in FAULT_SPECS:
        raise DomainError(f"unknown fault_id: {fault_id}", details={"fault_id": fault_id})
    row = session.get(FaultConfig, fault_id)
    if row is None:
        row = FaultConfig(fault_id=fault_id)
        session.add(row)
    row.enabled = enabled
    merged = dict(FAULT_SPECS[fault_id].default_params)
    if params:
        merged.update(params)
    row.params_json = json.dumps(merged, ensure_ascii=False)
    session.flush()
    _log(session, fault_id, "ARMED" if enabled else "DISARMED", row.params_json)
    session.commit()
    return row


def get_active_fault(session: Session, fault_id: str) -> dict[str, Any] | None:
    row = session.get(FaultConfig, fault_id)
    if row is None or not row.enabled:
        return None
    return json.loads(row.params_json)


def maybe_apply_network_delay(session: Session) -> None:
    params = get_active_fault(session, TF_NET_001)
    if params:
        delay = float(params.get("delay_ms", 3000)) / 1000
        _log(session, TF_NET_001, "DELAY_APPLIED", f"{delay}s")
        session.commit()
        time.sleep(delay)


def clear_all_faults(session: Session) -> int:
    rows = session.execute(select(FaultConfig)).scalars().all()
    for row in rows:
        row.enabled = False
        _log(session, row.fault_id, "CLEARED", None)
    session.commit()
    return len(rows)


def list_faults(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(select(FaultConfig)).scalars().all()
    by_id = {r.fault_id: r for r in rows}
    result = []
    for spec in FAULT_SPECS.values():
        row = by_id.get(spec.fault_id)
        result.append(
            {
                "fault_id": spec.fault_id,
                "label": spec.label,
                "enabled": bool(row and row.enabled),
                "params": json.loads(row.params_json) if row else spec.default_params,
            }
        )
    return result


def ui_success_hidden(session: Session) -> bool:
    params = get_active_fault(session, TF_UI_001)
    return bool(params and params.get("mode") == "missing_success")


def list_refresh_delayed(session: Session) -> int:
    params = get_active_fault(session, TF_TIME_001)
    return int(params.get("delay_ms", 0)) if params else 0


def _log(session: Session, fault_id: str, outcome: str, detail: str | None) -> None:
    session.add(EventLog(fault_id=fault_id, outcome=outcome, detail=detail))
