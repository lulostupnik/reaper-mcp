"""list_installed_fx over plugin caches written into the fake REAPER's resource folder,
in the formats REAPER 7 writes them."""

import pytest

VST_INI = """[vstcache]
reacomp.vst.so=00801700B8D1DC01,1919247213,ReaComp (Cockos)
reaeq.vst.so=00801700B8D1DC01,1919247729,ReaEQ (Cockos)
reasynth.vst.so=00801700B8D1DC01,1919251321,ReaSynth (Cockos)!!!VSTi
Dexed.vst3=00A1,{ABCDEF},Dexed (Digital Suburban)!!!VSTi
TAL-Reverb-4.vst3=00A2,{123456},TAL-Reverb-4 (TAL-Togu Audio Line)
unscanned.so=0,0,
"""
JSFX_INI = """VERSION 1
NAME analysis/loudness_meter "JS: Loudness Meter Peak/RMS/LUFS (Cockos)"
REV analysis/loudness_meter 1
NAME "My Folder/one knob comp" "JS: One Knob Compressor"
TAGS analysis/loudness_meter Analysis
"""
CLAP_INI = """[Surge XT.clap]
_=00AA
org.surge-synth-team.surge-xt=1|Surge XT (Surge Synth Team)
"""


@pytest.fixture
def caches(reaper):
    resource = reaper.tmp / "resources"
    resource.mkdir()
    (resource / "reaper-vstplugins64.ini").write_text(VST_INI)
    (resource / "reaper-jsfx.ini").write_text(JSFX_INI)
    (resource / "reaper-clap-linux-x86_64.ini").write_text(CLAP_INI)
    return resource


def test_lists_every_kind_sorted_by_name(caches, call):
    result = call("list_installed_fx")
    names = [p["name"] for p in result["plugins"]]
    assert result["count"] == 8
    assert names == sorted(names, key=str.casefold)
    by_name = {p["name"]: p for p in result["plugins"]}
    assert by_name["ReaComp (Cockos)"] == {
        "name": "ReaComp (Cockos)", "type": "VST", "instrument": False,
        "add_fx_name": "VST: ReaComp (Cockos)"}
    assert by_name["ReaSynth (Cockos)"]["type"] == "VSTi"
    assert by_name["Dexed (Digital Suburban)"] == {
        "name": "Dexed (Digital Suburban)", "type": "VST3i", "instrument": True,
        "add_fx_name": "VST3: Dexed (Digital Suburban)"}
    assert by_name["One Knob Compressor"]["add_fx_name"] == "JS:My Folder/one knob comp"
    assert by_name["Surge XT (Surge Synth Team)"]["type"] == "CLAP"
    assert by_name["Surge XT (Surge Synth Team)"]["instrument"] is None


def test_fuzzy_search_tolerates_typos(caches, call):
    assert [p["name"] for p in call("list_installed_fx", query="comp")["plugins"]] == [
        "One Knob Compressor", "ReaComp (Cockos)"]
    assert call("list_installed_fx", query="reverb")["plugins"][0]["name"] == (
        "TAL-Reverb-4 (TAL-Togu Audio Line)")
    typo = call("list_installed_fx", query="loudnes")["plugins"]
    assert typo[0]["name"] == "Loudness Meter Peak/RMS/LUFS (Cockos)"
    assert call("list_installed_fx", query="zzzzqq")["plugins"] == []


def test_instruments_only_and_limit(caches, call):
    instruments = call("list_installed_fx", instruments_only=True)
    # CLAP plugins' kind is unknown, so they are kept rather than hidden.
    assert [p["name"] for p in instruments["plugins"]] == [
        "Dexed (Digital Suburban)", "ReaSynth (Cockos)", "Surge XT (Surge Synth Team)"]
    limited = call("list_installed_fx", limit=2)
    assert (limited["count"], len(limited["plugins"])) == (8, 2)
    assert "Showing 2 of 8" in limited["note"]


def test_no_caches_says_why(reaper, call):
    result = call("list_installed_fx")
    assert result["plugins"] == []
    assert "No plugin caches found" in result["note"]


def test_is_read_only(caches, reaper, call):
    call("list_installed_fx", query="eq")
    assert reaper.undo_steps == []
