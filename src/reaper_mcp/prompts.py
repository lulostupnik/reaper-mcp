"""MCP prompts: short beginner guides that tell the assistant which tools to
call, in which order, and what numbers to aim for. Tool names are written in
backticks; tests/test_prompts.py checks each one is a real tool.

The level targets match the tools' own: check_input_level's verdicts
(record_setup_tools.py) and measure_track's readings (measure_tools.py).
"""

# Shared by the prompts that save a snapshot: a fixed name would clash on the
# second run, and overwriting would lose the baseline the user may still want.
SNAPSHOT_NAME = ("add the time to the name, e.g. \"{label} 14:05\"; if the name is taken, "
                 "pick another rather than overwrite")
# What recall can and cannot put back, so the assistant never claims too much.
RECALL_COVERS = (
    "`recall_mix_snapshot` puts back only fader, pan, mute, solo and the settings and "
    "on/off state of FX that were there when it was saved; FX, sends and tracks added "
    "since stay (its reply lists added FX: `bypass_fx` or `remove_fx` them, or use `undo`, "
    "one step per tool call)")
MEASURE_RANGE = ("over a representative 20-30 s around the loudest part, e.g. the chorus "
                 "(start/end in seconds; the whole song only if I ask, since each "
                 "measurement is a render)")

RECORD_VOCALS = """\
Help me set up REAPER to record my voice with a Focusrite Scarlett Solo. I am a \
beginner: explain each step in plain words and wait for me where I have to do \
something with my hands.

1. `get_audio_setup`: the Scarlett must be the input and output device and audio \
running. If not, tell me what to change in REAPER's preferences (the reply says where).
2. `get_project_summary`. If the project was never saved, `save_project` first, so \
the takes are stored next to it.
3. `create_track` (name "Vocal", track_type "audio"), then `set_track_input` with \
input="mono", channel=1: on the Scarlett Solo input 1 is the microphone (XLR) socket.
4. Ask me: condenser microphone? Then press the 48V button. Dynamic microphone \
(like an SM58)? No 48V. INST is only for a guitar in input 2; leave it off.
5. `arm_track` with exclusive=true.
6. Monitoring: with headphones on the Scarlett, turn its Direct Monitor on and keep \
REAPER's monitoring off (`set_monitoring` mode="off"), or I hear myself twice.
7. `check_input_level` while I sing my loudest part. Aim for peaks around -12 dBFS. \
"too quiet" or a little low: gain knob up; "clipping" (-1 dBFS or more): gain knob \
down. The ring around the knob should flash green, never red. Repeat until "good".
8. Optional: `set_metronome` enabled=true.
9. `start_recording`, and when I say stop, `stop_recording`. Read me each take's \
peak level: below -1 dBFS and not clipped is fine.
10. After several takes, `suggest_best_take`, but let me choose by ear.
"""

GAIN_STAGING = """\
Check my gain staging (the level at each stage) and explain it to a beginner.

While recording (a track armed):
- `check_input_level`: peaks between -18 and -6 dBFS are good, about -12 is ideal; \
-1 dBFS or more is clipping. Fix it with the interface's gain knob, not REAPER's fader.

In the mix:
1. `get_project_summary`, then `save_mix_snapshot` named "before gain staging" (\
{name}), so the faders can go back: {recall}.
2. `measure_track` on each track that has audio, {range}. A track peaking above -6 dBFS \
(sample_peak_dbfs) leaves little room once the others add up; nothing should reach \
0 dBFS. Very different integrated_lufs between tracks of the same role is a hint, \
not a rule: judge by ear.
3. `detect_clipping` and `analyze_loudness` on the whole mix: before mastering, \
peaks around -6 dBFS and no clipping.
4. Suggest fader moves (`set_track_volume`) one at a time. After each, measure the \
track again and use `compare_measurements` with the expected change to confirm it.
Each change is one undo step (`undo`).
""".format(name=SNAPSHOT_NAME.format(label="before gain staging"), recall=RECALL_COVERS,
           range=MEASURE_RANGE)

