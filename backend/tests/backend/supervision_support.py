"""Fixtures shared by real PostgreSQL and subprocess supervision attacks."""
import contextlib
import subprocess
import uuid
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.mainai_execution.canonical_supervisor import CanonicalSupervisor
from app.models.mainai_job import MainAIJob
from app.models.user import User
from app.models.workforce import WorkforceAgentProfile, WorkforceAssignment, WorkforceDelegationRequest
from app.request_context import current_user_id


def git(path,*args):
    return subprocess.run(['git','-C',str(path),*args],capture_output=True,text=True,check=True).stdout.strip()


def repo(root,name):
    path=root/name
    path.mkdir()
    git(path,'init','-b',f'work/{name}')
    git(path,'config','user.email','supervision@test.local')
    git(path,'config','user.name','Supervision Test')
    (path/'value.txt').write_text('base\n')
    git(path,'add','value.txt')
    git(path,'commit','-m','base')
    return path,git(path,'rev-parse','HEAD')


def commit(path,value='changed'):
    (path/'value.txt').write_text(value+'\n')
    git(path,'add','value.txt')
    git(path,'commit','-m','bounded local edit')
    return git(path,'rev-parse','HEAD')


@contextlib.contextmanager
def runtime(owner,root):
    token=current_user_id.set(str(owner))
    with SessionLocal() as db:
        try:
            yield CanonicalSupervisor(db,root=root,enabled=True)
            db.commit()
        finally:
            current_user_id.reset(token)


def seed(admin,root,*,name=None,review=True):
    name=name or uuid.uuid4().hex[:8]
    owner=User(email=f'{name}-{uuid.uuid4()}@test.local',password_hash='test',email_verified=True)
    admin.add(owner)
    admin.flush()
    path,base=repo(root,name)
    request=WorkforceDelegationRequest(owner_id=owner.id,goal_text='bounded local test work',required_capability='code_edit')
    admin.add(request)
    admin.flush()
    profiles=[]
    assignments=[]
    for i in range(3):
        caps=['review'] if i==2 else ['code_edit','repo_read','filesystem_write','test_run']
        p=WorkforceAgentProfile(owner_id=owner.id,agent_key=f'agent{i}',name=f'Agent {i}',role='examiner' if i==2 else 'builder',agent_type='local',provider_type=f'fake{i}',provider_model_id='deterministic',status='active',capability_tags=caps,allowed_tool_classes=caps)
        admin.add(p)
        admin.flush()
        a=WorkforceAssignment(owner_id=owner.id,delegation_request_id=request.id,profile_id=p.id,allowed_read_paths=[str(path)],allowed_write_paths=[str(path)] if i!=2 else [],allowed_tool_classes=caps,allow_execution_effects=True,expires_at=datetime.now(timezone.utc).replace(tzinfo=None)+timedelta(hours=6))
        admin.add(a)
        admin.flush()
        profiles.append(p.id)
        assignments.append(a.id)
    job=MainAIJob(owner_id=owner.id,job_type='supervised_execution',created_by='fixture',input_refs=[],output_refs=[])
    admin.add(job)
    admin.commit()
    owner_id,job_id=owner.id,job.id
    with runtime(owner_id,root) as s:
        for profile in profiles:
            s.provider_observation(owner_id,profile,availability='available',session_nonce='session1',observed_at=s.now(),ttl=3600)
        s.bind(owner_id,job_id,program='program-0',assignment_id=assignments[0],worktree=path,branch=git(path,'branch','--show-current'),base_sha=base,required=['code_edit','filesystem_write','test_run'],criteria=['value_changed'],portable=True,review_required=review,fallbacks=[assignments[1]])
    return dict(owner=owner_id,job=job_id,path=path,base=base,profiles=profiles,assignments=assignments,root=root)


def claim(f,agent='builder'):
    with runtime(f['owner'],f['root']) as s:
        return s.claim(f['owner'],f['job'],agent_id=agent,process_nonce='process1',lease_seconds=3600)


def deliver(f):
    with runtime(f['owner'],f['root']) as s:
        messages=s.prepare_delivery(f['owner'])
    for msg in messages:
        with runtime(f['owner'],f['root']) as s:
            s.receive(f['owner'],msg['id'])
        with runtime(f['owner'],f['root']) as s:
            s.ack(f['owner'],msg['id'],msg['delivery_token'])
    return messages


def tests_and_result(f,attempt,*,key='result1'):
    import sys
    sha=git(f['path'],'rev-parse','HEAD')
    with runtime(f['owner'],f['root']) as s:
        test=s.run_tests(f['owner'],attempt,argv=[sys.executable,'-c',"from pathlib import Path; assert Path('value.txt').read_text().strip() != 'base'"],criteria=['value_changed'],run_id=key)
        decision=s.result(f['owner'],attempt,result_id=key,reported_sha=sha,remaining=[],test_ids=[test])
    return sha,decision


def spend_grant(admin,f):
    from app.mainai_execution.planner import create_goal
    from app.execution_envelopes import propose_execution_scope, authorize_execution_scope
    from app.provider_spend import authorize_provider_spend
    goal=create_goal(admin,owner_id=f['owner'],title='supervision budget test',original_instruction='isolated work',created_by='test')
    proposal=propose_execution_scope(admin,owner_id=f['owner'],goal_id=goal.id,idempotency_key=str(uuid.uuid4()))
    _,envelope=authorize_execution_scope(admin,owner_id=f['owner'],proposal_id=proposal.id,authorized_by='founder',authorized_paths=['value.txt'],authorized_capabilities=['read_file','patch_file'],authorized_risk='low',envelope_idempotency_key=str(uuid.uuid4()))
    grant=authorize_provider_spend(admin,owner_id=f['owner'],goal_id=goal.id,execution_envelope_id=envelope.id,authorized_by='founder',max_cost_usd='10',max_requests=100,max_cost_per_request_usd='0.10',idempotency_key=str(uuid.uuid4()),allowed_providers=['fake0','fake1'],allowed_models=['deterministic'])
    for aid in f['assignments'][:2]:
        assignment=admin.get(WorkforceAssignment,aid,populate_existing=True)
        assignment.provider_spend_authorization_id=grant.id
    admin.commit()
    return grant.id
