"""Characterization tests for project_tools, run end to end against the fake REAPER."""

from datetime import datetime

from reapy import reascript_api

from reaper_mcp import project_tools

NEW_PROJECT_TAB = 41929


def test_create_project_sets_tempo_and_time_signature(reaper, call):
    reaper.add_track("old")
    result = call("create_project", tempo=90.0, time_signature="7/8", use_default_template=False)
    assert result["success"] is True
    assert (result["tempo"], result["time_signature"]) == (90.0, "7/8")
    assert reaper.commands == [NEW_PROJECT_TAB]
    assert reaper.tracks == []
    assert reaper.tempo_markers == [[0.0, 90.0, 7, 8]]
    [(old_tracks, *_)] = reaper.background_tabs
    assert [t.name for t in old_tracks] == ["old"]


def test_create_project_without_time_signature(reaper, call):
    reaper.add_track("old")
    result = call("create_project", tempo=100.0, time_signature="", name="Song",
                  use_default_template=False)
    assert result == {"success": True, "name": "Song", "from_default_template": False,
                      "tempo": 100.0, "time_signature": "4/4"}
    assert reaper.commands == [NEW_PROJECT_TAB]
    assert reaper.tracks == []
    assert reaper.bpm == 100.0


def _stamped(name: str, prefix: str) -> datetime:
    """The time in a default name such as "Project 2026-10-07 14-03-59"."""
    assert name.startswith(prefix), name
    return datetime.strptime(name.removeprefix(prefix), "%Y-%m-%d %H-%M-%S")


def test_python_defaults_match_the_advertised_ones(reaper, monkeypatch):
    """Called from Python with no arguments, the tools behave as an MCP call
    that leaves the arguments out."""
    monkeypatch.setenv("HOME", str(reaper.tmp))
    monkeypatch.chdir(reaper.tmp)
    before = datetime.now().replace(microsecond=0)
    result = project_tools.create_project()
    assert (result["tempo"], result["time_signature"], result["name"]) == (120.0, "4/4", "")
    assert result["from_default_template"] is True
    assert reaper.tempo_markers == []  # the template's (here REAPER's) own tempo stays
    saved = project_tools.save_project()
    folder = reaper.tmp / "Documents" / "REAPER Projects"
    assert saved["success"] is True
    name = saved["project_path"][len(f"{folder}/"):-4]
    assert before <= _stamped(name, "Project ") <= datetime.now()


def test_create_project_malformed_time_signature(reaper, call):
    result = call("create_project", tempo=80.0, time_signature="four")
    assert result == {"success": False, "error": "time_signature must look like 4/4 or 6/8, "
                                                 "not 'four'; no project was created"}
    assert reaper.commands == []
    assert reaper.bpm == 120.0


def test_save_project_to_a_path(reaper, call, tmp_path):
    target = tmp_path / "out" / "song.rpp"
    assert call("save_project", project_path=str(target)) == {
        "success": True, "project_path": str(target),
    }
    assert reaper.saved_paths == [str(target)]
    assert target.is_file()


def test_save_project_makes_the_file_the_open_project(reaper, call, tmp_path):
    reaper.dirty = True
    target = tmp_path / "song.rpp"
    call("save_project", project_path=str(target))
    assert (reaper.project_file, reaper.project_name, reaper.dirty) == (str(target), "song.rpp", False)
    assert call("get_context")["project_file"] == str(target)


def test_save_project_again_updates_the_same_file(reaper, call, tmp_path, monkeypatch):
    """After a save to a path, saving without one updates that file."""
    monkeypatch.setenv("HOME", str(reaper.tmp))
    target = tmp_path / "songs" / "song.rpp"
    call("save_project", project_path=str(target))
    assert call("save_project") == {"success": True, "project_path": str(target)}
    assert reaper.saved_paths == [str(target), str(target)]


