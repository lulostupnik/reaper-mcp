"""project_template_tools against the fake REAPER: project templates and
saving tracks as track templates."""

import pytest

from tests.fake_reaper.objects import ItemState
from tests.fake_reaper.recording import template_track_names

NEW_PROJECT_TAB = 41929


@pytest.fixture
def templates(reaper):
    """REAPER's ProjectTemplates folder with two templates (one in a subfolder)."""
    folder = reaper.tmp / "resources" / "ProjectTemplates"
    (folder / "Band").mkdir(parents=True)
    (folder / "Grabacion simple.RPP").write_text(
        '<REAPER_PROJECT\n<TRACK\n  NAME "Voz"\n>\n<TRACK\n  NAME "Guitarra"\n>\n>\n')
    (folder / "Band" / "Rock.rpp").write_text('<REAPER_PROJECT\n<TRACK\n  NAME "Drums"\n>\n>\n')
    (folder / "notes.txt").write_text("not a template")
    return folder


# list_project_templates


def test_list_project_templates(reaper, call, templates):
    assert call("list_project_templates") == {
        "success": True,
        "template_directory": str(templates),
        "count": 2,
        "templates": [{"name": "Band/Rock", "file": str(templates / "Band" / "Rock.rpp")},
                      {"name": "Grabacion simple",
                       "file": str(templates / "Grabacion simple.RPP")}],
    }


def test_list_project_templates_without_the_folder(reaper, call):
    assert call("list_project_templates")["templates"] == []


# new_project_from_template


def test_new_project_from_template_opens_a_new_tab(reaper, call, templates):
    reaper.add_track("old song")
    reaper.dirty = True  # unsaved changes stay in their own tab
    result = call("new_project_from_template", template="grabacion simple")
    file = str(templates / "Grabacion simple.RPP")
    assert result == {
        "success": True, "template": file, "track_count": 2, "tracks": ["Voz", "Guitarra"],
        "tempo": 120.0, "time_signature": "4/4", "project_file": "",
        "message": "New project from the template, in a new project tab; the previous "
                   "project is still open in its own tab. Save the new one with save_project "
                   "before recording.",
    }
    assert reaper.commands == [NEW_PROJECT_TAB]
    assert reaper.opened == ["template:" + file]
    assert [t.name for t in reaper.background_tabs[0][0]] == ["old song"]


@pytest.mark.parametrize("name", ["Band/Rock", "rock", "Band/Rock.rpp"])
def test_new_project_from_template_by_path_or_name(reaper, call, templates, name):
    assert call("new_project_from_template", template=name)["tracks"] == ["Drums"]


def test_new_project_from_template_by_file(reaper, call, tmp_path):
    file = tmp_path / "Mine.RPP"
    file.write_text('<REAPER_PROJECT\n<TRACK\n  NAME "Bass"\n>\n>\n')
    assert call("new_project_from_template", template=str(file))["tracks"] == ["Bass"]


def test_new_project_from_template_unknown(reaper, call, templates):
    result = call("new_project_from_template", template="Grabacion")
    assert result["success"] is False
    assert "Did you mean: Grabacion simple?" in result["error"]
    assert "Available: Band/Rock, Grabacion simple." in result["error"]
    assert (reaper.commands, reaper.opened) == ([], [])


# save_project_template


def test_save_project_template(reaper, call, tmp_path):
    reaper.add_track("Voz")
    reaper.project_file = str(tmp_path / "song.rpp")
    result = call("save_project_template", name="Mi plantilla")
    file = reaper.tmp / "resources" / "ProjectTemplates" / "Mi plantilla.RPP"
    assert result == {"success": True, "name": "Mi plantilla", "file": str(file), "items": 0}
    assert template_track_names(file) == ["Voz"]
    assert reaper.project_file == str(tmp_path / "song.rpp")  # still the project's own file
    assert reaper.undo_steps == []


def test_save_project_template_then_start_from_it(reaper, call):
    reaper.add_track("Voz")
    call("save_project_template", name="Vocal")
    assert call("new_project_from_template", template="Vocal")["tracks"] == ["Voz"]


def test_save_project_template_does_not_overwrite_unless_asked(reaper, call):
    call("save_project_template", name="Vocal")
    reaper.add_track("Voz")
    result = call("save_project_template", name="Vocal")
    assert result["success"] is False and "overwrite=true" in result["error"]
    assert call("save_project_template", name="Vocal", overwrite=True)["success"] is True
    file = reaper.tmp / "resources" / "ProjectTemplates" / "Vocal.RPP"
    assert template_track_names(file) == ["Voz"]


