"""기본 BGM / 효과음을 FFmpeg 신디사이저로 직접 생성한다.

외부 음원을 쓰지 않으므로 저작권 문제가 없다 (CC0 로 기록).
`python -m app.main setup` 에서 실행되며, 이미 파일이 있으면 덮어쓰지 않는다.
더 좋은 음원을 쓰려면 assets/music 에 파일을 넣고 music.json 에 라이선스를 기록하면 된다.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.utils.logger import get_logger
from app.video.ffmpeg import run_ffmpeg

logger = get_logger("synth")

LICENSE = "CC0 (프로젝트 스크립트로 직접 합성)"

BGM_TRACKS = [
    {
        "file": "dark_ambient_01.m4a",
        "title": "Dark Ambient Drone",
        "mood": "dark",
        "graph": (
            "sine=f=55:d={d}[a];sine=f=82.41:d={d}[b];sine=f=110.3:d={d}[c];"
            "anoisesrc=d={d}:c=brown:a=0.5[n];"
            "[a][b][c][n]amix=inputs=4:weights=1 0.7 0.35 0.6:normalize=0,"
            "lowpass=f=520,tremolo=f=0.12:d=0.55,"
            "aecho=0.8:0.85:900|1400:0.35|0.25,"
            "afade=t=in:d=3,afade=t=out:st={fo}:d=4,volume=0.55"
        ),
    },
    {
        "file": "mystery_pulse_01.m4a",
        "title": "Mystery Pulse",
        "mood": "mystery",
        "graph": (
            "sine=f=73.42:d={d}[a];sine=f=110:d={d}[b];sine=f=164.8:d={d}[c];sine=f=220.4:d={d}[e];"
            "[a][b][c][e]amix=inputs=4:weights=1 0.8 0.45 0.2:normalize=0,"
            "tremolo=f=1.6:d=0.45,lowpass=f=900,"
            "aecho=0.8:0.8:600|1100:0.3|0.2,"
            "afade=t=in:d=2,afade=t=out:st={fo}:d=4,volume=0.45"
        ),
    },
    {
        "file": "curious_light_01.m4a",
        "title": "Curious Light",
        "mood": "light",
        "graph": (
            "sine=f=261.63:d={d}[a];sine=f=329.63:d={d}[b];sine=f=392:d={d}[c];sine=f=523.25:d={d}[e];"
            "[a][b][c][e]amix=inputs=4:weights=1 0.8 0.7 0.25:normalize=0,"
            "tremolo=f=4:d=0.6,lowpass=f=2400,"
            "aecho=0.8:0.7:250|500:0.3|0.15,"
            "afade=t=in:d=1.5,afade=t=out:st={fo}:d=3,volume=0.25"
        ),
    },
]

SFX = {
    "whoosh": "anoisesrc=d=0.9:c=pink:a=0.9,bandpass=f=1400:w=1800,afade=t=in:d=0.45:curve=exp,afade=t=out:st=0.45:d=0.45,volume=1.4",
    "transition": "anoisesrc=d=0.6:c=white:a=0.6,highpass=f=2500,afade=t=in:d=0.15,afade=t=out:st=0.15:d=0.45,volume=0.9",
    "impact": (
        "aevalsrc='0.95*sin(2*PI*(48+60*exp(-8*t))*t)*exp(-3.2*t)':d=1.4[a];"
        "anoisesrc=d=0.25:c=brown:a=0.8,afade=t=out:d=0.25[n];"
        "[a][n]amix=inputs=2:normalize=0,lowpass=f=1800,volume=1.3"
    ),
    "heartbeat": (
        "aevalsrc='0.9*sin(2*PI*58*t)*exp(-14*t)':d=0.3[x];"
        "aevalsrc='0.7*sin(2*PI*52*t)*exp(-14*t)':d=0.3,adelay=230[y];"
        "[x][y]amix=inputs=2:normalize=0,apad=pad_dur=0.6,"
        "asplit[h1][h2];[h2]adelay=900[h2d];[h1][h2d]amix=inputs=2:normalize=0,lowpass=f=300,volume=2.2"
    ),
    "suspense": (
        "aevalsrc='0.35*sin(2*PI*(180*t+60*t*t))+0.2*sin(2*PI*(270*t+90*t*t))':d=2.2,"
        "tremolo=f=9:d=0.5,afade=t=in:d=1.2,afade=t=out:st=1.8:d=0.4,lowpass=f=2000"
    ),
    "click": "aevalsrc='0.9*sin(2*PI*2200*t)*exp(-90*t)':d=0.12,highpass=f=800",
}


def _render(graph: str, out: Path, sample_rate: int = 48000) -> None:
    codec = ["-c:a", "aac", "-b:a", "160k"] if out.suffix == ".m4a" else []
    if ";" in graph or "[" in graph:
        args = ["-filter_complex", graph + ",aresample=" + str(sample_rate)]
    else:
        args = ["-f", "lavfi", "-i", graph, "-af", f"aresample={sample_rate}"]
    run_ffmpeg([*args, "-ac", "2", *codec, str(out)], step=f"synth {out.name}")


def generate_default_audio(music_dir: Path, sfx_dir: Path, bgm_seconds: int = 75, overwrite: bool = False) -> list[Path]:
    music_dir.mkdir(parents=True, exist_ok=True)
    sfx_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    meta_file = music_dir / "music.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else []
    known = {m["file"] for m in meta}
    for track in BGM_TRACKS:
        out = music_dir / track["file"]
        if overwrite or not out.exists():
            _render(track["graph"].format(d=bgm_seconds, fo=bgm_seconds - 4), out)
            created.append(out)
        if track["file"] not in known:
            meta.append({
                "file": track["file"], "title": track["title"], "artist": "StrangeRecords (synth)",
                "source": "self-generated", "license": LICENSE, "url": "", "mood": track["mood"],
            })
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    for name, graph in SFX.items():
        out = sfx_dir / f"{name}.wav"
        if overwrite or not out.exists():
            _render(graph, out)
            created.append(out)
    (sfx_dir / "sfx.json").write_text(
        json.dumps([{"file": f"{n}.wav", "license": LICENSE, "source": "self-generated"} for n in SFX], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("기본 오디오 생성: %d개", len(created))
    return created
