"""Default-off continuous supervision over canonical jobs and workforce authority.

Transactions use a short database advisory mutex, then epoch -> assignment -> job
locks. The mutex bounds WIP and graph mutations across supervisor processes; it is
never held across provider I/O. Delivery is at-least-once; receivers deduplicate by
message id and revalidate execution authority at receipt. No live adapter is wired.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from datetime import timedelta, timezone
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.jobs.mainai_job_lease import claim_specific_mainai_job
from app.jobs.service import _guarded_job_write
from app.mainai_execution.execution_events import append_execution_event
from app.mainai_execution.production_adapter import PROTECTED_REFS
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.workforce import WorkforceAgentProfile, WorkforceAssignment
from app.models.mainai_supervision import MainAISupervisionTelemetry
from app.workforce.kill_switch import assert_grant_allowed


class Rejected(RuntimeError):
    """Closed-vocabulary refusal, safe for telemetry."""


def identity(*parts) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, ':'.join(map(str, parts)))


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class CanonicalSupervisor:
    def __init__(self, db: Session, *, root: str | Path, enabled: bool = False):
        self.db, self.root, self.enabled = db, Path(root).resolve(), enabled

    def rows(self, sql, **params):
        return self.db.execute(text(sql), params).mappings().all()

    def one(self, sql, **params):
        rows = self.rows(sql, **params)
        if not rows:
            raise Rejected('NOT_VISIBLE')
        return rows[0]

    def now(self):
        return self.db.scalar(text('SELECT clock_timestamp()'))

    def record_telemetry(self, owner, *, agent_id, state, observed_at=None, **values):
        """Persist bounded resource observations; never turns telemetry into authority."""
        self.lock(owner)
        allowed = {
            'job_id', 'attempt_id', 'provider', 'model', 'productive_seconds',
            'idle_seconds', 'blocked_seconds', 'stalled_seconds', 'continuation_count',
            'premature_return_count', 'restart_count', 'context_input_tokens',
            'context_output_tokens', 'context_cached_tokens', 'context_limit_tokens',
            'provider_quota_remaining', 'estimated_cost', 'reported_cost', 'validated_cost',
            'retries', 'failed_attempts', 'rework_count', 'examiner_outcome',
            'last_progress_at', 'handoff_ready', 'context_risk',
        }
        unknown = set(values) - allowed
        if unknown or not agent_id or len(agent_id) > 128 or not state or len(state) > 32:
            raise Rejected('INVALID_TELEMETRY')
        sample_at = observed_at or self.now()
        if sample_at > self.now() + timedelta(seconds=1) or sample_at < self.now() - timedelta(hours=24):
            raise Rejected('STALE_TELEMETRY')
        for key in ('productive_seconds', 'idle_seconds', 'blocked_seconds', 'stalled_seconds', 'provider_quota_remaining', 'estimated_cost', 'reported_cost', 'validated_cost'):
            if values.get(key) is not None and values[key] < 0:
                raise Rejected('INVALID_TELEMETRY')
        for key in ('continuation_count', 'premature_return_count', 'restart_count', 'context_input_tokens', 'context_output_tokens', 'context_cached_tokens', 'context_limit_tokens', 'retries', 'failed_attempts', 'rework_count'):
            if values.get(key) is not None and (not isinstance(values[key], int) or values[key] < 0):
                raise Rejected('INVALID_TELEMETRY')
        row = MainAISupervisionTelemetry(owner_id=owner, agent_id=agent_id, state=state, observed_at=sample_at, **values)
        self.db.add(row)
        self.db.flush()
        return row.id

    def latest_telemetry(self, owner, *, agent_id, limit=1):
        self.lock(owner)
        if not 1 <= limit <= 100:
            raise Rejected('INVALID_LIMIT')
        return self.rows('SELECT * FROM mainai_supervision_telemetry WHERE owner_id=:o AND agent_id=:a ORDER BY observed_at DESC LIMIT :n', o=owner, a=agent_id, n=limit)

    def lock(self, owner):
        # Owner comes from authenticated runtime context; never from an event payload.
        actual = self.db.scalar(text("SELECT NULLIF(current_setting('app.current_user_id',true),'')"))
        if actual != str(owner):
            raise Rejected('OWNER_MISMATCH')
        if not self.enabled:
            raise Rejected('SUPERVISOR_DISABLED')
        self.db.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        # Serialize decisions per owner.  A single global mutex made unrelated
        # owners queue behind one another and turned a healthy multi-supervisor
        # soak into a starvation/deadlock-like stall.
        self.db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(CAST(:owner AS text), 0))"), {'owner': str(owner)})

    def binding(self, owner, job_id):
        return self.one('SELECT * FROM mainai_supervision_bindings WHERE owner_id=:o AND job_id=:j', o=owner, j=job_id)

    def job(self, owner, job_id):
        job = self.db.scalar(select(MainAIJob).where(MainAIJob.owner_id == owner, MainAIJob.id == job_id).execution_options(populate_existing=True).with_for_update())
        if not job:
            raise Rejected('NOT_VISIBLE')
        return job

    def journal(self, owner, job_id, kind, key, *, attempt_id=None, **facts):
        safe = {k:v for k,v in facts.items() if k in {'reason','action','remaining','repeat','sha','evidence','recovery','expected','source','classification','count','premature_return','provider','generation'}}
        payload = json.dumps(safe, sort_keys=True)
        if len(payload) > 4000:
            raise Rejected('OVERSIZE_EVIDENCE')
        event_id = identity(owner, job_id, kind, key)
        self.db.execute(text('''INSERT INTO mainai_supervision_journal(id,owner_id,job_id,attempt_id,kind,facts)
          VALUES(:id,:o,:j,:a,:k,CAST(:f AS jsonb)) ON CONFLICT(id) DO NOTHING'''),
          dict(id=event_id,o=owner,j=job_id,a=attempt_id,k=kind,f=payload))
        append_execution_event(self.db, owner_id=owner, job_id=job_id, attempt_id=str(attempt_id) if attempt_id else None,
                               event_type=kind[:40], event_id=event_id, metadata={'reason': safe.get('reason', kind)})
        self.db.flush()
        return event_id

    def authority(self, owner, assignment_id, required, *, expected=None, provider=True):
        epoch = assert_grant_allowed(self.db, owner_id=owner)
        if expected and any(expected[k] != epoch[k] for k in ('owner_epoch','global_epoch')):
            raise Rejected('STALE_AUTHORITY_EPOCH')
        a = self.db.scalar(select(WorkforceAssignment).where(WorkforceAssignment.id == assignment_id, WorkforceAssignment.owner_id == owner).execution_options(populate_existing=True).with_for_update())
        now = self.now()
        if not a or a.status not in ('assigned','running','awaiting_verification') or a.revoked_at or not a.expires_at or utc(a.expires_at) <= now or not a.allow_execution_effects:
            raise Rejected('AUTHORITY_REVOKED')
        p = self.db.scalar(select(WorkforceAgentProfile).where(WorkforceAgentProfile.id == a.profile_id, WorkforceAgentProfile.owner_id == owner).execution_options(populate_existing=True).with_for_update())
        required = set(required)
        if not p or p.status not in ('active','available') or not required <= set(p.capability_tags) or not required <= set(a.allowed_tool_classes):
            raise Rejected('CAPABILITY_DENIED')
        if required & {'merge','deploy','publish','secret_change','authority_expand'}:
            raise Rejected('OFFLINE_POLICY_DENIED')
        if provider:
            observed = self.one('SELECT * FROM mainai_supervision_providers WHERE owner_id=:o AND profile_id=:p FOR SHARE', o=owner,p=p.id)
            if observed['availability'] != 'available' or observed['valid_until'] <= now:
                raise Rejected('PROVIDER_UNAVAILABLE')
        return a,p,epoch

    def repository(self, b, *, expected=None):
        path = Path(b['worktree'])
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root) or resolved == self.root or path != resolved:
            raise Rejected('WORKTREE_ESCAPE')
        if b['branch'] in PROTECTED_REFS or b['base_sha'] in PROTECTED_REFS:
            raise Rejected('PROTECTED_REF')
        def git(*args):
            try:
                return subprocess.run(['git','-C',str(resolved),*args],check=True,capture_output=True,text=True,timeout=10).stdout.strip()
            except (subprocess.SubprocessError,OSError):
                raise Rejected('REPO_INVALID') from None
        if Path(git('rev-parse','--show-toplevel')).resolve() != resolved:
            raise Rejected('WRONG_WORKTREE')
        if git('symbolic-ref','--short','HEAD') != b['branch']:
            raise Rejected('WRONG_BRANCH')
        sha = git('rev-parse','HEAD')
        if expected and sha != expected:
            raise Rejected('SHA_MISMATCH')
        if git('status','--porcelain'):
            raise Rejected('DIRTY_WORKTREE')
        git('merge-base','--is-ancestor',b['base_sha'],sha)
        return sha

    def bind(self, owner, job_id, *, program, assignment_id, worktree, branch, base_sha, required, criteria, portable=False, review_required=True, fallbacks=(), priority=0):
        self.lock(owner)
        a,_,_ = self.authority(owner, assignment_id, required, provider=False)
        job = self.job(owner,job_id)
        if job.status != MainAIJobStatus.queued or not criteria or len(criteria)>64 or any(not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',x) for x in criteria):
            raise Rejected('INVALID_CONTRACT')
        # The grant must name the bounded worktree; no caller-selected arbitrary path.
        path = str(Path(worktree).resolve())
        if path not in a.allowed_write_paths and path not in a.allowed_read_paths:
            raise Rejected('WORKTREE_NOT_AUTHORIZED')
        for fallback in fallbacks:
            self.authority(owner, fallback, required, provider=False)
        self.db.execute(text('''INSERT INTO mainai_supervision_bindings
         (job_id,owner_id,program,assignment_id,worktree,branch,base_sha,required,criteria,remaining,portable,review_required,fallback_assignments,priority)
         VALUES(:j,:o,:p,:a,:w,:b,:s,CAST(:r AS jsonb),CAST(:c AS jsonb),CAST(:c AS jsonb),:port,:review,CAST(:f AS jsonb),:priority)'''),
         dict(j=job_id,o=owner,p=program,a=assignment_id,w=path,b=branch,s=base_sha,r=json.dumps(required),c=json.dumps(criteria),port=portable,review=review_required,f=json.dumps([str(x) for x in fallbacks]),priority=priority))
        self.journal(owner,job_id,'JOB_READY','bind')

    def provider_observation(self, owner, profile_id, *, availability, session_nonce, observed_at, ttl=120):
        self.lock(owner)
        if availability not in ('available','unavailable','exhausted','quarantined','rate_limited') or not 1<=ttl<=3600:
            raise Rejected('INVALID_OBSERVATION')
        now = self.now()
        if observed_at > now + timedelta(seconds=1) or observed_at < now-timedelta(seconds=3600):
            raise Rejected('STALE_OBSERVATION')
        self.db.execute(text('''INSERT INTO mainai_supervision_providers(profile_id,owner_id,availability,session_nonce,observed_at,valid_until)
          VALUES(:p,:o,:a,:n,:t,:v) ON CONFLICT(profile_id) DO UPDATE SET availability=excluded.availability,session_nonce=excluded.session_nonce,observed_at=excluded.observed_at,valid_until=excluded.valid_until
          WHERE mainai_supervision_providers.owner_id=excluded.owner_id AND mainai_supervision_providers.observed_at < excluded.observed_at'''),
          dict(p=profile_id,o=owner,a=availability,n=session_nonce,t=observed_at,v=observed_at+timedelta(seconds=ttl)))

    def dependencies(self, owner, child):
        deps = self.rows('SELECT * FROM mainai_supervision_dependencies WHERE owner_id=:o AND child_id=:j ORDER BY parent_id', o=owner,j=child)
        for dep in deps:
            parent = self.job(owner,dep['parent_id'])
            if parent.status != MainAIJobStatus.completed or parent.cancel_requested:
                raise Rejected('DEPENDENCY_WAIT')
            matched = [r for r in parent.output_refs if r.get('sha') == dep['required_sha'] and (not dep['certified'] or r.get('certification_proposal'))]
            if not matched:
                raise Rejected('STALE_DEPENDENCY')

    def set_dependency(self, owner, child, parent, sha, *, certified=True):
        self.lock(owner)
        if not re.fullmatch('[0-9a-f]{40}',sha):
            raise Rejected('EXACT_SHA_REQUIRED')
        self.job(owner,child)
        self.job(owner,parent)
        edges = self.rows('SELECT child_id,parent_id FROM mainai_supervision_dependencies WHERE owner_id=:o',o=owner)
        graph = {}
        for e in edges:
            graph.setdefault(e['child_id'],[]).append(e['parent_id'])
        todo,seen=[parent],set()
        while todo:
            node=todo.pop()
            if node==child:
                raise Rejected('DEPENDENCY_CYCLE')
            if node not in seen:
                seen.add(node)
                todo.extend(graph.get(node,()))
        self.db.execute(text('''INSERT INTO mainai_supervision_dependencies(owner_id,child_id,parent_id,required_sha,certified)
          VALUES(:o,:c,:p,:s,:v) ON CONFLICT(child_id,parent_id) DO UPDATE SET required_sha=excluded.required_sha,certified=excluded.certified'''),dict(o=owner,c=child,p=parent,s=sha,v=certified))

    def current(self, owner, attempt_id, *, provider=True, lease=True):
        self.lock(owner)
        at = self.one('SELECT * FROM mainai_supervision_attempts WHERE owner_id=:o AND id=:a',o=owner,a=attempt_id)
        b = self.binding(owner,at['job_id'])
        a,p,_=self.authority(owner,at['assignment_id'],b['required'],expected=at,provider=provider)
        job=self.job(owner,at['job_id'])
        if job.status != MainAIJobStatus.running or job.cancel_requested or job.lease_generation!=at['generation'] or job.locked_by!=str(at['id']):
            raise Rejected('STALE_ATTEMPT')
        if lease and (not job.lease_expires_at or utc(job.lease_expires_at)<=self.now()):
            raise Rejected('LEASE_EXPIRED')
        self.dependencies(owner,job.id)
        return job,b,at,a,p

    def _resource(self, owner, job_id, attempt_id, resource):
        key=hashlib.sha256(resource.encode()).hexdigest()
        inserted=self.db.execute(text('''INSERT INTO mainai_supervision_resources(resource_key,owner_id,job_id,attempt_id)
          VALUES(:k,:o,:j,:a) ON CONFLICT DO NOTHING RETURNING resource_key'''),dict(k=key,o=owner,j=job_id,a=attempt_id)).scalar()
        if inserted is None:
            raise Rejected('WIP_CONFLICT')

    def claim(self, owner, job_id, *, agent_id, process_nonce, lease_seconds=120):
        self.lock(owner)
        if not 5<=lease_seconds<=3600:
            raise Rejected('INVALID_LEASE')
        b=self.binding(owner,job_id)
        a,p,epoch=self.authority(owner,b['assignment_id'],b['required'])
        job=self.job(owner,job_id)
        if job.status != MainAIJobStatus.queued or job.cancel_requested:
            raise Rejected('NOT_READY')
        if b['cooldown_until'] and b['cooldown_until']>self.now():
            raise Rejected('RETRY_LATER')
        self.dependencies(owner,job_id)
        self.repository(b)
        # Savepoint ensures a resource conflict cannot leave half a claim behind.
        with self.db.begin_nested():
            attempt=uuid.uuid4()
            generation=claim_specific_mainai_job(self.db,job_id=job_id,worker_id=str(attempt),lease_seconds=lease_seconds)
            if generation is None:
                raise Rejected('CLAIM_LOST')
            self.db.execute(text('''INSERT INTO mainai_supervision_attempts
             (id,owner_id,job_id,assignment_id,agent_id,process_nonce,generation,owner_epoch,global_epoch)
             VALUES(:id,:o,:j,:a,:agent,:n,:g,:oe,:ge)'''),dict(id=attempt,o=owner,j=job_id,a=a.id,agent=agent_id,n=process_nonce,g=generation,oe=epoch['owner_epoch'],ge=epoch['global_epoch']))
            for resource in (f'agent:{owner}:{agent_id}',f'profile:{p.id}',f'worktree:{b["worktree"]}'):
                self._resource(owner,job_id,attempt,resource)
            for kind,scope,limit in (('global','system',16),('program',f'{owner}:{b["program"]}',4),('provider',f'{owner}:{p.provider_type}',4)):
                for slot in range(limit):
                    try:
                        self._resource(owner,job_id,attempt,f'{kind}:{scope}:{slot}')
                        break
                    except Rejected:
                        continue
                else:
                    raise Rejected('BACKPRESSURE')
            self.db.execute(text('''INSERT INTO mainai_supervision_agents(agent_id,owner_id,state,process_nonce,job_id,attempt_id,provider,heartbeat_at,updated_at,observation_seq,last_progress_at,session_state)
             VALUES(:agent,:o,'CLAIMED',:n,:j,:a,:p,clock_timestamp(),clock_timestamp(),0,clock_timestamp(),'starting')
             ON CONFLICT(owner_id,agent_id) DO UPDATE SET state='CLAIMED',process_nonce=excluded.process_nonce,job_id=excluded.job_id,attempt_id=excluded.attempt_id,provider=excluded.provider,heartbeat_at=excluded.heartbeat_at,updated_at=excluded.updated_at,observation_seq=0,last_progress_at=excluded.last_progress_at,session_state='starting' '''),dict(agent=agent_id,o=owner,n=process_nonce,j=job_id,a=str(attempt),p=p.provider_type))
            self.journal(owner,job_id,'JOB_CLAIMED',attempt,attempt_id=attempt,generation=generation)
            self._message(owner,job_id,attempt,agent_id,generation,0,'START_JOB',b)
        return attempt

    def _message(self,owner,job_id,attempt,agent,generation,seq,kind,b):
        mid=identity(owner,job_id,attempt,kind,seq)
        facts={'sha':self.repository(b),'remaining':b['remaining'],'attempt_id':str(attempt),'generation':generation,
               'reporting':'completion_criteria_only' if b['return_count']>=2 else 'bounded_progress',
               'instruction':'Continue the same authorized job. Resolve the remaining criteria.'}
        # Sequence uniqueness spans attempts and is chosen from durable message history.
        existing=self.rows('SELECT id FROM mainai_supervision_messages WHERE owner_id=:o AND id=:id',o=owner,id=mid)
        if not existing:
            next_seq=self.db.scalar(text('SELECT COALESCE(max(sequence),0)+1 FROM mainai_supervision_messages WHERE owner_id=:o AND job_id=:j'),dict(o=owner,j=job_id))
            self.db.execute(text('''INSERT INTO mainai_supervision_messages(id,owner_id,job_id,attempt_id,kind,reason,sequence,state,attempts,created_at,agent_id,lease_generation,facts)
             VALUES(:id,:o,:j,:a,:k,'durable completion contract',:s,'pending',0,clock_timestamp(),:agent,:g,CAST(:f AS jsonb))'''),dict(id=mid,o=owner,j=job_id,a=str(attempt),k=kind,s=next_seq,agent=agent,g=generation,f=json.dumps(facts)))
            self.journal(owner,job_id,'CONTINUATION_CREATED',mid,attempt_id=attempt,remaining=b['remaining'],premature_return=kind=='CONTINUE_SAME_JOB')
        return mid

    def observe(self, owner, attempt_id, *, seq, state, process_nonce, progress_key='', session_state='busy'):
        job,b,at,_,_=self.current(owner,attempt_id)
        agent=self.one('SELECT * FROM mainai_supervision_agents WHERE owner_id=:o AND agent_id=:a FOR UPDATE',o=owner,a=at['agent_id'])
        if process_nonce!=at['process_nonce'] or agent['attempt_id']!=str(attempt_id):
            raise Rejected('STALE_PROCESS')
        if seq<=agent['observation_seq']:
            return None
        if state not in ('RUNNING','PROGRESSING','WAITING','IDLE','RESULT_SUBMITTED','BLOCKED','CRASHED') or session_state not in ('busy','prompt','waiting','dead') or seq>agent['observation_seq']+10000:
            raise Rejected('INVALID_OBSERVATION')
        now=self.now()
        changed=progress_key!=agent['progress_key']
        self.db.execute(text('''UPDATE mainai_supervision_agents SET observation_seq=:s,state=:st,session_state=:session,heartbeat_at=:t,updated_at=:t,progress_key=:p,last_progress_at=CASE WHEN :changed THEN :t ELSE last_progress_at END WHERE owner_id=:o AND agent_id=:a'''),dict(s=seq,st=state,session=session_state,t=now,p=progress_key[:256],changed=changed,o=owner,a=at['agent_id']))
        self.journal(owner,job.id,'AGENT_'+state,f'{attempt_id}:{seq}',attempt_id=attempt_id)
        if state=='IDLE' and session_state=='prompt':
            if job.current_phase=='verifying':
                return None
            pending=self.rows("SELECT id FROM mainai_supervision_messages WHERE owner_id=:o AND job_id=:j AND state IN ('pending','retrying')",o=owner,j=job.id)
            if pending:
                return pending[0]['id']
            if b['return_count']>=b['max_continuations']:
                self.journal(owner,job.id,'REPLAN_REQUIRED',attempt_id,reason='CONTINUATION_BUDGET_EXHAUSTED')
                return None
            self.db.execute(text('UPDATE mainai_supervision_bindings SET return_count=return_count+1 WHERE owner_id=:o AND job_id=:j'),dict(o=owner,j=job.id))
            self.journal(owner,job.id,'PREMATURE_RETURN',f'{attempt_id}:{seq}',remaining=b['remaining'],repeat=b['return_count']+1,action='CONTINUE_SAME_JOB')
            return self._message(owner,job.id,attempt_id,at['agent_id'],at['generation'],seq,'CONTINUE_SAME_JOB',self.binding(owner,job.id))
        return None

    def next_job(self,owner,*,agent_id,process_nonce):
        self.lock(owner)
        candidates=self.rows('''SELECT b.job_id FROM mainai_supervision_bindings b JOIN mainai_jobs j ON j.id=b.job_id AND j.owner_id=b.owner_id
          WHERE b.owner_id=:o AND j.status='queued' AND NOT j.cancel_requested
          ORDER BY (SELECT count(*) FROM mainai_supervision_dependencies d WHERE d.owner_id=b.owner_id AND d.parent_id=b.job_id) DESC,
          b.priority DESC,j.created_at,j.id LIMIT 64''',o=owner)
        for candidate in candidates:
            try:
                with self.db.begin_nested():
                    return self.claim(owner,candidate['job_id'],agent_id=agent_id,process_nonce=process_nonce)
            except Rejected:
                continue
        return None

    def prepare_delivery(self,owner,*,limit=32):
        self.lock(owner)
        if not 1<=limit<=1000:
            raise Rejected('UNBOUNDED_DELIVERY')
        rows=self.rows("""SELECT * FROM mainai_supervision_messages WHERE owner_id=:o AND state IN ('pending','retrying')
         AND (next_attempt_at IS NULL OR next_attempt_at<=clock_timestamp()) AND (delivery_until IS NULL OR delivery_until<=clock_timestamp())
         ORDER BY created_at,id LIMIT :n FOR UPDATE SKIP LOCKED""",o=owner,n=limit)
        result=[]
        for row in rows:
            try:
                job,b,_,_,_=self.current(owner,uuid.UUID(row['attempt_id']))
                self.repository(b)
            except (Rejected,ValueError):
                self.db.execute(text("UPDATE mainai_supervision_messages SET state='dead_letter',blocked_reason='STALE_AUTHORITY' WHERE id=:id AND owner_id=:o"),dict(id=row['id'],o=owner))
                continue
            if row['attempts']>=5:
                self.db.execute(text("UPDATE mainai_supervision_messages SET state='dead_letter',blocked_reason='RETRY_EXHAUSTED' WHERE id=:id AND owner_id=:o"),dict(id=row['id'],o=owner))
                self.journal(owner,job.id,'MESSAGE_DEAD_LETTER',row['id'],reason='RETRY_EXHAUSTED',recovery='QUARANTINE')
                continue
            token=uuid.uuid4()
            self.db.execute(text("UPDATE mainai_supervision_messages SET delivery_token=:token,delivery_until=clock_timestamp()+interval '30 seconds',attempts=attempts+1 WHERE id=:id AND owner_id=:o"),dict(id=row['id'],o=owner,token=token))
            result.append(dict(row)|{'delivery_token':token})
        return result

    def receive(self,owner,message_id):
        self.lock(owner)
        msg=self.one('SELECT * FROM mainai_supervision_messages WHERE owner_id=:o AND id=:id',o=owner,id=message_id)
        job,b,_,_,_=self.current(owner,uuid.UUID(msg['attempt_id']))
        self.repository(b)
        self.db.execute(text('''INSERT INTO mainai_supervision_receipts(message_id,owner_id,job_id,attempt_id)
          VALUES(:id,:o,:j,:a) ON CONFLICT(message_id) DO NOTHING'''),dict(id=message_id,o=owner,j=job.id,a=uuid.UUID(msg['attempt_id'])))
        return msg['facts']

    def ack(self,owner,message_id,token,*,success=True):
        self.lock(owner)
        msg=self.one('SELECT * FROM mainai_supervision_messages WHERE owner_id=:o AND id=:id FOR UPDATE',o=owner,id=message_id)
        if msg['state']=='delivered':
            return False
        if msg['delivery_token']!=token or not msg['delivery_until'] or msg['delivery_until']<self.now():
            raise Rejected('STALE_ACK')
        receipt=self.rows('SELECT message_id FROM mainai_supervision_receipts WHERE owner_id=:o AND message_id=:id',o=owner,id=message_id)
        if success and not receipt:
            raise Rejected('ACK_WITHOUT_RECEIPT')
        state='delivered' if success else ('dead_letter' if msg['attempts']>=5 else 'retrying')
        self.db.execute(text('''UPDATE mainai_supervision_messages SET state=:s,delivery_until=NULL,next_attempt_at=:retry,
          delivered_at=CASE WHEN :ok THEN clock_timestamp() ELSE NULL END WHERE owner_id=:o AND id=:id'''),dict(s=state,retry=self.now()+timedelta(seconds=min(300,2**msg['attempts'])),ok=success,o=owner,id=message_id))
        return True

    def cancel(self,owner,job_id,*,superseded_by=None):
        self.lock(owner)
        job=self.job(owner,job_id)
        if job.status in (MainAIJobStatus.cancelled,MainAIJobStatus.superseded):
            return
        if job.status==MainAIJobStatus.completed:
            raise Rejected('ALREADY_COMPLETED')
        if superseded_by:
            self.job(owner,superseded_by)
        status='superseded' if superseded_by else 'cancelled'
        self.db.execute(text('''UPDATE mainai_jobs SET status=:s,completed_at=clock_timestamp(),cancel_requested=true,
         cancel_acknowledged=true,superseded_by_job_id=:target WHERE owner_id=:o AND id=:j'''),dict(s=status,target=superseded_by,o=owner,j=job_id))
        self.release(owner,job_id)
        self.journal(owner,job_id,'JOB_'+status.upper(),'terminal')

    def release(self,owner,job_id):
        self.db.execute(text('DELETE FROM mainai_supervision_resources WHERE owner_id=:o AND job_id=:j'),dict(o=owner,j=job_id))
        self.db.execute(text("UPDATE mainai_supervision_messages SET state='dead_letter',blocked_reason='ATTEMPT_ENDED' WHERE owner_id=:o AND job_id=:j AND state IN ('pending','retrying')"),dict(o=owner,j=job_id))
        self.db.execute(text("UPDATE mainai_supervision_agents SET state='IDLE',job_id=NULL,attempt_id=NULL WHERE owner_id=:o AND job_id=:j"),dict(o=owner,j=job_id))

    def failover(self,owner,attempt_id,*,agent_id,process_nonce):
        job,b,at,a,p=self.current(owner,attempt_id,provider=False,lease=False)
        observed=self.one('SELECT * FROM mainai_supervision_providers WHERE owner_id=:o AND profile_id=:p',o=owner,p=p.id)
        if observed['availability']=='available':
            raise Rejected('FAILOVER_NOT_REQUIRED')
        if observed['availability'] in ('unavailable','rate_limited') and (self.now()-observed['observed_at']).total_seconds()<30:
            raise Rejected('RETRY_LATER')
        self.repository(b)
        # Fence immediately; reconnect cannot revive this generation even if no fallback exists.
        _guarded_job_write(self.db,job.id,worker_id=str(attempt_id),lease_generation=at['generation'],set_sql="status='failed',completed_at=clock_timestamp(),error_category='capability_unavailable'",params={})
        self.release(owner,job.id)
        self.journal(owner,job.id,'ATTEMPT_FENCED',attempt_id,attempt_id=attempt_id,recovery='REVALIDATE_FALLBACK')
        if not b['portable'] or job.retry_count>=job.max_retries:
            return None
        for fallback in b['fallback_assignments']:
            try:
                fa,_,_=self.authority(owner,uuid.UUID(fallback),b['required'])
                if fa.id==a.id or fa.context_package_id!=a.context_package_id or not set(fa.allowed_tool_classes)<=set(a.allowed_tool_classes) or not set(fa.allowed_write_paths)<=set(a.allowed_write_paths) or not set(fa.allowed_network_destinations)<=set(a.allowed_network_destinations):
                    continue
            except Rejected:
                continue
            self.db.execute(text('UPDATE mainai_supervision_bindings SET assignment_id=:a WHERE owner_id=:o AND job_id=:j'),dict(a=fa.id,o=owner,j=job.id))
            self.db.execute(text("UPDATE mainai_jobs SET status='queued',completed_at=NULL,locked_by=NULL,lease_expires_at=NULL,retry_count=retry_count+1 WHERE owner_id=:o AND id=:j"),dict(o=owner,j=job.id))
            new=self.claim(owner,job.id,agent_id=agent_id,process_nonce=process_nonce)
            self.journal(owner,job.id,'JOB_REASSIGNED',new,attempt_id=new)
            return new
        return None

    def evidence(self,owner,job_id,attempt,kind,sha,source,key,facts,*,ttl=3600):
        eid=identity(owner,job_id,attempt,kind,source,key)
        payload=json.dumps(facts,sort_keys=True)
        if len(payload)>4000 or not re.fullmatch('[0-9a-f]{40}',sha):
            raise Rejected('INVALID_EVIDENCE')
        self.db.execute(text('''INSERT INTO mainai_supervision_evidence(id,owner_id,job_id,attempt_id,kind,sha,source,facts,observed_at,valid_until)
          VALUES(:id,:o,:j,:a,:k,:s,:source,CAST(:f AS jsonb),clock_timestamp(),clock_timestamp()+make_interval(secs=>:ttl)) ON CONFLICT(id) DO NOTHING'''),dict(id=eid,o=owner,j=job_id,a=attempt,k=kind,s=sha,source=source,f=payload,ttl=ttl))
        existing=self.one('SELECT * FROM mainai_supervision_evidence WHERE id=:id AND owner_id=:o',id=eid,o=owner)
        if existing['facts']!=facts or existing['sha']!=sha:
            raise Rejected('EVIDENCE_ID_COLLISION')
        return eid

    def run_tests(self,owner,attempt_id,*,argv,criteria,run_id):
        """Trusted local test runner API. argv is configuration, never provider text.

        The transaction revalidates before and after the bounded local test. This
        holds the authority fence for at most 30 seconds and cannot invoke a shell.
        """
        job,b,_,_,_=self.current(owner,attempt_id)
        if not criteria or not set(criteria)<=set(b['criteria']) or not argv or len(argv)>32:
            raise Rejected('INVALID_TEST_CONTRACT')
        sha=self.repository(b)
        started=self.now()
        try:
            result=subprocess.run(argv,cwd=b['worktree'],capture_output=True,timeout=30,check=False)
            code=result.returncode
        except (OSError,subprocess.TimeoutExpired):
            code=124
        self.current(owner,attempt_id)
        self.repository(b,expected=sha)
        return self.evidence(owner,job.id,attempt_id,'tests',sha,'local_test_runner',run_id,
          {'command':hashlib.sha256(json.dumps(argv).encode()).hexdigest(),'exit_code':code,'criteria':sorted(criteria),
           'started_at':started.isoformat(),'finished_at':self.now().isoformat()})

    def result(self,owner,attempt_id,*,result_id,reported_sha,remaining,test_ids=()):
        self.lock(owner)
        prior=self.rows("SELECT * FROM mainai_supervision_evidence WHERE owner_id=:o AND attempt_id=:a AND kind='result'",o=owner,a=attempt_id)
        for item in prior:
            if item['id']==identity(owner,item['job_id'],attempt_id,'result','agent',result_id):
                job=self.job(owner,item['job_id'])
                if job.status==MainAIJobStatus.completed and job.locked_by==str(attempt_id) and item['sha']==reported_sha and item['facts']=={'remaining':sorted(remaining),'tests':sorted(map(str,test_ids))}:
                    return 'COMPLETE'
        job,b,at,_,_=self.current(owner,attempt_id)
        if not set(remaining)<=set(b['criteria']):
            raise Rejected('SCOPE_EXPANSION')
        sha=self.repository(b,expected=reported_sha)
        eid=self.evidence(owner,job.id,attempt_id,'result',sha,'agent',result_id,{'remaining':sorted(remaining),'tests':sorted(map(str,test_ids))})
        already=self.rows("SELECT id FROM mainai_supervision_journal WHERE owner_id=:o AND id=:id",o=owner,id=identity(owner,job.id,'RESULT_EVALUATED',eid))
        if already:
            return 'PARTIAL_CONTINUE' if remaining else 'VERIFY_REQUIRED'
        if remaining:
            self.db.execute(text('UPDATE mainai_supervision_bindings SET remaining=CAST(:r AS jsonb) WHERE job_id=:j AND owner_id=:o'),dict(r=json.dumps(sorted(remaining)),j=job.id,o=owner))
            self.journal(owner,job.id,'RESULT_EVALUATED',eid,attempt_id=attempt_id,remaining=sorted(remaining),action='PARTIAL_CONTINUE')
            return 'PARTIAL_CONTINUE'
        covered=set()
        for tid in set(test_ids):
            test=self.one("SELECT * FROM mainai_supervision_evidence WHERE owner_id=:o AND id=:id AND job_id=:j AND kind='tests'",o=owner,id=tid,j=job.id)
            if test['attempt_id']!=attempt_id or test['sha']!=sha or test['valid_until']<=self.now() or test['source']!='local_test_runner' or test['facts']['exit_code']!=0:
                raise Rejected('INVALID_TEST_EVIDENCE')
            covered.update(test['facts']['criteria'])
        if not set(b['criteria'])<=covered or sha==b['base_sha']:
            raise Rejected('COMPLETION_CONTRACT_UNMET')
        self.evidence(owner,job.id,attempt_id,'artifact',sha,'repo_inspection',sha,{'builder_assignment':str(at['assignment_id']),'test_ids':sorted(map(str,test_ids))})
        self.db.execute(text("UPDATE mainai_jobs SET current_phase='verifying' WHERE owner_id=:o AND id=:j"),dict(o=owner,j=job.id))
        self.db.execute(text("UPDATE mainai_supervision_agents SET state='VERIFYING' WHERE owner_id=:o AND job_id=:j"),dict(o=owner,j=job.id))
        self.db.execute(text("UPDATE mainai_supervision_messages SET state='dead_letter',blocked_reason='ARTIFACT_FROZEN' WHERE owner_id=:o AND job_id=:j AND state IN ('pending','retrying')"),dict(o=owner,j=job.id))
        self.journal(owner,job.id,'ARTIFACT_FROZEN',sha,attempt_id=attempt_id,sha=sha)
        self.journal(owner,job.id,'RESULT_EVALUATED',eid,attempt_id=attempt_id,action='VERIFY_REQUIRED')
        if not b['review_required']:
            self._complete(owner,job,b,at,sha,None)
            return 'COMPLETE'
        return 'VERIFY_REQUIRED'

    def submit_result(self,owner,attempt_id,*,result_id,reported_sha,remaining,test_ids=()):
        job,b,_,_,_=self.current(owner,attempt_id)
        self.repository(b,expected=reported_sha)
        if not set(remaining)<=set(b['criteria']):
            raise Rejected('SCOPE_EXPANSION')
        eid=self.evidence(owner,job.id,attempt_id,'result',reported_sha,'agent',result_id,{'remaining':sorted(remaining),'tests':sorted(map(str,test_ids))})
        self.journal(owner,job.id,'RESULT_SUBMITTED',eid,attempt_id=attempt_id,evidence=str(eid),source=result_id)
        return eid

    def tick(self,owner,*,limit=32):
        """Bounded event drain; a restarted caller supplies no reconstructed state."""
        self.lock(owner)
        if not 1<=limit<=1000:
            raise Rejected('UNBOUNDED_TICK')
        decisions=[]
        submitted=self.rows("""SELECT j.* FROM mainai_supervision_journal j
          WHERE j.owner_id=:o AND j.kind='RESULT_SUBMITTED' AND NOT EXISTS (
            SELECT 1 FROM mainai_supervision_journal done WHERE done.owner_id=j.owner_id
            AND done.kind='SUBMISSION_DRAINED' AND done.facts->>'evidence'=j.facts->>'evidence')
          ORDER BY j.created_at LIMIT :n""",o=owner,n=limit)
        for event in submitted:
            ev=self.one('SELECT * FROM mainai_supervision_evidence WHERE owner_id=:o AND id=:id',o=owner,id=uuid.UUID(event['facts']['evidence']))
            try:
                with self.db.begin_nested():
                    decision=self.result(owner,ev['attempt_id'],result_id=event['facts']['source'],reported_sha=ev['sha'],remaining=ev['facts']['remaining'],test_ids=[uuid.UUID(x) for x in ev['facts']['tests']])
            except Rejected:
                decision='REJECTED'
            self.journal(owner,ev['job_id'],'SUBMISSION_DRAINED',ev['id'],evidence=str(ev['id']),action=decision)
            decisions.append(decision)
        return decisions

    def health(self,owner,attempt_id,*,process_alive,session_state):
        job,b,at,_,_=self.current(owner,attempt_id,provider=False,lease=False)
        row=self.one('SELECT * FROM mainai_supervision_agents WHERE owner_id=:o AND agent_id=:a FOR UPDATE',o=owner,a=at['agent_id'])
        age=(self.now()-row['heartbeat_at']).total_seconds()
        no_progress=(self.now()-(row['last_progress_at'] or row['heartbeat_at'])).total_seconds()
        missed=not process_alive or age>60
        independent=not row['last_health_at'] or (self.now()-row['last_health_at']).total_seconds()>=5
        misses=row['misses']+int(independent) if missed else 0
        self.db.execute(text('UPDATE mainai_supervision_agents SET misses=:m,last_health_at=clock_timestamp() WHERE owner_id=:o AND agent_id=:a'),dict(m=misses,o=owner,a=at['agent_id']))
        # Hysteresis uses independent polls separated by heartbeat expiry; a single
        # slow heartbeat cannot turn into a new execution attempt.
        if missed and misses<2:
            return 'WATCH'
        if not process_alive:
            self.journal(owner,job.id,'PROCESS_LOST',attempt_id,recovery='INSPECT_BEFORE_REASSIGN')
            return 'CRASHED'
        if session_state=='prompt' and row['state']=='IDLE':
            return 'IDLE'
        if no_progress>1800 and age>60:
            self.journal(owner,job.id,'STALL_OBSERVED',attempt_id,reason='NO_FRESH_PROGRESS',recovery='QUARANTINE')
            return 'STALLED'
        return 'HEALTHY_LONG_RUNNING'

    @staticmethod
    def logical_profile(profile):
        return str(profile.coordination_agent_id) if profile.coordination_agent_id else f'{profile.provider_type}:{profile.provider_model_id}'

    def assign_examiner(self,owner,attempt_id,examiner_assignment):
        job,b,at,_,builder=self.current(owner,attempt_id)
        _,examiner,_=self.authority(owner,examiner_assignment,['review'])
        if self.logical_profile(builder)==self.logical_profile(examiner):
            raise Rejected('SELF_REVIEW')
        artifact=self.one("SELECT * FROM mainai_supervision_evidence WHERE owner_id=:o AND job_id=:j AND attempt_id=:a AND kind='artifact' ORDER BY observed_at DESC LIMIT 1",o=owner,j=job.id,a=attempt_id)
        self.repository(b,expected=artifact['sha'])
        self.journal(owner,job.id,'EXAMINER_ASSIGNED',artifact['id'],attempt_id=attempt_id,evidence=str(examiner_assignment),sha=artifact['sha'])
        return artifact['sha']

    def review(self,owner,attempt_id,*,examiner_assignment,sha,passed,verdict_id):
        job,b,at,_,builder=self.current(owner,attempt_id)
        _,examiner,_=self.authority(owner,examiner_assignment,['review'])
        if self.logical_profile(builder)==self.logical_profile(examiner):
            raise Rejected('SELF_REVIEW')
        assigned=self.one("SELECT * FROM mainai_supervision_journal WHERE owner_id=:o AND job_id=:j AND kind='EXAMINER_ASSIGNED' ORDER BY created_at DESC LIMIT 1",o=owner,j=job.id)
        if assigned['attempt_id']!=attempt_id or assigned['facts']['evidence']!=str(examiner_assignment) or assigned['facts']['sha']!=sha:
            raise Rejected('WRONG_REVIEW_AUTHORITY')
        self.repository(b,expected=sha)
        artifact=self.one("SELECT * FROM mainai_supervision_evidence WHERE owner_id=:o AND job_id=:j AND attempt_id=:a AND kind='artifact' AND sha=:s",o=owner,j=job.id,a=attempt_id,s=sha)
        for tid in artifact['facts']['test_ids']:
            test=self.one("SELECT * FROM mainai_supervision_evidence WHERE owner_id=:o AND id=:id",o=owner,id=uuid.UUID(tid))
            if test['valid_until']<=self.now() or test['sha']!=sha:
                raise Rejected('STALE_TEST_EVIDENCE')
        verdict=self.evidence(owner,job.id,attempt_id,'review',sha,str(examiner.id),verdict_id,{'passed':passed,'assignment':str(examiner_assignment)})
        self.journal(owner,job.id,'EXAMINER_PASS' if passed else 'EXAMINER_FAIL',verdict,attempt_id=attempt_id,sha=sha)
        if passed:
            self._complete(owner,job,b,at,sha,verdict)
            return None
        _guarded_job_write(self.db,job.id,worker_id=str(attempt_id),lease_generation=at['generation'],set_sql="status='failed',completed_at=clock_timestamp(),current_phase='needs_fix'",params={})
        self.release(owner,job.id)
        return self.create_fix(owner,job.id)

    def _complete(self,owner,job,b,at,sha,verdict):
        # Lifecycle and evidence/event insertion share one caller-owned commit.
        _guarded_job_write(self.db,job.id,worker_id=str(at['id']),lease_generation=at['generation'],
          set_sql="status='completed',completed_at=clock_timestamp(),current_phase='verified',output_refs=CAST(:out AS json)",
          params={'out':json.dumps([{'sha':sha,'certification_proposal':str(verdict) if verdict else None}])})
        self.release(owner,job.id)
        self.journal(owner,job.id,'JOB_COMPLETED',at['id'],attempt_id=at['id'],sha=sha)

    def create_fix(self,owner,parent_id):
        self.lock(owner)
        parent=self.job(owner,parent_id)
        b=self.binding(owner,parent_id)
        self.authority(owner,b['assignment_id'],b['required'])
        if parent.status!=MainAIJobStatus.failed or parent.current_phase!='needs_fix':
            raise Rejected('FIX_NOT_REQUIRED')
        if b['fix_depth']>=3:
            self.journal(owner,parent_id,'REPLAN_REQUIRED','max_fix',reason='MAX_FIX_ATTEMPTS')
            return None
        child=identity(owner,parent_id,'fix',b['fix_depth']+1)
        if self.rows('SELECT id FROM mainai_jobs WHERE owner_id=:o AND id=:j',o=owner,j=child):
            return child
        sha=self.repository(b)
        job=MainAIJob(id=child,owner_id=owner,job_type='supervised_execution',created_by='supervision_fix',input_refs=[],output_refs=[])
        self.db.add(job)
        self.db.flush()
        self.bind(owner,child,program=b['program'],assignment_id=b['assignment_id'],worktree=b['worktree'],branch=b['branch'],base_sha=sha,required=b['required'],criteria=b['criteria'],portable=b['portable'],review_required=b['review_required'],fallbacks=[uuid.UUID(x) for x in b['fallback_assignments']],priority=b['priority']+1)
        self.db.execute(text('UPDATE mainai_supervision_bindings SET parent_job_id=:p,fix_depth=:d WHERE owner_id=:o AND job_id=:j'),dict(p=parent_id,d=b['fix_depth']+1,o=owner,j=child))
        self.journal(owner,parent_id,'FIX_CREATED',child,expected='NEW_SHA_REVIEW')
        return child

    def recover(self,owner,job_id):
        self.lock(owner)
        job=self.job(owner,job_id)
        b=self.binding(owner,job_id)
        sha=self.repository(b)
        self.journal(owner,job_id,'RECOVERY_INSPECTED',f'{job.lease_generation}:{sha}',sha=sha,recovery='NO_BLIND_REPLAY')
        if job.status in (MainAIJobStatus.cancelled,MainAIJobStatus.superseded,MainAIJobStatus.completed,MainAIJobStatus.failed):
            self.release(owner,job_id)
            return 'TERMINAL'
        # Commit discovered before result ingestion: preserve it; request fresh tests.
        if sha!=b['base_sha']:
            return 'VERIFY_EXISTING_SHA'
        if job.lease_expires_at and utc(job.lease_expires_at)<self.now():
            return 'QUARANTINE_EXPIRED_ATTEMPT'
        return 'OBSERVE_CURRENT_ATTEMPT'

    def validate_blocker(self,owner,attempt_id,*,blocker_id,category,python_candidates=()):
        job,b,_,_,_=self.current(owner,attempt_id)
        action='INVESTIGATE'
        if category in ('missing_code','failing_tests','missing_harness'):
            action='CONTINUE_SAME_JOB'
        elif category=='pytest_missing':
            for path in python_candidates[:8]:
                try:
                    result=subprocess.run([str(path),'-c','import pytest'],capture_output=True,timeout=5)
                    if result.returncode==0:
                        action='CONTINUE_SAME_JOB'
                        break
                except (OSError,subprocess.TimeoutExpired):
                    continue
        elif category=='provider_unavailable':
            action='RETRY_LATER'
        elif category=='merge_requested':
            action='FOUNDER_DECISION_REQUIRED'
        self.journal(owner,job.id,'BLOCKER_VALIDATED',blocker_id,attempt_id=attempt_id,reason=category if category in ('missing_code','failing_tests','missing_harness','pytest_missing','provider_unavailable','merge_requested') else 'UNKNOWN',action=action)
        return action
