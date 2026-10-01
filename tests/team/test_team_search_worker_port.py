from frisket.team import control_plane


def test_team_search_port_requests_only_search_provider_keys(monkeypatch) -> None:
    seen = []

    def provider_keys(_engine, *, org_id, providers, decryptor):
        seen.append((org_id, providers, decryptor("ciphertext")))
        return {"exa": "tenant-key"}

    monkeypatch.setattr(control_plane, "control_plane_engine", lambda _url: object())
    monkeypatch.setattr(control_plane, "org_provider_keys", provider_keys)
    port = control_plane.TeamOrgSearchKeyCredentialPort(
        lambda value: f"decrypted:{value}"
    )

    assert port.search_provider_keys(
        org_id=11, control_database_url="postgres://control"
    ) == {"exa": "tenant-key"}
    assert seen == [(11, control_plane.SEARCH_KEY_PROVIDERS, "decrypted:ciphertext")]
