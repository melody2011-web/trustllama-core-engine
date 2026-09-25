#!/usr/bin/env python3
import hashlib
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
REQUIRED_ARTIFACTS = (
    "tlama_pool_adapter.so",
    "tlama_transfer_hook.so",
    "spl_token_2022.so",
    "spl_token.so",
)
REQUIRED_LOCKFILES = (
    "onchain/tlama-pool-hook/Cargo.lock",
    "onchain/tlama-transfer-hook/Cargo.lock",
)
REQUIRED_TOOLCHAIN_FIELDS = ("agave", "platformTools", "sbfCargo", "sbfRustc")
RELEASE_ARTIFACTS = ("tlama_pool_adapter.so", "tlama_transfer_hook.so")
RELEASE_RUNTIME_ARTIFACTS = ("spl_token_2022.so", "spl_token.so")
RELEASE_IDS = (
    "adapter",
    "hook",
    "tlamaMint",
    "tlamaVault",
    "wsolVault",
    "poolAuthority",
)
PRODUCTION_IDS_PATH = "onchain/production-ids.txt"
DEVELOPMENT_IDS_PATH = "onchain/development-ids.txt"
FROZEN_PATHS = (
    PRODUCTION_IDS_PATH,
    DEVELOPMENT_IDS_PATH,
    "onchain/tlama-pool-hook/src",
    "onchain/tlama-pool-hook/build.rs",
    "onchain/tlama-pool-hook/Cargo.toml",
    "onchain/tlama-pool-hook/Cargo.lock",
    "onchain/tlama-transfer-hook/src",
    "onchain/tlama-transfer-hook/build.rs",
    "onchain/tlama-transfer-hook/Cargo.toml",
    "onchain/tlama-transfer-hook/Cargo.lock",
)
RELEASE_VALIDATION_PATHS = (
    "onchain/tlama-pool-hook/tests/program_test.rs",
    "scripts/solana-audit.sh",
    "scripts/verify-tlama-reproducible-build.sh",
    "scripts/rustc-stable-no-ld-audit.sh",
    "audit/solana/verify_artifact_manifest.py",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fail(message: str) -> None:
    raise SystemExit(f"artifact manifest rejected: {message}")


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def valid_pubkey(value: object) -> bool:
    """Validate Solana's 32-byte base58 public-key representation."""
    if not isinstance(value, str) or not value:
        return False
    number = 0
    try:
        for character in value:
            number = number * 58 + BASE58_ALPHABET.index(character)
    except ValueError:
        return False
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return len(decoded) + len(value) - len(value.lstrip("1")) == 32


def git(root: pathlib.Path, *args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()
    except subprocess.CalledProcessError as error:
        fail(f"git {' '.join(args)} failed ({error.returncode})")


def git_bytes(root: pathlib.Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(["git", *args], cwd=root)
    except subprocess.CalledProcessError as error:
        fail(f"git {' '.join(args)} failed ({error.returncode})")


def require_hashes(values: object, names: tuple[str, ...], what: str) -> dict:
    if not isinstance(values, dict) or set(values) != set(names):
        fail(f"{what} must contain exactly: {', '.join(names)}")
    for name in names:
        if not is_sha256(values[name]):
            fail(f"{what} hash is not canonical lowercase SHA-256: {name}")
    return values


def require_exact_object(values: object, names: tuple[str, ...], what: str) -> dict:
    if not isinstance(values, dict) or set(values) != set(names):
        fail(f"{what} must contain exactly: {', '.join(names)}")
    return values


def verify_artifacts(artifacts: dict, names: tuple[str, ...], artifact_dir: pathlib.Path) -> None:
    for name in names:
        path = artifact_dir / name
        if not path.is_file():
            fail(f"missing artifact: {name}")
        with path.open("rb") as handle:
            if handle.read(4) != b"\x7fELF":
                fail(f"artifact is not an ELF file: {name}")
        if artifacts[name] != sha256(path):
            fail(f"artifact hash mismatch: {name}")


def source_config_ids(root: pathlib.Path, source_commit: str) -> dict:
    """Read the exact production-build input committed at sourceCommit."""
    try:
        contents = git_bytes(root, "show", f"{source_commit}:{PRODUCTION_IDS_PATH}").decode(
            "utf-8"
        )
    except UnicodeDecodeError:
        fail("committed production ID input is not UTF-8")
    lines = contents.splitlines()
    if len(lines) != len(RELEASE_IDS):
        fail("committed production ID input must contain exactly six key=value lines")
    found = {}
    for key, line in zip(RELEASE_IDS, lines):
        actual, separator, value = line.partition("=")
        if separator != "=" or actual != key or not value:
            fail(f"committed production ID input is malformed at {key}")
        found[key] = value
    if any(not valid_pubkey(value) for value in found.values()):
        fail("committed production ID input contains an invalid 32-byte base58 public key")
    if len(set(found.values())) != len(RELEASE_IDS):
        fail("committed production ID input contains duplicate public keys")
    development_contents = git_bytes(
        root, "show", f"{source_commit}:{DEVELOPMENT_IDS_PATH}"
    ).decode("utf-8")
    development_values = {
        line.partition("=")[2]
        for line in development_contents.splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    reused = [name for name, value in found.items() if value in development_values]
    if reused:
        fail(
            "committed production ID input reuses disposable development IDs: "
            + ", ".join(reused)
        )
    return found


def verify_fixture(manifest: dict, artifact_dir: pathlib.Path, root: pathlib.Path) -> None:
    # Keep v1 behavior byte-for-byte compatible with recorded four-artifact
    # campaigns: in particular old manifests were not required to use strict
    # lowercase SHA spelling.
    head = git(root, "rev-parse", "HEAD")
    if manifest.get("commit") != head:
        fail("manifest commit does not match checked-out commit")

    lockfiles = manifest.get("lockfiles")
    if not isinstance(lockfiles, dict):
        fail("lockfiles must be an object")
    for relative in REQUIRED_LOCKFILES:
        expected = lockfiles.get(relative)
        actual = sha256(ROOT / relative)
        if expected != actual:
            fail(f"lockfile hash mismatch: {relative}")

    toolchain = manifest.get("toolchain")
    if not isinstance(toolchain, dict) or any(
        not isinstance(toolchain.get(field), str) or not toolchain[field].strip()
        for field in REQUIRED_TOOLCHAIN_FIELDS
    ):
        fail("complete SBF toolchain provenance is required")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        fail("artifacts must be an object")
    for name in REQUIRED_ARTIFACTS:
        if name not in artifacts:
            fail(f"artifact hash mismatch: {name}")
    verify_artifacts(artifacts, REQUIRED_ARTIFACTS, artifact_dir)


def verify_release(manifest: dict, artifact_dir: pathlib.Path, root: pathlib.Path) -> None:
    head = git(root, "rev-parse", "HEAD")
    source_commit = manifest.get("sourceCommit")
    if not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", source_commit):
        fail("sourceCommit must be a lowercase full git commit ID")
    git(root, "rev-parse", "--verify", f"{source_commit}^{{commit}}")
    if subprocess.run(
        ["git", "merge-base", "--is-ancestor", source_commit, "HEAD"], cwd=root
    ).returncode:
        fail("sourceCommit is not an ancestor of evidence HEAD")
    validation_commit = manifest.get("validationCommit")
    if not isinstance(validation_commit, str) or not re.fullmatch(
        r"[0-9a-f]{40,64}", validation_commit
    ):
        fail("validationCommit must be a lowercase full git commit ID")
    git(root, "rev-parse", "--verify", f"{validation_commit}^{{commit}}")
    if subprocess.run(
        ["git", "merge-base", "--is-ancestor", source_commit, validation_commit],
        cwd=root,
    ).returncode:
        fail("sourceCommit is not an ancestor of validationCommit")
    if subprocess.run(
        ["git", "merge-base", "--is-ancestor", validation_commit, head], cwd=root
    ).returncode:
        fail("validationCommit is not an ancestor of evidence HEAD")
    for relative in RELEASE_VALIDATION_PATHS:
        path = root / relative
        if not path.is_file() or path.read_bytes() != git_bytes(
            root, "show", f"{validation_commit}:{relative}"
        ):
            fail("SBF validation harness changed after validationCommit")
    if git(root, "diff", "--name-only", source_commit, "HEAD", "--", *FROZEN_PATHS):
        fail("onchain source or lock path changed after sourceCommit")

    lockfiles = require_hashes(manifest.get("lockfiles"), REQUIRED_LOCKFILES, "lockfiles")
    for relative in REQUIRED_LOCKFILES:
        # Hash the frozen tree object, not a potentially modified worktree
        # file.  The source/HEAD diff above permits a newer evidence commit
        # only when these release inputs remained identical.
        if lockfiles[relative] != sha256_bytes(
            git_bytes(root, "show", f"{source_commit}:{relative}")
        ):
            fail(f"lockfile hash mismatch: {relative}")

    toolchain = manifest.get("toolchain")
    if not isinstance(toolchain, dict) or set(toolchain) != set(REQUIRED_TOOLCHAIN_FIELDS):
        fail("complete pinned toolchain provenance is required")
    for name in REQUIRED_TOOLCHAIN_FIELDS:
        entry = toolchain[name]
        if not isinstance(entry, dict) or set(entry) != {"version", "archiveSha256"}:
            fail(f"toolchain provenance must include version and archiveSha256: {name}")
        if not isinstance(entry["version"], str) or not entry["version"].strip():
            fail(f"toolchain version is required: {name}")
        if not is_sha256(entry["archiveSha256"]):
            fail(f"toolchain archive hash is not canonical lowercase SHA-256: {name}")

    ids = manifest.get("ids")
    if not isinstance(ids, dict) or set(ids) != set(RELEASE_IDS):
        fail(f"ids must contain exactly: {', '.join(RELEASE_IDS)}")
    if any(not valid_pubkey(ids[name]) for name in RELEASE_IDS):
        fail("each release ID must be a valid 32-byte base58 public key")
    if ids["adapter"] == ids["hook"]:
        fail("adapter and hook IDs must differ")
    if len(set(ids.values())) != len(RELEASE_IDS):
        fail("release IDs must be distinct")
    compiled_ids = source_config_ids(root, source_commit)
    for name in RELEASE_IDS:
        if ids[name] != compiled_ids[name]:
            fail(f"release ID does not match sourceCommit compiled config: {name}")

    artifacts = require_hashes(manifest.get("artifacts"), RELEASE_ARTIFACTS, "artifacts")
    verify_artifacts(artifacts, RELEASE_ARTIFACTS, artifact_dir)
    runtime_artifacts = require_hashes(
        manifest.get("runtimeArtifacts"), RELEASE_RUNTIME_ARTIFACTS, "runtime artifacts"
    )
    verify_artifacts(runtime_artifacts, RELEASE_RUNTIME_ARTIFACTS, artifact_dir)
    evidence_meta = require_exact_object(
        manifest.get("reproducibleBuildEvidence"),
        ("file", "sha256"),
        "reproducibleBuildEvidence",
    )
    if evidence_meta["file"] != "reproducible-build.json":
        fail("reproducible build evidence filename is not canonical")
    evidence_hash = evidence_meta["sha256"]
    if not is_sha256(evidence_hash):
        fail("reproducible build evidence hash is not canonical lowercase SHA-256")
    evidence_path = artifact_dir / evidence_meta["file"]
    if not evidence_path.is_file() or sha256(evidence_path) != evidence_hash:
        fail("reproducible build evidence hash mismatch")
    try:
        evidence = json.loads(evidence_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        fail(f"invalid reproducible build evidence: {error}")
    require_exact_object(
        evidence,
        ("schemaVersion", "sourceCommit", "toolchainArchives", "buildA", "buildB"),
        "reproducible build evidence",
    )
    if evidence["schemaVersion"] != 1 or evidence["sourceCommit"] != source_commit:
        fail("reproducible build evidence source mismatch")
    archive_hashes = require_exact_object(
        evidence["toolchainArchives"], ("agave", "platformTools"), "toolchain archives"
    )
    if archive_hashes["agave"] != toolchain["agave"]["archiveSha256"] or archive_hashes[
        "platformTools"
    ] != toolchain["platformTools"]["archiveSha256"]:
        fail("reproducible build evidence toolchain mismatch")
    for build_name in ("buildA", "buildB"):
        build = require_exact_object(
            evidence[build_name], RELEASE_ARTIFACTS, build_name
        )
        for artifact_name in RELEASE_ARTIFACTS:
            entry = require_exact_object(
                build[artifact_name], ("bytes", "sha256"), f"{build_name} artifact"
            )
            if not is_sha256(entry["sha256"]):
                fail(f"{build_name} artifact hash is not canonical lowercase SHA-256")
            if entry["sha256"] != artifacts[artifact_name]:
                fail("reproducible build evidence artifact hash mismatch")
            if entry["bytes"] != (artifact_dir / artifact_name).stat().st_size:
                fail("reproducible build evidence artifact size mismatch")


def verify(manifest: dict, artifact_dir: pathlib.Path, root: pathlib.Path = ROOT) -> None:
    if manifest.get("schemaVersion") == 1 and manifest.get("buildKind") == "fixture":
        verify_fixture(manifest, artifact_dir, root)
    elif manifest.get("schemaVersion") == 3 and manifest.get("buildKind") == "production":
        verify_release(manifest, artifact_dir, root)
    else:
        fail("supported manifests are schemaVersion 1 fixture or schemaVersion 3 production")


def main() -> None:
    if len(sys.argv) != 3:
        fail("usage: verify_artifact_manifest.py MANIFEST ARTIFACT_DIR")
    manifest_path = pathlib.Path(sys.argv[1]).resolve()
    artifact_dir = pathlib.Path(sys.argv[2]).resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(str(error))
    verify(manifest, artifact_dir)

    print("artifact manifest verified")


if __name__ == "__main__":
    main()