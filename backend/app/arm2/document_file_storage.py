"""Private runtime paths, content verification and the existing Disk provider."""

import hashlib
import math
import os
import re
import secrets
import shutil
import time
import unicodedata
import zipfile

from PIL import Image

from app.config import settings
from app.integrations import YandexDisk

MAX_FILES = 32
STAGE_SECONDS = 7200
# Closed platform catalogue: registered fields, not user formulas or B5-specific masks.
NAMING_TEMPLATES = {
    "DOCUMENT_NAME": ("", ("DOCUMENT.name",)),
    "PARENT_AND_KIND": ("", ("PARENT.name", "DOC-KIND.name")),
    "VEH_CALIBRATION": ("Тарировка", ("VEH.plate_number", "VEH.calibration_m3")),
}
PRESETS = tuple(NAMING_TEMPLATES)
MISSING_NAME_VALUE = "НЕТ ДАННЫХ"
CURRENT_REVISION = object()


class StorageUnavailable(RuntimeError):
    """Safe to expose: never wrap a provider URL, token, or private path."""


def storage():
    return YandexDisk()


def document_root():
    value = os.environ.get("ARM2_DOCUMENT_DISK_ROOT", "")
    parts = value.removeprefix("disk:/").split("/")
    if not value or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", part) for part in parts):
        raise StorageUnavailable("Отдельный корень документов не настроен")
    root = "disk:/" + "/".join(parts)
    cfg = settings()
    for other in [cfg.yandex_disk_root, cfg.exchange_folder, cfg.training_folder, cfg.spam_folder]:
        other = "disk:/" + other.removeprefix("disk:").strip("/")
        a, b = root.casefold(), other.casefold()
        if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
            raise StorageUnavailable("Корень документов пересекается с другим хранилищем")
    return root


def zone():
    base = settings().data_dir.absolute()
    path = base / "arm2-document-runtime-v1"
    for parent in [path, *path.parents]:
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise StorageUnavailable("Небезопасный каталог временных документов")
    path.mkdir(parents=True, exist_ok=True)
    return path


def private_path(token):
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("Неверный временный ID")
    path = zone() / token
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise StorageUnavailable("Небезопасный временный файл")
    return path


def fresh_path():
    directory = zone() / "reads"
    if directory.is_symlink() or (hasattr(directory, "is_junction") and directory.is_junction()):
        raise StorageUnavailable("Небезопасный каталог чтения документов")
    directory.mkdir(exist_ok=True)
    return directory / secrets.token_hex(32)


def verified_stage(stage):
    try:
        path = private_path(stage.id)
        if path.stat().st_size != stage.size or digest(path) != stage.sha256:
            raise OSError()
        return path
    except OSError:
        raise StorageUnavailable("Временный файл недоступен или не прошёл проверку целостности") from None


def staged_preview(stage):
    target = fresh_path()
    try:
        shutil.copyfile(verified_stage(stage), target)
        if target.stat().st_size != stage.size or digest(target) != stage.sha256:
            raise OSError()
        return target
    except OSError:
        target.unlink(missing_ok=True)
        raise StorageUnavailable("Временный файл недоступен или изменён") from None


def cleanup_reads():
    directory = zone() / "reads"
    if (
        not directory.exists()
        or directory.is_symlink()
        or (hasattr(directory, "is_junction") and directory.is_junction())
    ):
        return
    for index, path in enumerate(directory.iterdir()):
        if index >= 200:
            break
        if (
            re.fullmatch(r"[0-9a-f]{64}(\.part)?", path.name)
            and not path.is_symlink()
            and path.is_file()
            and path.stat().st_mtime < time.time() - STAGE_SECONDS
        ):
            try:
                path.unlink()
            except PermissionError:
                pass


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(65536):
            result.update(chunk)
    return result.hexdigest()


def original_name(value):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 255
        or any(ord(c) < 32 for c in value)
        or any(c in value for c in "/\\:")
        or value in {".", ".."}
    ):
        raise ValueError("Укажите исходное имя без пути и управляющих символов")
    return value  # Provenance must not be stripped or normalized.


