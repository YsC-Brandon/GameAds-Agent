"""Isolated generation-switch rehearsal; not an approved deployment activator.

Immutable generation manifests and a single locator support process-failure
experiments. Managed database owners hold the same instance lock as decisions.
A pending switch always blocks writer admission. Explicit recovery
can confirm a complete candidate, but is not proof of frozen old-pointer semantics
for an ambiguous replace, nor of power-loss persistence. Activation remains an
explicit offline experiment rather than an automatic server startup operation.
"""

import json
import os
from contextlib import contextmanager
from uuid import uuid4

from app.phase3.store import BusinessError, digest, encode
from app.phase4.maintenance import local_file, sha, verify_backup
from app.phase4.ownership import CandidateOwner


def _write(path, payload):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(encode(payload))
        stream.flush()
        os.fsync(stream.fileno())


class GenerationRehearsal:
    """Explicit temporary synthetic roots only; same lock spans all decisions.

    Database owners additionally exclude cooperating explicit-path writers. The
    root must be dedicated and immutable to untrusted processes under U04; this
    class cannot enforce that ACL requirement or exclude arbitrary SQLite users.
    """

    def __init__(self, root):
        self.root = local_file(root)
        if not self.root.is_dir():
            raise ValueError("Existing absolute rehearsal root required")
        self.owner = CandidateOwner(self.root / "instance", _control=True)

    @contextmanager
    def locked(self):
        with self.owner:
            yield self

    def _require(self):
        self.owner.require()

    def prepare(self, backup_directory):
        """Register a complete immutable snapshot; never mutate the old database."""
        self._require()
        directory = local_file(backup_directory).resolve()
        if not directory.is_relative_to(self.root) or directory == self.root:
            raise ValueError("Generation must be a child of the rehearsal root")
        with CandidateOwner(directory / "database.sqlite3", _control=True):
            return self._prepare_locked(directory)

    def _prepare_locked(self, directory):
        manifest = verify_backup(directory)
        # File contents precede the activation decision. Windows directory-entry
        # ordering and the storage device's power-loss behavior remain unproved.
        for name in [*manifest["files"], "manifest.json"]:
            with local_file(directory / name).open("r+b") as stream:
                os.fsync(stream.fileno())
        identity = uuid4().hex
        record = {
            "generation": identity,
            "directory": str(directory.relative_to(self.root)),
            "manifest_sha256": sha(directory / "manifest.json"),
            "files": manifest["files"],
        }
        _write(self.root / (identity + ".generation.json"), record)
        database = directory / "database.sqlite3"
        # Both locks span verification and binding installation. Future owners
        # acquire the instance lock first, then the physical database lock.
        _write(
            database.with_name(database.name + ".generation.json"),
            {"root": str(self.root.resolve()), "generation": identity},
        )
        return identity

    def _generation(self, identity, *, snapshot=False):
        if (
            not isinstance(identity, str)
            or len(identity) != 32
            or any(c not in "0123456789abcdef" for c in identity)
        ):
            raise BusinessError("stored_evidence_integrity_error")
        record = json.loads(local_file(self.root / (identity + ".generation.json")).read_text())
        directory = local_file(self.root / record["directory"]).resolve()
        if not directory.is_relative_to(self.root) or directory == self.root:
            raise BusinessError("stored_evidence_integrity_error")
        if (
            record["generation"] != identity
            or sha(directory / "manifest.json") != record["manifest_sha256"]
            or (snapshot and verify_backup(directory)["files"] != record["files"])
        ):
            raise BusinessError("stored_evidence_integrity_error")
        return record

    def _locator(self):
        record = json.loads(local_file(self.root / "active.json").read_text())
        expected = record.pop("sha256")
        if digest(record) != expected:
            raise BusinessError("stored_evidence_integrity_error")
        return record

    def validate_decisions(self):
        """Validate the persisted decision chain without repairing or deleting it."""
        self._require()
        try:

            def locator(value):
                if (
                    set(value) != {"sequence", "generation", "predecessor"}
                    or type(value["sequence"]) is not int
                    or value["sequence"] < 1
                    or (value["sequence"] == 1) != (value["predecessor"] is None)
                ):
                    raise ValueError("locator shape")
                self._generation(value["generation"])
                if value["predecessor"] is not None:
                    self._generation(value["predecessor"])
                    if value["predecessor"] == value["generation"]:
                        raise ValueError("self predecessor")
                return value

            def decision(value):
                if set(value) != {"old", "new"}:
                    raise ValueError("decision shape")
                old, new = locator(value["old"]), locator(value["new"])
                if (
                    new["sequence"] != old["sequence"] + 1
                    or new["predecessor"] != old["generation"]
                ):
                    raise ValueError("decision edge")
                return old, new

            active = locator(self._locator())
            selected = {}
            for path in sorted(self.root.glob("*.complete.json")):
                value = json.loads(local_file(path).read_text())
                old, new = decision(value)
                if path.name != f"{new['sequence']:08d}.complete.json":
                    raise ValueError("complete identity")
                selected[new["sequence"]] = (old, new)
            for path in sorted(self.root.glob("*.recovered.json")):
                value = json.loads(local_file(path).read_text())
                if set(value) != {
                    "observed",
                    "old",
                    "new",
                    "writer_admission",
                    "requires_explicit_decision",
                    "frozen_ambiguous_outcome",
                    "power_loss",
                }:
                    raise ValueError("recovered shape")
                old, new = decision({k: value[k] for k in ("old", "new")})
                observed = locator(value["observed"])
                if (
                    observed not in (old, new)
                    or path.name != f"{observed['sequence']:08d}.recovered.json"
                    or value["writer_admission"] is not False
                    or value["requires_explicit_decision"] is not True
                    or value["frozen_ambiguous_outcome"] != "UNPROVEN"
                    or value["power_loss"] != "UNPROVEN"
                ):
                    raise ValueError("recovered outcome")
                if observed == new:
                    if new["sequence"] in selected and selected[new["sequence"]] != (old, new):
                        raise ValueError("complete/recovered conflict")
                    selected[new["sequence"]] = (old, new)
            pending_path = self.root / "pending.json"
            pending = None
            if pending_path.exists():
                pending = decision(json.loads(local_file(pending_path).read_text()))
                if active not in pending:
                    raise ValueError("pending locator")
                if (
                    pending[1]["sequence"] in selected
                    and selected[pending[1]["sequence"]] != pending
                ):
                    raise ValueError("pending completion")
                if active == pending[0] and pending[1]["sequence"] in selected:
                    raise ValueError("completed but old active")
            chain = dict(selected)
            if pending and active == pending[1]:
                chain[active["sequence"]] = pending
            current = active
            while current["sequence"] > 1:
                old, new = chain[current["sequence"]]
                if current != new:
                    raise ValueError("broken decision chain")
                current = old
            if any(sequence > active["sequence"] for sequence in selected):
                raise ValueError("future completion")
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            error = BusinessError("stored_evidence_integrity_error")
            error.add_note("generation decision chain: " + str(exc))
            raise error from exc

    def bootstrap(self, identity):
        self._require()
        self._generation(identity, snapshot=True)
        locator = {"sequence": 1, "generation": identity, "predecessor": None}
        _write(self.root / "active.json", locator | {"sha256": digest(locator)})

    def resolve(self):
        self._require()
        self.validate_decisions()
        if (self.root / "pending.json").exists():
            raise BusinessError("generation_recovery_required", 503)
        locator = self._locator()
        return locator, self._generation(locator["generation"])

    def switch(self, identity):
        """Rehearse one offline decision; unknown outcomes retain pending evidence."""
        self._require()
        current, _ = self.resolve()
        self._generation(identity, snapshot=True)
        if identity == current["generation"]:
            raise BusinessError("invalid_generation_transition")
        decision = {
            "sequence": current["sequence"] + 1,
            "generation": identity,
            "predecessor": current["generation"],
        }
        pending = {"old": current, "new": decision}
        _write(self.root / "pending.json", pending)
        temporary = self.root / (uuid4().hex + ".locator.tmp")
        _write(temporary, decision | {"sha256": digest(decision)})
        os.replace(temporary, self.root / "active.json")
        # Read the decision back before marking the process-level switch complete.
        if self._locator() != decision:
            raise BusinessError("stored_evidence_integrity_error")
        self._generation(identity)
        _write(self.root / (f"{decision['sequence']:08d}.complete.json"), pending)
        (self.root / "pending.json").unlink()
        return decision

    def inspect_pending(self):
        """Diagnostic only: never guess rollback or grant writer admission."""
        self._require()
        self.validate_decisions()
        pending = json.loads(local_file(self.root / "pending.json").read_text())
        locator = self._locator()
        if locator not in (pending["old"], pending["new"]):
            raise BusinessError("stored_evidence_integrity_error")
        self._generation(locator["generation"])
        return {
            "observed": locator,
            "old": pending["old"],
            "new": pending["new"],
            "writer_admission": False,
            "requires_explicit_decision": True,
            "frozen_ambiguous_outcome": "UNPROVEN",
            "power_loss": "UNPROVEN",
        }

    def confirm_observed(self, expected_generation):
        """Explicit synthetic recovery; never automatically roll back a new decision.

        Managed owners are blocked while pending. Confirmation of a new
        observed decision is an experiment, not approval of changed r4 ambiguity
        semantics. A real deployment must prove those semantics independently.
        """
        evidence = self.inspect_pending()
        observed = evidence["observed"]
        if expected_generation != observed["generation"]:
            raise BusinessError("generation_confirmation_mismatch")
        path = self.root / (f"{observed['sequence']:08d}.recovered.json")
        if path.exists():
            if json.loads(local_file(path).read_text()) != evidence:
                raise BusinessError("stored_evidence_integrity_error")
        else:
            _write(path, evidence)
        (self.root / "pending.json").unlink()
        return self.resolve()
