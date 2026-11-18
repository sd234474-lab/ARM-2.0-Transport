"""Local AUTHOR-RUN server with a file-backed provider; never imports cloud credentials."""
import hashlib
import os
import shutil
from pathlib import Path

from sqlalchemy.engine import make_url

qa_url = make_url(os.environ["DATABASE_URL"])
if (os.environ.get("APP_ENV") != "test" or qa_url.host not in ("127.0.0.1", "localhost")
        or not qa_url.database.startswith("arm2_int_") or not qa_url.database.endswith("_test")):
    raise RuntimeError("Only disposable ARM2 integration databases are allowed")

from app.api import app  # noqa: E402, F401
from app.arm2 import document_file_storage  # noqa: E402


class FileProvider:
    def __init__(self):
        self.root = Path(os.environ["ARM2_QA_FILE_ROOT"]).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def location(self, ref):
        if not ref.startswith("disk:/ARM2_INT_QA/"):
            raise ValueError("Unexpected QA reference")
        return self.root / hashlib.sha256(ref.encode()).hexdigest()

    def metadata(self, ref):
        path = self.location(ref)
        if not path.exists():
            return None
        data = path.read_bytes()
        return {"type": "file", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def upload(self, path, ref, digest):
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Invalid upload digest")
        shutil.copyfile(path, self.location(ref))

    def download_document(self, ref, target, digest, size):
        shutil.copyfile(self.location(ref), target)


document_file_storage.storage = FileProvider
