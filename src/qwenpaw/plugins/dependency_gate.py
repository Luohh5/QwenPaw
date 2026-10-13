# -*- coding: utf-8 -*-
"""Dependency gate: boot check-only and host-package blacklist."""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from packaging.markers import default_environment
from packaging.requirements import Requirement
from .wheel_additions import WheelAddition  # pylint: disable=unused-import

logger = logging.getLogger(__name__)

# Known host packages that must not be pip-installed over a running process.
HOST_PACKAGE_NAMES: frozenset[str] = frozenset(
    {
        "httpx",
        "pydantic",
        "fastapi",
        "uvicorn",
        "starlette",
        "anyio",
        "openai",
        "agentscope",
        "reme-ai",
        "apscheduler",
    },
)

_IMPORT_NAME_OVERRIDES = {
    "pillow": "PIL",
    "pyyaml": "yaml",
    "beautifulsoup4": "bs4",
    "python-dateutil": "dateutil",
    "opencv-python": "cv2",
    "scikit-learn": "sklearn",
    "protobuf": "google.protobuf",
    "reme-ai": "reme",
}


@dataclass
class ParsedRequirements:
    """Installable requirements plus declarations this gate refuses."""

    requirements: list[Requirement] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)


@dataclass
class GateDecision:
    """Result of evaluating a plugin's requirements.txt."""

    allow_install: bool
    already_satisfied: bool
    missing: list[str] = field(default_factory=list)
    host_conflicts: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    require_restart: bool = False
    reason: str = ""
    reason_code: str = ""
    affected_instances: tuple[dict, ...] = ()


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def parse_requirement_lines(requirements_file: Path) -> list[Requirement]:
    """Parse installable requirement lines, skipping comments and flags."""
    return parse_requirements_file(requirements_file).requirements


def parse_requirements_file(requirements_file: Path) -> ParsedRequirements:
    """Parse requirements, recording unsupported declarations."""
    parsed = ParsedRequirements()
    if not requirements_file.is_file():
        return parsed
    for raw in requirements_file.read_text(encoding="utf-8").splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            parsed.unsupported.append(line)
            continue
        try:
            req = Requirement(line)
        except Exception:  # noqa: BLE001
            parsed.unsupported.append(line)
            continue
        if req.url or req.extras:
            parsed.unsupported.append(line)
            continue
        parsed.requirements.append(req)
    return parsed


def _dist_key(req: Requirement) -> str:
    return req.name.lower().replace("_", "-")


def _import_name(req: Requirement) -> str:
    dist = _dist_key(req)
    return _IMPORT_NAME_OVERRIDES.get(dist, req.name.replace("-", "_"))


def requirement_applies(req: Requirement) -> bool:
    """False when a PEP 508 marker excludes the current environment."""
    if req.marker is None:
        return True
    try:
        return bool(req.marker.evaluate(default_environment()))
    except Exception:  # noqa: BLE001
        return True


def is_requirement_satisfied(req: Requirement) -> bool:
    """Return True if *req* is importable, in-spec, or not applicable."""
    if not requirement_applies(req):
        return True
    try:
        installed = _dist_version(req.name)
    except PackageNotFoundError:
        installed = None
    if installed is not None:
        if not req.specifier:
            return True
        try:
            return req.specifier.contains(installed)
        except Exception:  # noqa: BLE001
            return True
    import_name = _import_name(req)
    top = import_name.split(".", 1)[0]
    try:
        return importlib.util.find_spec(top) is not None
    except (ImportError, ValueError):
        return False


def hits_imported_host_package(req: Requirement) -> bool:
    """True when installing *req* would overwrite a host-owned package."""
    if not requirement_applies(req):
        return False
    dist = _dist_key(req)
    if _is_frozen():
        return dist in HOST_PACKAGE_NAMES
    if dist in HOST_PACKAGE_NAMES:
        top = _import_name(req).split(".", 1)[0]
        if top in sys.modules:
            return True
        try:
            _dist_version(req.name)
            return True
        except PackageNotFoundError:
            return False
    return False


