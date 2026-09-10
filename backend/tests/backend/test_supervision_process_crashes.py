import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app.mainai_execution.canonical_supervisor import Rejected
from tests.backend.supervision_support import claim,commit,deliver,runtime,seed,tests_and_result as run_result


def crash(f,attempt,operation,when,**extra):
    config=f['root']/f'crash-{uuid.uuid4()}.json'
    config.write_text(json.dumps(dict(owner=str(f['owner']),job=str(f['job']),root=str(f['root']),attempt=str(attempt),operation=operation,when=when,**extra)))
    backend_root=Path(__file__).resolve().parents[2]
    child_env=os.environ.copy()
    child_env['PYTHONPATH']=str(backend_root)
    proc=subprocess.run([sys.executable,'-m','tests.supervision_crash_process',str(config)],cwd=backend_root,env=child_env,capture_output=True,text=True,timeout=30)
    assert proc.returncode==-signal.SIGKILL,proc.stderr[-4000:]


@pytest.mark.parametrize('operation',['idle','cancel','result','delivery','claim'])
@pytest.mark.parametrize('when',['before','uncommitted','committed'])
def test_real_sigkill_transaction_boundaries(superuser_db,tmp_path,operation,when):
    f=seed(superuser_db,tmp_path)
    at=uuid.uuid4() if operation=='claim' else claim(f)
    if operation=='idle':
        deliver(f)
    crash(f,at,operation,when,sha=f['base'])
    with runtime(f['owner'],tmp_path) as s:
        job=s.job(f['owner'],f['job'])
        if operation=='cancel' and when=='committed':
            assert job.status.value=='cancelled'
            with pytest.raises(Rejected):
                s.current(f['owner'],at)
        elif operation=='claim':
            assert job.status.value==('running' if when=='committed' else 'queued')
            attempts=s.rows('SELECT * FROM mainai_supervision_attempts WHERE owner_id=:o',o=f['owner'])
            assert len(attempts)==int(when=='committed')
        else:
            assert job.status.value=='running'
            s.current(f['owner'],at)
            if operation=='idle':
                assert s.one("SELECT count(*) AS n FROM mainai_supervision_messages WHERE owner_id=:o AND kind='CONTINUE_SAME_JOB'",o=f['owner'])['n']==int(when=='committed')
            if operation=='result':
                assert s.tick(f['owner'])==(['PARTIAL_CONTINUE'] if when=='committed' else [])
                assert s.tick(f['owner'])==[]
        assert s.recover(f['owner'],f['job']) in ('OBSERVE_CURRENT_ATTEMPT','TERMINAL')


@pytest.mark.parametrize('when',['uncommitted','committed'])
def test_real_send_ack_crash_recovers_one_receipt(superuser_db,tmp_path,when):
    f=seed(superuser_db,tmp_path)
    at=claim(f)
    with runtime(f['owner'],tmp_path) as s:
        msg=s.prepare_delivery(f['owner'])[0]
    crash(f,at,'receive',when,message=str(msg['id']))
    with runtime(f['owner'],tmp_path) as s:
        s.receive(f['owner'],msg['id'])
        s.receive(f['owner'],msg['id'])
        assert s.one('SELECT count(*) AS n FROM mainai_supervision_receipts WHERE owner_id=:o',o=f['owner'])['n']==1
    crash(f,at,'ack',when,message=str(msg['id']),token=str(msg['delivery_token']))
    with runtime(f['owner'],tmp_path) as s:
        s.ack(f['owner'],msg['id'],msg['delivery_token'])
        assert s.prepare_delivery(f['owner'])==[]


@pytest.mark.parametrize('operation',['freeze','examiner','pass','fail'])
@pytest.mark.parametrize('when',['uncommitted','committed'])
def test_real_artifact_review_crash_boundaries(superuser_db,tmp_path,operation,when):
    f=seed(superuser_db,tmp_path)
    at=claim(f)
    deliver(f)
    sha=commit(f['path'])
    with runtime(f['owner'],tmp_path) as s:
        test=s.run_tests(f['owner'],at,argv=[sys.executable,'-c',"from pathlib import Path; assert Path('value.txt').read_text()=='changed\\n'"],criteria=['value_changed'],run_id='crash-test')
        assert s.recover(f['owner'],f['job'])=='VERIFY_EXISTING_SHA'
    if operation!='freeze':
        run_result(f,at)
    if operation in ('pass','fail'):
        with runtime(f['owner'],tmp_path) as s:
            s.assign_examiner(f['owner'],at,f['assignments'][2])
    crash(f,at,operation,when,sha=sha,test=str(test),examiner=str(f['assignments'][2]))
    with runtime(f['owner'],tmp_path) as s:
        job=s.job(f['owner'],f['job'])
        if operation=='pass' and when=='committed':
            assert job.status.value=='completed' and job.output_refs[0]['sha']==sha
        elif operation=='fail' and when=='committed':
            assert job.status.value=='failed'
            child=s.create_fix(f['owner'],f['job'])
            assert s.create_fix(f['owner'],f['job'])==child
            assert len(s.rows('SELECT * FROM mainai_supervision_bindings WHERE owner_id=:o AND parent_job_id=:p',o=f['owner'],p=f['job']))==1
        else:
            assert job.status.value=='running'
            s.current(f['owner'],at)