CRITIQUE_MIX = """\
Listen to my mix through the analysis tools and give me honest, beginner-friendly \
feedback. Change no mix settings unless I say yes (saving a snapshot and \
measuring, which solos a track for a moment and puts it back, are fine).

1. `get_project_summary`: tracks, FX, routing.
2. `save_mix_snapshot` named "before critique" ({name}); tell me it was saved.
3. `detect_clipping`: any clipping is the first problem to fix.
4. `analyze_loudness`: an unmastered mix is usually quieter than -14 LUFS; that is \
fine. Peaks above -1 dBFS risk distortion when converted to MP3.
5. `analyze_stereo_field`: lr_correlation below 0 means phase problems (the mix \
loses parts in mono).
6. `analyze_dynamics` and `analyze_frequency_spectrum`: point out only big \
imbalances (one band far above the rest), and say these are hints for the ears.
7. `measure_track` on the main tracks (vocal, drums, bass), {range}: is the lead \
vocal much quieter or louder than the instruments?
Then give at most three suggestions, most important first, each with the tool that \
would do it. After a change, measure again and `compare_measurements`. If I prefer \
the old sound: `undo` for a change that added FX or a send; otherwise {recall}.
""".format(name=SNAPSHOT_NAME.format(label="before critique"), recall=RECALL_COVERS,
           range=MEASURE_RANGE)

VOCAL_CHAIN = """\
Set up a basic vocal chain on my vocal track with REAPER's own plug-ins, \
explaining each one in one sentence. Ask which track is the vocal if it is not clear. \
Parameter names below are what REAPER usually shows: if `get_fx_parameters` lists \
other names, use the closest ones and tell me.

1. `save_mix_snapshot` named "before vocal chain" ({name}) and `measure_track` on \
the vocal, {range}: keep the whole reply.
2. `add_fx` "ReaEQ": to cut rumble, a high-pass around 80-100 Hz. Call \
`get_fx_parameters`: if band 1 has a type parameter, set it to high pass and its \
frequency to about 90 Hz with `set_fx_parameter_by_name`. If there is no type \
parameter, band 1 is a low shelf: set its frequency to about 100 Hz and its gain to \
about -12 dB, which does nearly the same; mention that a true high pass can be \
chosen in the plug-in window.
3. `add_fx` "ReaComp": `set_fx_parameter_by_name` Ratio "4:1", Attack "10 ms", \
Release "100 ms", then lower the threshold until the loud phrases are reduced by \
about 3-6 dB (ask me to look at the gain reduction meter).
4. Reverb on its own track, not on the vocal: `create_track` "Vocal Reverb", \
`add_fx` "ReaVerbate" there, then `set_fx_parameter_by_name` Wet about "0 dB" and \
Dry all the way down (fully wet), then `create_send` from the vocal to it with \
volume_db about -15. (Not `create_bus`: that would take the vocal off the master.)
5. `measure_track` on the vocal again over the same range and `compare_measurements` \
with the first reply, so I know how much louder or quieter it is now; adjust with \
`set_track_volume`.
If I do not like it: `compare_mix_snapshots` shows what changed. The chain added FX, \
a track and a send, which a snapshot cannot take away: `undo` (one step per tool \
call, newest first), or `bypass_fx` to hear the vocal without one plug-in. Note: \
{recall}.
""".format(name=SNAPSHOT_NAME.format(label="before vocal chain"), recall=RECALL_COVERS,
           range=MEASURE_RANGE)


def record_vocals_scarlett_solo() -> str:
    """Set up REAPER to record vocals with a Focusrite Scarlett Solo, step by step."""
    return RECORD_VOCALS


def gain_staging_check() -> str:
    """Check recording and mix levels, with plain targets in dBFS."""
    return GAIN_STAGING


def critique_my_mix() -> str:
    """Analyse the mix and give at most three beginner-friendly suggestions."""
    return CRITIQUE_MIX


def basic_vocal_chain() -> str:
    """Add EQ, compression and a reverb send to a vocal, with REAPER's plug-ins."""
    return VOCAL_CHAIN


PROMPTS = (
    record_vocals_scarlett_solo,
    gain_staging_check,
    critique_my_mix,
    basic_vocal_chain,
)