def content_type(path):
    size = path.stat().st_size
    if not 0 < size <= settings().max_file_bytes:
        raise ValueError("Недопустимый размер файла")
    with path.open("rb") as source:
        head = source.read(16)
        source.seek(max(0, size - 1024))
        tail = source.read()
    if head.startswith(b"%PDF-") and b"%%EOF" in tail:
        return "application/pdf", "pdf"
    images = {
        "PNG": ("image/png", "png"),
        "JPEG": ("image/jpeg", "jpg"),
        "GIF": ("image/gif", "gif"),
        "WEBP": ("image/webp", "webp"),
    }
    try:
        with Image.open(path) as image:
            if image.format not in images or image.width * image.height > settings().max_image_pixels:
                raise ValueError("Недопустимый формат или размер изображения")
            result = images[image.format]
            image.verify()
            return result
    except Image.DecompressionBombError:
        raise ValueError("Изображение превышает допустимый размер") from None
    except (OSError, SyntaxError):
        pass
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                names = {entry.filename for entry in entries}
                if (
                    len(entries) > 512
                    or sum(entry.file_size for entry in entries) > settings().max_file_bytes * 4
                    or any(
                        entry.flag_bits & 1 or entry.file_size > max(1, entry.compress_size) * 200
                        for entry in entries
                    )
                ):
                    raise ValueError("Недопустимый контейнер документа")
                if "[Content_Types].xml" not in names or any(
                    "vbaproject" in name.casefold() for name in names
                ):
                    raise ValueError("Неподдерживаемое содержимое документа")
                for entry in entries:
                    if (
                        entry.filename.startswith("/")
                        or ".." in entry.filename.split("/")
                        or "\\" in entry.filename
                    ):
                        raise ValueError("Небезопасный контейнер документа")
                formats = [
                    ("word/document.xml", "wordprocessingml.document", "docx"),
                    ("xl/workbook.xml", "spreadsheetml.sheet", "xlsx"),
                    ("ppt/presentation.xml", "presentationml.presentation", "pptx"),
                ]
                selected = [(media, ext) for member, media, ext in formats if member in names]
                if len(selected) == 1 and archive.testzip() is None:
                    media, ext = selected[0]
                    return "application/vnd.openxmlformats-officedocument." + media, ext
        except zipfile.BadZipFile:
            pass
    raise ValueError(
        "Содержимое файла не соответствует поддерживаемому формату PDF, изображения или DOCX/XLSX/PPTX"
    )


def validate_naming_template(preset, name, parents):
    if preset not in (None, *PRESETS):
        raise ValueError("Unsupported document file naming template")
    if preset == "VEH_CALIBRATION" and (name.strip().casefold() != "тарировка" or parents != ["VEH"]):
        raise ValueError("Шаблон тарировки допустим только для типа «Тарировка» с применимостью только VEH")


def safe_name(preset, document, parent, kind, file_id, extension, *, generated_bundle=False,
              revision_id=CURRENT_REVISION):
    if (
        preset not in PRESETS
        or not re.fullmatch(r"FILE-[0-9]{6,}", file_id)
        or (extension not in {"pdf", "png", "jpg", "gif", "webp", "docx", "xlsx", "pptx", "bin"}
            and not (generated_bundle and extension == "zip"))
    ):
        raise ValueError("Настройте поддерживаемый шаблон имени файла в типе документа")
    if preset == 'VEH_CALIBRATION':
        validate_naming_template(preset, kind.name, kind.applicable_object_types)
        if getattr(document, 'parent_type', None) != 'VEH':
            raise ValueError("Шаблон тарировки неприменим к этому родителю")
    selected_revision = getattr(document, 'current_revision_id', None) if revision_id is CURRENT_REVISION else revision_id
    from app.arm2.vehicle_models import calibration_matches_document
    volume = getattr(parent, 'calibration_m3', None)
    confirmed = (selected_revision is not None and selected_revision == getattr(document, 'current_revision_id', None)
                 and calibration_matches_document(parent, document)) if preset == 'VEH_CALIBRATION' else False
    # Never use a questionnaire proposal, zero, stale proof or a different document/revision.
    volume = volume if confirmed and type(volume) in (int, float) and math.isfinite(volume) and volume > 0 else None
    parameters = {
        'DOCUMENT.name': getattr(document, 'name', None),
        'PARENT.name': getattr(parent, 'name', None),
        'DOC-KIND.name': getattr(kind, 'name', None),
        'VEH.plate_number': getattr(parent, 'plate_number', None),
        'VEH.calibration_m3': (str(volume).removesuffix('.0') + 'м3') if volume is not None else None,
    }
    literal, fields = NAMING_TEMPLATES[preset]
    values = [str(parameters[field]) if parameters[field] is not None and str(parameters[field]).strip()
              else MISSING_NAME_VALUE for field in fields]
    text = unicodedata.normalize('NFKC', ' '.join(([literal] if literal else []) + values))
    text = ''.join(c if (c.isalnum() or c in ' -_') and unicodedata.category(c)[0] != 'C' else '_' for c in text)
    text = re.sub(r'\s+', ' ', text).strip(' ._-')[:100].rstrip() or MISSING_NAME_VALUE
    # Limit UTF-8 bytes too, for portable Disk/ZIP names. No procedural date.
    while len(text.encode("utf8")) > 180:
        text = text[:-1]
    return f"{text}-{file_id}.{extension}"


