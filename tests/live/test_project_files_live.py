"""Project files, tabs and templates against a running REAPER.

Every test that opens a project tab closes it again (without REAPER's save
prompt) and leaves the tab it started in current. Templates written into the
user's REAPER folders are removed afterwards.
"""

import os
from pathlib import Path

import pytest

from reaper_mcp.reaper import string_or_none
from tests.live.rpr import RPR
from tests.live.test_project_tools_live import close_current_tab_without_prompt, open_projects

pytestmark = pytest.mark.live

NEW_TAB_IGNORING_TEMPLATE = 41929
DEFAULT_TEMPLATE_KEY = "newprojtmpl"
TEST_TEMPLATE = "reaper-mcp live test"


def current_tab() -> str:
    return RPR.EnumProjects(-1, "", 512)[0]


def current_file() -> str:
    return RPR.EnumProjects(-1, "", 4096)[2]


@pytest.fixture
def new_tab(live_project, tmp_path):
    """Runs the test's new tabs in isolation: closes every tab it opened and
    makes the starting tab current again."""
    start, tabs = current_tab(), open_projects()
    yield
    while len(open_projects()) > len(tabs) and current_tab() != start:
        close_current_tab_without_prompt(tmp_path)
    RPR.SelectProjectInstance(start)
    assert open_projects() == tabs


def default_template() -> Path | None:
    ini = Path(RPR.GetResourcePath()) / "reaper.ini"
    for line in ini.read_text(errors="replace").splitlines():
        if line.startswith(DEFAULT_TEMPLATE_KEY + "="):
            value = line.split("=", 1)[1].strip()
            return Path(value) if value else None
    return None


def template_track_names(path: Path) -> list[str]:
    lines = path.read_text(errors="replace").splitlines()
    return [line.strip()[len("NAME "):].strip('"') for line in lines
            if line.startswith("    NAME ")]


# save_project / load_project


def test_save_project_makes_the_file_the_open_project_and_clears_dirty(live_project, call,
                                                                        tmp_path):
    live_project.add_track(0, "a")
    RPR.MarkProjectDirty(0)
    target = tmp_path / "song.rpp"
    assert call("save_project", project_path=str(target))["success"] is True
    assert os.path.samefile(current_file(), target)
    assert RPR.GetProjectName(0, "", 512)[1] == "song.rpp"
    assert RPR.IsProjectDirty(0) == 0
    record_setting = RPR.GetSetProjectInfo_String(0, "RECORD_PATH", "", False)[3]
    assert RPR.GetProjectPathEx(0, "", 4096)[1] == str(tmp_path / record_setting).rstrip("/")


def test_saving_into_a_folder_reaper_cannot_write_is_refused_without_a_dialog(
        live_project, call, tmp_path):
    """REAPER would open a modal "Error creating project file" dialog."""
    target = tmp_path / "song.rpp"
    call("save_project", project_path=str(target))
    live_project.add_track(0, "unsaved")
    RPR.MarkProjectDirty(0)
    tmp_path.chmod(0o555)
    try:
        result = call("save_project")
    finally:
        tmp_path.chmod(0o755)
    assert result["success"] is False and "cannot write in" in result["error"]
    assert RPR.IsProjectDirty(0) == 1


def test_load_project_opens_without_a_dialog_and_refuses_unsaved_changes(live_project, call,
                                                                          tmp_path):
    live_project.add_track(0, "saved")
    first = tmp_path / "first.rpp"
    call("save_project", project_path=str(first))
    live_project.tracks[0].name = "changed"
    RPR.MarkProjectDirty(0)
    refused = call("load_project", project_path=str(first))
    assert refused["success"] is False and "unsaved changes" in refused["error"]
    assert live_project.tracks[0].name == "changed"

    call("save_project")
    second = tmp_path / "second.rpp"
    RPR.Main_SaveProjectEx(0, str(second), 0)  # a copy, with the track "changed"
    live_project.tracks[0].name = "saved again"
    call("save_project")
    loaded = call("load_project", project_path=str(second))
    assert loaded["success"] is True, loaded
    assert os.path.samefile(current_file(), second)
    assert [t.name for t in live_project.tracks] == ["changed"]
    assert RPR.IsProjectDirty(0) == 0


# New tabs


def test_a_never_saved_project_has_no_file_and_an_empty_undo_history(new_tab, call):
    RPR.Main_OnCommand(NEW_TAB_IGNORING_TEMPLATE, 0)
    assert current_file() == ""
    assert call("get_context")["project_file"] == ""
    assert call("get_undo_state") == {"success": True, "next_undo": None, "next_redo": None}
    assert string_or_none(RPR.Undo_CanRedo2, 0) is None


