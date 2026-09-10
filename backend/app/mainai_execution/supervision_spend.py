"""Supervision links to provider_spend's canonical ledger, never a second budget.

An undispatched hold can be released. Unknown effects after dispatch are charged
at the reserved upper bound with UNCERTAIN evidence; expiry never invents a free
retry. No external billing provider is invoked by this module.
"""
from decimal import Decimal

from sqlalchemy import select, text

from app.mainai_execution.canonical_supervisor import Rejected
from app.models.provider_spend import ProviderSpendAuthorization, ProviderSpendUsageEvent
from app.provider_spend.service import reserve_provider_spend_call, release_provider_spend_call, settle_provider_spend_call


class SupervisionSpend:
    def __init__(self, supervisor):
        self.s=supervisor
        self.db=supervisor.db

    def reserve(self,owner,attempt_id):
        job,_,_,assignment,profile=self.s.current(owner,attempt_id)
        auth=self.db.scalar(select(ProviderSpendAuthorization).where(ProviderSpendAuthorization.id==assignment.provider_spend_authorization_id,ProviderSpendAuthorization.owner_id==owner).execution_options(populate_existing=True).with_for_update())
        if not auth or auth.status!='active':
            raise Rejected('SPEND_NOT_AUTHORIZED')
        event,_=reserve_provider_spend_call(self.db,owner_id=owner,goal_id=auth.goal_id,job_id=job.id,source_ref=f'supervision:{attempt_id}',provider=profile.provider_type,model=profile.provider_model_id,evidence={'attempt':str(attempt_id)})
        if event.authorization_id!=auth.id:
            raise Rejected('SPEND_GRANT_MISMATCH')
        self.db.execute(text('''INSERT INTO mainai_supervision_spend_links(usage_id,owner_id,job_id,attempt_id,expires_at)
          VALUES(:u,:o,:j,:a,clock_timestamp()+interval '120 seconds') ON CONFLICT(usage_id) DO NOTHING'''),dict(u=event.id,o=owner,j=job.id,a=attempt_id))
        self.s.journal(owner,job.id,'BUDGET_RESERVED',event.id,attempt_id=attempt_id)
        return event.id

    def dispatched(self,owner,usage_id):
        self.s.lock(owner)
        link=self.s.one('SELECT * FROM mainai_supervision_spend_links WHERE owner_id=:o AND usage_id=:u FOR UPDATE',o=owner,u=usage_id)
        self.s.current(owner,link['attempt_id'])
        if link['phase']!='reserved' or link['expires_at']<=self.s.now():
            raise Rejected('SPEND_ALREADY_DISPATCHED')
        self.db.execute(text("UPDATE mainai_supervision_spend_links SET phase='dispatched' WHERE owner_id=:o AND usage_id=:u"),dict(o=owner,u=usage_id))

    def settle(self,owner,usage_id,*,reported=None,tokens=None,price=None):
        self.s.lock(owner)
        link=self.s.one('SELECT * FROM mainai_supervision_spend_links WHERE owner_id=:o AND usage_id=:u FOR UPDATE',o=owner,u=usage_id)
        event=self.db.scalar(select(ProviderSpendUsageEvent).where(ProviderSpendUsageEvent.id==usage_id,ProviderSpendUsageEvent.owner_id==owner).execution_options(populate_existing=True))
        if link['phase'] in ('settled','released','uncertain'):
            return link['phase']
        # All inputs must be finite and nonnegative, independently-priced metadata
        # must agree; otherwise account conservatively at the reservation ceiling.
        verified=False
        if reported is not None and tokens is not None and price is not None:
            try:
                r,p=Decimal(str(reported)),Decimal(str(price))
                verified=r.is_finite() and p.is_finite() and r>=0 and p>=0 and isinstance(tokens,int) and 0<=tokens<=10**7 and r==p*tokens and r<=event.reserved_cost_usd
            except Exception:
                verified=False
        amount=r if verified else event.reserved_cost_usd
        classification='VERIFIED' if verified else 'UNCERTAIN'
        source_ref=event.source_ref
        self.db.expire_all()
        # Refresh the identity map after SQL ledger settlement before further budget reads.
        settle_provider_spend_call(self.db,owner_id=owner,source_ref=source_ref,cost_usd=amount,
                                   evidence={'classification':classification,'source':'supervision','attempt':str(link['attempt_id'])})
        self.db.expire_all()
        phase='settled' if verified else 'uncertain'
        self.db.execute(text('UPDATE mainai_supervision_spend_links SET phase=:p WHERE owner_id=:o AND usage_id=:u'),dict(p=phase,o=owner,u=usage_id))
        self.s.journal(owner,link['job_id'],'COST_RECONCILED',usage_id,classification=classification,recovery='CEILING_ACCOUNTED')
        return phase

    def reconcile(self,owner,*,limit=64):
        self.s.lock(owner)
        if not 1<=limit<=1000:
            raise Rejected('UNBOUNDED_RECONCILIATION')
        rows=self.s.rows("""SELECT * FROM mainai_supervision_spend_links WHERE owner_id=:o AND phase IN ('reserved','dispatched')
         AND expires_at<=clock_timestamp() ORDER BY expires_at LIMIT :n FOR UPDATE SKIP LOCKED""",o=owner,n=limit)
        for link in rows:
            event=self.db.get(ProviderSpendUsageEvent,link['usage_id'],populate_existing=True)
            if link['phase']=='reserved':
                release_provider_spend_call(self.db,owner_id=owner,source_ref=event.source_ref,evidence={'reason':'NOT_DISPATCHED'})
                self.db.execute(text("UPDATE mainai_supervision_spend_links SET phase='released' WHERE owner_id=:o AND usage_id=:u"),dict(o=owner,u=link['usage_id']))
                self.s.journal(owner,link['job_id'],'BUDGET_RELEASED',link['usage_id'],recovery='NO_DISPATCH_PROVEN')
            else:
                self.settle(owner,link['usage_id'])
        return len(rows)