def prepare_original(path, *, file_id, namespace, name, original_upload_name, generated_bundle=False):
    """Private publication plan shared by document processes; never accepts a remote URL."""
    from types import SimpleNamespace

    if not re.fullmatch(r"[0-9a-f-]{36}", namespace):
        raise ValueError("Invalid publication namespace")
    if generated_bundle:
        # Only server generators call this branch. Ordinary uploads still use content_type.
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError("Invalid generated package")
        media, extension = "application/zip", "zip"
    else:
        media, extension = content_type(path)
    stored = safe_name("DOCUMENT_NAME", SimpleNamespace(name=name), None, None, file_id,
                       extension, generated_bundle=generated_bundle)
    token = secrets.token_hex(32)
    shutil.copyfile(path, private_path(token))
    return {"id": file_id, "original_upload_name": original_name(original_upload_name),
            "stored_name": stored, "storage_ref": f"{document_root()}/process/{namespace}/{file_id}/{stored}",
            "size": path.stat().st_size, "sha256": digest(path), "media_type": media,
            "extension": extension, "generated_bundle": generated_bundle, "token": token}


def ensure_original(plan):
    """Retry-safe verified upload. An existing mismatching original is never overwritten."""
    from types import SimpleNamespace

    original = SimpleNamespace(**plan)
    if not original.storage_ref.startswith(document_root() + "/"):
        raise StorageUnavailable("Оригинал вне защищённого корня")
    provider = storage()
    try:
        metadata = getattr(provider, "document_metadata", provider.metadata)(original.storage_ref)
        if metadata is not None:
            if not verify_remote(original, provider):
                raise StorageUnavailable("Существующий оригинал не совпадает; перезапись запрещена")
            return
        path = verified_stage(SimpleNamespace(id=plan['token'], size=plan['size'], sha256=plan['sha256']))
        provider.upload(path, original.storage_ref, original.sha256)
        if not verify_remote(original, provider):
            raise StorageUnavailable("Хранилище не подтвердило SHA и размер оригинала")
    except StorageUnavailable:
        raise
    except Exception:
        raise StorageUnavailable("Хранилище не подтвердило сохранение; повторите ту же команду") from None


def verify_remote(file, provider=None):
    try:
        if not file.storage_ref.startswith(document_root() + "/"):
            return False
        provider = provider or storage()
        meta = getattr(provider, "document_metadata", provider.metadata)(file.storage_ref)
        return bool(
            meta
            and meta.get("type") == "file"
            and meta.get("sha256") == file.sha256
            and meta.get("size") == file.size
        )
    except Exception:
        return False


def download(file, provider=None):
    if not file.storage_ref.startswith(document_root() + "/"):
        raise StorageUnavailable("Оригинал вне настроенного защищённого корня документов")
    target = fresh_path()
    try:
        (provider or storage()).download_document(file.storage_ref, target, file.sha256, file.size)
        if target.stat().st_size != file.size or digest(target) != file.sha256:
            raise StorageUnavailable("Оригинал не прошёл проверку целостности")
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise StorageUnavailable("Оригинал временно недоступен или не прошёл проверку целостности") from None
