"""Код кандидата проверяется только внутри настоящего Docker-сандбокса."""

import hashlib
import os

import pytest

from baf.runner import TASKS, validate_code
from contracts.v0 import FileData

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not os.getenv("SANDBOX_TEST_URL"), reason="Use make test-agent"),
]


@pytest.mark.parametrize(
    "source,expected",
    [
        (
            "import re\ndef slugify(text):\n"
            '    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")\n',
            True,
        ),
        ('def slugify(text):\n    return "wrong"\n', False),
        ('import os\nos._exit(0)\ndef slugify(text):\n    return "wrong"\n', False),
    ],
    ids=["valid", "wrong-result", "early-successful-exit"],
)
def test_hidden_validation_requires_completed_tests(source, expected):
    files = {
        "slug.py": FileData(content=source, sha256=hashlib.sha256(source.encode()).hexdigest())
    }
    assert validate_code(files, TASKS["B6.1"] / "hidden_tests.py") is expected