def test_save_project_template_reuses_a_name_in_another_case(reaper, call):
    folder = reaper.tmp / "resources" / "ProjectTemplates"
    folder.mkdir(parents=True)
    (folder / "Song.rpp").write_text("<REAPER_PROJECT\n>\n")
    result = call("save_project_template", name="song")
    assert result["success"] is False and "overwrite=true" in result["error"]
    reaper.add_track("Voz")
    assert call("save_project_template", name="Song", overwrite=True)["file"] == str(
        folder / "Song.rpp")
    assert sorted(p.name for p in folder.iterdir()) == ["Song.rpp"]
    assert call("new_project_from_template", template="Song")["tracks"] == ["Voz"]


@pytest.mark.parametrize("name", ["", "../escape", "a/b", ".hidden"])
def test_save_project_template_bad_name(reaper, call, name):
    result = call("save_project_template", name=name)
    assert result["success"] is False and "plain file name" in result["error"]
    assert reaper.saved_paths == []


def test_save_project_template_with_items_needs_include_items(reaper, call):
    track = reaper.add_track("Voz")
    track.items.append(ItemState(reaper.new_pointer("MediaItem"), 0.0, 1.0, midi=False))
    result = call("save_project_template", name="With take")
    assert result["success"] is False and "1 item(s)" in result["error"]
    assert reaper.saved_paths == []
    result = call("save_project_template", name="With take", include_items=True)
    assert (result["success"], result["items"]) == (True, 1)


# save_track_template


def test_save_track_template_of_given_tracks_restores_the_selection(reaper, call):
    names = ("Voz", "Guitarra", "Bajo")
    tracks = [reaper.add_track(name) for name in names]
    tracks[2].selected = True
    result = call("save_track_template", name="Voz y guitarra", track_indices=[0, 1])
    file = reaper.tmp / "resources" / "TrackTemplates" / "Voz y guitarra.RTrackTemplate"
    assert result == {"success": True, "name": "Voz y guitarra", "file": str(file),
                      "tracks": ["Voz", "Guitarra"]}
    assert template_track_names(file) == ["Voz", "Guitarra"]
    assert [t.selected for t in tracks] == [False, False, True]
    assert "<ITEM" not in file.read_text()


def test_save_track_template_of_the_selected_tracks(reaper, call):
    reaper.add_track("Voz")
    reaper.add_track("Guitarra").selected = True
    result = call("save_track_template", name="Gtr", include_items=True)
    assert result["tracks"] == ["Guitarra"]
    assert "<ITEM" in open(result["file"]).read()


def test_save_track_template_is_listed_for_insertion(reaper, call):
    reaper.add_track("Voz").selected = True
    call("save_track_template", name="Vocal chain")
    [template] = call("list_track_templates")["templates"]
    assert (template["name"], template["track_names"]) == ("Vocal chain", ["Voz"])


def test_save_track_template_needs_tracks(reaper, call):
    reaper.add_track("Voz")
    result = call("save_track_template", name="x")
    assert result == {"success": False, "error": "No track is selected: pass track_indices"}
    for indices in ([], [3]):
        result = call("save_track_template", name="x", track_indices=indices)
        assert result["success"] is False and "track_indices must name" in result["error"]
    assert reaper.saved_paths == []


def test_save_track_template_does_not_overwrite_unless_asked(reaper, call):
    reaper.add_track("Voz").selected = True
    call("save_track_template", name="Voz")
    result = call("save_track_template", name="Voz")
    assert result["success"] is False and "overwrite=true" in result["error"]
    assert call("save_track_template", name="Voz", overwrite=True)["success"] is True


@pytest.mark.parametrize(("tool", "folder"), [
    ("save_project_template", "ProjectTemplates"),
    ("save_track_template", "TrackTemplates"),
])
def test_saving_a_template_into_a_folder_reaper_cannot_write_is_refused_first(
        reaper, call, tool, folder):
    reaper.add_track("Vox").selected = True
    templates = reaper.tmp / "resources" / folder
    templates.mkdir(parents=True, exist_ok=True)
    templates.chmod(0o555)
    try:
        result = call(tool, name="Vocal")
    finally:
        templates.chmod(0o755)
    assert result["success"] is False and "cannot write in" in result["error"]
    assert reaper.saved_paths == []
