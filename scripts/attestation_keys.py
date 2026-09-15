#!/usr/bin/env python3
"""Ed25519 helpers for externally managed platform-attestation keys.

Private keys are only read from an explicit path supplied by the platform
adapter. This module never writes, copies, or persists private key material.
"""

from __future__ import annotations

import base64
import argparse
import binascii
import json
import os
import stat
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


class AttestationVerificationError(ValueError):
    """Raised when a signature cannot be tied to a pinned verifier key."""


def provision_key(*, workspace: Path, private_key_path: Path, key_id: str) -> dict[str, str]:
    """Create an Ed25519 key exclusively outside the project workspace.

    The returned object contains only the public pin and a trusted-key registry
    fragment. The private PEM is never returned or printed.
    """
    if not isinstance(key_id, str) or not key_id.strip():
        raise AttestationVerificationError("key_id must be non-empty")
    project_root = Path(workspace).expanduser().resolve(strict=True)
    requested = Path(private_key_path).expanduser()
    if not requested.is_absolute():
        raise AttestationVerificationError("private key path must be absolute")
    destination = requested.resolve()
    try:
        destination.relative_to(project_root)
    except ValueError:
        pass
    else:
        raise AttestationVerificationError("private key must be stored outside the project workspace")
    destination.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(destination), flags, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "wb") as stream:
        stream.write(pem)
        stream.flush()
        os.fsync(stream.fileno())
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_b64 = base64.b64encode(public).decode("ascii")
    return {
        "key_id": key_id.strip(),
        "public_key_b64": public_b64,
        "trusted_keys_json": json.dumps({key_id.strip(): public_b64}, sort_keys=True),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision an external Ed25519 platform-attestation key")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--key-id", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(provision_key(
            workspace=args.workspace,
            private_key_path=args.private_key,
            key_id=args.key_id,
        ), sort_keys=True))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


def canonical_payload(payload: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def public_key_b64(private_key_path: Path) -> str:
    """Return raw Ed25519 public-key bytes as base64 from an explicit PEM path."""
    key = _load_private_key(private_key_path)
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def sign_payload(
    payload: dict[str, Any], *, private_key_path: Path, key_id: str,
) -> dict[str, Any]:
    """Sign a payload with an Ed25519 PEM key at the explicit external path."""
    if not isinstance(key_id, str) or not key_id.strip():
        raise AttestationVerificationError("key_id must be non-empty")
    signature = _load_private_key(private_key_path).sign(canonical_payload(payload))
    signed = {key: value for key, value in payload.items() if key != "signature"}
    signed["signature"] = {
        "key_id": key_id,
        "algorithm": "ed25519",
        "signature_b64": base64.b64encode(signature).decode("ascii"),
    }
    return signed


def configured_verifier_binding(
    *, key_id: str | None = None, public_key_b64_value: str | None = None,
) -> dict[str, str] | None:
    """Read the adapter's verifier pin and ensure it is in the external trust map."""
    key_id = (key_id if key_id is not None else os.environ.get("MULTIAGENT_ATTESTATION_KEY_ID", "")).strip()
    public_b64 = (
        public_key_b64_value
        if public_key_b64_value is not None
        else os.environ.get("MULTIAGENT_ATTESTATION_PUBLIC_KEY_B64", "")
    ).strip()
    if not key_id and not public_b64:
        return None
    if not key_id or not public_b64:
        raise AttestationVerificationError("both verifier key id and public key are required")
    _check_trusted_pin(key_id, public_b64)
    _decode_public_key(public_b64)
    return {"key_id": key_id, "algorithm": "ed25519", "public_key_b64": public_b64}


def trusted_public_key_b64(key_id: str) -> str:
    """Resolve a key only from the process-external trust registry."""
    registry = _trusted_registry()
    public_key = registry.get(key_id)
    if not isinstance(public_key, str) or not public_key:
        raise AttestationVerificationError("verifier key is not pinned in the external trust registry")
    _decode_public_key(public_key)
    return public_key


def verify_payload_signature(
    payload: dict[str, Any], *, expected_key_id: str, expected_public_key_b64: str,
) -> str:
    """Verify signature against both the instruction pin and external trust map."""
    _check_trusted_pin(expected_key_id, expected_public_key_b64)
    signature = payload.get("signature")
    if not isinstance(signature, dict):
        raise AttestationVerificationError("signature metadata is missing")
    if set(signature) != {"key_id", "algorithm", "signature_b64"}:
        raise AttestationVerificationError("signature metadata has unexpected fields")
    if (
        signature.get("key_id") != expected_key_id
        or signature.get("algorithm") != "ed25519"
        or not isinstance(signature.get("signature_b64"), str)
    ):
        raise AttestationVerificationError("signature metadata does not match the pinned verifier")
    try:
        signature_bytes = base64.b64decode(signature["signature_b64"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttestationVerificationError("signature_b64 is invalid") from exc
    if len(signature_bytes) != 64:
        raise AttestationVerificationError("Ed25519 signature must be 64 bytes")
    try:
        _decode_public_key(expected_public_key_b64).verify(signature_bytes, canonical_payload(payload))
    except InvalidSignature as exc:
        raise AttestationVerificationError("signature verification failed") from exc
    return expected_key_id


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    if not isinstance(path, Path):
        raise AttestationVerificationError("private_key_path must be an explicit Path")
    try:
        key = serialization.load_pem_private_key(path.expanduser().resolve().read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise AttestationVerificationError("cannot load the explicit Ed25519 private-key path") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise AttestationVerificationError("private key must be Ed25519")
    return key


def _decode_public_key(value: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttestationVerificationError("public_key_b64 is invalid") from exc
    if len(raw) != 32:
        raise AttestationVerificationError("Ed25519 public key must be 32 bytes")
    if base64.b64encode(raw).decode("ascii") != value:
        raise AttestationVerificationError("public_key_b64 is not canonical base64")
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise AttestationVerificationError("Ed25519 public key is invalid") from exc


def _check_trusted_pin(key_id: str, public_key: str) -> None:
    registry = _trusted_registry()
    if registry.get(key_id) != public_key:
        raise AttestationVerificationError("verifier key is not pinned in the external trust registry")


def _trusted_registry() -> dict[str, Any]:
    registry_text = os.environ.get("MULTIAGENT_ATTESTATION_TRUSTED_KEYS_JSON", "")
    if not registry_text:
        raise AttestationVerificationError("external attestation trust registry is not configured")
    try:
        registry = json.loads(registry_text)
    except json.JSONDecodeError as exc:
        raise AttestationVerificationError("external attestation trust registry is invalid JSON") from exc
    if not isinstance(registry, dict):
        raise AttestationVerificationError("external attestation trust registry must be an object")
    return registry


if __name__ == "__main__":
    raise SystemExit(main())
