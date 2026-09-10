"""A disposable worker killed by SIGKILL around actual PostgreSQL operations.

Only test code invokes this entry point; no provider or production startup wiring.
"""
import json
import os
import signal
import sys
import uuid
from pathlib import Path

from sqlalchemy import text

from app.mainai_execution.supervision_spend import SupervisionSpend
from tests.backend.supervision_support import runtime


def kill():
    os.kill(os.getpid(),signal.SIGKILL)


def main():
    cfg=json.loads(Path(sys.argv[1]).read_text())
    owner=uuid.UUID(cfg['owner'])
    attempt=uuid.UUID(cfg['attempt'])
    operation=cfg['operation']
    when=cfg['when']
    with runtime(owner,cfg['root']) as s:
        if when=='before':
            kill()
        if operation=='idle':
            s.observe(owner,attempt,seq=100,state='IDLE',session_state='prompt',process_nonce='process1')
        elif operation=='cancel':
            s.cancel(owner,uuid.UUID(cfg['job']))
        elif operation=='result':
            s.submit_result(owner,attempt,result_id='crash-result',reported_sha=cfg['sha'],remaining=['value_changed'])
        elif operation=='evaluate':
            s.tick(owner)
        elif operation=='delivery':
            messages=s.prepare_delivery(owner)
            assert len(messages)==1
        elif operation=='receive':
            s.receive(owner,uuid.UUID(cfg['message']))
        elif operation=='ack':
            s.ack(owner,uuid.UUID(cfg['message']),uuid.UUID(cfg['token']))
        elif operation=='freeze':
            s.result(owner,attempt,result_id='freeze',reported_sha=cfg['sha'],remaining=[],test_ids=[uuid.UUID(cfg['test'])])
        elif operation=='examiner':
            s.assign_examiner(owner,attempt,uuid.UUID(cfg['examiner']))
        elif operation in ('pass','fail'):
            s.review(owner,attempt,examiner_assignment=uuid.UUID(cfg['examiner']),sha=cfg['sha'],passed=operation=='pass',verdict_id='crash-review')
        elif operation=='reserve':
            SupervisionSpend(s).reserve(owner,attempt)
        elif operation=='bind_budget':
            SupervisionSpend(s).dispatched(owner,uuid.UUID(cfg['usage']))
        elif operation=='settle':
            SupervisionSpend(s).settle(owner,uuid.UUID(cfg['usage']),reported='0.01',tokens=10,price='0.001')
        elif operation=='claim':
            s.claim(owner,uuid.UUID(cfg['job']),agent_id='crash-worker',process_nonce='process1')
        else:
            raise ValueError(operation)
        s.db.flush()
        if when=='uncommitted':
            kill()
        s.db.commit()
        # This marker is never authority: it proves the child reached the boundary.
        with runtime(owner,cfg['root']) as reader:
            reader.db.execute(text('SELECT 1'))
        kill()


if __name__=='__main__':
    main()
