# REAPER MCP Server

A Model Context Protocol (MCP) server that enables AI agents to control REAPER DAW — 126 tools covering project management, recording, tracks and routing, templates, MIDI, items and takes, FX, mixing, automation, markers, mastering, rendering, audio analysis, per-track measurement and A/B mix snapshots — plus beginner guides served as MCP prompts. Every change to the mix or the project's contents is one undo step (the exceptions, such as transport, saving, rendering and saving or deleting mix snapshots, are listed under [Context and Undo](#context-and-undo)).

## Requirements

- [REAPER](https://www.reaper.fm/) 7 or later, installed and running
- Python 3.10+
- The reaper-mcp bridge installed in REAPER (see [Setup](#setting-up-reaper))

## Installation

Install from source with the lockfile, so every dependency is hash-checked:

```bash
git clone https://github.com/lulostupnik/reaper-mcp.git
cd reaper-mcp
uv venv --python 3.12 .venv   # on Linux, use the Python whose libpython REAPER will load
uv sync --locked --no-dev
```

## Setting Up REAPER

The server drives REAPER through [python-reapy](https://github.com/RomeoDespres/reapy).
reapy's own setup (`reapy.config.enable_dist_api()`) is **not** used here: it
starts a server on `0.0.0.0:2306` with no authentication that runs any Python
callable a client names, and adds a password-less web interface on port 2307,
so anyone on the same network could run commands on your computer.

Instead, this server ships a small bridge that speaks the same protocol on
`127.0.0.1:2306` only, with no web interface. Install it once, **with REAPER
closed** (REAPER rewrites its settings when it quits):

```bash
.venv/bin/reaper-mcp-setup
```

This writes `Scripts/reaper_mcp_bridge.py` in REAPER's resource folder,
registers it as an action, and runs it from `Scripts/__startup.lua` every time
REAPER starts (an existing `__startup.lua` is kept; the bridge is added as a
marked block). On Linux it also points REAPER's ReaScript Python at the venv's
`libpython` if Python is not configured yet. It also creates the bridge's
secret token (see [Safety](#safety)) if there is no usable one yet; running
setup again keeps it. Then start REAPER.

To remove it: `.venv/bin/reaper-mcp-setup --uninstall`.

If you previously ran reapy's `enable_dist_api()` or `configure_reaper()`,
`reaper-mcp-setup` removes what they left behind (the password-less web
interface on port 2307, reapy's server action and its saved action ID): with
those in place, `import reapy` could start reapy's own open server. It warns
about any other password-less web interface it finds and leaves it alone.

### Safety

- The bridge only accepts connections from the same computer, and only from
  programs that know its secret token, which `reaper-mcp-setup` creates in a
  file only you can read: `~/.config/reaper-mcp/token` on Linux,
  `~/Library/Application Support/reaper-mcp/token` on macOS,
  `%APPDATA%\reaper-mcp\token` on Windows. If REAPER or this server
  complains about the token, close REAPER and run `reaper-mcp-setup` again.
  Other programs on the computer cannot drive REAPER without the token (they
  can still keep the port busy), but anything running as your own user can
  read it.
- If a tool says `"outcome": "uncertain"`, REAPER stopped answering in the
  middle of it: usually a REAPER window or dialog is open and waiting for you
  (Preferences, a plug-in window, "Save changes?"). Close it, then check the
  project before retrying, since the change may or may not have been made.
- `save_project`, `render_project`, `render_time_selection` and
  `render_stems` refuse to replace an existing file unless called with
  `overwrite=true` (saving without a path updates the project's own file; a
  never-saved project gets a new, unused file name in the documents folder's
  `REAPER Projects`), and never write through a symlink. `load_project`
  refuses while the open project has unsaved changes.
  `save_project`/`load_project` only take `.rpp` paths, `import_audio_file`
  only audio/MIDI files, and `insert_track_template` only `.RTrackTemplate`
  files.
- Project, track and marker names are passed to the AI verbatim. Keep
  per-tool approval on for file-writing tools when you open projects from
  others.

## Usage

### With Claude Desktop

Add to your Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "reaper": {
      "command": "reaper-mcp-server",
      "args": []
    }
  }
}
```

### With Claude Code

```bash
claude mcp add reaper --scope user -- /path/to/reaper-mcp/.venv/bin/reaper-mcp-server
```

### Standalone

```bash
reaper-mcp-server          # start the server
reaper-mcp-server --debug  # with debug logging
```

## Tools (126)

### Context and Undo
`get_context` `get_project_summary` `undo` `redo` `get_undo_state`

Every tool that changes the project is one named REAPER undo step ("MCP: Set track volume (track_index=0, volume_db=-6)"), so `undo` or Ctrl+Z takes back exactly one tool call; a call that fails is named "(failed)", and a track index or GUID that names no track is refused before any step is made. `undo` and `redo` only act on these "MCP: " steps unless called with `include_manual_steps=true` (so a recorded take is not removed by accident), and never while recording. Tools are annotated as read-only or changing (MCP `readOnlyHint` / `destructiveHint` / `idempotentHint`), `list_tracks`, `get_track_info` and the item tools report track and item GUIDs, and `delete_track`, `remove_fx`, `remove_send` and `measure_track` accept a track GUID instead of an index (every other tool takes the track's index). The project summary is also the resource `reaper://project/summary`.

Not undo steps, because they are not edits Ctrl+Z can take back: the transport (play, record, stop, cursor, time selection, loop, punch mode, metronome), creating, saving and opening projects and templates, rendering, saving or deleting mix snapshots, and `undo`/`redo` themselves.

### Project Management
`create_project` `load_project` `save_project` `get_project_info` `set_tempo` `set_time_signature` `set_cursor_position` `play_project` `stop_transport`

### Recording
`get_audio_setup` `set_track_input` `arm_track` `set_monitoring` `set_track_record_mode` `check_input_level` `start_recording` `stop_recording` `set_metronome` `set_recording_format` `set_record_path` `set_time_selection` `set_loop` `set_punch_mode`

`get_audio_setup` shows the interface REAPER uses (the device itself can only be changed in REAPER's preferences; the reply says where). `check_input_level` listens to an armed track for a few seconds and says "no signal" (with things to check: cable, 48V, INST, gain), "too quiet", "good" or "clipping", without recording. `start_recording` refuses until it can work (audio running, project saved or `save_first=true`, an input and a recording record mode on each track, not already recording), disarms other tracks by default, stops playback and can start from a position, warns about monitoring, auto-punch and repeat, and confirms REAPER is recording. Count-in is only reported; it is turned on in REAPER itself (right-click the metronome button). `stop_recording` keeps the take without REAPER's save/delete dialog and lists each new take's file, length and peak level. REAPER actions are run only after REAPER confirms the action's name (the core transport and metronome actions also run under a translated name, so stopping never refuses).

### Project Templates
`list_project_templates` `new_project_from_template` `save_project_template` `save_track_template`

### Tracks
`create_track` `delete_track` `rename_track` `list_tracks` `get_track_info` `set_track_color` `create_bus` `create_send` `remove_send` `list_sends` `edit_send` `get_folder_structure` `move_track_into_folder` `move_track_out_of_folder`

`edit_send` sets a send's volume, pan, mute and mode (post-fader, pre-fader for a headphone mix, or pre-FX). `set_send_volume` is legacy. The folder tools move a track (with anything inside it) into a folder or one level out (turning a normal track into a folder needs `allow_new_folder=true`), and rewrite every track's folder depth so they always add up correctly.

### Track Templates
`list_track_templates` `insert_track_template`

Inserts saved `.RTrackTemplate` files from REAPER's TrackTemplates folder (subfolders included) with their exact FX state, routing, folder structure and items, optionally at a given track position.

### MIDI
`create_midi_item` `add_midi_note` `create_chord_progression` `create_drum_pattern`

### FX & Instruments
`list_installed_fx` `add_fx` `remove_fx` `move_fx` `copy_fx` `bypass_fx` `list_track_fx` `get_fx_parameters` `set_fx_parameter` `set_fx_parameter_by_name` `list_fx_presets` `load_fx_preset` `add_master_fx` `list_master_fx` `set_master_fx_parameter`

`list_installed_fx` finds installed plugins by name, tolerating typos (it reads REAPER's plugin caches). `add_fx` can insert at a chain position. `set_fx_parameter_by_name` takes the value as the plugin shows it ("-3 dB", "300 Hz", "20 ms", "On") and finds it through REAPER's own display, so it works for any plugin whose display is a number. It also works on the master track's FX (`master=true`); thousands must be written without a separator ("1000 Hz", not "1.000 Hz"). `list_fx_presets` has to load each preset to name it, then puts the plugin back as it was (and is one undo step), so it only runs when asked; `load_fx_preset` takes an exact name or the preset's place in that list and never scans on its own.

### Audio
`import_audio_file` `edit_audio_item` `adjust_pitch` `adjust_playback_rate`

### Items and Takes
`list_items` `edit_item` `split_item` `delete_item` `move_item` `duplicate_item` `trim_item_to_time_selection` `glue_items` `remove_silence` `trim_silence` `normalize_item` `list_takes` `set_active_take` `crop_to_active_take` `suggest_best_take`

Items are named by GUID (from `list_items`), which survives splits and moves of other items, or by index on a track (indices shift after every split, delete or move, so tools that used one remind you of the GUID). `list_items` is compact by default; `detail=true` adds fades, volumes and source files. `edit_item` sets exact values: `start`/`end` trim the edges without moving the audio, a fade of 0 removes it (automatic crossfades included); `move_item` moves whole items. `edit_audio_item` is legacy (relative trims; a fade of 0 there leaves the fade alone). `remove_silence` and `trim_silence` analyze the active take's own audio file (start offset, playrate and channel mode included; reversed takes and takes with stretch markers are refused, take FX are warned about, other takes are cut at the same places) and never open REAPER's Dynamic split dialog; `dry_run=true` previews the cuts, the seconds removed and the pieces left. `normalize_item` sets the take volume so the recording, with the item's own volume, reaches the target. `glue_items` renders a new file (active take, FX, volume and fades baked in) and refuses items with several takes. Locked items are refused until `edit_item locked=false`. `suggest_best_take` checks takes (or REAPER 7 fixed lanes) for clipping, a very low level and sound that stops early, never ranks by loudness, reports ties for you to judge by ear, and changes nothing.

### Mixing
`set_track_volume` `set_track_pan` `set_track_mute` `set_track_solo` `set_send_volume` `set_master_volume` `add_volume_automation` `add_pan_automation`

### Automation
`get_envelope_points` `clear_automation` `set_volume_over_range` `set_pan_over_range` `set_automation_mode`

`set_volume_over_range` raises or lowers a stretch (e.g. a chorus 3 dB down) relative to the automation already there, with short ramps so it does not click. `set_pan_over_range` pans a stretch to an absolute position (e.g. hard left in the solo). Both keep the curve's shape (square steps, curves) outside the range. `clear_automation` removes points in a range and keeps the curve outside it unchanged. `set_automation_mode` warns that touch, latch and write record over automation on the next playback. (`add_volume_automation` and `add_pan_automation` under Mixing add a single point at an absolute value.)

### Markers and Regions
`list_markers` `add_marker` `add_region` `rename_marker` `move_marker` `delete_marker` `go_to_marker`

Markers and regions are named by the number REAPER shows on them (not their place in the list), by name (e.g. "Chorus"), or by GUID; markers and regions are searched together, and `kind="marker"` or `kind="region"` is only needed when both match (they are numbered separately). `list_markers` gives each region's start, end and length.

### Rendering
`render_project` `render_stems` `render_time_selection` `render_regions`

### Mastering
`apply_mastering_chain` `apply_limiter` `normalize_project`

### Analysis
`analyze_loudness` `analyze_dynamics` `analyze_frequency_spectrum` `analyze_stereo_field` `analyze_transients` `detect_clipping`

These render the whole mix to a temporary file and analyse it; File > Render settings are left as they were. The temporary render is always the plain master mix (no stems, normalization, limiting or render tail, whatever the render dialog last had), and it is refused while REAPER is playing or recording.

### Measurement and A/B
`measure_track` `compare_measurements` `save_mix_snapshot` `list_mix_snapshots` `recall_mix_snapshot` `compare_mix_snapshots` `delete_mix_snapshot`

`measure_track` gives one track's integrated loudness (LUFS), RMS and sample peak (dBFS), over the whole project or a time range, by rendering it soloed to a temporary file: after its FX, volume and pan, including what it sends to (a reverb track) and the master volume. The master track's FX are bypassed for the measurement (a limiter there would hide a fader move) unless `include_master_fx=true`. Solo, mute, master FX, the time selection and render settings are put back afterwards; a muted track is measured as if unmuted. A range is cut at the end of the project. `compare_measurements` takes two whole `measure_track` replies (before and after a change) and an expected change ("3 dB louder") and answers `verified`, `not_verified` or `cannot_verify` (different tracks, ranges or master FX settings, missing track or range, silence, too short for LUFS), never a guess.

A mix snapshot holds every track's and the master's volume, pan, mute, solo, and each FX's on/off state and parameter values (plug-ins with more than 128 parameters keep only their on/off state, since each parameter is a call to REAPER). Sends are not part of a snapshot. Up to 20 snapshots are stored inside the project (REAPER's project extension data), so save the project to keep them. `recall_mix_snapshot` is one undo step; it checks the snapshot first (values in range), finds tracks by GUID (moved tracks still match), FX by position and name, reports tracks or FX that are gone, and lists FX added since, which it leaves alone (`bypass_fx`, `remove_fx` or `undo` take those back). `compare_mix_snapshots` lists what differs between two snapshots, or between one and the current mix (`b="current"`, the default), with FX values as the plug-in displays them when it is still there; an FX with many changed parameters gets one summary row unless `detail=true`. Saving or deleting a snapshot changes nothing in the mix and is not an undo step.

## Prompts

Short guides for beginners, served as MCP prompts (in Claude Desktop, under the "+" / attachment menu; in Claude Code, as `/mcp__reaper__<name>`). Each tells the assistant which tools to call, in which order, and the levels to aim for:

- `record_vocals_scarlett_solo`: set up a vocal track with a Focusrite Scarlett Solo (input 1, 48V for condensers, Direct Monitor), check the input level (peaks around -12 dBFS) and record.
- `gain_staging_check`: recording levels, then each track's and the mix's peaks, with fader changes verified by measurement.
- `critique_my_mix`: snapshot the mix, run the analysis tools and give at most three suggestions; nothing is changed without asking.
- `basic_vocal_chain`: EQ (high-pass), compression and a reverb send on a vocal with REAPER's own plug-ins, measured before and after.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: one concern per pull request,
tested against a real REAPER (say which version), and an issue first for new
tools.

## License

MIT