def test_create_project_starts_from_the_default_template(new_tab, call):
    template = default_template()
    if template is None:
        pytest.skip("no default project template is configured in REAPER's preferences")
    result = call("create_project", name="Sketch")
    assert result["success"] is True, result
    assert result["from_default_template"] is True
    assert [t["name"] for t in call("list_tracks")["tracks"]] == template_track_names(template)
    assert current_file() == ""
    # Setting only PROJECT_TITLE records no undo step: REAPER does not keep it
    # in its undo history, so the new tab's history stays empty.
    assert call("get_undo_state")["next_undo"] is None


def test_the_project_title_is_saved_with_the_project(new_tab, call, tmp_path):
    call("create_project", name="Sketch", use_default_template=False)
    assert RPR.GetSetProjectInfo_String(0, "PROJECT_TITLE", "", False)[3] == "Sketch"
    target = tmp_path / "sketch.rpp"
    assert call("save_project", project_path=str(target))["success"] is True
    assert "  TITLE Sketch" in target.read_text(errors="replace").splitlines()
    assert RPR.GetSetProjectInfo_String(0, "PROJECT_TITLE", "", False)[3] == "Sketch"


def test_save_project_names_a_new_file_after_the_title(new_tab, call, tmp_path, monkeypatch):
    from reaper_mcp import project_tools

    monkeypatch.setattr(project_tools, "documents_dir", lambda: tmp_path)
    call("create_project", name="Sketch", use_default_template=False)
    result = call("save_project")
    assert result == {"success": True,
                      "project_path": str(tmp_path / "REAPER Projects" / "Sketch.rpp")}


# Templates


@pytest.fixture
def template_files():
    """The test templates' paths in REAPER's folders, removed afterwards."""
    resources = Path(RPR.GetResourcePath())
    paths = (resources / "ProjectTemplates" / f"{TEST_TEMPLATE}.RPP",
             resources / "TrackTemplates" / f"{TEST_TEMPLATE}.RTrackTemplate")
    assert not any(p.exists() for p in paths), "a leftover test template exists"
    yield paths
    for path in paths:
        path.unlink(missing_ok=True)


def test_save_project_template_writes_a_copy_and_new_project_from_template_opens_it(
        new_tab, call, live_project, tmp_path, template_files):
    project_template, _ = template_files
    live_project.add_track(0, "Vox")
    live_project.add_track(1, "Guitar")
    call("set_tempo", bpm=93.0)
    call("save_project", project_path=str(tmp_path / "song.rpp"))
    live_project.tracks[0].name = "Lead Vox"
    RPR.MarkProjectDirty(0)

    saved = call("save_project_template", name=TEST_TEMPLATE)
    assert saved["success"] is True, saved
    assert Path(saved["file"]) == project_template
    assert os.path.samefile(current_file(), tmp_path / "song.rpp")
    assert RPR.IsProjectDirty(0) == 1  # a copy: the project's own changes are still unsaved
    assert TEST_TEMPLATE in [t["name"] for t in call("list_project_templates")["templates"]]
    again = call("save_project_template", name=TEST_TEMPLATE)
    assert again["success"] is False and "overwrite" in again["error"]
    assert call("save_project_template", name=TEST_TEMPLATE, overwrite=True)["success"] is True

    tabs = len(open_projects())
    opened = call("new_project_from_template", template=TEST_TEMPLATE)
    assert opened["success"] is True, opened
    assert len(open_projects()) == tabs + 1
    assert opened["tracks"] == ["Lead Vox", "Guitar"]
    assert opened["tempo"] == pytest.approx(93.0)
    assert opened["project_file"] == "" == current_file()


def test_save_track_template_round_trip_keeps_the_selection(live_project, call, template_files):
    _, track_template = template_files
    for i, name in enumerate(("Vox", "Guitar", "Bass")):
        live_project.add_track(i, name)
    RPR.SetOnlyTrackSelected(live_project.tracks[2].id)
    live_project.tracks[0].add_fx("ReaEQ")
    saved = call("save_track_template", name=TEST_TEMPLATE, track_indices=[0, 1])
    assert saved["success"] is True, saved
    assert (Path(saved["file"]), saved["tracks"]) == (track_template, ["Vox", "Guitar"])
    assert [t.is_selected for t in live_project.tracks] == [False, False, True]

    inserted = call("insert_track_template", template=TEST_TEMPLATE)
    assert inserted["success"] is True, inserted
    assert [t.name for t in live_project.tracks] == ["Vox", "Guitar", "Bass", "Vox", "Guitar"]
    assert [fx.name for fx in live_project.tracks[3].fxs] == ["VST: ReaEQ (Cockos)"]
