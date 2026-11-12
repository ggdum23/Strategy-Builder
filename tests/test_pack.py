"""Pack files stay anonymous. The sidecar holds the identity."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backtester.__main__ import main
from backtester.data import load_named
from backtester.pack import (
    CHUNK_SESSIONS,
    MAX_FILE_BYTES,
    _gzip_csv,
    _pack_id,
    _read_csv_gz,
    assert_anonymized_pack_clean,
    build_pack,
    load_pack,
    load_sidecar,
    require_build_price_factor,
)
from backtester.runner import run_pack
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
    assert manifest["asset_name"] == "RANDOM"
    assert manifest["anonymized"] is True
    assert "price_seal" not in manifest
    assert "price_factor" not in (pack / "manifest.json").read_text(encoding="utf-8")
    secret = load_sidecar(sidecar)
    assert require_build_price_factor(secret) == 1.7
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


def _pack_views(pack: Path) -> list[bytes]:
    views = []
    for path in pack.rglob("*"):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        views.append(raw)
        if path.name.endswith(".gz"):
            views.append(gzip.decompress(raw))
    return views


def test_pack_has_no_unkeyed_digest_of_the_price_factor(tmp_path):
    src, registry = _series(tmp_path, 2)
    pack = tmp_path / "pack"
    build_pack(
        src, "NIK", pack, tmp_path / "side.json",
        anonymize=True, registry_path=registry, profile="arena",
        price_factor=1.7, volume_factor=0.42,
    )
    forms = {
        "1.7",
        "1.70",
        "1.700",
        "1.7000",
        "1.70000",
        json.dumps(1.7),
        json.dumps("1.7"),
        format(1.7, ".16g"),
        format(1.7, ".17g"),
        repr(1.7),
        str(1.7),
        f"{1.7:.1f}",
        f"{1.7:.2f}",
        f"{1.7:.3f}",
        f"{1.7:.4f}",
        f"{1.7:.8f}",
        f"{1.7:.17g}",
    }
    side = json.loads((tmp_path / "side.json").read_text(encoding="utf-8"))
    assert side["factor_mac"] != hashlib.sha256(b"1.7").hexdigest()
    assert side["factor_mac"] != hashlib.md5(b"1.7").hexdigest()
    views = _pack_views(pack)
    assert views
    for form in forms:
        for algo in (hashlib.sha256, hashlib.md5):
            digest = algo(form.encode("utf-8")).hexdigest()
            for view in views:
                assert digest.encode("ascii") not in view
    for view in views:
        text = view.decode("utf-8", errors="ignore")
        assert "price_seal" not in text
        assert "factor_key" not in text
        assert "factor_mac" not in text


def test_manifest_or_sidecar_tamper_is_refused(tmp_path):
    src, registry = _series(tmp_path, 1)
    pack = tmp_path / "pack"
    sidecar = tmp_path / "side.json"
    build_pack(
        src, "NIK", pack, sidecar,
        anonymize=True, registry_path=registry, profile="arena",
        price_factor=1.7, volume_factor=0.42,
    )
    manifest_path = pack / "manifest.json"
    original_manifest = manifest_path.read_text(encoding="utf-8")
    original_sidecar = sidecar.read_text(encoding="utf-8")
    manifest = json.loads(original_manifest)
    manifest["price_seal"] = hashlib.sha256(b"1.7").hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown"):
        load_pack(pack)
    manifest_path.write_text(original_manifest, encoding="utf-8")
    manifest = json.loads(original_manifest)
    manifest["pack_id"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="pack_id"):
        load_pack(pack)
    manifest_path.write_text(original_manifest, encoding="utf-8")
    load_pack(pack)

    forged = json.loads(original_sidecar)
    forged["price_factor"] = 2.5
    forged["share_scale"] = 2.5
    sidecar.write_text(json.dumps(forged, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="factor_mac"):
        load_sidecar(sidecar)

    forged = json.loads(original_sidecar)
    forged["factor_mac"] = hashlib.sha256(b"1.7").hexdigest()
    sidecar.write_text(json.dumps(forged, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="factor_mac"):
        load_sidecar(sidecar)

    forged = json.loads(original_sidecar)
    forged["factor_key"] = "ab" * 32
    sidecar.write_text(json.dumps(forged, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="factor_mac"):
        load_sidecar(sidecar)

    stripped = json.loads(original_sidecar)
    del stripped["factor_key"]
    del stripped["factor_mac"]
    sidecar.write_text(json.dumps(stripped, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="rebuild the pack"):
        load_sidecar(sidecar)

    named_pack = tmp_path / "named"
    named_side = tmp_path / "named.json"
    build_pack(
        src, "NIK", named_pack, named_side,
        anonymize=False, registry_path=registry, profile="arena",
    )
    legacy = json.loads(named_side.read_text(encoding="utf-8"))
    del legacy["factor_key"]
    del legacy["factor_mac"]
    named_side.write_text(json.dumps(legacy, indent=2) + "\n", encoding="utf-8")
    loaded = load_sidecar(named_side)
    assert require_build_price_factor(loaded) == 1.0
    named = load_pack(named_pack)
    assert named.anonymized is False
    assert str(named.frame["session_id"].iloc[0]).startswith("2024-")


class _Quiet:
    def on_bar(self, _bar, _account):
        return None


def _retouch(pack: Path, mutate) -> str:
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    hashed = []
    names = [chunk["file"] for chunk in manifest["chunks"]] + [manifest["daily_file"]]
    for name in names:
        path = pack / name
        frame = _read_csv_gz(path)
        mutate(frame)
        blob = _gzip_csv(frame)
        path.write_bytes(blob)
        hashed.append((name, blob))
    manifest["pack_id"] = _pack_id(hashed)
    (pack / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest["pack_id"]


def _write_json(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def test_metadata_downgrade_cannot_skip_the_mac(tmp_path):
    src, registry = _series(tmp_path, 2)
    pack = tmp_path / "pack"
    sidecar = tmp_path / "side.json"
    build_pack(
        src, "NIK", pack, sidecar,
        anonymize=True, registry_path=registry, profile="arena",
        price_factor=1.7, volume_factor=0.42,
    )
    bars = gzip.decompress((pack / "bars_part_001.csv.gz").read_bytes()).decode("utf-8")
    assert "S00001" in bars
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    manifest["anonymized"] = False
    manifest["asset_name"] = "NIK"
    _write_json(pack / "manifest.json", manifest)
    secret = json.loads(sidecar.read_text(encoding="utf-8"))
    secret["anonymized"] = False
    secret["price_factor"] = 1
    secret["volume_factor"] = 1
    secret["share_scale"] = 1
    del secret["factor_key"]
    del secret["factor_mac"]
    _write_json(sidecar, secret)
    with pytest.raises(ValueError, match="anonymized flag must be true"):
        load_pack(pack)
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="anonymized flag must be true"):
        run_pack(_Quiet, pack, sidecar, "arena", out, dividends={"2024-01-03": 1.25}, with_benchmarks=False)
    assert not (out / "metrics.json").exists()


def test_named_asset_stand_in_requires_the_anonymized_flag(tmp_path):
    src, registry = _series(tmp_path, 1)
    pack = tmp_path / "named"
    build_pack(
        src, "NIK", pack, tmp_path / "named.json",
        anonymize=False, registry_path=registry, profile="arena",
    )
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    manifest["asset_name"] = "RANDOM"
    _write_json(pack / "manifest.json", manifest)
    with pytest.raises(ValueError, match="anonymized flag must be true"):
        load_pack(pack)


def test_calendar_session_id_requires_a_named_pack(tmp_path):
    src, registry = _series(tmp_path, 1)
    pack = tmp_path / "pack"
    build_pack(
        src, "NIK", pack, tmp_path / "side.json",
        anonymize=True, registry_path=registry, profile="arena",
        price_factor=1.7, volume_factor=0.42,
    )

    def _date(frame):
        frame.loc[frame["session_id"] == "S00001", "session_id"] = "2024-01-02"

    _retouch(pack, _date)
    with pytest.raises(ValueError, match="anonymized flag must be false"):
        load_pack(pack)


def test_mac_binds_pack_id_date_map_and_share_scale(tmp_path):
    src, registry = _series(tmp_path, 2)
    pack = tmp_path / "pack"
    sidecar = tmp_path / "side.json"
    other = tmp_path / "other"
    other_side = tmp_path / "other.json"
    build_pack(
        src, "NIK", pack, sidecar,
        anonymize=True, registry_path=registry, profile="arena",
        price_factor=1.7, volume_factor=0.42,
    )
    build_pack(
        src, "NIK", other, other_side,
        anonymize=True, registry_path=registry, profile="arena",
        price_factor=2.0, volume_factor=0.42,
    )
    honest = json.loads(sidecar.read_text(encoding="utf-8"))
    assert require_build_price_factor(honest) == 1.7
    original_sidecar = sidecar.read_text(encoding="utf-8")

    def _scale(frame):
        for column in ("open", "high", "low", "close", "wap"):
            frame[column] = frame[column].astype(float) * 1.01

    new_id = _retouch(pack, _scale)
    secret = json.loads(original_sidecar)
    secret["pack_id"] = new_id
    _write_json(sidecar, secret)
    load_pack(pack)
    with pytest.raises(ValueError, match="factor_mac"):
        load_sidecar(sidecar)
    with pytest.raises(ValueError, match="factor_mac"):
        run_pack(_Quiet, pack, sidecar, "arena", tmp_path / "scaled", with_benchmarks=False)

    fresh = tmp_path / "fresh"
    fresh_side = tmp_path / "fresh.json"
    build_pack(
        src, "NIK", fresh, fresh_side,
        anonymize=True, registry_path=registry, profile="arena",
        price_factor=1.7, volume_factor=0.42,
    )
    swapped = json.loads(fresh_side.read_text(encoding="utf-8"))
    keys = list(swapped["date_map"])
    assert len(keys) >= 2
    swapped["date_map"][keys[0]], swapped["date_map"][keys[1]] = (
        swapped["date_map"][keys[1]],
        swapped["date_map"][keys[0]],
    )
    _write_json(fresh_side, swapped)
    load_pack(fresh)
    with pytest.raises(ValueError, match="factor_mac"):
        load_sidecar(fresh_side)
    with pytest.raises(ValueError, match="factor_mac"):
        run_pack(_Quiet, fresh, fresh_side, "arena", tmp_path / "swapped", dividends={"2024-01-03": 1.25}, with_benchmarks=False)

    donor = json.loads(other_side.read_text(encoding="utf-8"))
    graft_pack = tmp_path / "graft"
    graft_side = tmp_path / "graft.json"
    build_pack(
        src, "NIK", graft_pack, graft_side,
        anonymize=True, registry_path=registry, profile="arena",
        price_factor=1.7, volume_factor=0.42,
    )
    honest_graft = json.loads(graft_side.read_text(encoding="utf-8"))
    for field in ("factor_key", "factor_mac", "price_factor", "share_scale"):
        honest_graft[field] = donor[field]
    assert honest_graft["pack_id"] != donor["pack_id"]
    _write_json(graft_side, honest_graft)
    load_pack(graft_pack)
    with pytest.raises(ValueError, match="factor_mac"):
        load_sidecar(graft_side)
    with pytest.raises(ValueError, match="factor_mac"):
        run_pack(_Quiet, graft_pack, graft_side, "arena", tmp_path / "graft-out", with_benchmarks=False)


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


def test_symbol_asset_publishes_as_random(tmp_path):
    src, registry = _series(tmp_path, 1, symbol="asset")
    pack = tmp_path / "pack"
    sidecar = tmp_path / "secret.json"
    build_pack(
        src,
        "asset",
        pack,
        sidecar,
        anonymize=True,
        registry_path=registry,
        profile="arena",
        price_factor=1.5,
        volume_factor=0.5,
    )
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["asset_name"] == "RANDOM"
    loaded = load_pack(pack)
    assert loaded.manifest["asset_name"] == "RANDOM"
    assert_anonymized_pack_clean(pack, symbol="asset")


def test_named_pack_keeps_a_reserved_ticker(tmp_path):
    src, registry = _series(tmp_path, 1, symbol="CSV")
    pack = tmp_path / "named"
    sidecar = tmp_path / "named.json"
    build_pack(
        src, "CSV", pack, sidecar, anonymize=False, registry_path=registry, profile="arena",
    )
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["asset_name"] == "CSV"
    assert manifest["anonymized"] is False
    assert (pack / "bars_part_001.csv.gz").is_file()
    assert (pack / "daily_rth.csv.gz").is_file()
    assert (pack / "manifest.json").is_file()


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


def _with_fhcrc(blob: bytes, crc: bytes = b"MA") -> bytes:
    if blob[3] & 0x02:
        raise AssertionError("test gzip header already has FHCRC")
    if len(crc) != 2:
        raise AssertionError("FHCRC is two bytes")
    flagged = blob[:3] + bytes([blob[3] | 0x02]) + blob[4:10]
    return flagged + crc + blob[10:]


def _with_extra(blob: bytes, extra: bytes) -> bytes:
    if blob[3] & 0x1C:
        raise AssertionError("test gzip header already has optional fields")
    flagged = blob[:3] + bytes([blob[3] | 0x04]) + blob[4:10]
    length = len(extra).to_bytes(2, "little")
    return flagged + length + extra + blob[10:]


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


def test_load_pack_rejects_an_anonymized_gzip_fhcrc(tmp_path):
    src, registry = _series(tmp_path, 1)
    pack = tmp_path / "pack"
    build_pack(
        src, "NIK", pack, tmp_path / "side.json",
        anonymize=True, registry_path=registry, price_factor=0.5, volume_factor=0.4,
    )
    target = pack / "bars_part_001.csv.gz"
    original = target.read_bytes()
    target.write_bytes(_with_fhcrc(original, b"MA"))
    _rewrite_pack_id(pack)
    with pytest.raises(ValueError, match="FHCRC") as caught:
        load_pack(pack)
    assert "pack_id" not in str(caught.value)
    with pytest.raises(ValueError, match="FHCRC") as caught:
        assert_anonymized_pack_clean(pack, symbol="NIK")
    assert "pack_id" not in str(caught.value)
    target.write_bytes(original)
    _rewrite_pack_id(pack)
    load_pack(pack)


def test_pack_scan_does_not_treat_asset_substrings_as_tickers(tmp_path):
    for symbol in ("SS", "AS", "ET", "ASS", "ID"):
        src, registry = _series(tmp_path / symbol, 1, symbol=symbol)
        pack = tmp_path / symbol / "pack"
        build_pack(
            src, symbol, pack, tmp_path / symbol / "side.json",
            anonymize=True, registry_path=registry, price_factor=0.5, volume_factor=0.4,
        )
        assert_anonymized_pack_clean(pack, symbol=symbol)
        (pack / "note.txt").write_text(f"my_{symbol}_v2\n", encoding="utf-8")
        with pytest.raises(ValueError, match="ticker"):
            assert_anonymized_pack_clean(pack, symbol=symbol)


def test_load_pack_rejects_an_anonymized_gzip_extra_field(tmp_path):
    src, registry = _series(tmp_path, 1)
    pack = tmp_path / "pack"
    build_pack(
        src, "NIK", pack, tmp_path / "side.json",
        anonymize=True, registry_path=registry, price_factor=0.5, volume_factor=0.4,
    )
    target = pack / "bars_part_001.csv.gz"
    original = target.read_bytes()
    target.write_bytes(_with_extra(original, b"ZZZX"))
    with pytest.raises(ValueError, match="FEXTRA"):
        load_pack(pack)
    with pytest.raises(ValueError, match="FEXTRA"):
        assert_anonymized_pack_clean(pack, symbol="NIK")
    target.write_bytes(original)
    load_pack(pack)


def _rewrite_pack_id(pack: Path) -> None:
    manifest_path = pack / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hashed = [(chunk["file"], (pack / chunk["file"]).read_bytes()) for chunk in manifest["chunks"]]
    hashed.append((manifest["daily_file"], (pack / manifest["daily_file"]).read_bytes()))
    manifest["pack_id"] = _pack_id(hashed)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def test_load_pack_rejects_a_second_clean_gzip_member(tmp_path):
    src, registry = _series(tmp_path, 1)
    pack = tmp_path / "pack"
    build_pack(
        src, "NIK", pack, tmp_path / "side.json",
        anonymize=True, registry_path=registry, price_factor=0.5, volume_factor=0.4,
    )
    target = pack / "bars_part_001.csv.gz"
    original = target.read_bytes()
    payload = gzip.decompress(original)
    second = _recompress(payload, mtime=0, filename="")
    target.write_bytes(original + second)
    _rewrite_pack_id(pack)
    with pytest.raises(ValueError, match="members") as caught:
        load_pack(pack)
    assert "pack_id" not in str(caught.value)
    with pytest.raises(ValueError, match="members") as caught:
        assert_anonymized_pack_clean(pack, symbol="NIK")
    assert "pack_id" not in str(caught.value)
    target.write_bytes(original)
    _rewrite_pack_id(pack)
    loaded = load_pack(pack)
    assert len(loaded.frame) == len(gzip.decompress(original).splitlines()) - 1


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
