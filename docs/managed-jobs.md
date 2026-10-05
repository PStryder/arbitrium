# Durable managed jobs: first milestone

This milestone adds a local Python API alongside the existing four session MCP
tools. Managed jobs and producer events require Windows 10 or newer. It does not register tasks, install services, enable persistent access,
configure another product, or expose a new remote execution tool. No production
approval provider or approved production templates ship with it. The scheduled
CLI entry point always refuses execution (exit code 3), before opening any files.

## Sessions remain stateful

`arbitrium_spawn`, `arbitrium_exec`, `arbitrium_list`, and `arbitrium_close` keep
their names and existing response fields. Omitted shell selection uses platform
auto-detection. Bash/POSIX, cmd, Windows PowerShell and PowerShell 7 use their own
command framing; cmd runs with AutoRun disabled and PowerShell without profiles.
Session variables and working directories persist. A total command timeout now
taints the stream: partial output is returned with `requires_close: true`, and
further execution is refused until the caller closes and respawns the session.
Timeout is not cancellation: the process may continue until closed. Existing
session transcripts retain their previous behavior; do not send secrets through
that unrestricted legacy interface. Managed jobs do not inherit session state.

## Trusted template and saved job API

Use `Template`, `Registry`, `Runner` from `arbitrium.jobs` in reviewed local code.
A template supplies an explicit executable, fixed argument vector, a finite
allowlist for every input slot, absolute working directory, environment reference,
deadline, optional reviewed asset paths and relative result paths. Shell scripts,
batch files and common shell executables are rejected. Interpreters require a
reviewed fixed program; never offer a free-form code/command input slot.

`Registry(root).save(template, values)` saves an immutable UUID job. Re-saving an
identical digest/specification returns the existing job ID. The saved
spec includes resolved argument values, cwd and environment reference, never an
environment dump. Do not place secrets in argument choices, paths or template IDs.
The template digest covers its definition, executable bytes and listed asset
bytes. Declare fixed scripts/configuration as assets. Unlisted imports and runtime
dependencies are not hashed; the deployment review must cover them and filesystem
permissions. Hash checks detect changes, but are not an OS sandbox or a substitute
for preventing concurrent modification of trusted files.

`Runner(registry, templates)` denies execution by default. An embedding application
must supply an `authorize(job, run_id)` callback returning exactly `True` only for
an exact action-time approval and a `resolve_environment(env_ref)` callback returning
a minimal environment. No production implementation of either is provided. There
is no command-line `--approve`, environment-variable bypass or approval inferred
from an event. Test callbacks approve only harmless temporary fixture programs.
Environment values are held in memory, never persisted, logged or emitted. Supply
needed OS variables explicitly; the parent process environment is not copied.

The runner executes an explicit executable and Windows-quoted argv without a shell.
Standard input and stdout/stderr use the null device. The registry is the structured lifecycle log;
raw application output capture is deliberately unavailable pending an approved
logging/redaction policy. Applications can write reviewed results to their own
working directory; configured result references appear only after exit zero.
References do not promise that a result exists, is safe to read, or contains no
secrets. Consumers must not automatically open them.

Native Windows Job Objects contain the process group. `CreateProcessW` uses
`PROC_THREAD_ATTRIBUTE_JOB_LIST` to assign containment atomically before user code
runs. Unsupported or denied containment fails launch; there is no uncontained
fallback. The unnamed Job Object handle is non-inheritable, uses KILL_ON_JOB_CLOSE
and permits no breakaway. Ordinary descendants remain in the group. Completion
waits for root exit and an empty group. Cancellation/deadline/shutdown terminate
the group and confirm it is empty. Abrupt worker death closes its job handle,
causing Windows to terminate the group. Reported exit_code is the root's; the
reviewed program must aggregate child failures itself.

This is lifecycle containment, not a hostile-code sandbox. Templates must not
delegate execution through WMI, services, brokers or external launch mechanisms.
See Microsoft's [Job Objects documentation](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
and [process creation attributes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute).

## Runs, cancellation and recovery

Each attempt gets a UUID run ID and owner UUID. SQLite WAL with FULL synchronous
mode stores status, created/started/finished timestamps, exit code, diagnostic PID,
30-second owner lease, cancellation request and per-run sequence. Status and its
outbox event commit in the same transaction. Only one queued, running or unknown
run is admitted per template ID/working-directory scope, including across registry
connections, revised templates and pre-existing duplicate saved-job rows. Renaming
a template to bypass this hold is not an authorized recovery procedure.

