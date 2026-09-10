import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from app.mainai_execution.canonical_supervisor import Rejected
from app.models.mainai_job import MainAIJob
from tests.backend.supervision_support import claim,commit,deliver,runtime,seed,tests_and_result as run_result


def test_real_multi_agent_program(superuser_db,tmp_path):
    f=seed(superuser_db,tmp_path)
    owner=f['owner']
    at=claim(f)
    deliver(f)
    with runtime(owner,tmp_path) as s:
        # Working agent receives no interruption despite healthy repeated progress.
        for seq in range(1,4):
            assert s.observe(owner,at,seq=seq,state='PROGRESSING',process_nonce='process1',progress_key=str(seq)) is None
        assert s.prepare_delivery(owner)==[]
        assert s.result(owner,at,result_id='partial',reported_sha=f['base'],remaining=['value_changed'])=='PARTIAL_CONTINUE'
        mid=s.observe(owner,at,seq=4,state='IDLE',session_state='prompt',process_nonce='process1')
        assert mid
        assert s.observe(owner,at,seq=4,state='IDLE',session_state='prompt',process_nonce='process1') is None
    assert len(deliver(f))==1
    with runtime(owner,tmp_path) as s:
        assert s.validate_blocker(owner,at,blocker_id='false',category='pytest_missing',python_candidates=[__import__('sys').executable])=='CONTINUE_SAME_JOB'
    sha_a=commit(f['path'],'first attempt')
    assert run_result(f,at)[1]=='VERIFY_REQUIRED'
    with runtime(owner,tmp_path) as s:
        assert s.assign_examiner(owner,at,f['assignments'][2])==sha_a
        with pytest.raises(Rejected):
            s.assign_examiner(owner,at,f['assignments'][0])
        child=s.review(owner,at,examiner_assignment=f['assignments'][2],sha=sha_a,passed=False,verdict_id='fail')
        assert child
        assert s.create_fix(owner,f['job'])==child
    fix=f|{'job':child,'base':sha_a}
    fixed=claim(fix)
    deliver(fix)
    sha_b=commit(f['path'],'fixed second attempt')
    assert sha_b!=sha_a
    assert run_result(fix,fixed)[1]=='VERIFY_REQUIRED'
    with runtime(owner,tmp_path) as s:
        s.assign_examiner(owner,fixed,f['assignments'][2])
        with pytest.raises(Rejected):
            s.review(owner,fixed,examiner_assignment=f['assignments'][2],sha=sha_a,passed=True,verdict_id='stale')
        s.review(owner,fixed,examiner_assignment=f['assignments'][2],sha=sha_b,passed=True,verdict_id='pass')
    with runtime(owner,tmp_path) as s:
        row=s.job(owner,child)
        assert row.status.value=='completed' and row.output_refs[0]['sha']==sha_b
        assert s.rows('SELECT * FROM mainai_supervision_resources WHERE owner_id=:o',o=owner)==[]
        assert s.next_job(owner,agent_id='builder',process_nonce='next') is None


def test_two_supervisors_same_job_and_idle_event(superuser_db,tmp_path):
    f=seed(superuser_db,tmp_path)
    barrier=threading.Barrier(2)
    def race(i):
        barrier.wait(timeout=10)
        try:
            return claim(f,agent=f'worker{i}')
        except Rejected:
            return None
    with ThreadPoolExecutor(2) as pool:
        attempts=list(pool.map(race,range(2)))
    assert sum(x is not None for x in attempts)==1
    at=next(x for x in attempts if x)
    deliver(f)
    def idle(_):
        barrier.wait(timeout=10)
        with runtime(f['owner'],tmp_path) as s:
            return s.observe(f['owner'],at,seq=1,state='IDLE',session_state='prompt',process_nonce='process1')
    with ThreadPoolExecutor(2) as pool:
        messages=list(pool.map(idle,range(2)))
    assert sum(x is not None for x in messages)==1
    assert len(deliver(f))==1