class DependencyGate:
    """Front door for ``_ensure_dependencies_installed``."""

    def evaluate(
        self,
        requirements_file: Path,
        *,
        allow_install: bool,
        plugin_id: str,
    ) -> GateDecision:
        """Decide whether to install, fail, or require a backend restart.

        Args:
            requirements_file: Plugin ``requirements.txt``.
            allow_install: False on boot (check only). True for explicit
                install / update / repair.
            plugin_id: Plugin id (for messages).
        """
        parsed = parse_requirements_file(requirements_file)
        if parsed.unsupported:
            return GateDecision(
                allow_install=False,
                already_satisfied=False,
                unsupported=list(parsed.unsupported),
                reason=(
                    f"Plugin '{plugin_id}' uses unsupported requirement "
                    f"syntax: {', '.join(parsed.unsupported)}"
                ),
            )
        reqs = parsed.requirements
        missing = [
            str(req) for req in reqs if not is_requirement_satisfied(req)
        ]
        if not missing:
            return GateDecision(
                allow_install=False,
                already_satisfied=True,
                reason="already satisfied",
            )

        if not allow_install:
            return GateDecision(
                allow_install=False,
                already_satisfied=False,
                missing=missing,
                reason=(
                    f"Plugin '{plugin_id}' is missing dependencies: "
                    + ", ".join(missing)
                ),
            )

        conflicts = [
            str(req)
            for req in reqs
            if not is_requirement_satisfied(req)
            and hits_imported_host_package(req)
        ]
        if conflicts:
            return GateDecision(
                allow_install=False,
                already_satisfied=False,
                missing=missing,
                host_conflicts=conflicts,
                require_restart=True,
                reason=(
                    f"Plugin '{plugin_id}' declares host packages that "
                    f"are already imported ({', '.join(conflicts)}). "
                    "Restart the backend to apply."
                ),
            )

        return GateDecision(
            allow_install=True,
            already_satisfied=False,
            missing=missing,
            reason="install required",
        )


class SharedDependencyError(RuntimeError):
    """An actionable shared-environment refusal with affected consumers."""

    def __init__(self, decision: GateDecision):
        self.decision = decision
        self.reason_code = decision.reason_code
        super().__init__(decision.reason)


def evaluate_shared_dependencies(
    requirements_file: Path,
    snapshot,
    *,
    plugin_id: str,
    uses: tuple[dict, ...] = (),
) -> GateDecision:
    """Use child markers and complete relevant metadata; never import SDKs."""
    from packaging.utils import canonicalize_name
    from packaging.version import Version
    from .execution.dependencies import repair_marker

    sites = {Path(site).resolve() for site in snapshot.sites}
    uses = tuple(
        use
        for use in uses
        if not use.get("python_executable")
        or Path(use["python_executable"]).absolute()
        == Path(snapshot.executable).absolute()
        or any(
            Path(path).resolve() in sites
            for path in use.get("dependency_paths", ())
        )
    )
    parsed = parse_requirements_file(requirements_file)
    pending = list(parsed.requirements)
    unsupported = list(parsed.unsupported)
    for use in uses:
        for value in use.get("requirements", ()):
            try:
                pending.append(Requirement(value))
            except ValueError:
                unsupported.append(value)
    missing, conflicts, visited = [], [], set()
    while pending:
        requirement = pending.pop()
        if str(requirement) in visited:
            continue
        visited.add(str(requirement))
        if requirement.marker and not requirement.marker.evaluate(
            environment=dict(snapshot.marker_environment),
        ):
            continue
        if requirement.url or requirement.extras:
            unsupported.append(str(requirement))
            continue
        name = str(canonicalize_name(requirement.name))
        row = snapshot.distributions.get(name)
        if row is None:
            missing.append(str(requirement))
            continue
        try:
            satisfied = requirement.specifier.contains(
                Version(row["version"]),
                prereleases=True,
            )
        except (ValueError, TypeError):
            satisfied = False
        if not satisfied:
            missing.append(str(requirement))
            conflicts.append(str(requirement))
        for value in row["requires_dist"]:
            try:
                pending.append(Requirement(value))
            except ValueError:
                unsupported.append(value)
    dirty = any(repair_marker(site).exists() for site in snapshot.sites)
    code = (
        "DEPENDENCY_REPAIR_REQUIRED"
        if dirty
        else (
            "DEPENDENCY_PLAN_UNVERIFIED"
            if unsupported
            else (
                "DEPENDENCY_RESTART_REQUIRED"
                if conflicts
                else "DEPENDENCY_UNSATISFIED"
                if missing
                else ""
            )
        )
    )
    affected = tuple(
        {
            "plugin_id": use.get("plugin_id"),
            "instance_id": use.get("instance_id"),
            "generation": use.get("generation"),
        }
        for use in uses
    )
    reason = (
        "already satisfied"
        if not code
        else (
            f"{code}: {plugin_id}: "
            f"{', '.join(sorted(set(missing + unsupported)))}. "
            "Stop all environment consumers (including the backend and "
            "unknown users), "
            "repair dependencies explicitly, re-audit and probe, "
            "then restart enabled channels. "
            "Source rollback does not restore dependency packages."
        )
    )
    return GateDecision(
        False,
        not code,
        sorted(set(missing)),
        sorted(set(conflicts)),
        sorted(set(unsupported)),
        bool(conflicts or dirty),
        reason,
        code,
        affected,
    )


