"""Only temporary catalogs/grants and harmless counting fixtures are exercised."""
import dataclasses
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from arbitrium.approvals import ApprovalStore, GrantRequest, ScheduleScope, current_user_sid
from arbitrium.catalog import Catalog
from arbitrium.jobs import Registry, Runner, Publisher
from arbitrium.jobs import Template, canonical
from arbitrium.jobs_cli import main
from arbitrium.scheduler import TaskDefinition, WindowsTaskScheduler

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows account-bound grants')


@pytest.fixture
def configured(tmp_path, monkeypatch):
    script = tmp_path / 'fixture.py'
    script.write_text("import os\nfrom pathlib import Path\np=Path('count.txt')\np.write_text(str(int(p.read_text())+1) if p.exists() else '1')\nprint(os.environ.get('FIXTURE_SECRET',''))\n")
    template = Template('counter-v1', sys.executable, ('-I', str(script)), (), str(tmp_path), 'fixture-env', 3, assets=(str(script),))
    registry = Registry(tmp_path / 'state')
    catalog = Catalog.create(registry.root / 'catalog.json', [template], {'fixture-env': {'SystemRoot':'SystemRoot', 'FIXTURE_SECRET':'ARBITRIUM_TEST_SECRET'}})
    monkeypatch.setenv('ARBITRIUM_TEST_SECRET', 'never-persist-this-value')
    job_id = registry.save(template)
    store = ApprovalStore(registry, catalog)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return registry, template, catalog, store, job_id, now


def issue(configured, *, count=1, interval=None, start=None):
    registry, template, catalog, store, job_id, now = configured
    scope = ScheduleScope(str(uuid.uuid4()), (start or now).isoformat(), count, interval, 30)
    request = GrantRequest(job_id, current_user_sid(), scope, 'synthetic-approval', sys.executable, template.cwd)
    # A test-only reviewer verifies the exact harmless program is in this fixture.
    def approve(review):
        assert review['job']['job_id'] == job_id
        assert Path(review['job']['spec']['cwd']) == Path(template.cwd)
        assert review['catalog_digest'] == catalog.digest
        assert review['schedule'] == dataclasses.asdict(scope)
        return True
    return store.issue(request, authorize=approve), scope, request


def execute(configured, grant_id, now=None):
    registry, template, catalog, store, job_id, _ = configured
    return Runner(registry, catalog.templates,
                  authorize=lambda job, run: store.authorize(grant_id, job, run, now=now),
                  resolve_environment=catalog.resolve_environment).execute(job_id)


def test_grant_issuance_requires_review_and_cannot_choose_another_account(configured):
    registry, template, catalog, store, job_id, now = configured
    request = GrantRequest(job_id, current_user_sid(), ScheduleScope(str(uuid.uuid4()), now.isoformat()), 'test', sys.executable, template.cwd)
    with pytest.raises(PermissionError):
        store.issue(request)
    with pytest.raises(PermissionError):
        store.issue(request, authorize=lambda review: False)
    with pytest.raises(PermissionError):
        store.issue(dataclasses.replace(request, principal_sid='S-1-5-21-1-2-3-9999'), authorize=lambda review: True)
    assert not (Path(template.cwd) / 'count.txt').exists()


def test_catalog_restart_and_single_use_cli_execution(configured, capsys):
    registry, template, catalog, store, job_id, now = configured
    grant_id, _, _ = issue(configured)
    args = ['run', '--registry', str(registry.root), '--job-id', job_id, '--grant-id', grant_id]
    assert Catalog.load(registry.root / 'catalog.json').digest == catalog.digest
    assert main(args) == 0
    assert main(args) == 3
    assert (Path(template.cwd) / 'count.txt').read_text() == '1'
    output = capsys.readouterr()
    assert 'never-persist-this-value' not in output.out + output.err
    assert 'never-persist-this-value' not in (registry.root / 'catalog.json').read_text()
    with registry.connection() as db:
        assert db.execute('SELECT count(*) FROM grant_claims').fetchone()[0] == 1
        assert 'never-persist-this-value' not in '\n'.join(db.iterdump())
    inbox = Path(template.cwd) / 'fake-inbox'
    inbox.mkdir()
    assert Publisher(registry, inbox, authorize=lambda destination: destination == inbox).publish() == 5
    # One successful occurrence and one denied replay; no second fixture execution.
    observed = [json.loads(path.read_text()) for path in inbox.glob('*.json')]
    assert sum(event['status'] == 'completed' for event in observed) == 1
    assert sum(event['context']['reason_code'] == 'approval_denied' for event in observed) == 1


