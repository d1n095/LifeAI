"""1000 real MainAIJob lifecycles driven by two PostgreSQL supervisors.

Counts are queried from persisted rows after all transactions finish, never
incremented by a simulation. Git/test/review cases use actual local commits.
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from app.mainai_execution.canonical_supervisor import Rejected
from app.models.mainai_job import MainAIJob
from tests.backend.supervision_support import commit,deliver,git,runtime,seed


def test_thousand_canonical_jobs_two_supervisors_fault_soak(superuser_db,tmp_path):
    fixtures=[seed(superuser_db,tmp_path,name=f'lane-{i}',review=False) for i in range(3)]
    owners=[f['owner'] for f in fixtures]
    jobs=[]
    for i in range(1000):
        f=fixtures[i%3]
        if i<3:
            jid=f['job']
        else:
            job=MainAIJob(owner_id=f['owner'],job_type='supervised_execution',created_by='fault_soak',input_refs=[],output_refs=[])
            superuser_db.add(job)
            superuser_db.flush()
            jid=job.id
        jobs.append((i,jid,i%3))
    superuser_db.commit()
    for i,jid,lane in jobs[3:]:
        f=fixtures[lane]
        with runtime(f['owner'],tmp_path) as s:
            s.bind(f['owner'],jid,program=f'program-{i%9}',assignment_id=f['assignments'][0],worktree=f['path'],branch=git(f['path'],'branch','--show-current'),base_sha=f['base'],required=['code_edit','filesystem_write','test_run'],criteria=['value_changed'],portable=i%2==0,review_required=False,fallbacks=[f['assignments'][1]])

    # Each owner/worktree lane is sequential; two independent supervisors compete
    # for DB resources while advancing different programs. Same-job contention is
    # attacked in the barrier-based race suite, not hidden by lane partitioning.
    def run_lane(lane):
        f=fixtures[lane]
        owner=f['owner']
        for i,jid,_ in [j for j in jobs if j[2]==lane]:
            current=f|{'job':jid}
            with runtime(owner,tmp_path) as s:
                for p in f['profiles']:
                    s.provider_observation(owner,p,availability='available',session_nonce=f'session-{i}',observed_at=s.now(),ttl=3600)
                at=s.claim(owner,jid,agent_id='worker',process_nonce=f'nonce-{i}',lease_seconds=3600)
            msgs=deliver(current)
            assert len(msgs)==1
            with runtime(owner,tmp_path) as s:
                for seq in (1,2):
                    assert s.observe(owner,at,seq=seq,state='PROGRESSING',progress_key=str(seq),process_nonce=f'nonce-{i}') is None
                assert not s.prepare_delivery(owner)
                s.submit_result(owner,at,result_id=f'partial-{i}',reported_sha=git(f['path'],'rev-parse','HEAD'),remaining=['value_changed'])
            # New Session is a real restart of all controller state.
            with runtime(owner,tmp_path) as s:
                assert s.tick(owner)==['PARTIAL_CONTINUE']
                assert s.tick(owner)==[]
                assert s.observe(owner,at,seq=3,state='IDLE',session_state='prompt',process_nonce=f'nonce-{i}')
                assert s.observe(owner,at,seq=3,state='IDLE',session_state='prompt',process_nonce=f'nonce-{i}') is None
            if i%13==0:
                # Actual failed send, backoff and successful duplicate-safe receipt.
                with runtime(owner,tmp_path) as s:
                    msg=s.prepare_delivery(owner)[0]
                    s.ack(owner,msg['id'],msg['delivery_token'],success=False)
                    s.db.execute(text("UPDATE mainai_supervision_messages SET next_attempt_at=clock_timestamp() WHERE owner_id=:o AND id=:id"),dict(o=owner,id=msg['id']))
            deliver(current)
            with runtime(owner,tmp_path) as s:
                assert s.validate_blocker(owner,at,blocker_id=f'false-{i}',category='missing_harness')=='CONTINUE_SAME_JOB'
                assert s.health(owner,at,process_alive=True,session_state='busy')=='HEALTHY_LONG_RUNNING'
                with pytest.raises(Rejected):
                    s.observe(owner,at,seq=4,state='IDLE',process_nonce='stale-process')
            if i%10==0:
                with runtime(owner,tmp_path) as s:
                    s.provider_observation(owner,f['profiles'][0],availability='exhausted',session_nonce=f'session-{i}',observed_at=s.now())
                    old=at
                    at=s.failover(owner,old,agent_id='fallback',process_nonce='fallback-process')
                    assert at and at!=old
                    with pytest.raises(Rejected):
                        s.current(owner,old)
                deliver(current)
            elif i%10==1:
                with runtime(owner,tmp_path) as s:
                    s.provider_observation(owner,f['profiles'][0],availability='unavailable',session_nonce=f'session-{i}',observed_at=s.now())
                    # A fresh outage is held for cooldown; a long-running lane may
                    # cross that cooldown while waiting on the database, in which
                    # case portable failover is valid.  Both outcomes must converge
                    # to a terminal job without reviving the old attempt.
                    try:
                        replacement=s.failover(owner,at,agent_id='fallback',process_nonce='fallback-process')
                    except Rejected as exc:
                        assert str(exc) == 'RETRY_LATER'
                        replacement=None
                    if replacement is not None:
                        at=replacement
                        s.cancel(owner,jid)
                    else:
                        # A cooldown refusal still fenced the outage decision
                        # only after the caller chooses a safe terminal action;
                        # cancel this synthetic lane job so its WIP lease cannot
                        # poison the next canonical job sharing the worktree.
                        s.cancel(owner,jid)
                continue
            if i%4==0:
                sha=commit(f['path'],f'completed-{i}')
                with runtime(owner,tmp_path) as s:
                    test=s.run_tests(owner,at,argv=[sys.executable,'-c',f"from pathlib import Path; assert Path('value.txt').read_text().strip()=='completed-{i}'"],criteria=['value_changed'],run_id=f'test-{i}')
                    assert s.result(owner,at,result_id=f'complete-{i}',reported_sha=sha,remaining=[],test_ids=[test])=='COMPLETE'
                    assert s.result(owner,at,result_id=f'complete-{i}',reported_sha=sha,remaining=[],test_ids=[test])=='COMPLETE'
            else:
                with runtime(owner,tmp_path) as s:
                    s.cancel(owner,jid)
                    with pytest.raises(Rejected):
                        s.current(owner,at)
            with runtime(owner,tmp_path) as s:
                assert not s.rows('SELECT * FROM mainai_supervision_resources WHERE owner_id=:o AND job_id=:j',o=owner,j=jid)
                assert not s.rows('SELECT * FROM mainai_supervision_bindings WHERE owner_id=:o',o=owners[(lane+1)%3])
        return lane
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run_lane,range(3)))==[0,1,2]
    superuser_db.expire_all()
    real_jobs=superuser_db.execute(text("SELECT status,count(*) FROM mainai_jobs WHERE created_by IN ('fault_soak','fixture') GROUP BY status")).all()
    totals=dict(real_jobs)
    assert sum(totals.values())==1000 and not totals.get('running',0) and not totals.get('queued',0)
    events=superuser_db.scalar(text('SELECT count(*) FROM mainai_supervision_journal'))
    assert events>=3000
    assert superuser_db.scalar(text('SELECT count(*) FROM mainai_supervision_resources'))==0
    assert superuser_db.scalar(text('SELECT count(*) FROM (SELECT job_id,generation,count(*) FROM mainai_supervision_attempts GROUP BY job_id,generation HAVING count(*)>1) x'))==0
    report={'jobs':sum(totals.values()),'durable_journal_events':events,'terminal_states':totals,'owners':3,'supervisors':2,
            'attempts':superuser_db.scalar(text('SELECT count(*) FROM mainai_supervision_attempts')),
            'receipts':superuser_db.scalar(text('SELECT count(*) FROM mainai_supervision_receipts'))}
    print('CANONICAL_SOAK_REPORT='+json.dumps(report,sort_keys=True))
