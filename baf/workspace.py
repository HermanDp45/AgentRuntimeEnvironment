"""Временная синтетическая папка для локального прогона через сетевой mount."""

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from client.workdir import Workdir


@contextmanager
def mounted_fixture(source):
    # Копируется только фикстура задачи, никогда выбранная пользовательская папка.
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError("Symlinks are not allowed in fixtures")
    with tempfile.TemporaryDirectory(prefix="are-baf-mount-") as directory:
        root = Path(directory)
        shutil.copytree(source, root, dirs_exist_ok=True)
        export = Workdir(root)
        try:
            yield root, {"port": export.server_port, "export_token": export.token}
        finally:
            export.close()
