"""Letter contents only. Storage, naming and delivery belong to B3."""

import json
import os
import textwrap
import zipfile

from PIL import Image, ImageDraw, ImageFont

from app.arm2 import document_file_storage as files


def render_letter(path, numbers, snapshots):
    candidates = [
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", "arial.ttf"),
    ]
    font = None
    for candidate in candidates:
        try:
            font = ImageFont.truetype(candidate, 24)
            break
        except OSError:
            continue
    if font is None:
        raise files.StorageUnavailable("Шрифт письма недоступен; письмо не выпущено")
    lines = [
        "Согласование техники с заказчиком",
        "",
        f"Контракт № {numbers[0]}",
        f"Контракт № {numbers[1]}",
        "",
        "Просим согласовать следующие машины:",
        "",
    ]
    for index, vehicle in enumerate(snapshots, 1):
        lines.extend(
            textwrap.wrap(
                f"{index}. {vehicle['name']} · {vehicle['plate_number']} · "
                f"{vehicle['volume_m3']:g} м³ · {vehicle['vehicle_id']}",
                65,
            )
        )
    pages = []
    for start in range(0, len(lines), 38):
        page = Image.new("RGB", (1240, 1754), "white")
        draw = ImageDraw.Draw(page)
        for row, line in enumerate(lines[start : start + 38]):
            draw.text((90, 90 + row * 39), line, fill="black", font=font)
        pages.append(page)
    try:
        pages[0].save(
            path,
            format="PDF",
            resolution=150,
            save_all=True,
            append_images=pages[1:],
            title="Согласование техники",
            subject=" / ".join(numbers),
        )
    finally:
        for page in pages:
            page.close()


def render_package(path, letter_path, letter_name, snapshots, originals):
    # Files have globally unique platform names. Never extract client ZIP members.
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.write(letter_path, files.original_name(letter_name))
        total = letter_path.stat().st_size
        seen = set()
        names = {letter_name, "manifest.json"}
        for original in originals:
            if original.id in seen:
                continue
            seen.add(original.id)
            name = files.original_name(original.stored_name)
            if name in names:
                raise ValueError("В комплекте совпали имена оригиналов; выпуск остановлен")
            names.add(name)
            total += original.size
            if total > 256 * 1024 * 1024:
                raise ValueError("Комплект превышает 256 МиБ; выберите меньше машин")
            local = files.download(original)
            try:
                package.write(local, name)
            finally:
                local.unlink(missing_ok=True)
        package.writestr("manifest.json", json.dumps(snapshots, ensure_ascii=False, indent=2))
