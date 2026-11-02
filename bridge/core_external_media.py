"""Bounded local media preparation for the external-chat transcription plugin.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Sampling parameters come from the plugin; Core retains and serves the evidence.
"""
from __future__ import annotations

import base64
import hashlib
import io
import math
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageOps

MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_MATERIALS = 20
MAX_VIDEO_SECONDS = 180
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}


def inspect_material(path, name=None):
    path = Path(path)
    extension = Path(name or path.name).suffix.lower()
    if not 0 < path.stat().st_size <= MAX_FILE_BYTES:
        raise ValueError("单个素材须为 1 字节至 128 MB")
    if extension in IMAGE_EXTENSIONS:
        try:
            with Image.open(path) as image:
                if image.format not in ("PNG", "JPEG", "WEBP") or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("请选择静态 PNG、JPEG 或 WebP 截图")
                if image.width * image.height > 80_000_000 or min(image.size) < 32:
                    raise ValueError("截图尺寸不适合识别，请拆分超长截图")
                image.verify()
            return {"kind": "image"}
        except (OSError, Image.DecompressionBombError) as exc:
            raise ValueError("截图无法读取或尺寸过大") from exc
    if extension in VIDEO_EXTENSIONS:
        import cv2
        capture = cv2.VideoCapture(str(path))
        try:
            fps = capture.get(cv2.CAP_PROP_FPS)
            frames = capture.get(cv2.CAP_PROP_FRAME_COUNT)
            width, height = capture.get(cv2.CAP_PROP_FRAME_WIDTH), capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
            if not capture.isOpened() or not math.isfinite(fps) or fps <= 0 or frames < 1:
                raise ValueError("无法读取这段录屏，请转换为 MP4 后重试")
            seconds = frames / fps
            if not math.isfinite(seconds) or seconds > MAX_VIDEO_SECONDS:
                raise ValueError("首版每段录屏最多 3 分钟，请分段导入")
            if width * height > 16_000_000 or min(width, height) < 32:
                raise ValueError("录屏分辨率不受支持")
            return {"kind": "video", "durationMs": round(seconds * 1000), "fps": fps, "frames": int(frames)}
        finally:
            capture.release()
    raise ValueError("支持 PNG、JPEG、WebP 截图及 MP4、MOV、WebM 录屏")