def test_save_project_reports_a_save_that_did_not_become_the_project(reaper, call, monkeypatch):
    fake_save = reaper.Main_SaveProjectEx
    monkeypatch.setattr(reascript_api, "Main_SaveProjectEx",
                        lambda project, path, options: fake_save(project, path, 0))
    target = reaper.tmp / "song.rpp"
    result = call("save_project", project_path=str(target))
    assert result == {"success": False,
                      "error": f"REAPER wrote {target}, but the open project is still unsaved"}


def test_save_project_default_path(reaper, call, monkeypatch):
    monkeypatch.setenv("HOME", str(reaper.tmp))
    result = call("save_project")
    expected = reaper.tmp / "Documents" / "REAPER Projects" / "Test Project.rpp"
    assert result == {"success": True, "project_path": str(expected)}
    assert expected.is_file()


def test_save_project_names_an_unnamed_project_by_time(reaper, call, monkeypatch):
    monkeypatch.setenv("HOME", str(reaper.tmp))
    reaper.project_name = ""
    before = datetime.now().replace(microsecond=0)
    result = call("save_project")
    path = reaper.tmp / "Documents" / "REAPER Projects"
    saved = result["project_path"]
    assert result == {"success": True, "project_path": saved}
    assert saved.startswith(f"{path}/") and saved.endswith(".rpp")
    assert before <= _stamped(saved[len(f"{path}/"):-4], "Project ") <= datetime.now()
    assert reaper.saved_paths == [saved]


def test_save_project_default_folder_already_exists(reaper, call, monkeypatch):
    monkeypatch.setenv("HOME", str(reaper.tmp))
    folder = reaper.tmp / "Documents" / "REAPER Projects"
    folder.mkdir(parents=True)
    result = call("save_project")
    assert result == {"success": True, "project_path": str(folder / "Test Project.rpp")}


def test_save_project_reports_a_file_reaper_did_not_write(reaper, call, monkeypatch):
    monkeypatch.setattr(reascript_api, "Main_SaveProjectEx", lambda *_: None)
    target = reaper.tmp / "song.rpp"
    result = call("save_project", project_path=str(target))
    assert result == {"success": False, "error": f"REAPER did not write {target}"}


def test_load_project_missing_file(reaper, call, tmp_path):
    path = str(tmp_path / "missing.rpp")
    result = call("load_project", project_path=path)
    assert result == {"success": False, "error": f"File not found: {path}"}
    assert reaper.opened == []


def test_load_project(reaper, call, tmp_path):
    path = tmp_path / "song.rpp"
    path.write_text("<REAPER_PROJECT\n>\n")
    reaper.bpm = 95.0
    reaper.time_signature = (3, 4)
    result = call("load_project", project_path=str(path))
    assert result == {
        "success": True,
        "name": "Test Project",
        "tempo": 95.0,
        "time_signature": "3/4",
        "project_path": str(path),
    }
    assert reaper.opened == [str(path)]


def test_get_project_info_empty(reaper, call):
    reaper.project_path = "/songs"
    reaper.add_track("a")
    result = call("get_project_info")
    assert result == {
        "success": True,
        "name": "Test Project",
        "project_file": "",
        "path": "/songs",
        "tempo": 120.0,
        "time_signature": "4/4",
        "length": 4.0,
        "track_count": 1,
        "markers": [],
        "regions": [],
    }


def test_get_project_info_markers_and_regions(reaper, call):
    reaper.markers = [(1.0, "verse"), (6.0, "")]
    reaper.regions = [(0.5, 4.0, "intro")]
    result = call("get_project_info")
    assert result["markers"] == [
        {"number": 1, "name": "verse", "position": 1.0},
        {"number": 2, "name": "", "position": 6.0},
    ]
    assert result["regions"] == [
        {"number": 1, "name": "intro", "start": 0.5, "end": 4.0}]


