"""Resource-isolated file validation with bounded concurrency per API worker."""
from __future__ import annotations

import json
import subprocess
import sys
from threading import BoundedSemaphore

from .attachment_validation import ValidatedAttachment

_slots = BoundedSemaphore(2)


def process_file(filename: str, content: bytes) -> ValidatedAttachment:
    # Parser work cannot monopolize the ASGI loop, create unbounded parser
    # tasks, or leave a stuck PDF parser running after its request times out.
    if not _slots.acquire(timeout=30):
        raise ValueError("File preparation is busy. Try uploading again shortly.")
    try:
        result = subprocess.run([sys.executable, "-m", "app.services.attachment_processing", filename],
                                input=content, capture_output=True, timeout=20, check=False)
        if result.returncode:
            raise ValueError("This file could not be processed safely. Export a simpler copy and try again.")
        output = json.loads(result.stdout)
        if "failure" in output:
            raise ValueError(output["failure"])
        return ValidatedAttachment(**output)
    except subprocess.TimeoutExpired as error:
        raise ValueError("This file took too long to prepare. Split it into smaller files.") from error
    finally:
        _slots.release()


if __name__ == "__main__":
    from dataclasses import asdict
    from ..config import ATTACHMENT_UPLOAD_MAX_BYTES as MAX_FILE_BYTES
    from .attachment_validation import validate_file
    # Production containers run Linux. CPU limits also work on macOS; its
    # virtual-memory accounting does not reliably support RLIMIT_AS.
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    try:
        content = sys.stdin.buffer.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise ValueError("This file is too large.")
        output = asdict(validate_file(sys.argv[1], content))
    except ValueError as error:
        output = {"failure": str(error)}
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
