import pytest
from sqlalchemy import text

from app.mainai_execution.supervision_spend import SupervisionSpend
from tests.backend.supervision_support import claim,runtime,seed,spend_grant
from tests.backend.test_supervision_process_crashes import crash


@pytest.mark.parametrize('operation',['reserve','bind_budget','settle'])
@pytest.mark.parametrize('when',['uncommitted','committed'])
def test_budget_sigkill_does_not_strand_canonical_reservation(superuser_db,tmp_path,operation,when):
    f=seed(superuser_db,tmp_path)
    grant_id=spend_grant(superuser_db,f)
    at=claim(f)
    usage=None
    if operation!='reserve':
        with runtime(f['owner'],tmp_path) as s:
            usage=SupervisionSpend(s).reserve(f['owner'],at)
        if operation=='settle':
            with runtime(f['owner'],tmp_path) as s:
                SupervisionSpend(s).dispatched(f['owner'],usage)
    crash(f,at,operation,when,usage=str(usage))
    with runtime(f['owner'],tmp_path) as s:
        s.db.execute(text("UPDATE mainai_supervision_spend_links SET expires_at=clock_timestamp()-interval '1 second' WHERE owner_id=:o"),dict(o=f['owner']))
        SupervisionSpend(s).reconcile(f['owner'])
        SupervisionSpend(s).reconcile(f['owner'])
    superuser_db.expire_all()
    row=superuser_db.execute(text('SELECT * FROM provider_spend_authorizations WHERE id=:id'),dict(id=grant_id)).mappings().one()
    assert row['reserved_cost_usd']==0 and row['reserved_requests']==0
    if operation=='settle' or operation=='bind_budget' and when=='committed':
        assert row['spent_requests']==1 and row['spent_cost_usd']>0
    else:
        assert row['spent_requests']==0


def test_cost_lies_and_duplicate_settlement_are_bounded(superuser_db,tmp_path):
    f=seed(superuser_db,tmp_path)
    grant_id=spend_grant(superuser_db,f)
    at=claim(f)
    with runtime(f['owner'],tmp_path) as s:
        spend=SupervisionSpend(s)
        usage=spend.reserve(f['owner'],at)
        assert spend.reserve(f['owner'],at)==usage
        spend.dispatched(f['owner'],usage)
        assert spend.settle(f['owner'],usage,reported='0',tokens=100,price='1')=='uncertain'
        assert spend.settle(f['owner'],usage,reported='0',tokens=100,price='1')=='uncertain'
    row=superuser_db.execute(text('SELECT * FROM provider_spend_authorizations WHERE id=:id'),dict(id=grant_id)).mappings().one()
    assert str(row['spent_cost_usd'])=='0.100000' and row['spent_requests']==1 and row['reserved_requests']==0
