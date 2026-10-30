"""``hiveloom_home`` and the locations derived from it.

The trust store, the extensions dir, ``models.yaml`` and the Hive db all root at
``hiveloom_home()``, so a wrong home plants user-level state — above all the
trust store — in an unintended place. An empty ``HIVELOOM_HOME`` used to make
that the working directory; it now counts as unset.
"""

from __future__ import annotations

from hiveloom import paths, trust


def test_empty_home_is_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HIVELOOM_HOME", "")
    assert paths.hiveloom_home() == (tmp_path / ".hiveloom").resolve()


def test_unset_home_defaults_to_dot_hiveloom(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HIVELOOM_HOME", raising=False)
    assert paths.hiveloom_home() == (tmp_path / ".hiveloom").resolve()


def test_relative_home_is_made_absolute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIVELOOM_HOME", "state")
    home = paths.hiveloom_home()
    assert home.is_absolute() and home == (tmp_path / "state").resolve()


def test_trust_store_follows_the_configured_home(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    # An empty HIVELOOM_HOME follows $HOME's .hiveloom, not the cwd.
    monkeypatch.setenv("HIVELOOM_HOME", "")
    assert (
        trust.trust_store_path() == (tmp_path / "user" / ".hiveloom" / "trust.json").resolve()
    )
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path / "custom"))
    assert trust.trust_store_path().parent == (tmp_path / "custom").resolve()


def test_models_yaml_follows_the_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HIVELOOM_HOME", str(tmp_path / "home"))
    assert paths.models_yaml_path() == (tmp_path / "home" / "models.yaml")
