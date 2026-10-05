"""Persistent, data-only trusted templates and named environment references."""
import hashlib
import json
import os
from dataclasses import asdict, fields
from pathlib import Path
import re
from types import MappingProxyType

from .jobs import Template, canonical, identifier, local_reference


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError("Non-finite JSON number")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)


class Catalog:
    """A catalog is configuration, never an approval by itself.

    No Python import paths, callbacks, shell text, or environment values are loaded
    from this file. Grants pin its canonical digest and the exact template digest.
    """
    def __init__(self, document):
        if not isinstance(document, dict) or set(document) != {"version", "templates", "environments"} or type(document["version"]) is not int or document["version"] != 1:
            raise ValueError("Invalid catalog")
        definitions, profiles = document["templates"], document["environments"]
        if not isinstance(definitions, dict) or not 1 <= len(definitions) <= 64 or not isinstance(profiles, dict) or not 1 <= len(profiles) <= 64:
            raise ValueError("Catalog bounds exceeded")
        templates, pins = {}, {}
        for name, record in definitions.items():
            identifier(name)
            if not isinstance(record, dict) or set(record) != {"definition", "digest"}:
                raise ValueError("Invalid template record")
            definition = dict(record["definition"])
            if set(definition) != {field.name for field in fields(Template)}:
                raise ValueError("Unexpected template fields")
            for key in ("fixed_args", "result_paths", "assets"):
                if not isinstance(definition[key], list):
                    raise ValueError("Template argument arrays required")
                definition[key] = tuple(definition[key])
            if not isinstance(definition["choices"], list) or any(not isinstance(v, list) for v in definition["choices"]):
                raise ValueError("Template choices must be arrays")
            definition["choices"] = tuple(tuple(v) for v in definition["choices"])
            template = Template(**definition)
            template.validate()
            if template.template_id != name or not isinstance(record["digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["digest"]):
                raise ValueError("Invalid template identity or digest")
            templates[name], pins[name] = template, record["digest"]
        immutable_profiles = {}
        for name, profile in profiles.items():
            identifier(name)
            if not isinstance(profile, dict) or len(profile) > 64:
                raise ValueError("Invalid environment profile")
            for target, source in profile.items():
                if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", v) for v in (target, source)):
                    raise ValueError("Environment entries must be variable-name references")
            if len({v.upper() for v in profile}) != len(profile):
                raise ValueError("Duplicate Windows environment variable")
            immutable_profiles[name] = MappingProxyType(dict(profile))
        if any(t.env_ref not in profiles for t in templates.values()):
            raise ValueError("Missing environment profile")
        body = canonical(document).encode()
        if len(body) > 262144:
            raise ValueError("Catalog exceeds 256 KiB")
        self.digest = hashlib.sha256(body).hexdigest()
        self.templates = MappingProxyType(templates)
        self._pins = MappingProxyType(pins)
        self._profiles = MappingProxyType(immutable_profiles)
        self._body = body

    @classmethod
    def create(cls, path, templates, environment_refs):
        # Trusted development/operator API only. No CLI/MCP catalog-writing route.
        templates = tuple(templates)
        if len({t.template_id for t in templates}) != len(templates):
            raise ValueError("Duplicate template identifier")
        document = {"version": 1, "templates": {t.template_id: {"definition": asdict(t), "digest": t.digest()} for t in templates}, "environments": environment_refs}
        catalog = cls(strict_json(canonical(document)))
        path = Path(path)
        local_reference(str(path.resolve()))
        # Exclusive creation avoids overwriting an approved catalog accidentally.
        with path.open("xb") as stream:
            stream.write(catalog._body)
            stream.flush()
            os.fsync(stream.fileno())
        return catalog

    @classmethod
    def load(cls, path):
        path = Path(path)
        local_reference(str(path.resolve()))
        if path.is_symlink():
            raise ValueError("Catalog symlinks are not supported")
        with path.open("rb") as stream:
            raw = stream.read(262145)
        if len(raw) > 262144:
            raise ValueError("Catalog exceeds 256 KiB")
        return cls(strict_json(raw.decode("utf-8")))

    def validate_job(self, job):
        template = self.templates[job["template_id"]]
        if template.digest() != self._pins[template.template_id] or job["template_digest"] != self._pins[template.template_id] or template.spec(job["spec"]["values"]) != job["spec"]:
            raise PermissionError("Saved job or reviewed assets differ from catalog")
        return template

    def resolve_environment(self, reference):
        profile = self._profiles[reference]
        result = {}
        for target, source in profile.items():
            value = os.environ.get(source)
            if value is None or "\0" in value or len(value) > 32768:
                raise PermissionError("Required environment reference unavailable")
            result[target] = value
        return result
