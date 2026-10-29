from frisket.runtime import model_install, spacy_install


def test_spacy_profile_is_locked_and_installs_no_application(tmp_path, monkeypatch):
    monkeypatch.setenv("FRISKET_MODEL_RUNTIME_DIR", str(tmp_path))
    profiles = []
    monkeypatch.setattr(
        model_install,
        "_install_profile",
        lambda profile, **kw: profiles.append(profile),
    )
    spacy_install.install_spacy(should_cancel=lambda: False, progress=lambda _: None)
    profile = profiles[0]
    assert profile.root == tmp_path / "spacy-v1"
    assert profile.extra is None
    assert profile.requirements_file == "spacy-requirements.txt"
    assert profile.require_hashes


def test_spacy_presence_is_passive_host_or_private_profile(monkeypatch):
    monkeypatch.setattr(spacy_install.importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(spacy_install, "is_installed", lambda: True)
    monkeypatch.setattr(
        model_install,
        "_probe_python",
        lambda *args: (_ for _ in ()).throw(AssertionError("active probe")),
    )
    assert spacy_install.runtime_present()
    monkeypatch.setattr(spacy_install, "is_installed", lambda: False)
    assert not spacy_install.runtime_present()
    monkeypatch.setattr(spacy_install.importlib.util, "find_spec", lambda _: object())
    assert spacy_install.runtime_present()
