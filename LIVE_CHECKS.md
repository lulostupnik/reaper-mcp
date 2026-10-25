# Live checks

Things the fake REAPER assumes that have not been confirmed against a real
REAPER yet. Confirm each one on a scratch project, then tick it.

## Foundations: undo, context, snapshot, GUIDs, existing bugs

- [ ] src/reaper_mcp/undo.py:undo_block: nested Undo_BeginBlock2/Undo_EndBlock2 record one step under the outermost description (create_project and any tool that opens its own block inside the server's).
- [ ] src/reaper_mcp/undo.py:undo_block: a tool that fails inside its block (e.g. remove_fx with a bad fx_index) leaves an "MCP: ... (failed)" undo point or none; check whether REAPER skips empty blocks, and whether an empty block clears the redo list.
- [ ] src/reaper_mcp/undo.py:undo_block: Undo_EndBlock2 with extraflags -1 makes the step undo track, item, FX, envelope and marker changes alike (try set_track_volume, add_fx, create_midi_item, add_volume_automation, create_bus, then undo each).
- [ ] src/reaper_mcp/audio_tools.py:start_recording: starting Transport: Record (1013) inside an undo block works, the arm is one "MCP: Start recording" step, and stopping still creates REAPER's own undo point for the recorded media.
- [ ] src/reaper_mcp/mastering_tools.py:normalize_project: rendering (action 42230) inside an undo block works and does not leave the block open.
- [ ] src/reaper_mcp/reaper.py:string_or_none: with an empty undo history, Undo_CanUndo2/Undo_CanRedo2 raise through reapy (reaper_python.py decodes the NULL with r.decode()) as DistError, so get_undo_state reports null instead of failing.
- [ ] src/reaper_mcp/undo_tools.py:undo: after a recording, Undo_CanUndo2 names REAPER's own step (e.g. "Recorded media"), not one starting with "MCP: ", so undo refuses it without include_manual_steps=true.
- [ ] src/reaper_mcp/undo_tools.py:undo: Undo_DoUndo2/Undo_DoRedo2 return nonzero on success through reapy.
- [ ] src/reaper_mcp/project_tools.py:create_project: action 40859 is "File: New project tab" (uses the default project template) and never opens a dialog, with and without a default template configured; without one it gives an empty project with REAPER's default tempo (it is now the default, use_default_template=true).
- [ ] src/reaper_mcp/project_tools.py:create_project: GetSetProjectInfo_String "PROJECT_TITLE" set on a new tab shows up in Project Settings > Notes and survives the first save.
- [ ] src/reaper_mcp/project_tools.py:save_project: Main_SaveProjectEx option 8 makes the file the open project (tab title, GetProjectName, recording path), and EnumProjects(-1, "", 4096)[2] then names the same file as the path passed (compared with os.path.samefile).
- [ ] src/reaper_mcp/project_tools.py:save_project: IsProjectDirty(0) is 0 right after a successful Main_SaveProjectEx with option 8, and stays 1 when the write fails (read-only folder), so a failed re-save over an existing file is reported.
- [ ] src/reaper_mcp/project_tools.py:load_project: Main_openProject("noprompt:" + path) opens the .rpp without any dialog, and IsProjectDirty(0) is 1 after an unsaved edit (load_project refuses then).
- [ ] src/reaper_mcp/project_tools.py:documents_dir: on the user's Spanish Ubuntu, ~/.config/user-dirs.dirs has XDG_DOCUMENTS_DIR="$HOME/Documentos" and save_project's default folder is ~/Documentos/REAPER Projects.
- [ ] src/reaper_mcp/project_tools.py:project_file: EnumProjects(-1, ...) returns "" for a never-saved project.
- [ ] tests/fake_reaper/api.py:Main_SaveProjectEx: after a save with option 8, GetProjectPathEx (reapy's Project.path) points at the project's folder or its media subfolder; the fake leaves it unchanged.
- [ ] src/reaper_mcp/mixing_tools.py:track_sends: GetTrackSendInfo_Value(track, 0, i, "P_DESTTRACK") returns the destination pointer's address as a float through reapy, which "(MediaTrack*)0x%016X" turns back into the track id (reapy's own Send.dest_track does this).
- [ ] src/reaper_mcp/mixing_tools.py:create_bus: B_MAINSEND=0 on a routed track removes its direct master send and the bus is heard once (render a source alone through the bus at 0 dB: the level should match the source, not +6 dB).
- [ ] src/reaper_mcp/track_tools.py:create_track: I_RECINPUT 6112 (4096 + 63<<5) shows as "All MIDI inputs > All channels" in the track's input menu; instrument tracks show monitoring on, and with default preferences they only sound while playing once record-armed (as the docstring says).
- [ ] src/reaper_mcp/track_tools.py:describe_input: I_RECINPUT audio channel numbers 512 and up are the loopback/ReaRoute inputs.
- [ ] src/reaper_mcp/guids.py:pointer_guid: GetSetMediaTrackInfo_String(track, "GUID", "", False)[3] is the track GUID as "{xxxxxxxx-...}" through reapy, the same after undoing a track deletion (GetTrackGUID gives "(GUID*)0x..." and must not be used).
- [ ] src/reaper_mcp/mixing_tools.py:_close_open_folders: with a folder left open at the end (folder track, then a child with I_FOLDERDEPTH 0), create_bus sets the child to -1 and the bus lands at top level, not inside the folder.
- [ ] src/reaper_mcp/render_tools.py:render_to_temp_file: the analysis tools leave File > Render settings (folder, file name, format, sample rate, channels, bounds, add-to-project) as they were, and RENDER_FORMAT read back as a string restores the same format.
- [ ] tests/fake_reaper/api.py:InsertMedia: REAPER keeps a track's items sorted by position, so GetTrackMediaItem index 0 is the earliest item after an import before an existing item.
- [ ] src/reaper_mcp/audio_tools.py:import_audio_file: GetSetMediaItemInfo_String(item, "GUID", "", False)[3] is the item GUID string through reapy.
- [ ] src/reaper_mcp/context_tools.py:_range: GetSet_LoopTimeRange2(0, False, loop, 0.0, 0.0, False) returns (proj, isSet, isLoop, start, end, allowautoseek) through reapy, start/end at indices 3 and 4.
- [ ] src/reaper_mcp/context_tools.py:_last_touched_fx: GetLastTouchedFX(0, 0, 0) returns (retval, tracknumber, fxnumber, paramnumber) through reapy; a master FX reports track number 0; input FX carry 0x1000000.
- [ ] src/reaper_mcp/context_tools.py:get_context: GetPlayState reports 2 (paused) and 6 (recording paused) as expected; GetSetRepeat(-1) only queries.
- [ ] src/reaper_mcp/context_tools.py:_selected_item: GetMediaItemInfo_Value(item, "IP_ITEMNUMBER") is the 0-based index in the track's item list.
