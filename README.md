# REAPER MCP Server

A Model Context Protocol (MCP) server that enables AI agents to control REAPER DAW — 61 tools covering project management, tracks, track templates, MIDI, FX, mixing, mastering, rendering, and audio analysis.

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

## Tools (98)

### Context and Undo
`get_context` `get_project_summary` `undo` `redo` `get_undo_state`

Every tool that changes the project is one named REAPER undo step ("MCP: Set track volume (track_index=0, volume_db=-6)"), so `undo` or Ctrl+Z takes back exactly one tool call; a call that fails is named "(failed)", and a bad track index is refused before any step is made. `undo` and `redo` only act on these "MCP: " steps unless called with `include_manual_steps=true` (so a recorded take is not removed by accident), and never while recording. Tools are annotated as read-only or changing (MCP `readOnlyHint` / `destructiveHint` / `idempotentHint`), list and get tools report track and item GUIDs, and `delete_track`, `remove_fx` and `remove_send` accept a track GUID instead of an index. The project summary is also the resource `reaper://project/summary`.

### Project Management
`create_project` `load_project` `save_project` `get_project_info` `set_tempo` `set_time_signature` `set_cursor_position` `play_project` `stop_transport`

### Recording
`get_audio_setup` `set_track_input` `arm_track` `set_monitoring` `set_track_record_mode` `check_input_level` `start_recording` `stop_recording` `set_metronome` `set_recording_format` `set_record_path` `set_time_selection` `set_loop` `set_punch_mode`

`get_audio_setup` shows the interface REAPER uses (the device itself can only be changed in REAPER's preferences; the reply says where). `check_input_level` listens to an armed track for a few seconds and says "no signal" (with things to check: cable, 48V, INST, gain), "too quiet", "good" or "clipping", without recording. `start_recording` refuses until it can work (audio running, project saved or `save_first=true`, an input and a recording record mode on each track, not already recording), disarms other tracks by default, stops playback and can start from a position, warns about monitoring, auto-punch and repeat, and confirms REAPER is recording. Count-in is only reported; it is turned on in REAPER itself (right-click the metronome button). `stop_recording` keeps the take without REAPER's save/delete dialog and lists each new take's file, length and peak level. REAPER actions are run only after REAPER confirms the action's name (the core transport and metronome actions also run under a translated name, so stopping never refuses).

### Project Templates
`list_project_templates` `new_project_from_template` `save_project_template` `save_track_template`

### Tracks
`create_track` `delete_track` `rename_track` `list_tracks` `get_track_info` `set_track_color` `create_bus` `create_send` `remove_send` `list_sends`

### Track Templates
`list_track_templates` `insert_track_template`

Inserts saved `.RTrackTemplate` files from REAPER's TrackTemplates folder (subfolders included) with their exact FX state, routing, folder structure and items, optionally at a given track position.

### MIDI
`create_midi_item` `add_midi_note` `create_chord_progression` `create_drum_pattern`

### FX & Instruments
`add_fx` `remove_fx` `bypass_fx` `list_track_fx` `get_fx_parameters` `set_fx_parameter` `load_fx_preset` `add_master_fx` `list_master_fx` `set_master_fx_parameter`

### Audio
`import_audio_file` `edit_audio_item` `adjust_pitch` `adjust_playback_rate`

### Items and Takes
`list_items` `edit_item` `split_item` `delete_item` `move_item` `duplicate_item` `trim_item_to_time_selection` `glue_items` `remove_silence` `trim_silence` `normalize_item` `list_takes` `set_active_take` `crop_to_active_take` `suggest_best_take`

Items are named by GUID (from `list_items`), which survives splits and moves of other items, or by index on a track (indices shift after every split, delete or move, so tools that used one remind you of the GUID). `list_items` is compact by default; `detail=true` adds fades, volumes and source files. `edit_item` sets exact values: `start`/`end` trim the edges without moving the audio, a fade of 0 removes it (automatic crossfades included); `move_item` moves whole items. `edit_audio_item` is legacy (relative trims; a fade of 0 there leaves the fade alone). `remove_silence` and `trim_silence` analyze the active take's own audio file (start offset, playrate and channel mode included; reversed takes and takes with stretch markers are refused, take FX are warned about, other takes are cut at the same places) and never open REAPER's Dynamic split dialog; `dry_run=true` previews the cuts, the seconds removed and the pieces left. `normalize_item` sets the take volume so the recording, with the item's own volume, reaches the target. `glue_items` renders a new file (active take, FX, volume and fades baked in) and refuses items with several takes. Locked items are refused until `edit_item locked=false`. `suggest_best_take` checks takes (or REAPER 7 fixed lanes) for clipping, a very low level and sound that stops early, never ranks by loudness, reports ties for you to judge by ear, and changes nothing.

### Mixing
`set_track_volume` `set_track_pan` `set_track_mute` `set_track_solo` `set_send_volume` `set_master_volume` `add_volume_automation` `add_pan_automation`

### Rendering
`render_project` `render_stems` `render_time_selection` `render_regions`

### Mastering
`apply_mastering_chain` `apply_limiter` `normalize_project`

### Analysis
`analyze_loudness` `analyze_dynamics` `analyze_frequency_spectrum` `analyze_stereo_field` `analyze_transients` `detect_clipping`

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: one concern per pull request,
tested against a real REAPER (say which version), and an issue first for new
tools.

## License

MIT