def test_fresh_process_cli_and_replay_use_persistent_grant(configured):
    registry, template, catalog, store, job_id, now = configured
    grant_id, _, _ = issue(configured)
    source = str(Path(__file__).resolve().parents[1] / 'src')
    bootstrap = 'import sys; sys.path.insert(0, ' + repr(source) + '); from arbitrium.jobs_cli import main; raise SystemExit(main())'
    command = [sys.executable, '-I', '-c', bootstrap, 'run', '--registry', str(registry.root), '--job-id', job_id, '--grant-id', grant_id]
    # Only named variables needed by the fixture; never copy the parent environment.
    env = {'SystemRoot': os.environ['SystemRoot'], 'ARBITRIUM_TEST_SECRET':'never-persist-this-value'}
    first = subprocess.run(command, cwd=template.cwd, env=env, capture_output=True, timeout=10)
    second = subprocess.run(command, cwd=template.cwd, env=env, capture_output=True, timeout=10)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 3, second.stderr
    assert (Path(template.cwd) / 'count.txt').read_text() == '1'
    assert b'never-persist-this-value' not in first.stdout + first.stderr + second.stdout + second.stderr


def test_bounded_recurring_grant_needs_one_review_and_consumes_each_occurrence_once(configured):
    registry, template, catalog, store, job_id, now = configured
    grant_id, scope, _ = issue(configured, count=3, interval=60)
    for index in range(3):
        moment = now + timedelta(seconds=index * 60)
        assert execute(configured, grant_id, moment)['status'] == 'completed'
        assert execute(configured, grant_id, moment)['reason_code'] == 'approval_denied'
    assert execute(configured, grant_id, now + timedelta(seconds=180))['reason_code'] == 'approval_denied'
    assert (Path(template.cwd) / 'count.txt').read_text() == '3'
    with registry.connection() as db:
        assert db.execute('SELECT count(*) FROM grants').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM grant_claims').fetchone()[0] == 3


def test_missed_occurrence_not_caught_up_and_revocation_stops_future(configured):
    registry, template, catalog, store, job_id, now = configured
    grant_id, _, _ = issue(configured, count=3, interval=60)
    assert execute(configured, grant_id, now - timedelta(seconds=1))['reason_code'] == 'approval_denied'
    assert execute(configured, grant_id, now + timedelta(seconds=45))['reason_code'] == 'approval_denied'
    assert execute(configured, grant_id, now + timedelta(seconds=60))['status'] == 'completed'
    assert store.revoke(grant_id)
    assert execute(configured, grant_id, now + timedelta(seconds=120))['reason_code'] == 'approval_denied'
    assert (Path(template.cwd) / 'count.txt').read_text() == '1'


def test_concurrent_consumption_has_exactly_one_winner(configured):
    registry, template, catalog, store, job_id, now = configured
    grant_id, _, _ = issue(configured)
    run_id = registry.reserve(job_id, str(uuid.uuid4()))
    def claim(_):
        reopened = ApprovalStore(Registry(registry.root), Catalog.load(registry.root / 'catalog.json'))
        return reopened.authorize(grant_id, registry.job(job_id), run_id, now=now)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sorted(results) == [False, True]


def test_crash_after_claim_never_refunds_occurrence(configured):
    registry, template, catalog, store, job_id, now = configured
    grant_id, _, _ = issue(configured)
    run_id = registry.reserve(job_id, str(uuid.uuid4()))
    assert store.authorize(grant_id, registry.job(job_id), run_id, now=now)
    with registry.connection() as db:
        db.execute('UPDATE runs SET lease_until=0')
    assert registry.recover() == [run_id]
    assert registry.run(run_id)['status'] == 'interrupted'
    assert execute(configured, grant_id, now)['reason_code'] == 'approval_denied'
    assert not (Path(template.cwd) / 'count.txt').exists()


