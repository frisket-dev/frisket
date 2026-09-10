from __future__ import annotations

import json
from pathlib import Path

import pytest

from frisket.contracts.plugin import (
    PluginManifestLoadError,
    load_plugin_manifest_file,
)


def _write_manifest(
    tmp_path: Path,
    *,
    plugin_id: str,
    version: str = "1.2.3",
    contributes: dict | None = None,
    runtime: dict | None = None,
) -> Path:
    path = tmp_path / "identity.plugin.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "frisket.plugin.v1",
                "id": plugin_id,
                "version": version,
                "contributes": contributes
                or {
                    "actions": [],
                    "importers": ["ndjson"],
                    "column_types": [],
                    "job_handlers": [],
                },
                "requires": {
                    "capabilities": [],
                    "secrets": [],
                },
                "runtime": runtime or {},
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("plugin_id", "version"),
    [
        ("frisket-ndjson", "0.1.0"),
        ("frisket.geo", "1.2.3"),
        ("acme_geo_tools", "2.0.0-alpha.1"),
        ("acme_geo_tools", "2.0.0-alpha.1+build.7"),
        ("a1.b2-c3_d4", "10.20.30+build.7"),
    ],
)
def test_plugin_manifest_accepts_canonical_identity(
    tmp_path: Path, plugin_id: str, version: str
) -> None:
    loaded = load_plugin_manifest_file(
        _write_manifest(tmp_path, plugin_id=plugin_id, version=version)
    )
    assert loaded.manifest.id == plugin_id
    assert loaded.manifest.version == version


@pytest.mark.parametrize(
    "plugin_id",
    [
        "Frisket.Geo",
        "frisket geo",
        "frisket/geo",
        "frisket..geo",
        ".frisket.geo",
        "frisket.geo.",
        "frisket:geo",
        "frisket\ngeo",
        "-frisket",
        "frisket-",
        "frisket._geo",
        "frisket.geo_",
        "frisket.-geo",
        "frisket.geo-",
    ],
)
def test_plugin_manifest_rejects_ambiguous_plugin_ids(
    tmp_path: Path, plugin_id: str
) -> None:
    with pytest.raises(PluginManifestLoadError) as exc_info:
        load_plugin_manifest_file(_write_manifest(tmp_path, plugin_id=plugin_id))
    assert exc_info.value.code == "invalid_plugin_manifest"
    assert "plugin id" in exc_info.value.message


@pytest.mark.parametrize(
    "version",
    [
        "",
        "v1.2.3",
        "1",
        "1.2",
        "1.2.3 beta",
        "1.2.3/beta",
        "1.2.3-01",
        "1.2.3-alpha..1",
        "01.2.3",
        "latest",
        "2026.06.23",
    ],
)
def test_plugin_manifest_rejects_non_semver_versions(
    tmp_path: Path, version: str
) -> None:
    with pytest.raises(PluginManifestLoadError) as exc_info:
        load_plugin_manifest_file(
            _write_manifest(tmp_path, plugin_id="frisket.geo", version=version)
        )
    assert exc_info.value.code == "invalid_plugin_manifest"
    assert "plugin version" in exc_info.value.message


def test_plugin_manifest_normalizes_and_rejects_ambiguous_contribution_names(
    tmp_path: Path,
) -> None:
    with pytest.raises(PluginManifestLoadError) as exc_info:
        load_plugin_manifest_file(
            _write_manifest(
                tmp_path,
                plugin_id="frisket.geo",
                contributes={
                    "actions": ["frisket.demo.action", " frisket.demo.action "],
                    "importers": [],
                    "column_types": [],
                    "job_handlers": [],
                },
            )
        )
    assert exc_info.value.code == "invalid_plugin_manifest"
    assert "duplicate contribution name" in exc_info.value.message

    with pytest.raises(PluginManifestLoadError) as control_exc:
        load_plugin_manifest_file(
            _write_manifest(
                tmp_path,
                plugin_id="frisket.geo",
                contributes={
                    "actions": ["frisket.demo.\naction"],
                    "importers": [],
                    "column_types": [],
                    "job_handlers": [],
                },
            )
        )
    assert control_exc.value.code == "invalid_plugin_manifest"
    assert "control characters" in control_exc.value.message


def test_plugin_manifest_rejects_ambiguous_runtime_bindings(tmp_path: Path) -> None:
    with pytest.raises(PluginManifestLoadError) as duplicate_key:
        load_plugin_manifest_file(
            _write_manifest(
                tmp_path,
                plugin_id="frisket.geo",
                contributes={
                    "actions": ["frisket.demo.one", "frisket.demo.two"],
                    "importers": [],
                    "column_types": [],
                    "job_handlers": [],
                },
                runtime={
                    "actions": [
                        {
                            "kind": "frisket.demo.one",
                            "handler_key": "frisket.geo:shared",
                        },
                        {
                            "kind": "frisket.demo.two",
                            "handler_key": "frisket.geo:shared",
                        },
                    ]
                },
            )
        )
    assert duplicate_key.value.code == "invalid_plugin_manifest"
    assert "duplicate runtime binding handler_key" in duplicate_key.value.message

    with pytest.raises(PluginManifestLoadError) as whitespace:
        load_plugin_manifest_file(
            _write_manifest(
                tmp_path,
                plugin_id="frisket.geo",
                contributes={
                    "actions": ["frisket.demo.action"],
                    "importers": [],
                    "column_types": [],
                    "job_handlers": [],
                },
                runtime={
                    "actions": [
                        {
                            "kind": " frisket.demo.action ",
                            "handler_key": "frisket.geo:action",
                        }
                    ]
                },
            )
        )
    assert whitespace.value.code == "invalid_plugin_manifest"
    assert "surrounding whitespace" in whitespace.value.message


def test_plugin_manifest_derives_runtime_handler_key_from_binding_kind(
    tmp_path: Path,
) -> None:
    loaded = load_plugin_manifest_file(
        _write_manifest(
            tmp_path,
            plugin_id="demo.weather_forecast",
            contributes={
                "actions": ["demo.weather_forecast.op.forecast_highs"],
                "importers": ["demo.weather_forecast.importer.cases"],
                "column_types": [],
                "job_handlers": [],
            },
            runtime={
                "actions": [{"kind": "demo.weather_forecast.op.forecast_highs"}],
                "importers": [
                    {
                        "kind": "demo.weather_forecast.importer.cases",
                        "handler_api": "plugin_importer",
                    }
                ],
            },
        )
    )

    assert (
        loaded.manifest.runtime.actions[0].handler_key
        == "demo.weather_forecast:forecast_highs"
    )
    assert (
        loaded.manifest.runtime.importers[0].handler_key
        == "demo.weather_forecast:cases"
    )


def test_plugin_manifest_rejects_handler_key_collision_after_derivation(
    tmp_path: Path,
) -> None:
    with pytest.raises(PluginManifestLoadError) as exc_info:
        load_plugin_manifest_file(
            _write_manifest(
                tmp_path,
                plugin_id="demo.weather_forecast",
                contributes={
                    "actions": [
                        "demo.weather_forecast.op.forecast_highs",
                        "demo.weather_forecast.alt.forecast_highs",
                    ],
                    "importers": [],
                    "column_types": [],
                    "job_handlers": [],
                },
                runtime={
                    "actions": [
                        {"kind": "demo.weather_forecast.op.forecast_highs"},
                        {"kind": "demo.weather_forecast.alt.forecast_highs"},
                    ]
                },
            )
        )

    assert exc_info.value.code == "invalid_plugin_manifest"
    assert "duplicate runtime binding handler_key" in exc_info.value.message
