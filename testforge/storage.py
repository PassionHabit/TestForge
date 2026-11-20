import hashlib
import os
import tempfile
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from testforge.config import Settings
from testforge.models import ArtifactBlob


class InvalidArtifactPath(ValueError):
    pass


def normalize_artifact_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise InvalidArtifactPath("artifact path must be relative and cannot contain '..'")
    return str(path)


def store_artifact(
    db: Session, settings: Settings, execution_id: str, path: str, content: bytes
) -> ArtifactBlob:
    normalized = normalize_artifact_path(path)
    root = Path(settings.artifact_root).resolve()
    target = (root / execution_id / normalized).resolve()
    execution_root = (root / execution_id).resolve()
    if execution_root not in target.parents:
        raise InvalidArtifactPath("artifact path escapes execution directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    blob = db.scalar(
        select(ArtifactBlob).where(
            ArtifactBlob.execution_id == execution_id, ArtifactBlob.path == normalized
        )
    )
    if blob is None:
        blob = ArtifactBlob(execution_id=execution_id, path=normalized)
        db.add(blob)
    blob.size = len(content)
    blob.sha256 = digest
    blob.storage_uri = f"artifact://{execution_id}/{normalized}"
    db.commit()
    db.refresh(blob)
    return blob
