import pathlib
import sys

mirror = sys.argv[1]
if not mirror.endswith("/"):
    mirror += "/"
needles = (
    "mirror+file:/etc/apt/apt-mirrors.txt",
    "https://mirrors.edge.kernel.org/ubuntu/",
    "https://ftp.osuosl.org/pub/ubuntu/",
    "http://azure.archive.ubuntu.com/ubuntu/",
    "https://azure.archive.ubuntu.com/ubuntu/",
    "http://azure.archive.ubuntu.com/ubuntu",
    "https://azure.archive.ubuntu.com/ubuntu",
    "http://archive.ubuntu.com/ubuntu/",
    "https://archive.ubuntu.com/ubuntu/",
    "http://archive.ubuntu.com/ubuntu",
    "https://archive.ubuntu.com/ubuntu",
    "http://security.ubuntu.com/ubuntu/",
    "https://security.ubuntu.com/ubuntu/",
    "http://security.ubuntu.com/ubuntu",
    "https://security.ubuntu.com/ubuntu",
)
roots = [pathlib.Path("/etc/apt/sources.list")]
extra = pathlib.Path("/etc/apt/sources.list.d")
if extra.is_dir():
    roots.extend(sorted(path for path in extra.iterdir() if path.is_file()))
for path in roots:
    if not path.is_file():
        continue
    text = path.read_text(errors="replace")
    rewritten = text
    for needle in needles:
        rewritten = rewritten.replace(needle, mirror)
    if rewritten != text:
        path.write_text(rewritten)
        print(f"rewrote {path} -> {mirror}")
