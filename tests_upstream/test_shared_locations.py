from zju_ac.config import CONFIG_PATH, load_config
from zju_ac.desktop.storage import ConfigStore
from zju_ac.ios.locations import coordinates, merge_presets


def test_presets_are_added_without_selecting_or_overwriting_existing_places(tmp_path):
    path = tmp_path / "location.md"
    path.write_text("紫金港：\n北1：120.1,30.2", "utf-8")
    original = {
        "gps": {
            "location": "old",
            "locations": {"old": {"name": "已有地点", "latitude": 30.0, "longitude": 120.0}},
        }
    }
    merged = merge_presets(original, path)
    assert merged["gps"]["location"] == "old"
    assert merged["gps"]["locations"]["old"] == original["gps"]["locations"]["old"]
    key = next(k for k in merged["gps"]["locations"] if k.startswith("md_"))
    point = coordinates("120.1", "30.2", "gcj02")
    assert merged["gps"]["locations"][key]["latitude"] == point.latitude
    assert len(original["gps"]["locations"]) == 1


def test_selected_preset_roundtrips_into_real_checkin_settings(tmp_path, monkeypatch):
    path = tmp_path / "location.md"
    path.write_text("测试：120.1,30.2", "utf-8")
    monkeypatch.setattr("zju_ac.desktop.storage.locations_file", lambda _: path)
    template = tmp_path / "template.toml"
    template.write_text('[gps]\nlocation=""\n', "utf-8")
    store = ConfigStore(tmp_path / "data", template)
    assert store.settings.gps is None
    key = next(place["key"] for place in store.locations if place["key"].startswith("md_"))
    store.save({"location": key})
    assert store.settings.gps.location == key
    assert ConfigStore(tmp_path / "data", CONFIG_PATH).settings.gps == store.settings.gps


def test_terminal_config_uses_same_optional_presets(tmp_path):
    md = tmp_path / "location.md"
    md.write_text("测试：120.1,30.2", "utf-8")
    merged = merge_presets({}, md)
    key = next(iter(merged["gps"]["locations"]))
    cfg = tmp_path / "config.toml"
    cfg.write_text('[gps]\nlocation="' + key + '"\n', "utf-8")
    assert load_config(cfg).gps.name == "测试"


def test_removed_preset_clears_selection_instead_of_choosing_another(tmp_path):
    path = tmp_path / "location.md"
    path.write_text("旧：120,30", "utf-8")
    data = merge_presets({}, path)
    data["gps"]["location"] = next(iter(data["gps"]["locations"]))
    path.write_text("新：121,31", "utf-8")
    assert merge_presets(data, path)["gps"]["location"] == ""
