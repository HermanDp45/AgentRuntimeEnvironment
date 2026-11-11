import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from baf.check_task import main  # noqa: E402

main(Path(__file__).parent, sys.argv[1])
