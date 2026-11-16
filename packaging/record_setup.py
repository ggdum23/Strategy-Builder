import hashlib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    dist = ROOT / "packaging" / "dist"
    sums = dist / "SHA256SUMS"
    print(sums.read_text(encoding="utf-8"), end="" if sums.read_text(encoding="utf-8").endswith("\n") else "\n")
    matches = sorted(dist.glob("StrategyBuilder-Setup-*.exe"))
    if len(matches) != 1:
        raise SystemExit(f"expected one setup executable, found {len(matches)}")
    setup = matches[0]
    digest = hashlib.sha256(setup.read_bytes()).hexdigest()
    print(f"SHA256 {digest}")
    print(f"SIZE {setup.stat().st_size}")
    print(f"ARTIFACT {setup.name}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(
                "\n".join(
                    [
                        "### Installer artifact",
                        "",
                        f"- Name: ``{setup.name}``",
                        f"- Size: {setup.stat().st_size} bytes",
                        f"- SHA256: ``{digest}``",
                        "",
                    ]
                )
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
