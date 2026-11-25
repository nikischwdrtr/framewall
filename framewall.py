#!/usr/bin/env python3
"""Framewall: analyse visual changes, then optionally export selected JPEGs."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

VERSION = 1
SCAN_VERSION = 2
# Increasing thresholds produce generally sparser selections.
PRESETS = [
    ("ultra", 0.003, 0.025, 0.0015, "Ultra sensitive | tiny changes"),
    ("micro", 0.01, 0.06, 0.005, "Micro          | very small changes"),
    ("veryfine", 0.025, 0.10, 0.0125, "Very fine      | small changes"),
    ("fine", 0.06, 0.18, 0.03, "Fine           | more intermediate frames"),
    ("mediumfine", 0.08, 0.21, 0.04, "Medium fine    | subtle changes"),
    ("normal", 0.10, 0.25, 0.05, "Normal         | noticeable changes"),
    ("mediumstrict", 0.13, 0.32, 0.065, "Medium strict  | stronger changes"),
    ("strict", 0.16, 0.40, 0.08, "Strict         | major changes"),
    ("verystrict", 0.25, 0.55, 0.125, "Very strict    | large changes"),
    ("extreme", 0.40, 0.75, 0.20, "Extreme        | only dramatic changes"),
]
SIDE = 64
FRAME_BYTES = SIDE * SIDE * 3
COLOR = sys.stdout.isatty() and not os.environ.get('NO_COLOR') and os.name != 'nt'


def paint(s, code):
    return f'\033[{code}m{s}\033[0m' if COLOR else s


def banner():
    print(paint('\n  +------------------------------------------+\n'
                '  | F R A M E W A L L                        |\n'
                '  | video -> visual index -> photographs     |\n'
                '  +------------------------------------------+', '36'))


def choose(title, options, default=0):
    print('\n  ' + paint(title, '1'))
    for i, option in enumerate(options):
        print(f'    {i+1}  {option}' + ('  [Default]' if i == default else ''))
    while True:
        answer = input(f'  > [{default+1}] ').strip()
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)-1
        print('  Please enter one of the listed numbers.')


def number(label, default, low, high):
    while True:
        raw = input(f'  {label} [{default:g}]: ').strip().replace(',', '.')
        try:
            value = float(raw) if raw else default
            if math.isfinite(value) and low <= value <= high:
                return value
        except ValueError:
            pass
        print(f'  Please enter a value between {low:g} and {high:g}.')


def settings():
    p = choose('Frame selection sensitivity (dense -> sparse)',
               [f'{label} | RGB {a*100:g}%' for _, a, _, _, label in PRESETS]
               + ['Custom         | enter your own thresholds'], 5)
    if p < len(PRESETS):
        _, a, b, c, _ = PRESETS[p]
    else:
        a = number('RGB difference from last selected frame (%)', 10, 0.01, 100)/100
        b = number('FFmpeg scene score (0 to 1)', 0.25, 0, 1)
        c = number('Minimum RGB difference for scene changes (%)', min(5, a*100), 0, a*100)/100
    end = choose('End of video', ['Always include the final frame (recommended)',
                                 'Only include final frame if visually distinct'], 0)
    return dict(last_threshold=a, scene_threshold=b, scene_guard=c, include_final=(end == 0))


def fingerprint(video):
    st = video.stat()
    return dict(path=str(video), size=st.st_size, mtime_ns=st.st_mtime_ns)


def video_info(video, count=False):
    # V:0 excludes attached cover art, matching both FFmpeg passes.
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'V:0']
    if count:
        cmd += ['-count_frames']
    cmd += ['-show_entries', 'stream=duration,start_time,nb_read_frames', '-of', 'json', str(video)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode or p.stderr.strip():
        raise RuntimeError(p.stderr.strip() or 'FFprobe could not read the video stream.')
    streams = json.loads(p.stdout).get('streams', [])
    if not streams:
        raise RuntimeError('No video stream found.')
    return streams[0]


def finite_number(value):
    try:
        d = float(value)
        return d if math.isfinite(d) else None
    except (TypeError, ValueError):
        return None


def clock(ts):
    if ts is None:
        return 'unknown'
    sign = '-' if ts < 0 else ''
    ms = round(abs(ts)*1000)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    return f'{sign}{h:02d}:{m:02d}:{ms/1000:06.3f}'


def progress(n, selected, timestamp, total):
    elapsed = f'{timestamp:8.1f}s' if timestamp is not None else '      --'
    if total and timestamp is not None:
        frac = max(0, min(1, timestamp/total))
        fill = int(frac * 22)
        bar = '[' + '#' * fill + '-' * (22-fill) + ']'
    else:
        bar = '[ Analysing ]'
    print(f'\r  {bar} {elapsed} | {n:,} frames | {selected:,} selected',
          end='', flush=True)


def read_frame(pipe):
    data = pipe.read(FRAME_BYTES)
    if data and len(data) != FRAME_BYTES:
        raise RuntimeError('Incomplete analysis frame received from FFmpeg.')
    return data


def analyse(video, cfg):
    """Stream tiny RGB frames; read native FFmpeg scene metadata in lockstep."""
    initial = fingerprint(video)
    total = finite_number(video_info(video).get("duration"))
    first_ts = None
    last_row = None
    rows = []
    previous_selected = None
    n = 0
    last_update = 0.0
    print('\n  ' + paint('01 / ANALYSE', '36') + '  (no JPEGs written yet)')
    with tempfile.TemporaryDirectory(prefix='framewall-analysis-') as work:
        work = Path(work)
        meta_path = work / 'scene.log'
        meta_path.touch()
        err_path = work / 'ffmpeg.log'
        vf = (f'scale={SIDE}:{SIDE}:flags=area,format=rgb24,'
              "select='if(isnan(scene),1,1)',"
              'metadata=mode=print:key=lavfi.scene_score:file=scene.log:direct=1')
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-xerror',
               '-i', str(video), '-map', '0:V:0', '-an', '-sn', '-dn',
               '-vf', vf, '-fps_mode', 'passthrough', '-pix_fmt', 'rgb24',
               '-c:v', 'rawvideo', '-threads:v', '1', '-f', 'rawvideo', 'pipe:1']
        with err_path.open('wb') as errors, meta_path.open('r', encoding='utf-8') as meta:
            proc = subprocess.Popen(cmd, cwd=work, stdout=subprocess.PIPE, stderr=errors)
            try:
                while True:
                    frame = read_frame(proc.stdout)
                    if not frame:
                        break
                    # Metadata is written synchronously upstream of the raw output.
                    header = meta.readline()
                    score_line = meta.readline()
                    match = re.search(r'frame:\s*(\d+).*?pts_time:\s*(\S+)', header)
                    if not match or int(match.group(1)) != n or not score_line.startswith('lavfi.scene_score='):
                        raise RuntimeError(f'Frame/metadata synchronisation failed at frame {n}.')
                    score = finite_number(score_line.split('=', 1)[1])
                    if score is None:
                        raise RuntimeError(f'Invalid scene score at frame {n}.')
                    try:
                        ts = float(match.group(2))
                        if not math.isfinite(ts):
                            ts = None
                    except ValueError:
                        ts = None
                    if n == 0:
                        first_ts = ts
                    distance = (sum(abs(a-b) for a, b in zip(frame, previous_selected)) /
                                (FRAME_BYTES*255) if previous_selected is not None else None)
                    reasons = []
                    if distance is None:
                        reasons.append('first')
                    else:
                        if distance >= cfg['last_threshold']:
                            reasons.append('last_difference')
                        if score >= cfg['scene_threshold'] and distance >= cfg['scene_guard']:
                            reasons.append('scene_change')
                    last_row = dict(frame=n, time_seconds=ts, scene_score=round(score, 6),
                                    last_difference=round(distance, 6) if distance is not None else None,
                                    reasons=reasons)
                    if reasons:
                        rows.append(last_row)
                        previous_selected = frame
                    n += 1
                    now = time.monotonic()
                    if now-last_update > 0.25:
                        progress(n, len(rows), ts-first_ts if ts is not None and first_ts is not None else None, total)
                        last_update = now
                rc = proc.wait()
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                proc.stdout.close()
        if n:
            progress(n, len(rows), ts-first_ts if ts is not None and first_ts is not None else None, total)
        print()
        errors_text = err_path.read_text(encoding='utf-8', errors='replace').strip()
        if rc or errors_text:
            raise RuntimeError(errors_text or 'FFmpeg analysis failed.')
        if not n:
            raise RuntimeError('No video frames found.')
    # Independent full-stream decode: do not trust a container duration or
    # a successful pipe exit alone. FFprobe counts video frames, not packets.
    print('  Verifying complete video decode with FFprobe...', flush=True)
    info = video_info(video, count=True)
    try:
        expected = int(info['nb_read_frames'])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError('FFprobe could not verify the total decoded frame count.')
    if n != expected:
        raise RuntimeError(f'Incomplete analysis: {n:,} of {expected:,} decoded frames. No index accepted.')
    if cfg.get('include_final', True) and rows[-1]['frame'] != n-1:
        last_row['reasons'] = ['final']
        rows.append(last_row)
    if fingerprint(video) != initial:
        raise RuntimeError('The video file changed during analysis.')
    print(f'  Verified: {n:,} / {expected:,} frames | end of video reached.')
    return dict(version=VERSION, scan_version=SCAN_VERSION, source=initial,
                analysis_size=[SIDE, SIDE], thresholds=cfg, decoded_frames=n,
                verified_frames=expected, first_decoded_time=first_ts,
                last_decoded_time=ts, duration_seconds=total, frames=rows)


def save_index(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement, so an interrupted write does not destroy an old index.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.framewall-', delete=False) as f:
        tmp = Path(f.name)
        try:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write('\n')
        except BaseException:
            f.close()
            tmp.unlink(missing_ok=True)
            raise
    try:
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def load_index(path, video):
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('version') != VERSION or data.get('source') != fingerprint(video):
        raise ValueError('Index does not match the current video (path, size, modification time or version).')
    rows = data['frames']
    ids = [r['frame'] for r in rows]
    if (not ids or any(type(i) is not int or i < 0 or i >= data['decoded_frames'] for i in ids)
            or ids != sorted(set(ids))):
        raise ValueError('Invalid frame numbers in the index.')
    return data


def report(data, path):
    rows = data['frames']
    scene = sum('scene_change' in r['reasons'] for r in rows)
    diff = sum('last_difference' in r['reasons'] for r in rows)
    print('\n  ' + paint('02 / VISUAL INDEX', '36'))
    print('  +------------------------------------------+')
    print(f'  | {len(rows):>12,}  selected images          |')
    print('  +------------------------------------------+')
    print(f"  Analysed            : {data['decoded_frames']:,} frames")
    if data.get('scan_version') == SCAN_VERSION and data.get('verified_frames') == data['decoded_frames']:
        print(f"  Full scan           : verified {data['verified_frames']:,} frames")
        print(f"  Last decoded frame  : #{data['decoded_frames']-1:,} at {clock(data.get('last_decoded_time'))}")
    else:
        print('  Full scan           : legacy index, not independently verified')
    print(f"  Last selected frame : #{rows[-1]['frame']:,} at {clock(rows[-1].get('time_seconds'))}")
    print(f"  Final frame included: {'yes' if rows[-1]['frame'] == data['decoded_frames']-1 else 'no'}")
    print(f'  Scene criterion     : {scene:,}')
    print(f'  Difference criterion: {diff:,}  (criteria may overlap)')
    print(f'  Index               : {path or "Ready to save with this run"}')
    print('  JPEGs written       : 0')


def export_settings():
    w = choose('JPEG resolution (maximum width, no upscaling)',
               ['480 px', '800 px', '1280 px', 'Original resolution'], 1)
    q = choose('JPEG compression', ['2  | high quality', '5  | balanced',
                                   '8  | smaller files', '12 | strong compression'], 1)
    return [480, 800, 1280, None][w], [2, 5, 8, 12][q]


def select_expression(ids):
    # Compress consecutive frame numbers; use a balanced decision tree so large
    # indices do not need an enormous linear sum of FFmpeg expressions.
    ranges = []
    for i in ids:
        if ranges and i == ranges[-1][1]+1:
            ranges[-1] = (ranges[-1][0], i)
        else:
            ranges.append((i, i))
    def tree(lo, hi):
        if hi-lo == 1:
            a, b = ranges[lo]
            return f'eq(n,{a})' if a == b else f'between(n,{a},{b})'
        mid = (lo+hi)//2
        return f'if(lt(n,{ranges[mid][0]}),{tree(lo,mid)},{tree(mid,hi)})'
    return tree(0, len(ranges))


def profile_name(cfg):
    values = tuple(cfg[k] for k in ('last_threshold', 'scene_threshold', 'scene_guard'))
    for name, a, b, c, _ in PRESETS:
        if values == (a, b, c):
            return name
    # Include a compact fingerprint so different custom settings do not collide.
    digest = hashlib.sha256(json.dumps(values).encode('ascii')).hexdigest()[:8]
    return 'custom-' + digest


def run_name(data, width=None, quality=None):
    name = profile_name(data['thresholds'])
    if data['thresholds'].get('include_final') is False:
        name += '-distinct'
    if quality is None:
        return name + '-index'
    return f'{name}-{width if width is not None else "orig"}-q{quality}'


def present(path):
    return path.exists() or path.is_symlink()


def reusable_indices(root, video, explicit=None):
    candidates = []
    if explicit is not None:
        candidates.append(explicit)
    if root.is_dir():
        candidates.extend(p / 'index.json' for p in root.iterdir()
                          if p.is_dir() and not p.is_symlink() and not p.name.startswith('.'))
        candidates.append(root / 'index.json')  # Legacy export layout.
    candidates.append(video.with_suffix(video.suffix + '.frames.json'))
    found = []
    for path in dict.fromkeys(candidates):
        if not path.is_file():
            continue
        try:
            data = load_index(path, video)
            found.append((path, data, path.stat().st_mtime_ns))
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(f'  Skipping index {path}: {e}')
    found.sort(key=lambda item: (item[0] == explicit, item[2]), reverse=True)
    return [(path, data) for path, data, _ in found]


def safe_to_replace(path, video):
    # Never remove unrelated files, symlinks or folders. Only replace a recognised
    # Framewall run with exactly its index and recorded JPEGs (missing JPEGs OK).
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        index = path / 'index.json'
        if index.is_symlink():
            return False
        data = load_index(index, video)
        allowed = {'index.json'}
        for row in data['frames']:
            name = row.get('filename')
            if name and re.fullmatch(r'image_[0-9]{6,}\.jpg', name):
                allowed.add(name)
        return all(p.name in allowed and p.is_file() and not p.is_symlink()
                   for p in path.iterdir())
    except (OSError, ValueError, KeyError, TypeError):
        return False


def choose_destination(root, name, video):
    base = root / name
    if not present(base):
        return base, False
    # A different settings combination gets its own folder automatically.
    i = 2
    while present(root / f'{name}_v{i}'):
        i += 1
    new = root / f'{name}_v{i}'
    print(f'\n  Existing run: {base}')
    options = [f'Create new version (recommended) -> {new.name}']
    # Permit replacing any matching version, not just the original.
    existing = [base] + sorted((p for p in root.iterdir()
                                if re.fullmatch(re.escape(name) + r'_v[0-9]+', p.name)),
                               key=lambda p: int(p.name.rsplit('_v', 1)[1]))
    replaceable = [p for p in existing if safe_to_replace(p, video)]
    options += [f'Overwrite {p.name}' for p in replaceable]
    options.append('Cancel (keep existing runs unchanged)')
    action = choose('Existing export detected', options, 0)
    if action == 0:
        return new, False
    if action == len(options) - 1:
        return None, False
    target = replaceable[action - 1]
    if choose(f'Replace all Framewall files in {target.name}?',
              ['No, keep existing run', 'Yes, overwrite'], 0) == 1:
        return target, True
    return None, False


def commit_run(stage, output, video, overwrite):
    if not overwrite:
        if present(output):
            raise RuntimeError('The output path was created during export.')
        stage.rename(output)
        return
    if not safe_to_replace(output, video):
        raise RuntimeError('Existing run is no longer safe to overwrite.')
    # Stage the complete replacement first; keep a recovery copy next to the
    # output (outside the temporary staging directory) until commit succeeds.
    backup_dir = Path(tempfile.mkdtemp(prefix='.framewall-backup-', dir=output.parent))
    backup = backup_dir / output.name
    try:
        output.rename(backup)
        try:
            stage.rename(output)
        except BaseException:
            if not present(output):
                backup.rename(output)
            raise
    finally:
        if backup.exists():
            if stage.exists():
                print(f'  Recovery copy retained: {backup}', file=sys.stderr)
            else:
                shutil.rmtree(backup_dir)
        else:
            backup_dir.rmdir()


def keep_index(root, data, video):
    output, overwrite = choose_destination(root, run_name(data), video)
    if output is None:
        print('  Cancelled. Existing runs unchanged.')
        return
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.framewall-index-', dir=root) as work:
        stage = Path(work) / 'run'
        stage.mkdir()
        save_index(stage / 'index.json', data)
        commit_run(stage, output, video, overwrite)
    print(f'\n  Index saved: {output / "index.json"}\n  No JPEGs exported.')


def export(video, index_path, output, width, quality, overwrite=False):
    # Re-read the saved index; no image comparisons are performed on this pass.
    data = load_index(index_path, video)
    ids = [r['frame'] for r in data['frames']]
    output.parent.mkdir(parents=True, exist_ok=True)
    print('\n  ' + paint('03 / JPEG EXPORT', '36'))
    with tempfile.TemporaryDirectory(prefix='.framewall-export-', dir=output.parent) as work:
        work = Path(work)
        stage = work / 'images'
        stage.mkdir()
        vf = "select='" + select_expression(ids) + "'"
        if width is not None:
            vf += f",scale=w='min(iw,{width})':h=-2:flags=lanczos"
        graph = work / 'export.filter'
        graph.write_text(vf, encoding='ascii')
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-xerror',
               '-i', str(video), '-map', '0:V:0', '-an', '-sn', '-dn',
               '-/filter:v', str(graph), '-fps_mode', 'passthrough',
               '-c:v', 'mjpeg', '-q:v', str(quality), '-start_number', '1',
               '-progress', 'pipe:1', '-nostats', str(stage / 'image_%06d.jpg')]
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True) as proc:
            try:
                for line in proc.stdout:
                    if line.startswith('frame='):
                        done = int(line.split('=', 1)[1])
                        print(f'\r  {done:,} / {len(ids):,} JPEGs', end='', flush=True)
                rc = proc.wait()
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
        print()
        files = sorted(stage.glob('image_*.jpg'), key=lambda p: int(p.stem.split('_')[-1]))
        if rc or len(files) != len(ids):
            raise RuntimeError(f'Export failed: {len(files)} of {len(ids)} images produced. Partial output was discarded.')
        if fingerprint(video) != data['source']:
            raise RuntimeError('The video changed during export.')
        # Keep an exact mapping from filenames to source frames with the output.
        mapping = dict(data, export=dict(width=width, jpeg_q=quality),
                       frames=[dict(row, filename=f.name) for row, f in zip(data['frames'], files)])
        save_index(stage / 'index.json', mapping)
        commit_run(stage, output, video, overwrite)
    print(f'\n  {len(ids):,} JPEGs saved: {output}\n  Frame mapping: {output / "index.json"}\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', type=Path, help='Input video')
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument('--index', type=Path, help='Load an existing JSON index')
    source_group.add_argument('--fresh', action='store_true', help='Skip existing indices and analyse the entire video again')
    parser.add_argument('--output', type=Path, help='Parent directory for named runs')
    args = parser.parse_args()
    for tool in ('ffmpeg', 'ffprobe'):
        if not shutil.which(tool):
            raise RuntimeError(f'{tool} was not found in PATH. Please install FFmpeg.')
    video = args.video.expanduser().resolve(strict=True)
    if not video.is_file():
        raise RuntimeError('Input is not a file.')
    root = (args.output or video.parent / ('frames_'+video.stem)).expanduser().resolve()
    if present(root) and not root.is_dir():
        raise RuntimeError('Output parent must be a directory.')
    explicit = args.index.expanduser().resolve(strict=True) if args.index else None
    if explicit is not None:
        load_index(explicit, video)  # Fail clearly if an explicitly requested index is invalid.
    banner()
    print(f'  Video: {video.name}\n  Project: {root}')
    data, source_index = None, None
    found = [] if args.fresh else reusable_indices(root, video, explicit)
    if found:
        options = [f'Load {path.parent.name}/{path.name} | {len(d["frames"]):,} frames'
                   + (' | legacy / unverified' if d.get('scan_version') != SCAN_VERSION else '')
                   for path, d in found] + ['Run a new analysis']
        default = 0 if explicit is not None or found[0][1].get('scan_version') == SCAN_VERSION else len(found)
        choice = choose('Existing indices found', options, default)
        if choice < len(found):
            source_index, data = found[choice]
            # Remove stale export information from a reused index.
            data = dict(data)
            data.pop('export', None)
            data['frames'] = [{k: v for k, v in row.items() if k != 'filename'}
                              for row in data['frames']]
    while True:
        if data is None:
            data = analyse(video, settings())
            source_index = None
        report(data, source_index)
        action = choose('Accept this selection?', ['Configure JPEG export',
                                                'Adjust sensitivity / analyse again',
                                                'Keep index only and exit'], 0)
        if action == 1:
            data = None
            continue
        if action == 2:
            keep_index(root, data, video)
            return
        width, quality = export_settings()
        name = run_name(data, width, quality)
        print(f'\n  Export: {len(data["frames"]):,} images | '
              f'{str(width)+" px" if width else "Original"} | JPEG q={quality}\n  Run: {name}')
        if choose('Start export now?', ['Yes, export JPEGs', 'No, keep index only'], 0) != 0:
            keep_index(root, data, video)
            return
        output, overwrite = choose_destination(root, name, video)
        if output is None:
            print('  Cancelled. Existing runs unchanged.')
            return
        # Persist the analysed index before FFmpeg reads it, but do not leave a
        # separate sidecar in the project. The final run contains index.json.
        with tempfile.TemporaryDirectory(prefix='framewall-selection-') as work:
            index = Path(work) / 'index.json'
            save_index(index, data)
            export(video, index, output, width, quality, overwrite)
        return


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\n  Cancelled.', file=sys.stderr)
        sys.exit(130)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as e:
        print(f'\n  Error: {e}', file=sys.stderr)
        sys.exit(1)