| Status | Meaning |
| --- | --- |
| queued | Reserved attempt, before launch intent |
| running | Launch intent committed; process may be starting or running |
| completed | Owned root exit zero observed and contained group empty |
| failed | Nonzero exit, launch failure or confirmed deadline termination |
| canceled | Approval denied, prelaunch cancellation, or contained group termination confirmed after a cancellation request |
| interrupted | Queued lease expired, or live worker stopped and contained group termination confirmed |
| unknown | Running lease expired or termination could not be confirmed |

`registry.cancel(run_id)` persists intent. The owning worker polls and confirms
termination using retained native process and Job Object handles.
No PID read from disk is ever killed or adopted. `runner.stop()` requests
worker shutdown; it is not a force-kill of another worker. The completion-versus-
cancellation race resolves to observed completion only once the group is empty.

`registry.recover()` leaves fresh leases alone. Expired queued attempts become
interrupted; expired running attempts become unknown. It does not inspect, resume,
kill or relaunch processes. A lease expiry can reflect a crash, reboot, blocked
worker or clock change; it proves neither process death nor absence of completed
side effects. Nothing here claims a process survives reboot. Unknown blocks
resubmission within its template ID/working-directory scope. This milestone
has no automatic unknown-resolution API: an operator must investigate ownership
and side effects before approving a new job or a future reconciliation facility.
For unknown, finished_at is the time observation ended, not proof of process exit.

## Version 1 producer event contract

Machine-readable shape: [job-events-v1.schema.json](job-events-v1.schema.json).
Canonical synthetic fixture: [job-event-v1.example.json](job-event-v1.example.json).
Shared validation cases: [job-event-v1.cases.json](job-event-v1.cases.json).
The [35 consumer-origin cases](job-event-v1.shared-cases.json) are also retained;
their UTC +00:00 case is explicitly corrected to accepted under the clarified
contract. The original consumer workspace file is unchanged.
Its IDs and path are illustrative; it is documentation, not a published event.
The entire UTF-8 JSON event must be at most 16 KiB. Reject unknown versions and
extra fields. UUID strings are lowercase canonical UUIDs. `source` is `arbitrium`;
`instance_id` persists in the registry. Do not clone a registry into a second active
producer with the same instance identity.

Each event contains `event_id`, `job_id`, `run_id`, `status`, `observed_at` (UTC
RFC3339 ending Z or +00:00, with at most six fractional digits; output uses Z),
and `sequence` (positive 32-bit integer, increasing per run;
this milestone emits at most three events per run). Context contains only
`template_id` (80-character identifier), fixed `reason_code` and nullable integer
`exit_code`. There is no arbitrary prose, command, payload or environment field.
Both reference arrays allow at most eight absolute local Windows paths, each at most 1024
characters with no controls, UNC/device prefixes, parent traversal or alternate streams.
Runtime validation additionally enforces local absolute
paths and the total serialized byte limit. `log_refs` names the SQLite registry;
use run_id to identify its lifecycle row. SQLite is live storage, not a text log.

Canonical constraints require all three context fields; identifiers use
`[A-Za-z0-9_.-]{1,80}`; UUIDs are lowercase canonical strings; sequence is
1 through 2147483647. Status/reason combinations are encoded in schema `allOf`;
completed requires exit_code exactly zero. The consumer should align its broader
initial parser to these constraints. JSON Schema validation must enable date-time
format checking. Strict JSON decoding must separately reject duplicate keys and
non-integer numeric representations for integer fields; the 16 KiB UTF-8 wire
limit is also enforced outside JSON Schema.

Normal lifecycle is queued → running → terminal (three events). Prelaunch denial
or interruption produces queued → terminal (two). `observed_at` is observation
time, not a promise of delivery order. Use instance_id/run_id/sequence for ordering,
and event_id for deduplication. Events, paths and context are data, never authority,
instructions, permission to execute, or permission to open another resource.

### Publication, retry and acknowledgment

The eventual flat inbox is `C:\Users\pstry\ExpergisSignals\job-events`.
No production default points there. `Publisher(registry, inbox, authorize=...)`
requires exact destination approval on every `publish()` call and an already
existing directory. Tests use only a temporary fake inbox.

Publication writes an exclusive temporary file in the destination directory,
flushes/fsyncs its contents and renames it to `<event_id>.json`. Windows rename
refuses to replace an existing filename. A matching existing event must have
identical bytes; differing content stops publication. SQLite serializes publishers.
Crash after rename but before the delivery transaction commits causes retry with
the same event ID and content. Consumers must therefore durably deduplicate before
relay, even if they have removed an earlier copy from the inbox. Do not interpret
temporary files. File fsync plus rename provides atomic visible content, but does
not promise immunity to all filesystem/hardware power-loss behavior.

