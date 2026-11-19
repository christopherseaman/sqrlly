"""Load JSON Schemas and their local references into an offline registry."""

import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote, unquote, urldefrag, urljoin, urlsplit

from jsonschema import SchemaError
from jsonschema.validators import validator_for
from referencing import Registry, Resource
from referencing.exceptions import CannotDetermineSpecification, Unresolvable
from referencing.jsonschema import DRAFT202012


def load_validator(path: Path, root: Path):
    root = root.resolve()
    start = path.resolve().as_uri()
    registry = Registry()
    documents = {}
    references = []
    scopes = {}

    def load(uri):
        nonlocal registry
        if uri in documents:
            return
        parsed = urlsplit(uri)
        if parsed.scheme != "file" or parsed.netloc or parsed.query:
            raise ValueError(f"unsupported external schema reference {uri}")
        source = Path(unquote(parsed.path)).resolve()
        if not source.is_relative_to(root):
            raise ValueError(f"schema reference {source} resolves outside {root}")
        document = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(document, (bool, dict)):
            raise ValueError(f"{source}: expected an object or boolean schema")
        validator_for(document).check_schema(document)
        resource = Resource.from_contents(document, default_specification=DRAFT202012)
        documents[uri] = resource
        registry = registry.with_resource(uri, resource)
        visit(resource, uri)

    def visit(resource, base):
        base = urljoin(base, resource.id() or "")
        if isinstance(resource.contents, dict):
            scopes[id(resource.contents)] = (resource.contents, base, resource.id())
            for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                if key in resource.contents:
                    ref = resource.contents[key]
                    if urlsplit(ref).scheme not in ("", "file") or urlsplit(ref).netloc:
                        raise ValueError(f"unsupported external schema reference {ref}")
                    target, _ = urldefrag(urljoin(base, ref))
                    references.append((base, ref, target))
            for child in resource.subresources():
                visit(child, base)

    try:
        load(start)
        for base, ref, target in references:
            registry = registry.crawl()
            if target not in registry:
                load(target)
        registry = registry.crawl()
        for base, ref, _ in references:
            target = registry.resolver(base).lookup(ref).contents
            if not isinstance(target, bool) and id(target) not in scopes:
                raise ValueError(f"{base}: {ref} does not target a schema keyword")
    except (OSError, ValueError, SchemaError, Unresolvable, CannotDetermineSpecification) as exc:
        raise ValueError(f"{path}: invalid JSON schema: {exc}") from exc

    document = documents[start].contents
    dynamic = any("$dynamicRef" in original or "$recursiveRef" in original
                  for original, _, _ in scopes.values())
    if references and (not dynamic or len(documents) > 1):
        document = bundle_documents(start, documents, registry, scopes)
    return document, validator_for(document)(document, registry=Registry())


def bundle_documents(start, documents, registry, scopes):
    """Move local documents under $defs and rewrite static references by identity."""
    root = documents[start].contents
    if not isinstance(root, dict):
        return root
    pointers = {}
    memo = {}
    bundled = deepcopy(root, memo)
    definitions = bundled.setdefault("$defs", {}) if len(documents) > 1 else {}

    def index(value, pointer):
        if isinstance(value, (dict, bool)):
            pointers[id(value)] = pointer
        if isinstance(value, (list, dict)):
            for key, child in (value.items() if isinstance(value, dict) else enumerate(value)):
                token = quote(str(key).replace("~", "~0").replace("/", "~1"), safe="~")
                index(child, f"{pointer}/{token}")

    index(root, "#")
    for i, (uri, resource) in enumerate(documents.items()):
        if uri == start:
            continue
        name = f"sqrlly_{i}"
        while name in definitions:
            name += "_"
        if isinstance(resource.contents, dict) and "$schema" in resource.contents:
            if validator_for(resource.contents) != validator_for(root):
                raise ValueError(f"{uri}: mixed JSON Schema dialects across files are unsupported")
        definitions[name] = deepcopy(resource.contents, memo)
        index(resource.contents, f"#/$defs/{name}")
    for identity, (original, base, identifier) in scopes.items():
        copy = memo[identity]
        if "$dynamicRef" in original or "$recursiveRef" in original:
            raise ValueError(f"{base}: dynamic references across schema files are unsupported")
        if "$ref" in original:
            target = registry.resolver(base).lookup(original["$ref"]).contents
            copy["$ref"] = pointers[id(target)]
        for key in ("$id", "$anchor", "$dynamicAnchor", "$recursiveAnchor"):
            copy.pop(key, None)
        if "id" in original and identifier == original["id"]:
            copy.pop("id", None)
    return bundled



def provider_envelope(schema):
    """Wrap non-object results while preserving paths into the original document."""
    schema = deepcopy(schema)
    resource = Resource.from_contents(schema, default_specification=DRAFT202012)
    pending = [resource]
    while pending:
        part = pending.pop()
        if not isinstance(part.contents, dict):
            continue
        if "$dynamicRef" in part.contents or "$recursiveRef" in part.contents:
            raise ValueError("non-object provider schemas cannot use dynamic references")
        ref = part.contents.get("$ref")
        if ref == "#" or (ref and ref.startswith("#/") and
                          ref.split("/", 2)[1] not in ("$defs", "definitions")):
            part.contents["$ref"] = "#/properties/value" + ref[1:]
        pending.extend(part.subresources())
    definitions = {key: schema.pop(key) for key in ("$defs", "definitions")
                   if isinstance(schema, dict) and key in schema}
    return {
        **definitions, "type": "object", "properties": {"value": schema},
        "required": ["value"], "additionalProperties": False,
    }
