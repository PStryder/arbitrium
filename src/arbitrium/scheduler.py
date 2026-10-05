"""Windows Task Scheduler definition adapter; no registration API."""
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .jobs import Registry, Template, bounded, uuid_id

NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
ET.register_namespace("", NS)


@dataclass(frozen=True)
class TaskDefinition:
    job_id: str
    python_executable: str
    working_directory: str
    start_at: str
    user_id: str


class WindowsTaskScheduler:
    """Generate only disabled, least-privilege, interactive-token one-shot tasks.

    Approval to save a template is not approval to run it or register a task.
    Validation checks our narrow generated profile, not the complete Windows XSD.
    """
    def __init__(self, registry: Registry, templates: dict[str, Template]):
        self.registry = registry
        self.templates = dict(templates)

    def generate(self, definition: TaskDefinition) -> str:
        uuid_id(definition.job_id)
        job = self.registry.job(definition.job_id)
        template = self.templates[job["template_id"]]
        if template.digest() != job["template_digest"]:
            raise ValueError("Saved job template no longer approved")
        if template.spec(job["spec"]["values"]) != job["spec"]:
            raise ValueError("Saved job does not match approved template")
        for path in (definition.python_executable, definition.working_directory, str(self.registry.root)):
            bounded(path, 260)
            if not Path(path).is_absolute() or "%" in path:
                raise ValueError("Literal absolute scheduler paths required")
        if not Path(definition.python_executable).is_file() or not Path(definition.working_directory).is_dir():
            raise ValueError("Scheduler runtime paths must exist")
        if Path(definition.python_executable).stem.lower() not in {"python", "python3", "pythonw"}:
            raise ValueError("An explicit Python runtime is required")
        bounded(definition.user_id, 184)
        # SID only: avoid ambiguous names and SYSTEM/service principals.
        import re
        if not re.fullmatch(r"S-1-5-21-(?:\d+-){3}\d+", definition.user_id):
            raise ValueError("An explicit ordinary user SID is required")
        start = datetime.fromisoformat(definition.start_at.replace("Z", "+00:00"))
        if start.tzinfo is None:
            raise ValueError("Start timestamp must include a timezone")
        task = ET.Element(f"{{{NS}}}Task", {"version": "1.3"})

        def child(parent, tag, text=None, **attrs):
            node = ET.SubElement(parent, f"{{{NS}}}{tag}", attrs)
            node.text = text
            return node

        triggers = child(task, "Triggers")
        trigger = child(triggers, "TimeTrigger")
        child(trigger, "Enabled", "false")
        child(trigger, "StartBoundary", start.isoformat())
        principals = child(task, "Principals")
        principal = child(principals, "Principal", id="ArbitriumUser")
        child(principal, "UserId", definition.user_id)
        child(principal, "LogonType", "InteractiveToken")
        child(principal, "RunLevel", "LeastPrivilege")
        settings = child(task, "Settings")
        child(settings, "MultipleInstancesPolicy", "IgnoreNew")
        child(settings, "StartWhenAvailable", "false")
        child(settings, "Enabled", "false")
        child(settings, "ExecutionTimeLimit", f"PT{template.timeout_seconds + 30}S")
        actions = child(task, "Actions", Context="ArbitriumUser")
        action = child(actions, "Exec")
        child(action, "Command", definition.python_executable)
        # Isolated Python ignores PYTHONPATH and the working directory for imports.
        # Deployment must install the reviewed package into this exact interpreter.
        child(action, "Arguments", subprocess.list2cmdline(["-I", "-m", "arbitrium.jobs_cli", "run", "--registry", str(self.registry.root), "--job-id", definition.job_id]))
        child(action, "WorkingDirectory", definition.working_directory)
        return ET.tostring(task, encoding="unicode", xml_declaration=True)

    def validate(self, xml: str, definition: TaskDefinition):
        if not isinstance(xml, str) or len(xml.encode()) > 16384 or "<!" in xml:
            raise ValueError("Invalid task XML")
        actual = ET.fromstring(xml)
        expected = ET.fromstring(self.generate(definition))

        def structure(node):
            return node.tag, sorted(node.attrib.items()), (node.text or "").strip(), tuple(structure(c) for c in node)

        if structure(actual) != structure(expected):
            raise ValueError("Task differs from approved disabled profile")
        return True

    def dry_run(self, definition: TaskDefinition):
        xml = self.generate(definition)
        self.validate(xml, definition)
        return {"adapter": "windows-task-scheduler", "job_id": definition.job_id,
                "registered": False, "enabled": False, "execution_available": False, "xml": xml}

    def register(self, *args, **kwargs):
        raise PermissionError("Task registration is disabled; exact action-time approval and deployment review are required")
