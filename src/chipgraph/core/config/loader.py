"""Find, read, merge and resolve the layered configuration (DESIGN 8.3, 8.4, 8.6, 12.6).

Layer order, later overrides earlier::

    tool defaults -> each `extends` entry, in order (recursively, ancestors first)
                  -> project `.chipgraph.yml`

The personal user layer (`~/.config/chipgraph/user.yml`) is resolved separately and
applied only to its own keys (autonomy, notify, answer_language, prefer_models, editor);
it never merges into the project's artifact conventions.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import yaml
from pydantic import ValidationError

from chipgraph.core.config.defaults import DEFAULT_PROFILE
from chipgraph.core.config.errors import ConfigError
from chipgraph.core.config.models import Level, PathRule, Profile, UserConfig

SourceKind = Literal["tool", "org", "preset", "path", "git", "project", "user"]

_LEVEL_ORDER: dict[Level, int] = {"L1": 1, "L2": 2, "L3": 3, "L4": 4, "L5": 5}

_UNSET_LOCATION = "(unset, model default)"


class SourceFetcher(Protocol):
    """Fetches a git-hosted rule repository, pinned to a ref, to a local directory."""

    def fetch(self, url: str, ref: str) -> tuple[Path, str]:
        """Return (local directory containing the fetched profile, resolved commit)."""
        ...


@dataclass(frozen=True)
class SourceRef:
    """Where one merged value came from: which layer, which file/URL, which version."""

    kind: SourceKind
    location: str
    version: str


@dataclass(frozen=True)
class ConfigIssue:
    """One consistency problem found by `ResolvedProfile.check()`."""

    severity: Literal["error", "warning", "info"]
    key: str
    message: str


@dataclass
class _Layer:
    source: SourceRef
    data: dict[str, Any]


# --------------------------------------------------------------------------------------
# YAML reading
# --------------------------------------------------------------------------------------


def _read_yaml_with_hash(path: Path) -> tuple[Any, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read file: {exc}", file=path) from exc
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        return yaml.safe_load(text), digest
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = (mark.line + 1) if mark is not None else None
        raise ConfigError(f"invalid YAML: {exc}", file=path, line=line) from exc


def _sha256_json(data: Any) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# find_profile
# --------------------------------------------------------------------------------------


def find_profile(start: Path) -> Path | None:
    """Walk up from `start` to the nearest directory with `.chipgraph.yml`.

    Stops (inclusive) at the first directory containing `.git`, or at the filesystem root.
    """
    current = start.resolve()
    if current.is_file():
        current = current.parent
    while True:
        candidate = current / ".chipgraph.yml"
        if candidate.is_file():
            return candidate
        if (current / ".git").exists():
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent


# --------------------------------------------------------------------------------------
# extends resolution
# --------------------------------------------------------------------------------------


def _split_extends_entry(entry: str) -> tuple[Literal["org", "preset", "path", "git"], str]:
    if entry.startswith("git+"):
        return "git", entry[len("git+") :]
    if ":" not in entry:
        raise ConfigError(
            f"invalid extends entry {entry!r}: expected 'org:'/'preset:'/'path:'/'git+'"
        )
    kind, rest = entry.split(":", 1)
    if kind in ("org", "preset", "path"):
        return cast(Literal["org", "preset", "path"], kind), rest
    raise ConfigError(f"invalid extends entry {entry!r}: unknown kind {kind!r}")


def _load_layer_chain(
    file_path: Path,
    kind: SourceKind,
    location: str,
    data_dir: Path | None,
    fetcher: SourceFetcher | None,
    visited: frozenset[str],
    *,
    version: str | None = None,
) -> list[_Layer]:
    """Load one profile file, recursing into its own `extends` first (ancestors first)."""
    key = str(file_path)
    if key in visited:
        raise ConfigError(f"cycle in 'extends': {key} is already being loaded", file=file_path)
    visited = visited | {key}

    raw, file_hash = _read_yaml_with_hash(file_path)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("profile must be a YAML mapping", file=file_path)

    source = SourceRef(
        kind=kind, location=location, version=version if version is not None else file_hash
    )

    layers: list[_Layer] = []
    for entry in raw.get("extends", []) or []:
        layers.extend(_resolve_extends(entry, file_path.parent, data_dir, fetcher, visited))
    layers.append(_Layer(source=source, data=raw))
    return layers


def _resolve_extends(
    entry: str,
    referencing_dir: Path,
    data_dir: Path | None,
    fetcher: SourceFetcher | None,
    visited: frozenset[str],
) -> list[_Layer]:
    kind, rest = _split_extends_entry(entry)

    if kind == "path":
        target = (referencing_dir / rest).resolve()
        if not target.is_file():
            raise ConfigError(f"extends entry {entry!r}: no file at {target}")
        return _load_layer_chain(target, "path", str(target), data_dir, fetcher, visited)

    if kind == "org" or kind == "preset":
        if data_dir is None:
            raise ConfigError(
                f"extends entry {entry!r} needs a data_dir with an '{kind}s/' directory; "
                "none was found or given"
            )
        target = data_dir / f"{kind}s" / rest / "profile.yml"
        if not target.is_file():
            raise ConfigError(f"extends entry {entry!r}: no profile.yml at {target}")
        return _load_layer_chain(target, kind, entry, data_dir, fetcher, visited)

    if kind == "git":
        if "@" not in rest:
            raise ConfigError(f"extends entry {entry!r}: pin a tag or commit ('@<ref>')")
        url, ref = rest.rsplit("@", 1)
        if fetcher is None:
            raise ConfigError(
                f"extends entry {entry!r} needs a git fetcher for {url!r}, none given"
            )
        local_dir, commit = fetcher.fetch(url, ref)
        target = local_dir / "profile.yml"
        if not target.is_file():
            raise ConfigError(f"extends entry {entry!r}: no profile.yml in {local_dir}")
        return _load_layer_chain(target, "git", url, data_dir, fetcher, visited, version=commit)

    raise ConfigError(f"invalid extends entry {entry!r}")  # pragma: no cover - unreachable


# --------------------------------------------------------------------------------------
# merge with provenance
# --------------------------------------------------------------------------------------


def _merge(
    base: Mapping[str, Any],
    overlay: Mapping[str, Any],
    source: SourceRef,
    provenance: dict[str, SourceRef],
    prefix: str = "",
) -> dict[str, Any]:
    """Recursively merge `overlay` over `base`; mappings merge, everything else replaces."""
    result = dict(base)
    for raw_key, value in overlay.items():
        key = str(raw_key)
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            base_value = base.get(key)
            base_sub = base_value if isinstance(base_value, Mapping) else {}
            result[key] = _merge(base_sub, value, source, provenance, path)
        else:
            result[key] = value
            provenance[path] = source
    return result


def _record_leaves(
    data: Any, source: SourceRef, provenance: dict[str, SourceRef], prefix: str
) -> None:
    if isinstance(data, Mapping):
        for raw_key, value in data.items():
            path = f"{prefix}.{raw_key}" if prefix else str(raw_key)
            _record_leaves(value, source, provenance, path)
    else:
        provenance[prefix] = source


def _collect_leaves(
    data: Any,
    prefix: str,
    provenance: Mapping[str, SourceRef],
    out: list[tuple[str, object, SourceRef]],
    fallback_kind: SourceKind,
) -> None:
    if isinstance(data, Mapping):
        for raw_key, value in data.items():
            path = f"{prefix}.{raw_key}" if prefix else str(raw_key)
            _collect_leaves(value, path, provenance, out, fallback_kind)
        return
    source = provenance.get(prefix)
    if source is None:
        source = SourceRef(kind=fallback_kind, location=_UNSET_LOCATION, version="")
    if isinstance(data, list):
        data = tuple(data)
    out.append((prefix, data, source))


def _loc_to_key(loc: tuple[Any, ...]) -> str:
    return ".".join(str(part) for part in loc)


def _find_provenance(provenance: Mapping[str, SourceRef], key: str) -> SourceRef | None:
    if key in provenance:
        return provenance[key]
    # fall back to the closest ancestor or descendant prefix we do have provenance for
    for candidate in sorted(provenance, key=len, reverse=True):
        if key.startswith(candidate + ".") or candidate.startswith(key + "."):
            return provenance[candidate]
    return None


def _translate_validation_error(
    exc: ValidationError, provenance: Mapping[str, SourceRef], fallback_file: str
) -> ConfigError:
    first = exc.errors()[0]
    key = _loc_to_key(first["loc"])
    source = _find_provenance(provenance, key) if key else None
    file = source.location if source is not None else fallback_file
    return ConfigError(str(first["msg"]), file=file, key=key or None)


# --------------------------------------------------------------------------------------
# defaults for data_dir / user config path
# --------------------------------------------------------------------------------------


def builtin_data_dir() -> Path | None:
    """The directory holding the `orgs/` and `presets/` shipped with chipgraph (D36).

    It is the `chipgraph` package directory, found through `importlib.resources` so it is
    the same from a checkout and from a wheel. `None` if the package is not on a real
    filesystem or ships neither `orgs/` nor `presets/`.
    """
    root = resources.files("chipgraph")
    if not isinstance(root, Path):
        return None
    if (root / "orgs").is_dir() or (root / "presets").is_dir():
        return root
    return None


def _default_data_dir() -> Path | None:
    return builtin_data_dir()


def resolve_data_ref(ref: str, base_dir: Path, data_dir: Path | None = None) -> Path:
    """Resolve a data file reference from a profile to an existing file.

    - `org:<org>/<file>` and `preset:<preset>/<file>` resolve under `data_dir/orgs/` and
      `data_dir/presets/` (default: `builtin_data_dir()`), e.g. `org:qnsc/naming-v1.yml`.
    - `path:<rel>` or a plain relative path resolves against `base_dir`; an absolute path
      is used as is.

    Raises `ConfigError` if the reference is malformed or the file does not exist.
    """
    kind, sep, rest = ref.partition(":")
    if sep and kind in ("org", "preset"):
        root = data_dir if data_dir is not None else builtin_data_dir()
        if root is None:
            raise ConfigError(f"reference {ref!r} needs a data_dir with '{kind}s/'; none found")
        if "/" not in rest or rest.startswith("/") or ".." in Path(rest).parts:
            raise ConfigError(f"invalid reference {ref!r}: expected '{kind}:<name>/<file>'")
        target = root / f"{kind}s" / rest
    else:
        rel = rest if sep and kind == "path" else ref
        target = base_dir / rel
    if not target.is_file():
        raise ConfigError(f"reference {ref!r}: no file at {target}")
    return target


def _default_user_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "chipgraph" / "user.yml"


# --------------------------------------------------------------------------------------
# ResolvedProfile
# --------------------------------------------------------------------------------------


@dataclass
class ResolvedProfile:
    """The fully merged project profile, plus the personal layer and full provenance."""

    profile: Profile
    user: UserConfig
    sources: tuple[SourceRef, ...]
    profile_hash: str
    provenance: dict[str, SourceRef] = field(default_factory=dict)

    def explain(self) -> list[tuple[str, object, SourceRef]]:
        """Every leaf value (dotted key, value, source), sorted by key.

        Backs `chipgraph config show --explain`. User-layer leaves are prefixed `user.`.
        """
        leaves: list[tuple[str, object, SourceRef]] = []
        _collect_leaves(self.profile.model_dump(mode="python"), "", self.provenance, leaves, "tool")
        _collect_leaves(
            self.user.model_dump(mode="python"), "user", self.provenance, leaves, "user"
        )
        leaves.sort(key=lambda item: item[0])
        return leaves

    def check(self) -> list[ConfigIssue]:
        """Consistency problems: user overreach, duplicate layout templates, local plugins."""
        issues: list[ConfigIssue] = []
        for area, user_level in self.user.autonomy.items():
            project_level = self.profile.autonomy.get(area)
            if project_level is not None and _LEVEL_ORDER[user_level] > _LEVEL_ORDER[project_level]:
                issues.append(
                    ConfigIssue(
                        severity="error",
                        key=f"autonomy.{area}",
                        message="the user layer may lower autonomy, not raise it",
                    )
                )

        allowed_models = set(self.profile.models.tiers.values()) | set(
            self.profile.models.alt.values()
        )
        for tier, model in self.user.prefer_models.items():
            if model not in allowed_models:
                issues.append(
                    ConfigIssue(
                        severity="error",
                        key=f"user.prefer_models.{tier}",
                        message=f"model {model!r} is not in this project's models.tiers/alt",
                    )
                )

        seen: dict[str, str] = {}
        for name, template in self.profile.layout.items():
            if not isinstance(template, str):
                continue
            other = next((k for k, v in seen.items() if v == template), None)
            if other is not None:
                issues.append(
                    ConfigIssue(
                        severity="error",
                        key=f"layout.{name}",
                        message=f"layout template is identical to 'layout.{other}'",
                    )
                )
            seen[name] = template

        instance_owner: dict[str, str] = {}
        for block_name in sorted(self.profile.blocks):
            override = self.profile.blocks[block_name]
            for instance in override.instances:
                other = instance_owner.get(instance)
                if other is not None:
                    issues.append(
                        ConfigIssue(
                            severity="error",
                            key=f"blocks.{block_name}.instances",
                            message=(
                                f"instance {instance!r} is already an instance of block {other!r}"
                            ),
                        )
                    )
                else:
                    instance_owner[instance] = block_name

        for index, plugin in enumerate(self.profile.plugins):
            issues.append(
                ConfigIssue(
                    severity="info",
                    key=f"plugins[{index}]",
                    message=f"local plugin {plugin!r} runs code: review it like code",
                )
            )

        return issues

    def effective_autonomy(self, area: str) -> Level:
        """The effective autonomy for `area`: min(project, user), user only ever lowers it."""
        project_level: Level = self.profile.autonomy.get(area, "L3")
        user_level = self.user.autonomy.get(area)
        if user_level is None:
            return project_level
        return (
            user_level if _LEVEL_ORDER[user_level] < _LEVEL_ORDER[project_level] else project_level
        )

    def for_block(self, name: str) -> Profile:
        """The profile with `blocks[name]` merged over it, if any.

        Only the fields the block sets are merged. `instances` is a block-level fact (D38),
        not a `Profile` field, so it is dropped from the overlay here; it is consumed
        directly from `profile.blocks` by ingest.
        """
        override = self.profile.blocks.get(name)
        if override is None:
            return self.profile
        base = self.profile.model_dump(mode="python")
        # Only what the block sets: an unset nested field (say `spec.requirements.infer`)
        # must not reset the project's value to the model default.
        overlay = override.model_dump(mode="python", exclude_unset=True)
        overlay.pop("instances", None)
        dummy_source = SourceRef(kind="project", location="", version="")
        merged = _merge(base, overlay, dummy_source, {})
        return Profile.model_validate(merged)

    def for_path(self, rel_path: str) -> PathRule:
        """Combine every `paths` glob matching `rel_path`; more specific (longer) wins."""
        matches = [
            (pattern, rule)
            for pattern, rule in self.profile.paths.items()
            if fnmatch.fnmatchcase(rel_path, pattern)
        ]
        matches.sort(key=lambda pair: len(pair[0]))

        checks: dict[str, Literal["on", "off"]] = {}
        write: Literal["allow", "deny"] = "allow"
        reason: str | None = None
        for _, rule in matches:
            checks.update(rule.checks)
            write = rule.write
            if rule.reason is not None:
                reason = rule.reason
        return PathRule(checks=checks, write=write, reason=reason)


# --------------------------------------------------------------------------------------
# load()
# --------------------------------------------------------------------------------------


def _enforce_tighten_only_policy(layers: list[_Layer]) -> None:
    """`policy.local_plugins` may only tighten across layers.

    Any layer may set it to ``deny``; once a layer has denied, no later layer may set it
    back to ``allow``. Raises `ConfigError` naming the offending file and the layer that
    denied. Layers that do not mention the key are ignored.
    """
    denied_by: SourceRef | None = None
    for layer in layers:
        policy = layer.data.get("policy")
        if not isinstance(policy, Mapping) or "local_plugins" not in policy:
            continue
        value = policy["local_plugins"]
        if value == "deny":
            denied_by = layer.source
        elif value == "allow" and denied_by is not None:
            raise ConfigError(
                "policy.local_plugins is tighten-only: cannot set 'allow' after "
                f"{denied_by.location!r} ({denied_by.kind}) set 'deny'",
                file=layer.source.location,
                key="policy.local_plugins",
            )


def load(
    root: Path,
    *,
    profile_path: Path | None = None,
    user_config: Path | None = None,
    data_dir: Path | None = None,
    fetcher: SourceFetcher | None = None,
) -> ResolvedProfile | None:
    """Find, read and merge every configuration layer under `root`.

    Returns `None` when no project profile exists (callers then allow only read-only
    commands, DESIGN 8.4/6.1).
    """
    project_file = profile_path.resolve() if profile_path is not None else find_profile(root)
    if project_file is None:
        return None

    if data_dir is None:
        data_dir = _default_data_dir()

    tool_source = SourceRef(
        kind="tool",
        location="chipgraph.core.config.defaults",
        version=_sha256_json(DEFAULT_PROFILE),
    )
    tool_layer = _Layer(source=tool_source, data=DEFAULT_PROFILE)

    project_layers = _load_layer_chain(
        project_file, "project", str(project_file), data_dir, fetcher, frozenset()
    )
    layers = [tool_layer, *project_layers]

    _enforce_tighten_only_policy(layers)

    merged: dict[str, Any] = {}
    provenance: dict[str, SourceRef] = {}
    for layer in layers:
        merged = _merge(merged, layer.data, layer.source, provenance)

    try:
        profile = Profile.model_validate(merged)
    except ValidationError as exc:
        raise _translate_validation_error(exc, provenance, str(project_file)) from exc

    user_path = user_config if user_config is not None else _default_user_config_path()
    user_raw: dict[str, Any] = {}
    user_source = SourceRef(kind="user", location=str(user_path), version="")
    if user_path.is_file():
        loaded, user_hash = _read_yaml_with_hash(user_path)
        user_raw = loaded or {}
        if not isinstance(user_raw, dict):
            raise ConfigError("profile must be a YAML mapping", file=user_path)
        user_source = SourceRef(kind="user", location=str(user_path), version=user_hash)

    try:
        user = UserConfig.model_validate(user_raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        key = _loc_to_key(first["loc"])
        raise ConfigError(str(first["msg"]), file=str(user_path), key=key or None) from exc

    _record_leaves(user_raw, user_source, provenance, "user")

    return ResolvedProfile(
        profile=profile,
        user=user,
        sources=(*(layer.source for layer in layers), user_source),
        profile_hash=_sha256_json(profile.model_dump(mode="json")),
        provenance=provenance,
    )