async def prepare_shared_dependencies(
    process,
    spec,
    *,
    install_target: Path | None = None,
    wheels=(),
    uses: tuple[dict, ...] = (),
    lock_timeout: float = 300.0,
):
    """Audit, lock, re-audit and publish verified local additions."""
    import asyncio
    from .execution.process import _settle
    from .install_lock import (
        environment_install_lock_path,
        plugin_install_lock,
    )
    from .wheel_additions import validate_additions

    requirements = getattr(spec, "requirements_path", None)
    if requirements is None and hasattr(spec, "descriptor_path"):
        from ..channel_protocol.shared import SharedChannelDescriptor

        descriptor = SharedChannelDescriptor.load(spec.descriptor_path)
        requirements = (
            Path(spec.descriptor_path).parent / descriptor.requirements_file
        )
    requirements = (
        Path(requirements)
        if requirements is not None
        else Path(spec.code_root) / "requirements.txt"
    )
    snapshot = (await process.audit(spec)).snapshot
    decision = evaluate_shared_dependencies(
        requirements,
        snapshot,
        plugin_id=getattr(
            spec,
            "plugin_id",
            getattr(spec, "channel_key", "plugin"),
        ),
        uses=uses,
    )
    if decision.already_satisfied:
        return snapshot
    if (
        not wheels
        or install_target is None
        or decision.require_restart
        or decision.unsupported
    ):
        raise SharedDependencyError(decision)
    target = Path(install_target).resolve()
    if (
        str(target) not in snapshot.sites
        or os.environ.get("QWENPAW_RUNTIME_PROVISIONER") == "local"
    ):
        decision.reason_code = "DEPENDENCY_PLAN_UNVERIFIED"
        raise SharedDependencyError(decision)
    lock = plugin_install_lock(
        environment_install_lock_path(target),
        timeout=lock_timeout,
    )
    acquisition = asyncio.create_task(asyncio.to_thread(lock.__enter__))
    try:
        await asyncio.shield(acquisition)
    except asyncio.CancelledError:
        try:
            await _settle(acquisition)
        finally:
            if (
                acquisition.done()
                and not acquisition.cancelled()
                and acquisition.exception() is None
            ):
                lock.__exit__(None, None, None)
        raise
    try:
        snapshot = (await process.audit(spec)).snapshot
        decision = evaluate_shared_dependencies(
            requirements,
            snapshot,
            plugin_id=getattr(
                spec,
                "plugin_id",
                getattr(spec, "channel_key", "plugin"),
            ),
            uses=uses,
        )
        if decision.already_satisfied:
            return snapshot
        if decision.require_restart or decision.unsupported:
            raise SharedDependencyError(decision)
        try:
            files, proposed = validate_additions(
                tuple(wheels),
                snapshot,
                target,
            )
        except Exception as exc:
            code = getattr(exc, "reason_code", "DEPENDENCY_PLAN_UNVERIFIED")
            raise SharedDependencyError(
                GateDecision(
                    False,
                    False,
                    reason=code,
                    reason_code=code,
                    require_restart=code == "DEPENDENCY_RESTART_REQUIRED",
                ),
            ) from exc
        # Validate each addition's closure, including unused wheels.
        added = tuple(
            f"{name}=={row['version']}"
            for name, row in proposed.distributions.items()
            if name not in snapshot.distributions
        )
        decision = evaluate_shared_dependencies(
            requirements,
            proposed,
            plugin_id="shared install",
            uses=(*uses, {"requirements": added}),
        )
        if not decision.already_satisfied:
            raise SharedDependencyError(decision)
        return await _publish_additions(
            process,
            spec,
            requirements,
            uses,
            target,
            files,
        )
    finally:
        lock.__exit__(None, None, None)


async def _publish_additions(process, spec, requirements, uses, target, files):
    """Keep exclusion through exact-byte publication and child verification."""
    import asyncio
    from .execution.dependencies import repair_marker
    from .execution.process import _settle

    marker = repair_marker(target)
    marker.write_text(
        "Shared dependency publication requires verification.\n",
        encoding="utf-8",
    )
    writer = asyncio.create_task(
        asyncio.to_thread(_write_additions, target, files),
    )
    try:
        await asyncio.shield(writer)
    except asyncio.CancelledError:
        try:
            await _settle(writer)
        finally:
            marker.write_text(
                "Cancelled dependency publication requires repair.\n",
                encoding="utf-8",
            )
        raise
    except Exception as exc:
        failure = GateDecision(
            False,
            False,
            reason="Dependency publication failed; repair is required.",
            reason_code="DEPENDENCY_REPAIR_REQUIRED",
            require_restart=True,
        )
        raise SharedDependencyError(failure) from exc
    # All bytes have been published; a failed verification restores blocker.
    marker.unlink()
    try:
        snapshot = (await process.audit(spec)).snapshot
        decision = evaluate_shared_dependencies(
            requirements,
            snapshot,
            plugin_id="shared install",
            uses=uses,
        )
        if not decision.already_satisfied:
            raise SharedDependencyError(decision)
    except BaseException:
        marker.write_text(
            "Dependency verification failed; repair required.\n",
            encoding="utf-8",
        )
        raise
    return snapshot


def _write_additions(target: Path, files: dict[str, bytes]) -> None:
    """Publish the exact validated bytes while the site lock remains held."""
    for relative, content in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.resolve().is_relative_to(target) or path.is_symlink():
            raise RuntimeError("DEPENDENCY_REPAIR_REQUIRED")
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
