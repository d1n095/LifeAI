"""Opt-in event drain and provider-neutral local delivery; no startup registration."""
from contextlib import contextmanager
from typing import Protocol

from app.db import SessionLocal
from app.mainai_execution.canonical_supervisor import CanonicalSupervisor,Rejected
from app.request_context import current_user_id


class AgentDeliveryAdapter(Protocol):
    def send(self, *, owner, message_id):
        """Return only after durable receiver deduplication and authority validation."""


class SupervisionLoop:
    def __init__(self,root,*,enabled=False):
        self.root,self.enabled=root,enabled

    @contextmanager
    def transaction(self,owner):
        token=current_user_id.set(str(owner))
        try:
            with SessionLocal.begin() as db:
                yield CanonicalSupervisor(db,root=self.root,enabled=self.enabled)
        finally:
            current_user_id.reset(token)

    def run_once(self,owner,adapter: AgentDeliveryAdapter,*,limit=32):
        with self.transaction(owner) as s:
            decisions=s.tick(owner,limit=limit)
            idle=s.rows("SELECT * FROM mainai_supervision_agents WHERE owner_id=:o AND state='IDLE' AND job_id IS NULL ORDER BY updated_at LIMIT :n",o=owner,n=limit)
            for agent in idle:
                s.next_job(owner,agent_id=agent['agent_id'],process_nonce=agent['process_nonce'])
            deliveries=s.prepare_delivery(owner,limit=limit)
        for msg in deliveries:
            try:
                adapter.send(owner=owner,message_id=msg['id'])
                ok=True
            except Exception:
                ok=False
            with self.transaction(owner) as s:
                try:
                    s.ack(owner,msg['id'],msg['delivery_token'],success=ok)
                except Rejected:
                    pass  # Current cancellation/expiry always wins a late ack.
        return {'evaluated':len(decisions),'deliveries':len(deliveries)}


class LocalAgentSessions:
    """Deterministic local receiver. It records messages; invokes no real provider."""
    def __init__(self,loop):
        self.loop=loop

    def send(self,*,owner,message_id):
        with self.loop.transaction(owner) as s:
            return s.receive(owner,message_id)
