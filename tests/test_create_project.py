"""create_project's default template, title and undo step, against the fake REAPER."""

NEW_PROJECT_TAB_FROM_TEMPLATE = 40859


def test_create_project_without_a_name_has_no_title(reaper, call):
    result = call("create_project")
    assert (result["success"], result["name"], result["tempo"]) == (True, "", 120.0)
    assert "PROJECT_TITLE" not in reaper.project_info


def test_create_project_name_becomes_the_title_and_the_file_name(reaper, call, monkeypatch):
    monkeypatch.setenv("HOME", str(reaper.tmp))
    reaper.project_name = "Old.rpp"
    assert call("create_project", name="Mi cancion")["name"] == "Mi cancion"
    assert reaper.project_info["PROJECT_TITLE"] == "Mi cancion"
    expected = reaper.tmp / "Documents" / "REAPER Projects" / "Mi cancion.rpp"
    assert call("save_project") == {"success": True, "project_path": str(expected)}


def test_create_project_from_the_default_template(reaper, call):
    reaper.default_template_tracks = ["Voz", "Guitarra"]
    reaper.tempo_markers = [[0.0, 120.0, 4, 4]]
    result = call("create_project", use_default_template=True)
    assert result["success"] is True
    assert result["from_default_template"] is True
    assert reaper.commands == [NEW_PROJECT_TAB_FROM_TEMPLATE]
    assert [t.name for t in reaper.tracks] == ["Voz", "Guitarra"]
    # Nothing given: the template's own tempo and time signature stay.
    assert reaper.tempo_markers == []


def test_create_project_from_the_template_with_a_tempo(reaper, call):
    result = call("create_project", use_default_template=True, tempo=90.0, time_signature="3/4")
    assert (result["tempo"], result["time_signature"]) == (90.0, "3/4")


def test_create_project_setup_is_one_undo_step_in_the_new_project(reaper, call):
    call("set_tempo", bpm=100.0)  # an undo step in the old project
    call("create_project", tempo=90.0)
    assert [name for name, _ in reaper.undo_steps] == ["MCP: Set up new project"]
    *_, old_history = reaper.background_tabs[0]
    assert [name for name, _ in old_history] == ["MCP: Set tempo (bpm=100)"]


def test_create_project_uses_the_default_template_by_default(reaper, call):
    reaper.default_template_tracks = ["Scarlett"]
    result = call("create_project")
    assert (result["success"], result["from_default_template"]) == (True, True)
    assert reaper.commands == [NEW_PROJECT_TAB_FROM_TEMPLATE]
    assert [t.name for t in reaper.tracks] == ["Scarlett"]


def test_create_project_checks_its_arguments_before_opening_a_tab(reaper, call):
    reaper.add_track("old")
    for arguments in ({"time_signature": "3-4"}, {"time_signature": "0/4"}, {"tempo": 0.0},
                      {"tempo": -90.0}):
        result = call("create_project", **arguments)
        assert result["success"] is False, arguments
        assert "no project was created" in result["error"]
    assert reaper.commands == []
    assert [t.name for t in reaper.tracks] == ["old"]
