# FRAMEWALL — Full Scan

A lightweight interactive CLI for extracting visually distinct video frames for photographic sequences, visual archives and art installations.

## Requirements

- Python 3.8+ (standard library only; no pip packages).
- FFmpeg and ffprobe on PATH. FFmpeg 7.1+ recommended (`-/filter:v` is used for JPEG export).
- Tested on Linux with FFmpeg 7.1.5; macOS and Windows are not separately tested.

## Usage

```sh
python3 framewall.py "my_video.mp4"
python3 framewall.py "my_video.mp4" --fresh
python3 framewall.py "my_video.mp4" --index selection.json --output print_export
```

Use `--fresh` to skip existing indices and start a new full analysis. Legacy indices can still be loaded manually; they are marked unverified, and a new analysis is the default if the most recent index is legacy. On Windows use `python` instead of `python3` if needed.

## Selection and full-stream verification

1. Choose a sensitivity profile and whether to always include the final video frame (default) or require it to be visually distinct.
2. FFmpeg decodes every frame of the first non-cover-art video stream in presentation order. Each frame is resized to 64 × 64 RGB for analysis, streamed in memory, and compared with the **last selected frame**. Native FFmpeg scene scores are also evaluated. No JPEGs are created.
3. FFprobe independently decodes and counts frames of the same stream. If the counts disagree, a decoding error occurs, or the source changes during analysis, the run stops rather than accepting an incomplete index. Progress uses the video stream duration when available, not the audio or overall container duration. The final decoded and selected frame numbers and timestamps are reported separately. Verification requires an additional decode pass and cannot establish whether the source file itself is missing content.
4. Accept the selection (default), adjust sensitivity and analyse again, or save only the index. No maximum image count is imposed.
5. Select JPEG maximum width (480, 800, 1280 px or original) and compression (q=2, 5, 8 or 12). Default: 800 px, q=5. Smaller inputs are not upscaled. The source aspect ratio is preserved for typical square-pixel videos.
6. Confirm JPEG export (default). FFmpeg decodes the original video and writes exactly the indexed frames. A mismatch in JPEG count aborts the export.

The first frame is always selected. Subsequent frames qualify when their RGB difference reaches the regular threshold, or when both the scene score and its minimum RGB difference are met. The final frame can be included even if identical to the previous selected frame; the index labels a forced ending with the reason `final`.

| Profile | RGB difference | Scene score | Scene RGB guard |
|---|---:|---:|---:|
| Ultra sensitive | 0.3% | 0.025 | 0.15% |
| Micro | 1% | 0.06 | 0.5% |
| Very fine | 2.5% | 0.10 | 1.25% |
| Fine | 6% | 0.18 | 3% |
| Medium fine | 8% | 0.21 | 4% |
| Normal (default) | 10% | 0.25 | 5% |
| Medium strict | 13% | 0.32 | 6.5% |
| Strict | 16% | 0.40 | 8% |
| Very strict | 25% | 0.55 | 12.5% |
| Extreme | 40% | 0.75 | 20% |
| Custom | 0.01–100% | 0–1 | 0–regular threshold |

RGB difference means the mean absolute channel-value difference normalized by 255, not the percentage of pixels that changed. Higher sensitivity generally yields more images, but counts are not guaranteed to be strictly monotonic because each selected frame becomes the new reference. This is a visual heuristic, not semantic recognition. Flicker, shake, tiny details, or repeated subjects can affect results. Analysis uses square thumbnails; anamorphic video may require separate aspect-ratio correction.

## Output and versioning

Each run is self-contained:

```text
frames_my_video/
  normal-800-q5/
    index.json
    image_000001.jpg
    image_000002.jpg
  normal-800-q5_v2/
    index.json
    ...
  veryfine-480-q8/
    index.json
    ...
  strict-distinct-orig-q2/
    index.json
    ...
```

The folder encodes the sensitivity, width and JPEG quality. `-distinct` indicates no forced final frame; custom thresholds use a short hash. Index-only runs use `-index`. `--output` sets the parent project directory.

If a matching run already exists, create a new numbered version (default), explicitly overwrite a recognized run after confirmation, or cancel. Unrelated files prevent overwrite. Replacement is staged before the old run is removed.

Each `index.json` records source fingerprint, thresholds, frame numbers (zero-based), timestamps, scores, selection reasons, verification counts and JPEG mapping. An index can be reused to export the same frames at different resolutions, without repeating visual analysis. Source path, size and modification time must match; this is not a cryptographic checksum.

## Tests

Linux / FFmpeg 7.1.5: a synthetic 50-frame video with a static ending and a longer audio track was independently verified at 50/50 frames; its final frame (#49, 4.9 s) was included by default. All 11 selected JPEGs were exported. All ten sensitivity profiles were checked against that source with forced ending disabled (31 selections at Ultra, 10 at Normal, 2 at Extreme); the static tail was not selected. The actual user footage has not been tested.