def test_stale_authority_cancel_failover_and_owner_isolation(superuser_db,tmp_path):
    f=seed(superuser_db,tmp_path)
    other=seed(superuser_db,tmp_path)
    at=claim(f)
    deliver(f)
    with runtime(f['owner'],tmp_path) as s:
        s.provider_observation(f['owner'],f['profiles'][0],availability='exhausted',session_nonce='session1',observed_at=s.now())
        new=s.failover(f['owner'],at,agent_id='fallback',process_nonce='process2')
        assert new and new!=at
    with runtime(f['owner'],tmp_path) as s:
        with pytest.raises(Rejected):
            s.observe(f['owner'],at,seq=1,state='IDLE',process_nonce='process1')
        s.cancel(f['owner'],f['job'])
    with runtime(f['owner'],tmp_path) as s:
        with pytest.raises(Rejected):
            s.current(f['owner'],new)
        assert s.prepare_delivery(f['owner'])==[]
    with runtime(other['owner'],tmp_path) as s:
        for table in ('mainai_supervision_bindings','mainai_supervision_attempts','mainai_supervision_journal','mainai_supervision_messages','mainai_supervision_receipts'):
            assert not s.rows(f'SELECT * FROM {table} WHERE owner_id=:o',o=f['owner'])
        with pytest.raises(Rejected):
            s.cancel(f['owner'],f['job'])


def test_dependency_mutation_and_epoch_are_checked_at_delivery(superuser_db,tmp_path):
    f=seed(superuser_db,tmp_path)
    at=claim(f)
    owner=f['owner']
    parent=MainAIJob(owner_id=owner,job_type='supervised_execution',created_by='test')
    superuser_db.add(parent)
    superuser_db.commit()
    with runtime(owner,tmp_path) as s:
        s.set_dependency(owner,f['job'],parent.id,'1'*40)
        assert s.prepare_delivery(owner)==[]
        with pytest.raises(Rejected,match='DEPENDENCY_CYCLE'):
            s.set_dependency(owner,parent.id,f['job'],'1'*40)
    superuser_db.execute(text('UPDATE workforce_authority_epoch SET epoch=epoch+1 WHERE scope_key=:o'),dict(o=str(owner)))
    superuser_db.commit()
    with runtime(owner,tmp_path) as s:
        with pytest.raises(Rejected,match='STALE_AUTHORITY_EPOCH'):
            s.current(owner,at)


def test_ack_replay_dead_letter_and_protected_worktree(superuser_db,tmp_path):
    f=seed(superuser_db,tmp_path)
    claim(f)
    with runtime(f['owner'],tmp_path) as s:
        msg=s.prepare_delivery(f['owner'])[0]
    with runtime(f['owner'],tmp_path) as s:
        s.receive(f['owner'],msg['id'])
        s.receive(f['owner'],msg['id'])
    with runtime(f['owner'],tmp_path) as s:
        with pytest.raises(Rejected,match='STALE_ACK'):
            s.ack(f['owner'],msg['id'],uuid.uuid4())
        assert s.ack(f['owner'],msg['id'],msg['delivery_token'])
        assert not s.ack(f['owner'],msg['id'],msg['delivery_token'])
        assert len(s.rows('SELECT * FROM mainai_supervision_receipts WHERE owner_id=:o',o=f['owner']))==1
        b=s.binding(f['owner'],f['job'])
        with pytest.raises(Rejected,match='PROTECTED_REF'):
            s.repository(dict(b)|{'branch':'main'})
        with pytest.raises(Rejected,match='WORKTREE_ESCAPE'):
            s.repository(dict(b)|{'worktree':'/etc'})


def test_expired_lease_does_not_renew_via_observation(superuser_db,tmp_path):
    f=seed(superuser_db,tmp_path)
    at=claim(f)
    superuser_db.execute(text("UPDATE mainai_jobs SET lease_expires_at=(clock_timestamp() AT TIME ZONE 'UTC')-interval '1 second' WHERE id=:j"),dict(j=f['job']))
    superuser_db.commit()
    with runtime(f['owner'],tmp_path) as s:
        with pytest.raises(Rejected,match='LEASE_EXPIRED'):
            s.observe(f['owner'],at,seq=2,state='PROGRESSING',process_nonce='process1')