`delivered_at` means published, not consumed or relayed. There is no acknowledgment
channel in v1. Expergis owns validation, durable deduplication and relay through
its single standing route. Neither producer nor consumer deletes, renames or
rewrites published JSON files. Retention requires a separately approved operator
archive policy; absence cannot imply successful processing. The current consumer
stops intake at 4096 flat entries or 10,000 durable dedup rows, preserving data for
operator review. These bounds do not constitute an acknowledgment protocol.
Unknown version, invalid content and unsafe references must not trigger actions.

The producer keeps at most 1000 delivered outbox rows, retains all undelivered rows,
and applies conservative admission backpressure at 10,000 pending/reserved event
slots (configurable in local code). Active runs reserve room for terminal events.
Publication batches are bounded to 1000, default 100. Orphan temporary files with
this instance's exact generated naming pattern are removed under the publisher
lock. Other producers' files are untouched. Job/run history remains durable until
an explicit future archive policy; there is no silent history purge. Inbox growth
without a consumer is an operational responsibility; no end-to-end acknowledgment
or global inbox retention is claimed by this milestone.

## Windows Task Scheduler adapter

`WindowsTaskScheduler(registry, templates).dry_run(TaskDefinition(...))` produces
XML and a report without contacting Task Scheduler or spawning a process. The
definition needs an existing saved job UUID, approved template mapping, explicit
Python runtime and cwd, timezone-bearing start timestamp and ordinary user SID.
The single TimeTrigger and task are disabled, use InteractiveToken/LeastPrivilege,
IgnoreNew and a bounded execution time. There are no credentials, logon/boot
triggers, elevation or second scheduler backend.

The Exec action invokes `python -I -m arbitrium.jobs_cli run --registry <root>
--job-id <UUID>`. Isolated Python requires the reviewed package to be installed in
that exact interpreter; it ignores PYTHONPATH and cwd imports. Arguments are quoted
for Windows, not concatenated shell commands. The CLI refuses execution even if
someone separately enables the generated task. `register()` always raises.

`validate()` parses XML and compares its structure to the exact generated disabled
profile, rejecting changed actions, extra elements, privilege changes or enablement.
It is not a full Windows XSD validator or a Task Scheduler service acceptance test.
Reference: Microsoft's [Task Scheduler schema](https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-schema)
and [Exec action](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-exec-actiongroup-element).

## Deployment gates

### What works now and the minimum next milestone

With a reviewed in-memory template mapping and fixture-only approval/environment
callbacks, the local API already runs a saved harmless job through containment,
durable status, cancellation/recovery, outbox publication to an approved temporary
inbox and disabled task-definition generation. Tests exercise this complete local
path, including abrupt worker death and descendant termination. There is no
automatic execution retry. Exactly-once external side effects are not promised.

Real scheduled execution still needs a trusted persistent template catalog and
environment resolver, a production action-time approval provider, and CLI wiring
to those components. The minimum provider should consume an expiring single-use
grant bound to the exact job ID, template digest, principal and scheduled occurrence
in a durable transaction. Absence, replay or mismatch must deny execution. Test
the complete scheduled entry point with temporary catalogs/grants before enabling
any real command. No arbitrary template/module path or general approval flag should
be exposed through the CLI. Task registration remains disabled in this milestone.

Metadata-only logging is a usable safe default and needs no output-capture policy
decision now. Capturing application stdout/stderr later requires a separately
reviewed bounded logging/redaction policy for that actual job. Unknown-run holds
remain permanent until a future explicit reconciliation action records operator
evidence and authorizes a fresh linked attempt; it must preserve the old unknown
observation rather than invent a successful completion. No hold is silently lifted.

The next user decisions are the exact real template/inputs, account, schedule and
required result/output handling. Installation, configured paths/permissions,
publication, task registration/enablement and actual execution each still require
the applicable exact operational approval. More isolated implementation and tests
do not require another permission request.

Before operational use, obtain exact approval for reviewed templates and assets,
environment-reference resolution, execution approval provider, runtime installation,
registry/log permissions, output policy, platform containment compatibility, inbox
publication destination, and each task registration/enablement. Validate the final
task with Windows after approval. Verify paths in Peter's ordinary terminal; a
worker-visible AppData path may be packaged-app redirected. Coordinate the consumer
separately with Expergis. No real jobs, production events, service installation,
credentials, shared configuration changes or Git push are part of this milestone.

Tests run entirely with disposable fixtures. From a development checkout use
`python -m pytest tests`; PowerShell/cmd tests skip on other platforms. Existing
Bash integration tests require an available POSIX shell. A test fixture isolates
working directories and session logs from the repository and production paths.
Managed-job fixture tests require Windows, matching this first producer backend.