def test_unknown_run_blocks_later_recurring_occurrence(configured):
    registry, template, catalog, store, job_id, now = configured
    grant_id, _, _ = issue(configured, count=2, interval=60)
    owner = str(uuid.uuid4())
    run_id = registry.reserve(job_id, owner)
    assert store.authorize(grant_id, registry.job(job_id), run_id, now=now)
    registry.transition(run_id, owner, 'running', 'started')
    with registry.connection() as db:
        db.execute('UPDATE runs SET lease_until=0')
    registry.recover()
    with pytest.raises(sqlite3.IntegrityError):
        execute(configured, grant_id, now + timedelta(seconds=60))
    assert not (Path(template.cwd) / 'count.txt').exists()


def test_account_runtime_and_catalog_changes_deny_existing_grant(configured):
    registry, template, catalog, store, job_id, now = configured
    grant_id, _, _ = issue(configured)
    with patch('arbitrium.approvals.current_user_sid', return_value='S-1-5-21-1-2-3-9999'):
        assert execute(configured, grant_id, now)['reason_code'] == 'approval_denied'
    with patch('arbitrium.approvals.runtime_scope', return_value={}):
        assert execute(configured, grant_id, now)['reason_code'] == 'approval_denied'
    document = json.loads((registry.root / 'catalog.json').read_text())
    document['environments']['fixture-env']['FIXTURE_SECRET'] = 'DIFFERENT_SOURCE'
    changed = ApprovalStore(registry, Catalog(document))
    run_id = registry.reserve(job_id, str(uuid.uuid4()))
    assert not changed.authorize(grant_id, registry.job(job_id), run_id, now=now)
    assert not (Path(template.cwd) / 'count.txt').exists()


def test_catalog_rejects_duplicate_keys_missing_env_and_modified_assets(configured, monkeypatch):
    registry, template, catalog, store, job_id, now = configured
    monkeypatch.delenv('ARBITRIUM_TEST_SECRET')
    with pytest.raises(PermissionError):
        catalog.resolve_environment('fixture-env')
    Path(template.assets[0]).write_text('raise SystemExit(9)\n')
    with pytest.raises(PermissionError):
        catalog.validate_job(registry.job(job_id))
    (registry.root / 'catalog.json').write_text('{"version":1,"version":1}')
    with pytest.raises(ValueError, match='Duplicate'):
        Catalog.load(registry.root / 'catalog.json')


def test_disabled_scheduler_definition_binds_exact_recurring_grant(configured):
    registry, template, catalog, store, job_id, now = configured
    grant_id, scope, request = issue(configured, count=3, interval=60)
    definition = TaskDefinition(job_id, request.runner_executable, request.runner_cwd, scope.start_at, request.principal_sid, grant_id)
    adapter = WindowsTaskScheduler(registry, catalog.templates)
    report = adapter.dry_run(definition)
    assert report['registered'] is False and report['enabled'] is False and report['grant_bound'] is True
    assert '<Interval>PT60S</Interval>' in report['xml'] and '--grant-id' in report['xml']
    assert '<Enabled>true</Enabled>' not in report['xml']
    with pytest.raises(ValueError):
        adapter.generate(dataclasses.replace(definition, start_at=(now + timedelta(seconds=1)).isoformat()))
    with pytest.raises(PermissionError):
        adapter.register(definition)


@pytest.mark.parametrize('kwargs', [{'occurrences':0}, {'occurrences':10001}, {'occurrences':2,'interval_seconds':1}, {'occurrences':2,'interval_seconds':60,'grace_seconds':60}, {'occurrences':10000,'interval_seconds':86400}])
def test_unbounded_or_overlapping_schedule_denied(kwargs):
    scope = ScheduleScope(str(uuid.uuid4()), '2030-01-01T00:00:00Z', **kwargs)
    with pytest.raises(ValueError):
        scope.validate()