def evidence_frames(path, kind, plan, directory, *, direction="forward", check=lambda: None):
    """Yield deterministic key, order, evidence path and source video position.

    No transient file escapes the caller's local temporary directory. Exact
    consecutive identical frames can be skipped; similar text is never removed.
    """
    path, directory = Path(path), Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if kind == "image":
        tile_height = min(2400, max(800, int(plan.get("screenshot", {}).get("tileHeight", 1600))))
        overlap = min(tile_height // 2, max(100, int(plan.get("screenshot", {}).get("overlap", 400))))
        with Image.open(path) as raw:
            picture = ImageOps.exif_transpose(raw).convert("RGB")
            # Tile at original resolution so retained evidence stays readable.
            for index, top in enumerate(range(0, picture.height, tile_height - overlap)):
                check()
                bottom = min(top + tile_height, picture.height)
                frame = picture.crop((0, top, picture.width, bottom))
                target = directory / f"image-{top}.png"
                frame.save(target, "PNG")
                yield {"key": f"image:{top}:{bottom}", "order": index, "path": target, "videoMs": None}
                if bottom == picture.height:
                    break
        return
    metadata = _presented_video(path, inspect_material(path), check)
    import cv2
    video = plan.get("video", {})
    maximum = min(240, max(2, int(video.get("maxFrames", 240))))
    interval = max(250, int(video.get("intervalMs", 750)))
    last_ms = max(0, math.floor((metadata["frames"] - 1) / metadata["fps"] * 1000))
    interval = max(interval, math.ceil(last_ms / (maximum - 1)))
    positions = list(range(0, last_ms + 1, interval))
    if positions[-1] != last_ms:
        positions.append(last_ms)
    if direction == "reverse":
        positions.reverse()
    capture = cv2.VideoCapture(str(path))
    previous_hash = None
    sample_window = min(100, interval // 6, max(0, int(video.get("sampleWindowMs", 0))))
    try:
        for index, position in enumerate(positions):
            check()
            if sample_window:
                frame, selected_ms = _sample_video_window(
                    capture, metadata, position, sample_window,
                    endpoint=position in (0, last_ms), check=check)
            else:
                frame_index = min(metadata["frames"] - 1, max(0, round(position * metadata["fps"] / 1000)))
                frame, selected_ms = _read_video_frame(capture, frame_index, metadata,
                                                     exact=position == last_ms, check=check)
            digest = hashlib.sha256(frame.tobytes()).hexdigest()
            # Window sampling keeps every anchor, including stationary endpoints.
            # Semantic overlap is resolved by the plugin, never by pixel similarity.
            if not sample_window and digest == previous_hash:
                continue
            previous_hash = digest
            target = directory / f"video-{position}.png"
            Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).save(target, "PNG")
            result = {"key": f"video:{position}", "order": index, "path": target, "videoMs": selected_ms}
            if sample_window:
                result.update(anchorMs=position, selectedMs=selected_ms)
            yield result
    finally:
        capture.release()


def _presented_video(path, metadata, check):
    """Use FFmpeg's clean full decode when the container overcounts frames.

    MOV/MP4 edit lists (common in phone screen recordings) can drop leading
    frames from presentation while the container still counts them, so the
    counted last frame never exists. Only an error-free decode that yields
    fewer frames replaces the container count; damaged or truncated media
    keeps it and is still reported as unreadable. The frame rate is kept:
    OpenCV maps frame numbers to seek times with its own rate.
    """
    try:
        import imageio_ffmpeg
        executable = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        executable = shutil.which("ffmpeg")
    if not executable:
        return metadata
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            process = subprocess.Popen([executable, "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v:0",
                                        "-f", "null", "-progress", "pipe:1", "-"],
                                       stdout=output, stderr=errors,
                                       creationflags=0x08000000 if os.name == "nt" else 0)
            try:
                deadline = time.monotonic() + 300
                while process.poll() is None:
                    check()
                    if time.monotonic() > deadline:
                        return metadata
                    time.sleep(.1)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            output.seek(0)
            errors.seek(0)
            if process.returncode or errors.read().strip():
                return metadata
            progress = dict(line.split("=", 1) for line in output.read().decode("ascii", "ignore").split()
                            if "=" in line)
    except OSError:
        return metadata
    try:
        frames = int(progress["frame"])
    except (KeyError, ValueError):
        return metadata
    return {**metadata, "frames": frames} if 0 < frames < metadata["frames"] else metadata


def _chat_roi(gray):
    """Exclude the usual fixed header/composer; this does not identify messages."""
    height, width = gray.shape[:2]
    return gray[round(height * .14):round(height * .86),
                round(width * .04):round(width * .96)]


class _UnreadableVideoFrame(ValueError):
    pass


def _video_timestamp(capture):
    import cv2
    timestamp = capture.get(cv2.CAP_PROP_POS_MSEC)
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError("录屏画面时间无法读取，请转换为 MP4 后重试")
    return round(timestamp)


def _read_video_frame(capture, frame_index, metadata, *, check, exact=False):
    """Recover failed random seeks by decoding the requested ordinal from zero.

    OpenCV's FFmpeg seek can reach EOF too early when an MP4 has irregular
    timestamps and its average frame rate differs from the codec frame rate.
    Random seeks can also succeed at an earlier frame. Terminal evidence always
    requests an exact sequential decode. Earlier frames are never substituted
    for genuinely unreadable or missing material.
    """
    import cv2
    check()
    ok = False
    if not exact:
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = capture.read()
    if not ok:
        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
        for _ in range(frame_index + 1):
            check()
            ok, frame = capture.read()
            if not ok:
                position = frame_index / metadata["fps"]
                raise _UnreadableVideoFrame(f"无法读取录屏 {position:.1f} 秒处的画面；已完成部分已保留")
    return frame, _video_timestamp(capture)


def _sequential_video_window(capture, metadata, anchor_ms, window_ms, *, check):
    """Locate at most five nearby real timestamps when average-fps seeks drift."""
    import cv2
    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
    targets = [max(0, anchor_ms + offset) for offset in (-window_ms, -window_ms / 2, 0, window_ms / 2, window_ms)]
    nearest = {}
    for frame_index in range(metadata["frames"]):
        check()
        ok, frame = capture.read()
        if not ok:
            raise _UnreadableVideoFrame(f"无法读取录屏 {anchor_ms / 1000:.1f} 秒处的画面；已完成部分已保留")
        timestamp = _video_timestamp(capture)
        for target in targets:
            distance = abs(timestamp - target)
            if target not in nearest or distance < nearest[target][0]:
                nearest[target] = (distance, frame_index, timestamp, frame)
        if timestamp >= anchor_ms + window_ms:
            break
    candidates = {item[1]: item[1:] for item in nearest.values()}
    bounded = [item for item in candidates.values() if abs(item[1] - anchor_ms) <= window_ms]
    if bounded:
        return sorted(bounded, key=lambda item: item[0])
    # A true low-fps timestamp gap has no frame in the window. Keep its nearest
    # real frame with its real timestamp, rather than inventing a source time.
    return [min(candidates.values(), key=lambda item: (abs(item[1] - anchor_ms), item[0]))]


def _sample_video_window(capture, metadata, anchor_ms, window_ms, *, endpoint, check):
    """Pick a sharp, less-changing nearby frame without creating coverage gaps."""
    import cv2
    fps, last_index = metadata["fps"], metadata["frames"] - 1
    nearest = min(last_index, max(0, round(anchor_ms * fps / 1000)))
    if endpoint:
        indices = [0 if anchor_ms == 0 else last_index]
    else:
        indices = sorted({min(last_index, max(0, round((anchor_ms + delta) * fps / 1000)))
                          for delta in (-window_ms, -window_ms / 2, 0, window_ms / 2, window_ms)})
        indices = [i for i in indices if abs(i / fps * 1000 - anchor_ms) <= window_ms + .001]
        # Very low frame rates may have no frame inside a 200 ms window.
        # Preserve the nearest real frame and report its actual source position.
        if not indices:
            indices = [nearest]
    candidates = []
    for frame_index in indices:
        try:
            frame, timestamp = _read_video_frame(capture, frame_index, metadata,
                                                exact=endpoint and anchor_ms != 0, check=check)
        except _UnreadableVideoFrame:
            if endpoint:
                raise
            continue
        candidates.append((frame_index, timestamp, frame))
    if not candidates:
        raise ValueError(f"无法读取录屏 {anchor_ms / 1000:.1f} 秒处的画面；已完成部分已保留")
    if not endpoint:
        bounded = [item for item in candidates if abs(item[1] - anchor_ms) <= window_ms]
        candidates = bounded or _sequential_video_window(capture, metadata, anchor_ms, window_ms, check=check)
    frames = []
    for frame_index, timestamp, frame in candidates:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if gray.shape[1] > 720:
            gray = cv2.resize(gray, (720, max(1, round(gray.shape[0] * 720 / gray.shape[1]))))
        roi = _chat_roi(gray)
        frames.append((frame_index, timestamp, frame, roi, float(cv2.Laplacian(roi, cv2.CV_64F).var())))
    scored = []
    for index, (frame_index, timestamp, frame, roi, sharpness) in enumerate(frames):
        changes = [float(cv2.absdiff(roi, frames[neighbor][3]).mean())
                   for neighbor in (index - 1, index + 1) if 0 <= neighbor < len(frames)]
        motion = min(changes) if changes else 0
        score = math.log1p(sharpness) / (1 + motion / 32)
        distance = abs(timestamp - anchor_ms)
        scored.append((score, -distance, -frame_index, timestamp, frame))
    chosen = max(scored, key=lambda item: item[:3])
    return chosen[4], chosen[3]


def frame_alignment(previous_path, current_path):
    """Estimate conservative screen geometry, never whether two texts are equal.

    offsetY is the previous-to-current vertical displacement divided by the full
    image height (scrolling upward is negative). Only a well-supported, almost
    pure translation is reliable. Blank/repetitive/changed-scale frames remain
    uncertain so callers cannot silently merge their messages.
    """
    import cv2
    import numpy as np
    result = {"reliable": False, "offsetY": 0.0, "offsetX": 0.0,
              "stationary": False, "confidence": 0.0, "matches": 0, "inliers": 0}
    with Image.open(previous_path) as previous_raw, Image.open(current_path) as current_raw:
        if previous_raw.size != current_raw.size:
            return {**result, "reason": "size-mismatch"}
        if min(previous_raw.size) < 32:
            return {**result, "reason": "too-small"}
        size = previous_raw.size
        factor = min(1.0, 720 / size[0], 1280 / size[1])
        scaled = (round(size[0] * factor), round(size[1] * factor))
        previous = np.asarray(previous_raw.convert("L").resize(scaled, Image.Resampling.LANCZOS))
        current = np.asarray(current_raw.convert("L").resize(scaled, Image.Resampling.LANCZOS))
    first, second = _chat_roi(previous), _chat_roi(current)
    detector = cv2.ORB_create(nfeatures=1600, edgeThreshold=12, patchSize=25, fastThreshold=10)
    first_points, first_descriptors = detector.detectAndCompute(first, None)
    second_points, second_descriptors = detector.detectAndCompute(second, None)
    if first_descriptors is None or second_descriptors is None:
        return {**result, "reason": "insufficient-features"}
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    pairs = matcher.knnMatch(first_descriptors, second_descriptors, k=2)
    matches = [pair[0] for pair in pairs if len(pair) == 2 and pair[0].distance < .72 * pair[1].distance]
    # A descriptor cannot support several different geometric correspondences.
    matches = sorted(matches, key=lambda item: (item.distance, item.queryIdx, item.trainIdx))
    unique, used = [], set()
    for match in matches:
        if match.trainIdx not in used:
            unique.append(match)
            used.add(match.trainIdx)
    result["matches"] = len(unique)
    if len(unique) < 12:
        return {**result, "reason": "insufficient-matches"}
    source = np.float32([first_points[m.queryIdx].pt for m in unique])
    target = np.float32([second_points[m.trainIdx].pt for m in unique])
    transform, mask = cv2.estimateAffinePartial2D(source, target, method=cv2.RANSAC,
        ransacReprojThreshold=2.5, maxIters=1500, confidence=.995, refineIters=10)
    if transform is None or mask is None:
        return {**result, "reason": "inconsistent-motion"}
    inliers = mask.ravel().astype(bool)
    count, ratio = int(inliers.sum()), float(inliers.mean())
    result["inliers"] = count
    scale = math.hypot(transform[0, 0], transform[1, 0])
    rotation = abs(math.degrees(math.atan2(transform[1, 0], transform[0, 0])))
    dx, dy = float(transform[0, 2]), float(transform[1, 2])
    if not .985 <= scale <= 1.015 or rotation > .7 or abs(dx) > max(2.5, scaled[0] * .012):
        return {**result, "reason": "non-scroll-transform"}
    supported = source[inliers]
    span_x = float(np.ptp(supported[:, 0])) if count else 0
    span_y = float(np.ptp(supported[:, 1])) if count else 0
    if count < 12 or ratio < .65 or span_x < first.shape[1] * .18 or span_y < first.shape[0] * .25:
        return {**result, "reason": "weak-spatial-support"}
    confidence = min(1.0, ratio * min(1.0, count / 30) * min(1.0, span_y / (first.shape[0] * .5)))
    return {**result, "reliable": True, "offsetY": round(dy / scaled[1], 6),
            "offsetX": round(dx / scaled[0], 6), "stationary": abs(dy) < max(1.5, scaled[1] * .004),
            "confidence": round(confidence, 4), "reason": "consistent-scroll"}


def picture_region(image, bbox):
    """Locate a sent picture or sticker near the model's approximate box.

    The box is only a hint. Chat backgrounds and bubble fills are flat colors,
    so the picture is the textured area that differs from them; thin text
    strokes are removed before the area is measured. Returns pixel bounds, or
    None when no picture is clearly visible there.
    """
    import cv2
    import numpy as np
    width, height = image.size
    x, y, w, h = bbox
    margin_x, margin_y = .05 * width, max(.5 * h * height, .04 * height)
    left, top = max(0, int(x * width - margin_x)), max(0, int(y * height - margin_y))
    right = min(width, int((x + w) * width + margin_x))
    bottom = min(height, int((y + h) * height + margin_y))
    if right - left < 24 or bottom - top < 24:
        return None
    region = np.asarray(image.crop((left, top, right, bottom)).convert("RGB")).astype(np.int16)
    pixels = region.reshape(-1, 3)
    keys = (pixels[:, 0] >> 3) * 1024 + (pixels[:, 1] >> 3) * 32 + (pixels[:, 2] >> 3)
    counts = np.bincount(keys, minlength=32768)
    content = np.ones(len(keys), bool)
    for key in np.nonzero(counts >= .015 * len(keys))[0]:
        center = pixels[keys == key].mean(axis=0)
        content &= np.abs(pixels - center).sum(axis=1) > 30
    mask = content.reshape(region.shape[:2]).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    count, _labels, stats, _centers = cv2.connectedComponentsWithStats(mask, 8)
    box_left, box_top = x * width - left, y * height - top
    box_right, box_bottom = box_left + w * width, box_top + h * height
    target = w * width * h * height
    found = None
    for index in range(1, count):
        cx, cy, cw, ch, area = (int(value) for value in stats[index])
        overlap_x = max(0, min(cx + cw, box_right) - max(cx, box_left))
        overlap_y = max(0, min(cy + ch, box_bottom) - max(cy, box_top))
        # Parts of one sticker may separate; neighbors outside the hint do not join.
        if area < .02 * target or overlap_x * overlap_y < .5 * cw * ch:
            continue
        rect = (cx, cy, cx + cw, cy + ch)
        found = rect if found is None else (min(found[0], rect[0]), min(found[1], rect[1]),
                                            max(found[2], rect[2]), max(found[3], rect[3]))
    if found is None or (found[2] - found[0]) * (found[3] - found[1]) < .2 * target or \
            min(found[2] - found[0], found[3] - found[1]) < 24:
        return None
    return left + found[0], top + found[1], left + found[2], top + found[3]


def crop_picture(observations, target):
    """Save the most complete view of a picture seen in one or more frames.

    A picture scrolled half out of one frame is usually whole in another, so
    the largest located area wins. Returns False when no frame shows it.
    """
    best = None
    for path, bbox in observations:
        with Image.open(path) as raw:
            image = raw.convert("RGB")
        rect = picture_region(image, bbox)
        if rect and (best is None or (rect[2] - rect[0]) * (rect[3] - rect[1]) > best[0]):
            best = ((rect[2] - rect[0]) * (rect[3] - rect[1]), image.crop(rect))
    if best is None:
        return False
    target = Path(target)
    temporary = target.with_name(target.name + ".tmp")
    best[1].save(temporary, "PNG")
    temporary.replace(target)
    return True


def model_image(path, max_bytes=2_621_440, max_width=1600, *, min_width=0,
                format="jpeg", max_upscale=3):
    """Prepare a bounded model view; original evidence is never resized in place.

    Upscaling is interpolation for small displayed text, not new source detail.
    Legacy callers retain JPEG/no-upscale behavior unless the plugin opts in.
    """
    image_format = str(format).lower()
    if image_format not in ("png", "jpeg", "jpg"):
        raise ValueError("模型画面格式须为 PNG 或 JPEG")
    limit = min(2_621_440, int(max_bytes))
    if limit <= 0:
        raise ValueError("模型画面大小限制须为正数")
    width_limit = min(1800, max(320, int(max_width)))
    with Image.open(path) as raw:
        image = raw.convert("RGB")
        scale = min(max(1, max(0, int(min_width)) / image.width),
                    max(1, min(3, float(max_upscale))), width_limit / image.width, 2400 / image.height)
        if scale != 1:
            image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                                 Image.Resampling.LANCZOS)
        for _attempt in range(18):
            for quality in ((None,) if image_format == "png" else (92, 82, 70, 55)):
                buffer = io.BytesIO()
                if quality is None:
                    image.save(buffer, "PNG", optimize=True)
                else:
                    image.save(buffer, "JPEG", quality=quality)
                payload = buffer.getvalue()
                if len(payload) <= limit:
                    mime = "png" if image_format == "png" else "jpeg"
                    return f"data:image/{mime};base64," + base64.b64encode(payload).decode("ascii")
            if min(image.size) <= 32:
                break
            image = image.resize((max(1, round(image.width * .8)), max(1, round(image.height * .8))),
                                 Image.Resampling.LANCZOS)
    raise ValueError("证据画面仍过大，请拆分素材")