def test_get_project_info_marker_numbers_are_not_list_positions(reaper, call):
    reaper.markers = [(1.0, "verse"), (6.0, "chorus")]
    reaper.DeleteProjectMarker(0, 1, False)
    reaper.AddRegionOrMarker(0, False, 9.0, 9.0, "outro", -1, 0)
    markers = call("get_project_info")["markers"]
    assert [(m["number"], m["name"]) for m in markers] == [
        (2, "chorus"), (1, "outro")]


def test_set_tempo(reaper, call):
    assert call("set_tempo", bpm=140.5) == {"success": True, "tempo": 140.5}
    assert reaper.bpm == 140.5


def test_set_time_signature_adds_a_marker_at_the_start(reaper, call):
    assert call("set_time_signature", numerator=6, denominator=8) == {
        "success": True, "time_signature": "6/8",
    }
    assert reaper.tempo_markers == [[0.0, 120.0, 6, 8]]


def test_set_time_signature_edits_the_marker_at_the_start(reaper, call):
    reaper.tempo_markers = [[0.0, 100.0, 4, 4], [8.0, 140.0, 4, 4]]
    reaper.bpm = 100.0
    call("set_time_signature", numerator=3, denominator=4)
    assert reaper.tempo_markers == [[0.0, 100.0, 3, 4], [8.0, 140.0, 4, 4]]


def test_set_time_signature_keeps_a_later_marker(reaper, call):
    reaper.tempo_markers = [[8.0, 140.0, 4, 4]]
    call("set_time_signature", numerator=5, denominator=4)
    assert reaper.tempo_markers == [[0.0, 120.0, 5, 4], [8.0, 140.0, 4, 4]]


def test_set_tempo_edits_the_marker_at_the_start(reaper, call):
    reaper.tempo_markers = [[0.0, 120.0, 3, 4]]
    assert call("set_tempo", bpm=90.0) == {"success": True, "tempo": 90.0}
    assert reaper.tempo_markers == [[0.0, 90.0, 3, 4]]


def test_set_time_signature_treats_a_marker_a_hair_past_zero_as_the_start(reaper, call):
    reaper.tempo_markers = [[1e-9, 100.0, 4, 4]]
    reaper.bpm = 100.0
    call("set_time_signature", numerator=3, denominator=4)
    assert reaper.tempo_markers == [[0.0, 100.0, 3, 4]]


def test_set_time_signature_leaves_a_marker_half_a_second_in(reaper, call):
    reaper.tempo_markers = [[0.5, 140.0, 4, 4]]
    call("set_time_signature", numerator=7, denominator=8)
    assert reaper.tempo_markers == [[0.0, 120.0, 7, 8], [0.5, 140.0, 4, 4]]


def test_save_project_refuses_a_non_rpp_path(reaper, call, tmp_path):
    target = tmp_path / ".bashrc"
    result = call("save_project", project_path=str(target))
    assert result["success"] is False and ".rpp" in result["error"]
    assert not target.exists()


def test_save_project_refuses_to_overwrite_by_default(reaper, call, tmp_path):
    target = tmp_path / "song.rpp"
    target.write_text("old")
    result = call("save_project", project_path=str(target))
    assert result["success"] is False and "overwrite=true" in result["error"]
    assert target.read_text() == "old"
    assert call("save_project", project_path=str(target), overwrite=True)["success"] is True


