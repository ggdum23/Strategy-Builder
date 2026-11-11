"""Pack files stay anonymous. The sidecar holds the identity."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backtester.__main__ import main
from backtester.data import load_named
from backtester.pack import (
    CHUNK_SESSIONS,
    MAX_FILE_BYTES,
    assert_anonymized_pack_clean,
    build_pack,
    load_pack,
    load_sidecar,
)
from backtester.registry import entry_from_inspection, save_registry, upsert_series

ET = timezone(timedelta(hours=-4))


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "timestamp_et", "epoch_utc", "open", "high", "low", "close", "volume", "wap", "count", "filled", "session",
    ]
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(str(row[name]) for name in header))
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def _row(day: datetime, minute: int, price: float) -> dict:
    stamp = day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
    stamp = stamp.replace(tzinfo=ET)
    return {
        "timestamp_et": stamp.isoformat(),
        "epoch_utc": int(stamp.timestamp()),
        "open": price,
        "high": price + 0.4,
        "low": price - 0.4,
        "close": price + 0.1,
        "volume": 100,
        "wap": price,
        "count": 3,
        "filled": 0,
        "session": "rth",
    }


def _series(tmp_path, sessions: int, symbol="NIK"):
    rows = []
    day = datetime(2024, 1, 2)
    made = 0
    while made < sessions:
        if day.weekday() < 5:
            rows.append(_row(day, 8 * 60, 10.0 + made))
            rows.append(_row(day, 8 * 60 + 1, 10.5 + made))
            made += 1
        day += timedelta(days=1)
    path = tmp_path / "src" / f"{symbol}_1min_filled_2024-01.csv.gz"
    _write(path, rows)
    loaded = load_named(path.parent, symbol)
    entry = entry_from_inspection(loaded.inspection, source_root=str(path.parent), profile="arena")
    registry = tmp_path / "registry.json"
    save_registry(registry, upsert_series({"schema_version": 1, "series": []}, entry))
    return path.parent, registry


def test_chunk_defaults_and_anonymized_scrub(tmp_path):
    assert CHUNK_SESSIONS == 250
    assert MAX_FILE_BYTES == 25 * 1024 * 1024
    src, registry = _series(tmp_path, 3)
    pack = tmp_path / "pack"
    sidecar = tmp_path / "operator" / "pack-key.json"
    built = build_pack(
        src, "NIK", pack, sidecar,
        anonymize=True, registry_path=registry, profile="arena",
        price_factor=1.7, volume_factor=0.42, chunk_sessions=2,
        operator_note="keep-me-out-of-the-pack",
    )
    assert built.n_chunks == 2
    assert built.share_scale == 1.7
    names = sorted(path.name for path in pack.iterdir())
    assert names == ["bars_part_001.csv.gz", "bars_part_002.csv.gz", "daily_rth.csv.gz", "manifest.json"]
    for name in names:
        assert "NIK" not in name
        assert "2024" not in name
    assert_anonymized_pack_clean(pack, symbol="NIK")
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["asset_name"] == "ASSET"
    assert manifest["anonymized"] is True
    secret = load_sidecar(sidecar)
    assert secret["symbol"] == "NIK"
    assert secret["price_factor"] == 1.7
    assert secret["volume_factor"] == 0.42
    assert secret["share_scale"] == 1.7
    assert secret["date_map"]["1"].startswith("2024-")
    assert "keep-me-out-of-the-pack" == secret["operator_note"]
    loaded = load_pack(pack)
    assert list(loaded.frame["date"].unique()) == ["S00001", "S00002", "S00003"]
    assert float(loaded.frame.iloc[0]["open"]) == pytest.approx(10.0 * 1.7)
    assert int(loaded.frame.iloc[0]["count"]) == 3
    assert "NIK" not in loaded.frame.to_csv(index=False)
    text = gzip.decompress((pack / "bars_part_001.csv.gz").read_bytes()).decode("utf-8")
    assert "2024-" not in text
    assert "price_factor" not in text
    assert sidecar.resolve().parent != pack.resolve()


def test_a_session_is_not_split_and_a_failed_build_keeps_an_old_sidecar(tmp_path):
    src, registry = _series(tmp_path, 1)
    pack = tmp_path / "pack"
    sidecar = tmp_path / "old.json"
    sidecar.write_text("keep\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not 1"):
        build_pack(
            src, "NIK", pack, sidecar,
            anonymize=True, registry_path=registry, price_factor=1.0, volume_factor=0.5,
        )
    assert sidecar.read_text(encoding="utf-8") == "keep\n"
    assert not pack.exists() or not any(pack.iterdir())
    with pytest.raises(ValueError, match="not under"):
        build_pack(
            src, "NIK", pack, tmp_path / "fresh.json",
            anonymize=True, registry_path=registry, price_factor=0.5, volume_factor=0.5,
            max_bytes=20,
        )
    inside = tmp_path / "inside"
    inside.mkdir()
    with pytest.raises(ValueError, match="inside"):
        build_pack(
            src, "NIK", inside, inside / "secret.json",
            anonymize=True, registry_path=registry, price_factor=0.5, volume_factor=0.5,
        )


def test_named_pack_is_unscaled_and_factors_of_one_are_refused(tmp_path):
    src, registry = _series(tmp_path, 1)
    pack = tmp_path / "named"
    sidecar = tmp_path / "named.json"
    built = build_pack(
        src, "NIK", pack, sidecar, anonymize=False, registry_path=registry, profile="arena",
    )
    assert built.share_scale == 1.0
    secret = load_sidecar(sidecar)
    assert secret["anonymized"] is False
    assert secret["share_scale"] == 1.0
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["asset_name"] == "NIK"
    assert "2024-" in secret["date_map"]["0"] or secret["date_map"]["0"].startswith("2024-")
    with pytest.raises(ValueError, match="not rescaled|Omit"):
        build_pack(
            src, "NIK", tmp_path / "bad", tmp_path / "bad.json",
            anonymize=False, registry_path=registry, price_factor=1.7, volume_factor=1.0,
        )


def test_pack_cli_does_not_print_the_scale_factor(tmp_path, capsys):
    src, registry = _series(tmp_path, 1)
    pack = tmp_path / "cli-pack"
    sidecar = tmp_path / "cli.json"
    code = main(
        [
            "pack", "--ticker", "NIK", "--src", str(src), "--registry", str(registry),
            "--out", str(pack), "--sidecar", str(sidecar), "--anonymize",
            "--price-factor", "0.25", "--volume-factor", "0.3", "--profile", "arena",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "anonymized pack" in out
    assert "sidecar" in out
    assert "0.25" not in out
    assert "0.3" not in out
    assert "price_factor" not in out
    hold = Path(__file__).resolve().parents[1] / "examples" / "desk" / "buy_and_hold.py"
    ran = main(
        [
            "run", "--profile", "desk", "--strategy", str(hold),
            "--pack", str(pack), "--sidecar", str(sidecar), "--out", str(tmp_path / "run"),
        ]
    )
    assert ran == 0
    report = capsys.readouterr().out
    assert "cost basis" in report
    assert "share_scale=0.25" in report
    assert "desk" in report


def test_total_return_is_recorded_by_refusing_the_pack(tmp_path):
    src, registry = _series(tmp_path, 1)
    with pytest.raises(ValueError, match="total_return"):
        build_pack(
            src, "NIK", tmp_path / "p", tmp_path / "s.json",
            anonymize=False, registry_path=registry, price_kind="total_return",
        )


def test_default_chunk_splits_after_250_sessions(tmp_path):
    src, registry = _series(tmp_path, 251)
    pack = tmp_path / "wide"
    built = build_pack(
        src, "NIK", pack, tmp_path / "wide.json",
        anonymize=True, registry_path=registry, price_factor=0.2, volume_factor=0.3,
    )
    assert built.n_sessions == 251
    assert built.n_chunks == 2
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    assert [item["sessions"] for item in manifest["chunks"]] == [250, 1]
    assert all(path.stat().st_size < MAX_FILE_BYTES for path in pack.iterdir())


def test_registry_and_sidecar_reject_duplicate_json_keys(tmp_path):
    registry = tmp_path / "reg.json"
    registry.write_text('{"schema_version": 1, "series": [], "schema_version": 1}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key 'schema_version'"):
        from backtester.registry import load_registry

        load_registry(registry)
    nested = tmp_path / "nested.json"
    nested.write_text(
        '{"schema_version": 1, "series": [{"symbol": "NIK", "symbol": "QQQ"}]}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate JSON key 'symbol'"):
        from backtester.registry import load_registry

        load_registry(nested)
    sidecar = tmp_path / "side.json"
    sidecar.write_text('{"pack_id": "abc", "pack_id": "def"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key 'pack_id'"):
        load_sidecar(sidecar)


def _recompress(payload: bytes, *, mtime: int = 0, filename: str = "") -> bytes:
    import io

    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=mtime, filename=filename) as handle:
        handle.write(payload)
    return buffer.getvalue()


def _with_comment(blob: bytes, comment: bytes) -> bytes:
    if blob[3] & 0x1C:
        raise AssertionError("test gzip header already has optional fields")
    flagged = blob[:3] + bytes([blob[3] | 0x10]) + blob[4:10]
    return flagged + comment + b"\x00" + blob[10:]


def test_gzip_members_carry_no_name_comment_or_mtime(tmp_path):
    src, registry = _series(tmp_path, 1)
    pack = tmp_path / "pack"
    build_pack(
        src, "NIK", pack, tmp_path / "side.json",
        anonymize=True, registry_path=registry, price_factor=0.5, volume_factor=0.4,
    )
    assert_anonymized_pack_clean(pack, symbol="NIK")
    target = pack / "bars_part_001.csv.gz"
    original = target.read_bytes()
    payload = gzip.decompress(original)
    cases = [
        _recompress(payload, filename="QQQ.csv"),
        _recompress(payload, mtime=1),
        _with_comment(original, b"note"),
    ]
    for blob in cases:
        target.write_bytes(blob)
        with pytest.raises(ValueError, match="gzip"):
            assert_anonymized_pack_clean(pack, symbol="NIK")
        target.write_bytes(original)
    assert_anonymized_pack_clean(pack, symbol="NIK")


def test_volume_is_scaled_and_trade_count_is_not(tmp_path):
    src, registry = _series(tmp_path, 1)
    pack = tmp_path / "pack"
    build_pack(
        src, "NIK", pack, tmp_path / "side.json",
        anonymize=True, registry_path=registry, price_factor=1.7, volume_factor=0.42,
    )
    loaded = load_pack(pack)
    assert float(loaded.frame.iloc[0]["volume"]) == pytest.approx(100 * 0.42)
    assert int(loaded.frame.iloc[0]["count"]) == 3
    assert float(loaded.frame.iloc[0]["open"]) == pytest.approx(10.0 * 1.7)


def test_gzip_audit_checks_every_member(tmp_path):
    src, registry = _series(tmp_path, 1)
    pack = tmp_path / "pack"
    build_pack(
        src, "NIK", pack, tmp_path / "side.json",
        anonymize=True, registry_path=registry, price_factor=0.5, volume_factor=0.4,
    )
    target = pack / "bars_part_001.csv.gz"
    original = target.read_bytes()
    hidden = _recompress(b"hidden\n", filename="QQQ.csv")
    target.write_bytes(original + hidden)
    with pytest.raises(ValueError, match="gzip"):
        assert_anonymized_pack_clean(pack, symbol="NIK")
    target.write_bytes(original)
    assert_anonymized_pack_clean(pack, symbol="NIK")


def test_load_pack_rejects_duplicate_manifest_keys(tmp_path):
    src, registry = _series(tmp_path, 1)
    pack = tmp_path / "pack"
    build_pack(
        src, "NIK", pack, tmp_path / "side.json",
        anonymize=True, registry_path=registry, price_factor=0.5, volume_factor=0.4,
    )
    manifest = pack / "manifest.json"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text("{\n  \"schema_version\": 9," + text[1:], encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key 'schema_version'"):
        load_pack(pack)
