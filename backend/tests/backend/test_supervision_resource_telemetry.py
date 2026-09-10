import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from app.mainai_execution.canonical_supervisor import Rejected
from tests.backend.supervision_support import runtime, seed


def test_resource_context_cost_telemetry_is_bounded_and_owner_scoped(superuser_db, tmp_path):
    fixture = seed(superuser_db, tmp_path)
    owner = fixture['owner']
    with runtime(owner, tmp_path) as supervisor:
        telemetry_id = supervisor.record_telemetry(
            owner,
            agent_id='builder',
            state='RUNNING',
            provider='fake-local',
            model='planner-v2',
            productive_seconds=12.5,
            context_input_tokens=100,
            context_output_tokens=20,
            context_limit_tokens=1000,
            estimated_cost=0.02,
            validated_cost=0.01,
            continuation_count=1,
            handoff_ready=True,
            context_risk='LOW',
        )
        assert telemetry_id
        rows = supervisor.latest_telemetry(owner, agent_id='builder')
        assert rows[0]['provider'] == 'fake-local'
        assert rows[0]['context_limit_tokens'] == 1000
        assert rows[0]['validated_cost'] == Decimal('0.010000')

    other = uuid.uuid4()
    with runtime(other, tmp_path) as supervisor:
        assert supervisor.latest_telemetry(other, agent_id='builder') == []
        with pytest.raises(Rejected, match='INVALID_TELEMETRY'):
            supervisor.record_telemetry(other, agent_id='builder', state='RUNNING', estimated_cost=-1)


def test_resource_telemetry_rejects_future_and_unknown_values(superuser_db, tmp_path):
    owner = seed(superuser_db, tmp_path)['owner']
    with runtime(owner, tmp_path) as supervisor:
        with pytest.raises(Rejected, match='STALE_TELEMETRY'):
            supervisor.record_telemetry(owner, agent_id='builder', state='IDLE', observed_at=supervisor.now() + timedelta(minutes=2))
        with pytest.raises(Rejected, match='INVALID_TELEMETRY'):
            supervisor.record_telemetry(owner, agent_id='builder', state='IDLE', unexpected='value')
