"""Seed the five stage-1 benchmark scenarios with Ground Truth.

Ground Truth is recorded on the EVAL plane only (never exposed to business/Agent).
Run: python -m scripts.seed_scenarios
"""

from __future__ import annotations

from apps.teamflow import faults, service
from apps.teamflow.eval_models import Base as EvalBase
from apps.teamflow.models import Base as BusinessBase
from packages.core import database
from packages.core.config import get_settings

SCENARIOS = [
    (
        "TF-BASE-001",
        None,
        "基线：正常创建项目，无故障",
        {"expected_project_count": 1, "expected_verdict": "PASSED"},
    ),
    (
        "TF-API-001-S1",
        faults.TF_API_001,
        "创建接口返回 500",
        {"expected_project_count": 0, "expected_verdict": "FAILED"},
    ),
    (
        "TF-NET-001-S1",
        faults.TF_NET_001,
        "请求延迟/超时",
        {"expected_project_count": 1, "expected_verdict": "PASSED"},
    ),
    (
        "TF-DB-001-S1",
        faults.TF_DB_001,
        "持久化失败事务回滚",
        {"expected_project_count": 0, "expected_verdict": "FAILED"},
    ),
    (
        "TF-UI-001-S1",
        faults.TF_UI_001,
        "成功提示缺失但业务成功",
        {"expected_project_count": 1, "expected_verdict": "PASSED"},
    ),
    (
        "TF-TIME-001-S1",
        faults.TF_TIME_001,
        "列表刷新延迟",
        {"expected_project_count": 1, "expected_verdict": "PASSED"},
    ),
]

META = {
    "teamflow_version": "0.1.0-m1",
    "data_seed": "seed-1",
    "injection_config": {},
    "cleanup_method": "POST /__eval__/data/reset",
}


def main() -> None:
    settings = get_settings()
    engine = database.create_db_engine(settings.database_url)
    BusinessBase.metadata.create_all(engine)
    EvalBase.metadata.create_all(engine)
    factory = database.make_session_factory(engine)
    with factory() as session:
        for sid, fid, desc, gt in SCENARIOS:
            meta = dict(META)
            meta["fault_id"] = fid or "NONE"
            service.seed_scenario(session, sid, fid, desc, meta, gt)
    engine.dispose()
    print(f"OK: seeded {len(SCENARIOS)} scenarios with Ground Truth (eval plane only)")


if __name__ == "__main__":
    main()
