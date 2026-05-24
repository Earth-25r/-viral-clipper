import os
import uuid
import asyncio
import subprocess
import json
import httpx
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY", "")
WORK_DIR = Path(os.getenv("WORK_DIR", "/tmp/viral-clipper"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_DIR = WORK_DIR / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Viral Clipper API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/clips", StaticFiles(directory=str(OUTPUT_DIR)), name="clips")

# ── In-memory job store (swap for Redis in production) ────────────────────────
jobs: dict[str, dict] = {}


# ── Schemas ───────────────────────────────────────────────────────────────────
class ClipRequest(BaseModel):
    url: str
    mode: str = "viral"          # viral | highlights | funny
    aspect_ratio: str = "16:9"   # 16:9 | 9:16
    captions: bool = True
    max_clips: int = 3


class JobStatus(BaseModel):
    job_id: str
    status: str      # queued | downloading | transcribing | clipping | done | error
    progress: int    # 0-100
    message: str
    clips: list[dict] = []
    error: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────
def update_job(job_id: str, **kwargs):
    jobs[job_id].update(kwargs)


def run(cmd: list[str], cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, cwd=cwd, timeout=600
    )


async def download_video(url: str, job_dir: Path, job_id: str) -> Path:
    update_job(job_id, status="downloading", progress=10, message="Downloading video…")
    out_template = str(job_dir / "source.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--format", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--output", out_template,
        "--no-warnings",
        url,
    ]
    result = run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {result.stderr[:400]}")
    files = list(job_dir.glob("source.*"))
    if not files:
        raise RuntimeError("Download produced no output file")
    return files[0]


async def extract_audio(video_path: Path, job_dir: Path) -> Path:
    audio_path = job_dir / "audio.wav"
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000",
        "-acodec", "pcm_s16le", str(audio_path),
    ]
    result = run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg audio extraction failed: {result.stderr[:400]}")
    return audio_path


async def transcribe_and_detect_highlights(audio_path: Path, job_id: str) -> list[dict]:
    update_job(job_id, status="transcribing", progress=35, message="AI scanning for viral moments…")
    headers = {"authorization": ASSEMBLYAI_API_KEY, "content-type": "application/json"}

    async with httpx.AsyncClient(timeout=300) as client:
        with open(audio_path, "rb") as f:
            upload_resp = await client.post(
                "https://api.assemblyai.com/v2/upload",
                headers={"authorization": ASSEMBLYAI_API_KEY},
                content=f.read(),
            )
        upload_resp.raise_for_status()
        audio_url = upload_resp.json()["upload_url"]

        transcript_resp = await client.post(
            "https://api.assemblyai.com/v2/transcript",
            headers=headers,
            json={
                "audio_url": audio_url,
                "auto_highlights": True,
                "sentiment_analysis": True,
                "auto_chapters": True,
            },
        )
        transcript_resp.raise_for_status()
        transcript_id = transcript_resp.json()["id"]

        update_job(job_id, progress=45, message="Transcribing audio…")
        for _ in range(120):
            await asyncio.sleep(5)
            poll = await client.get(
                f"https://api.assemblyai.com/v2/transcript/{transcript_id}",
                headers=headers,
            )
            poll.raise_for_status()
            data = poll.json()
            if data["status"] == "completed":
                break
            if data["status"] == "error":
                raise RuntimeError(f"AssemblyAI error: {data.get('error')}")
        else:
            raise RuntimeError("Transcription timed out")

    # Cache word-level transcript for captions
    transcript_cache = audio_path.parent / "transcript.json"
    with open(transcript_cache, "w") as f:
        json.dump({"words": data.get("words", [])}, f)

    update_job(job_id, progress=65, message="Detecting best moments…")
    segments = []

    highlights = data.get("auto_highlights_result", {}).get("results", [])
    for h in highlights:
        for ts in h.get("timestamps", []):
            segments.append({
                "start_ms": ts["start"],
                "end_ms": ts["end"],
                "text": h["text"],
                "score": h["rank"],
            })

    chapters = data.get("chapters", [])
    for ch in chapters:
        segments.append({
            "start_ms": ch["start"],
            "end_ms": ch["end"],
            "text": ch.get("headline", ch.get("summary", "")),
            "score": 0.6,
        })

    return merge_segments(segments)


def merge_segments(segments: list[dict], pad_ms=5000, min_ms=15000, max_ms=90000) -> list[dict]:
    if not segments:
        return []
    segments = sorted(segments, key=lambda x: -x["score"])
    clips = []
    for seg in segments:
        start = max(0, seg["start_ms"] - pad_ms)
        end = seg["end_ms"] + pad_ms
        duration = end - start
        if duration < min_ms:
            center = (start + end) // 2
            start = max(0, center - min_ms // 2)
            end = start + min_ms
        if duration > max_ms:
            end = start + max_ms
        overlap = any(start < c["end_ms"] and end > c["start_ms"] for c in clips)
        if not overlap:
            clips.append({"start_ms": start, "end_ms": end, "text": seg["text"], "score": seg["score"]})
        if len(clips) >= 10:
            break
    return sorted(clips, key=lambda x: x["start_ms"])


def ms_to_seconds(ms: int) -> float:
    return ms / 1000.0


def seconds_to_ts(s: float) -> str:
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}"


async def cut_clip(video_path, clip, index, job_dir, aspect_ratio, captions, transcript_words):
    start_s = ms_to_seconds(clip["start_ms"])
    end_s = ms_to_seconds(clip["end_ms"])
    duration = end_s - start_s
    out_name = f"clip_{index:02d}.mp4"
    out_path = OUTPUT_DIR / out_name
    filters = []

    if aspect_ratio == "9:16":
        filters.append("crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920")
    else:
        filters.append("scale=1920:1080")

    if captions and transcript_words:
        words_in_clip = [
            w for w in transcript_words
            if w.get("start", 0) >= clip["start_ms"] and w.get("end", 0) <= clip["end_ms"]
        ]
        for i in range(0, len(words_in_clip), 5):
            group = words_in_clip[i:i+5]
            text = " ".join(w["text"] for w in group)
            safe = text.replace("'", "\\'").replace(":", "\\:")
            t0 = (group[0]["start"] - clip["start_ms"]) / 1000
            t1 = (group[-1]["end"] - clip["start_ms"]) / 1000
            filters.append(
                f"drawtext=text='{safe}':fontsize=48:fontcolor=white"
                f":borderw=3:bordercolor=black:x=(w-text_w)/2:y=h-120"
                f":enable='between(t,{t0:.2f},{t1:.2f})'"
            )

    vf = ",".join(filters)
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_s), "-i", str(video_path),
        "-t", str(duration),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    result = run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg clip failed: {result.stderr[:400]}")

    return {
        "clip_id": f"clip_{index:02d}",
        "filename": out_name,
        "url": f"/clips/{out_name}",
        "start": seconds_to_ts(start_s),
        "end": seconds_to_ts(end_s),
        "duration_s": round(duration, 1),
        "headline": clip["text"][:120],
        "score": round(clip["score"], 3),
        "size_mb": round(out_path.stat().st_size / 1_048_576, 1),
    }