def test_load_project_refuses_a_non_rpp_file(reaper, call, tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hi")
    result = call("load_project", project_path=str(path))
    assert result["success"] is False and ".rpp" in result["error"]


def test_save_project_refuses_to_write_through_a_symlink(reaper, call, tmp_path):
    victim = tmp_path / "victim.txt"
    link = tmp_path / "song.rpp"
    link.symlink_to(victim)
    result = call("save_project", project_path=str(link), overwrite=True)
    assert result["success"] is False and "symlink" in result["error"]
    assert not victim.exists()


def test_save_project_default_path_can_be_saved_again(reaper, call, monkeypatch):
    monkeypatch.setenv("HOME", str(reaper.tmp))
    assert call("save_project")["success"] is True
    assert call("save_project")["success"] is True


def test_save_project_hands_reaper_an_absolute_path(reaper, call, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = call("save_project", project_path="rel.rpp")
    assert result == {"success": True, "project_path": str(tmp_path / "rel.rpp")}


def test_save_project_never_replaces_another_project_with_the_same_name(reaper, call, monkeypatch):
    """Two new projects named "Song": the second save gets its own file."""
    monkeypatch.setenv("HOME", str(reaper.tmp))
    folder = reaper.tmp / "Documents" / "REAPER Projects"
    call("create_project", name="Song")
    assert call("save_project")["project_path"] == str(folder / "Song.rpp")
    (folder / "Song.rpp").write_text("first song")
    call("create_project", name="Song")
    assert call("save_project")["project_path"] == str(folder / "Song (2).rpp")
    assert (folder / "Song.rpp").read_text() == "first song"
    # Saving the second project again updates its own file.
    assert call("save_project")["project_path"] == str(folder / "Song (2).rpp")


def test_save_project_goes_to_the_xdg_documents_folder(reaper, call, monkeypatch):
    monkeypatch.setenv("HOME", str(reaper.tmp))
    config = reaper.tmp / ".config"
    config.mkdir()
    (config / "user-dirs.dirs").write_text('# comment\nXDG_DOCUMENTS_DIR="$HOME/Documentos"\n')
    result = call("save_project")
    expected = reaper.tmp / "Documentos" / "REAPER Projects" / "Test Project.rpp"
    assert result == {"success": True, "project_path": str(expected)}


def test_documents_dir_ignores_a_documents_folder_set_to_home(reaper, monkeypatch):
    monkeypatch.setenv("HOME", str(reaper.tmp))
    config = reaper.tmp / ".config"
    config.mkdir()
    (config / "user-dirs.dirs").write_text('XDG_DOCUMENTS_DIR="$HOME/"\n')
    assert project_tools.documents_dir() == reaper.tmp / "Documents"


def test_save_project_reports_a_save_that_left_the_project_unsaved(reaper, call, monkeypatch):
    """Re-saving over an existing file: the file is there either way, so a
    project still needing a save is the sign REAPER could not write it."""
    target = reaper.tmp / "song.rpp"
    call("save_project", project_path=str(target))
    reaper.dirty = True
    monkeypatch.setattr(reascript_api, "Main_SaveProjectEx", lambda *_: None)
    result = call("save_project")
    assert result["success"] is False
    assert "could not save" in result["error"]


def test_save_project_accepts_a_path_reaper_spells_differently(reaper, call, tmp_path, monkeypatch):
    fake_save = reaper.Main_SaveProjectEx

    def save_and_respell(project, path, options):
        fake_save(project, path, options)
        reaper.project_file = path.replace("/song.rpp", "//song.rpp")

    monkeypatch.setattr(reascript_api, "Main_SaveProjectEx", save_and_respell)
    target = tmp_path / "song.rpp"
    assert call("save_project", project_path=str(target)) == {
        "success": True, "project_path": str(target)}


def test_load_project_refuses_while_there_are_unsaved_changes(reaper, call, tmp_path):
    path = tmp_path / "song.rpp"
    path.write_text("<REAPER_PROJECT\n>\n")
    reaper.dirty = True
    result = call("load_project", project_path=str(path))
    assert result["success"] is False
    assert "save_project" in result["error"]
    assert reaper.opened == []


def test_save_project_into_a_folder_reaper_cannot_write_is_refused_first(reaper, call, tmp_path):
    """REAPER answers such a save with a modal error dialog that blocks every
    call until someone clicks OK (seen on 7.69), so it is never asked to."""
    folder = tmp_path / "read-only"
    folder.mkdir()
    folder.chmod(0o555)
    try:
        result = call("save_project", project_path=str(folder / "song.rpp"))
    finally:
        folder.chmod(0o755)
    assert result["success"] is False
    assert "cannot write in" in result["error"] and "nothing was saved" in result["error"]
    assert reaper.saved_paths == []
