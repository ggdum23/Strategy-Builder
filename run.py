import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
raise SystemExit(subprocess.call([sys.executable, "-m", "backtester", *sys.argv[1:]], cwd=root))