async def process_clip_job(job_id: str, req: ClipRequest):
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(exist_ok=True)
    try:
        video_path = await download_video(req.url, job_dir, job_id)
        update_job(job_id, progress=25, message="Extracting audio…")
        audio_path = await extract_audio(video_path, job_dir)
        highlight_segments = await transcribe_and_detect_highlights(audio_path, job_id)

        if not highlight_segments:
            probe = run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(video_path)])
            duration_s = float(json.loads(probe.stdout)["format"]["duration"])
            seg_s = min(60, duration_s / max(req.max_clips, 1))
            highlight_segments = [
                {"start_ms": int(i*seg_s*1000), "end_ms": int((i+1)*seg_s*1000), "text": f"Segment {i+1}", "score": 0.5}
                for i in range(req.max_clips)
            ]

        update_job(job_id, status="clipping", progress=70, message="Cutting clips…")
        segments_to_cut = highlight_segments[:req.max_clips]

        transcript_words = []
        if req.captions:
            tc = job_dir / "transcript.json"
            if tc.exists():
                with open(tc) as f:
                    transcript_words = json.load(f).get("words", [])

        results = []
        for i, seg in enumerate(segments_to_cut):
            update_job(job_id, progress=70 + int(25*i/len(segments_to_cut)),
                       message=f"Rendering clip {i+1} of {len(segments_to_cut)}…")
            clip_info = await cut_clip(video_path, seg, i+1, job_dir, req.aspect_ratio, req.captions, transcript_words)
            results.append(clip_info)

        update_job(job_id, status="done", progress=100,
                   message=f"Done! {len(results)} clip(s) ready.", clips=results)

    except Exception as e:
        update_job(job_id, status="error", progress=0, message="Failed.", error=str(e))
    finally:
        for fname in ["source.mp4", "source.webm", "audio.wav"]:
            try:
                (job_dir / fname).unlink(missing_ok=True)
            except Exception:
                pass


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"service": "Viral Clipper API", "version": "1.0.0"}


@app.post("/api/clip")
async def create_clip(req: ClipRequest, background_tasks: BackgroundTasks):
    if not ASSEMBLYAI_API_KEY:
        raise HTTPException(500, "ASSEMBLYAI_API_KEY not configured")
    if not req.url.strip():
        raise HTTPException(400, "URL is required")
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"job_id": job_id, "status": "queued", "progress": 0,
                    "message": "Job queued…", "clips": [], "error": None}
    background_tasks.add_task(process_clip_job, job_id, req)
    return jobs[job_id]


@app.get("/api/job/{job_id}")
def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    return jobs[job_id]


@app.delete("/api/job/{job_id}")
def delete_job(job_id: str):
    jobs.pop(job_id, None)
    return {"deleted": job_id}


@app.get("/health")
def health():
    return {"ok": True}
