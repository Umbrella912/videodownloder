import os, re, json, uuid, time, threading, subprocess, sys, shutil, zipfile
from collections import deque
import instaloader
import requests
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from flask import Flask, request, jsonify, send_file, send_from_directory
import imageio_ffmpeg

ROOT = Path(__file__).parent
DOWNLOADS = Path(os.environ.get('CLIPNEST_DOWNLOAD_DIR', str(ROOT / 'downloads'))).expanduser()
DOWNLOADS.mkdir(exist_ok=True)
app = Flask(__name__, static_folder='static', static_url_path='/static')
jobs = {}
lock = threading.Lock()
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
JS_RUNTIME = ['--js-runtimes', 'node'] if shutil.which('node') else []

@app.after_request
def allow_extension_origin(response):
    # An unpacked Chrome extension receives an install-specific extension ID.
    origin = request.headers.get('Origin', '')
    if re.fullmatch(r'chrome-extension://[a-p]{32}', origin):
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        response.headers.add('Vary', 'Origin')
    return response

def canonical_url(raw):
    try:
        u = urlparse(raw.strip())
        host = (u.hostname or '').lower().rstrip('.')
        if u.scheme not in ('http', 'https') or u.username or u.password or u.port not in (None, 80, 443): return None
        if host in ('youtu.be', 'www.youtu.be'):
            video_id = u.path.strip('/').split('/')[0]
        elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
            parts = u.path.strip('/').split('/')
            if u.path.rstrip('/') == '/watch': video_id = parse_qs(u.query).get('v', [''])[0]
            elif len(parts) >= 2 and parts[0] in ('shorts', 'live', 'embed', 'v'): video_id = parts[1]
            else: return None
        elif host in ('twitch.tv', 'www.twitch.tv', 'm.twitch.tv', 'clips.twitch.tv'):
            parts = [p for p in u.path.split('/') if p]
            if host == 'clips.twitch.tv':
                if len(parts) in (1, 2) and all(re.fullmatch(r'[A-Za-z0-9_-]{1,150}', part) for part in parts):
                    return 'https://clips.twitch.tv/' + '/'.join(parts)
                return None
            if len(parts) == 2 and parts[0] == 'videos' and re.fullmatch(r'\d{1,20}', parts[1]):
                return f'https://www.twitch.tv/videos/{parts[1]}'
            if len(parts) == 3 and parts[1] == 'v' and re.fullmatch(r'\d{1,20}', parts[2]) and re.fullmatch(r'[A-Za-z0-9_]{1,25}', parts[0]):
                return f'https://www.twitch.tv/videos/{parts[2]}'
            if len(parts) == 3 and parts[1] == 'clip' and re.fullmatch(r'[A-Za-z0-9_]{1,25}', parts[0]) and re.fullmatch(r'[A-Za-z0-9_-]{1,150}', parts[2]):
                return f'https://www.twitch.tv/{parts[0]}/clip/{parts[2]}'
            if len(parts) == 1 and re.fullmatch(r'[A-Za-z0-9_]{1,25}', parts[0]) and parts[0].lower() not in ('directory', 'videos', 'settings', 'downloads', 'login', 'signup', 'search', 'p'):
                return f'https://www.twitch.tv/{parts[0]}'
            return None
        elif host in ('tiktok.com', 'www.tiktok.com', 'm.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'):
            parts = [p for p in u.path.split('/') if p]
            if host in ('vm.tiktok.com', 'vt.tiktok.com'):
                if len(parts) == 1 and re.fullmatch(r'[A-Za-z0-9]{3,40}', parts[0]): return f'https://{host}/{parts[0]}'
                return None
            if len(parts) == 2 and parts[0] == 't' and re.fullmatch(r'[A-Za-z0-9]{3,40}', parts[1]):
                return f'https://www.tiktok.com/t/{parts[1]}'
            if len(parts) == 3 and re.fullmatch(r'@[A-Za-z0-9_.-]{1,100}', parts[0]) and parts[1] == 'video' and re.fullmatch(r'\d{5,30}', parts[2]):
                return f'https://www.tiktok.com/{parts[0]}/video/{parts[2]}'
            if len(parts) == 2 and re.fullmatch(r'@[A-Za-z0-9_.-]{1,100}', parts[0]) and parts[1] == 'live':
                return f'https://www.tiktok.com/{parts[0]}/live'
            if len(parts) == 3 and parts[:2] == ['share', 'video'] and re.fullmatch(r'\d{5,30}', parts[2]):
                return f'https://www.tiktok.com/share/video/{parts[2]}'
            if len(parts) == 3 and parts[:2] == ['share', 'live'] and re.fullmatch(r'\d{5,30}', parts[2]):
                return f'https://m.tiktok.com/share/live/{parts[2]}'
            return None
        elif host in ('facebook.com', 'www.facebook.com', 'm.facebook.com', 'web.facebook.com', 'fb.com', 'www.fb.com', 'fb.watch', 'www.fb.watch'):
            parts = [p for p in u.path.split('/') if p]
            if host in ('fb.watch', 'www.fb.watch'):
                if len(parts) == 1 and re.fullmatch(r'[A-Za-z0-9_-]{3,100}', parts[0]): return f'https://fb.watch/{parts[0]}/'
                return None
            # Accept only common video/post paths and known share links, never arbitrary FB redirects.
            if len(parts) == 3 and parts[0] == 'share' and parts[1] in ('v', 'r', 'p') and re.fullmatch(r'[A-Za-z0-9_-]{3,100}', parts[2]):
                return f'https://www.facebook.com/share/{parts[1]}/{parts[2]}/'
            if len(parts) == 2 and parts[0] == 'reel' and re.fullmatch(r'\d{4,30}', parts[1]):
                return f'https://www.facebook.com/reel/{parts[1]}/'
            ident = r'(?:\d{4,30}|pfbid[A-Za-z0-9]{8,150})'
            if re.fullmatch(rf'/(?:[A-Za-z0-9_.-]+/(?:videos/(?:[A-Za-z0-9_.-]+/)?|posts/)|groups/[A-Za-z0-9_.-]+/(?:posts|permalink)/){ident}/?', u.path):
                return 'https://www.facebook.com' + u.path.rstrip('/') + '/'
            if u.path.rstrip('/') in ('/watch', '/watch/live', '/video.php', '/video/video.php', '/story.php', '/permalink.php', '/photo.php'):
                query = parse_qs(u.query)
                for key in ('v', 'video_id', 'story_fbid'):
                    value = query.get(key, [''])[0]
                    if re.fullmatch(ident, value):
                        owner = query.get('id', [''])[0]
                        extra = '&id=' + owner if re.fullmatch(r'\d{1,30}', owner) else ''
                        return f'https://www.facebook.com{u.path.rstrip("/")}?{key}={value}{extra}'
            return None
        elif host in ('instagram.com', 'www.instagram.com', 'm.instagram.com'):
            parts = [p for p in u.path.split('/') if p]
            if len(parts) >= 2 and parts[0] in ('p', 'reel', 'reels', 'tv') and re.fullmatch(r'[A-Za-z0-9_-]{3,40}', parts[1]):
                return f'https://www.instagram.com/{parts[0]}/{parts[1]}/'
            if len(parts) >= 3 and parts[0] == 'stories' and re.fullmatch(r'[A-Za-z0-9_.]{1,30}', parts[1]) and re.fullmatch(r'[0-9]{1,30}', parts[2]):
                return f'https://www.instagram.com/stories/{parts[1]}/{parts[2]}/'
            return None
        else: return None
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id): return None
        return 'https://www.youtube.com/watch?v=' + video_id
    except (ValueError, AttributeError): return None

def classify_ytdlp_error(text):
    """Classify safe, user-actionable failures without exposing media URLs or credentials."""
    text = (text or '').lower()
    if any(x in text for x in ('sign in to confirm', 'confirm you’re not a bot', "confirm you're not a bot", 'login required', 'private video', 'private content', 'members-only', 'subscriber-only', 'age-restricted', 'age restricted', 'ip address is blocked', 'http error 403', 'http error 429', 'geo-restricted', 'geo restricted')):
        return 'restricted'
    if any(x in text for x in ('no space left on device', 'disk full', 'permission denied', 'access is denied')):
        return 'storage'
    if any(x in text for x in ('timed out', 'timeout', 'connection reset', 'connection aborted', 'temporary failure', 'unable to resolve', 'name resolution', 'network is unreachable', 'connection refused', 'remote end closed')):
        return 'network'
    if any(x in text for x in ('requested format is not available', 'no video formats', 'requested format not available', 'format selection failed')):
        return 'format'
    return 'other'


def download_error_message(kind):
    if kind == 'restricted': return 'The platform denied access or requires verification. Only accessible public media can be downloaded; switching formats will not remove this restriction.'
    if kind == 'storage': return 'The download could not be saved. Check free disk space and folder permissions.'
    if kind == 'network': return 'The connection failed or timed out. Check your internet connection and try again.'
    if kind == 'format': return 'No compatible format was available for the requested quality and section. Try a different quality or a full video.'
    return 'The download failed after compatible format attempts. The media may be unavailable or unsupported; check that yt-dlp is up to date and try another public link.'


def extract_instagram(url):
    parts = urlparse(url).path.strip('/').split('/')
    if parts[0] == 'stories':
        raise RuntimeError('Instagram Stories generally require an account login. This app does not access private accounts or accept Instagram credentials.')
    shortcode = parts[1]
    try:
        loader = instaloader.Instaloader(quiet=True, max_connection_attempts=1, request_timeout=15)
        post = instaloader.Post.from_shortcode(loader.context, shortcode)
        if post.typename == 'GraphSidecar':
            items = [{'type': 'video' if n.is_video else 'photo', 'url': n.video_url if n.is_video else n.display_url} for n in post.get_sidecar_nodes()]
        else:
            items = [{'type': 'video' if post.is_video else 'photo', 'url': post.video_url if post.is_video else post.url}]
        if not items or any(not item['url'] for item in items):
            raise RuntimeError('This Instagram post has no downloadable media available.')
        if len(items) > 20: raise RuntimeError('This post contains too many items to download.')
        caption = post.caption or 'Instagram post'
        return {'id': shortcode, 'title': caption[:140], 'channel': 'Instagram', 'source': 'Instagram', 'duration': None, 'thumbnail': post.url, 'is_live': False, 'heights': [], 'has_audio': any(i['type'] == 'video' for i in items), 'media_count': len(items), 'media_types': sorted({i['type'] for i in items}), '_items': items}
    except RuntimeError:
        raise
    except Exception:
        # Instaloader's anonymous post endpoint may fail even for public videos.
        # yt-dlp uses a different public extraction path; never supply credentials.
        return extract_instagram_video_fallback(url)

def extract_instagram_video_fallback(url):
    cmd = [sys.executable, '-m', 'yt_dlp', *JS_RUNTIME, '--dump-single-json', '--skip-download', '--no-warnings', '--socket-timeout', '15', '--retries', '2', '--', url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError('Instagram did not make this post available. It may require login, be private, deleted, or blocked from this network. Both public access methods failed; try an accessible public video.')
        data = json.loads(proc.stdout)
        if data.get('_type') in ('playlist', 'multi_video') or data.get('entries'):
            raise RuntimeError('Instagram returned a multi-item post, but its public video fallback cannot safely download the whole carousel. Try an accessible single video or Reel.')
        formats = data.get('formats') or []
        if not formats and not data.get('url'):
            raise RuntimeError('Instagram did not return a downloadable video. The public fallback does not support photo-only posts.')
        # Some Instagram direct formats omit codec details. Only claim audio if
        # it is known to exist or the audio codec was not specified.
        has_audio = any(f.get('acodec') != 'none' for f in formats)
        return {'id': data.get('id'), 'title': data.get('title') or 'Instagram video', 'channel': data.get('channel') or data.get('uploader') or 'Instagram', 'source': 'Instagram', 'duration': data.get('duration'), 'thumbnail': data.get('thumbnail'), 'is_live': False, 'heights': [], 'has_audio': has_audio, 'media_count': 1, 'media_types': ['video'], 'delivery_mode': 'public-video-fallback', '_download_engine': 'yt-dlp'}
    except subprocess.TimeoutExpired:
        raise RuntimeError('Instagram took too long to respond. Please try again.')
    except (ValueError, KeyError):
        raise RuntimeError('Instagram returned an unreadable response. Please try again later.')

def extract_info(url):
    host = urlparse(url).hostname
    if host == 'www.instagram.com': return extract_instagram(url)
    if host in ('www.twitch.tv', 'clips.twitch.tv'): source = 'Twitch'
    elif host and (host == 'tiktok.com' or host.endswith('.tiktok.com')): source = 'TikTok'
    elif host in ('www.facebook.com', 'fb.watch'): source = 'Facebook'
    else: source = 'YouTube'
    cmd = [sys.executable, '-m', 'yt_dlp', *JS_RUNTIME, '--dump-single-json', '--skip-download', '--no-playlist', '--no-warnings', '--socket-timeout', '15', '--retries', '2', '--', url]
    # Retry only transient metadata/network failures, not authentication or access blocks.
    for attempt in range(2):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=50)
        except subprocess.TimeoutExpired:
            if attempt == 0: continue
            raise
        if proc.returncode == 0 or attempt == 1 or classify_ytdlp_error(proc.stderr) != 'network':
            break
    if proc.returncode != 0:
        if source == 'YouTube' and ('sign in to confirm' in proc.stderr.lower() or 'confirm you’re not a bot' in proc.stderr.lower()):
            raise RuntimeError('YouTube is asking this server to sign in to confirm it is not a bot. Downloads are unavailable from this server right now; try running Clipnest on your own computer.')
        if source == 'Twitch':
            raise RuntimeError('Could not retrieve this Twitch content. The channel may be offline, or the video may be expired, subscriber-only, restricted, or unavailable from this server.')
        if source == 'TikTok':
            if 'ip address is blocked' in proc.stderr.lower():
                raise RuntimeError('TikTok blocked this server from accessing the video. Try a public video from another network or run Clipnest on your own computer; access is not guaranteed.')
            raise RuntimeError('Could not retrieve this TikTok video. It may be private, restricted, unavailable, or blocked from this server. Photo slideshows are not supported.')
        if source == 'Facebook':
            raise RuntimeError('Could not retrieve this Facebook video. It may require login, be private, restricted, deleted, or blocked from this server. Photo-only posts are not supported.')
        raise RuntimeError('Could not retrieve this video. It may be private, unavailable, or restricted by YouTube.')
    data = json.loads(proc.stdout)
    if data.get('_type') == 'playlist': raise RuntimeError('Please use a link to a single video.')
    formats = data.get('formats') or []
    # Some platforms omit codec metadata even when a format contains video and audio.
    heights = sorted({int(f['height']) for f in formats if f.get('height') and f.get('vcodec') != 'none' and f.get('height') <= 4320}, reverse=True)
    has_audio = any(f.get('acodec') not in (None, 'none') for f in formats)
    if source in ('Twitch', 'TikTok', 'Facebook') and not has_audio:
        has_audio = any(f.get('acodec') is None for f in formats)
    return {'id': data.get('id'), 'title': data.get('title') or 'Untitled video', 'channel': data.get('channel') or data.get('uploader') or source, 'duration': data.get('duration'), 'thumbnail': data.get('thumbnail'), 'is_live': data.get('is_live', False), 'live_status': data.get('live_status'), 'heights': heights, 'has_audio': has_audio, 'source': source}

@app.get('/')
def index(): return send_from_directory(ROOT / 'static', 'index.html')

@app.get('/api/health')
def health(): return jsonify(service='clipnest-local', status='ok')

@app.post('/api/info')
def info():
    raw = (request.get_json(silent=True) or {}).get('url', '')
    url = canonical_url(raw)
    if not url:
        try:
            parsed = urlparse(raw.strip())
            if (parsed.hostname or '').lower() in ('tiktok.com', 'www.tiktok.com', 'm.tiktok.com') and '/photo/' in parsed.path:
                return jsonify(error='TikTok photo slideshows are not supported. Paste a video or live link instead.'), 400
        except (ValueError, AttributeError):
            pass
        return jsonify(error='Enter a supported YouTube, Instagram, Twitch, TikTok, or Facebook video link.'), 400
    try:
        data = extract_info(url)
        return jsonify({k: v for k, v in data.items() if not k.startswith('_')})
    except subprocess.TimeoutExpired: return jsonify(error='The platform took too long to respond. Please try again.'), 504
    except Exception as e: return jsonify(error=str(e) or 'Could not inspect this video.'), 400

def run_ytdlp_attempt(cmd, folder, job_id, timeout=1200):
    """Run a yt-dlp attempt with a real wall-clock deadline and bounded diagnostics."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, errors='replace')
    lines = deque(maxlen=30)
    def read_output():
        try:
            for line in proc.stdout:
                lines.append(line.strip()[:1000])
                match = re.search(r'\[download\]\s+(\d+(?:\.\d+)?)%', line)
                if match:
                    with lock:
                        if job_id in jobs: jobs[job_id]['progress'] = min(99, float(match.group(1)))
        except (OSError, ValueError):
            pass
    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    try:
        try: proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            reader.join(timeout=5)
            return None, 'download timed out'
        reader.join(timeout=5)
        return proc.returncode, '\n'.join(lines)
    finally:
        if proc.stdout is not None:
            try: proc.stdout.close()
            except OSError: pass


def download_commands(url, kind, quality, is_live, section, attempt_folder, alternative=False):
    out = str(attempt_folder / '%(title).100B [%(id)s].%(ext)s')
    cmd = [sys.executable, '-m', 'yt_dlp', *JS_RUNTIME, '--no-playlist', '--no-warnings', '--newline', '--socket-timeout', '20', '--retries', '3', '--fragment-retries', '3', '--ffmpeg-location', FFMPEG, '-o', out]
    if is_live:
        cmd += ['--downloader', 'ffmpeg', '--downloader-args', 'ffmpeg_i:-t 00:01:00']
    if section is not None:
        cmd += ['--download-sections', f"*{section['start']}-{section['end']}", '--force-keyframes-at-cuts']
    if kind == 'audio':
        # The alternate prioritizes a combined A/V stream over a separate audio stream.
        fmt = 'best[acodec!=?none]/bestaudio' if alternative else 'bestaudio/best'
        cmd += ['-f', fmt, '-x', '--audio-format', 'mp3', '--audio-quality', str(quality) + 'K']
    else:
        h = int(quality)
        if h == 0 and urlparse(url).hostname == 'www.instagram.com':
            fmt = ('best[vcodec!=?none][acodec!=?none]/bestvideo+bestaudio' if alternative else
                   'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best/bestvideo+bestaudio')
        else:
            # Preserve the selected maximum height where known; unknown heights remain eligible.
            lim = f'[height<=?{h}]'
            fmt = (f'best{lim}[vcodec!=?none][acodec!=?none]/bestvideo{lim}+bestaudio' if alternative else
                   f'bestvideo{lim}[ext=mp4]+bestaudio[ext=m4a]/bestvideo{lim}+bestaudio/best{lim}[vcodec!=?none][acodec!=?none]')
        cmd += ['-f', fmt, '--merge-output-format', 'mp4', '--remux-video', 'mp4']
    return cmd + ['--', url]


def run_download(job_id, url, kind, quality, is_live, section=None):
    folder = DOWNLOADS / job_id
    folder.mkdir(exist_ok=True)
    last_error = 'other'
    try:
        # Retry a recorded item once with a different public stream-selection strategy.
        # Never weaken a selected section, requested output type, or live recording limit.
        for attempt in range(1 if is_live else 2):
            attempt_folder = folder / ('attempt-' + str(attempt + 1))
            attempt_folder.mkdir(exist_ok=True)
            if attempt:
                with lock: jobs[job_id].update(progress=0, message='Trying another compatible stream…')
            cmd = download_commands(url, kind, quality, is_live, section, attempt_folder, alternative=bool(attempt))
            code, output = run_ytdlp_attempt(cmd, attempt_folder, job_id)
            allowed_ext = ('.mp3',) if kind == 'audio' else ('.mp4', '.mkv', '.webm', '.mov')
            files = [p for p in attempt_folder.iterdir() if p.is_file() and p.suffix.lower() in allowed_ext and p.stat().st_size > 0]
            if code == 0 and files:
                with lock: jobs[job_id].update(state='ready', progress=100, file=str(max(files, key=lambda p: p.stat().st_size)), message='')
                return
            last_error = classify_ytdlp_error(output)
            shutil.rmtree(attempt_folder, ignore_errors=True)
            # Retrying cannot overcome access restrictions or missing disk space.
            if last_error in ('restricted', 'storage'): break
        with lock: jobs[job_id].update(state='error', error=download_error_message(last_error), message='')
    except Exception:
        with lock: jobs[job_id].update(state='error', error='Could not prepare the download. Check the available disk space and try again.', message='')


def fetch_instagram_media(url, dest):
    # Only accept Instagram/Facebook CDN URLs and validate every redirect.
    allowed = ('cdninstagram.com', 'fbcdn.net', 'instagram.com', 'facebook.com', 'fbsbx.com')
    for _ in range(5):
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()
        if parsed.scheme != 'https' or not any(host == d or host.endswith('.' + d) for d in allowed):
            raise RuntimeError('Instagram returned an unsupported media URL.')
        with requests.get(url, stream=True, timeout=(10, 60), allow_redirects=False) as response:
            if response.is_redirect:
                from urllib.parse import urljoin
                url = urljoin(url, response.headers.get('Location', ''))
                continue
            response.raise_for_status()
            size = 0
            with open(dest, 'wb') as f:
                for chunk in response.iter_content(1024 * 256):
                    size += len(chunk)
                    if size > 500 * 1024 * 1024: raise RuntimeError('An Instagram item exceeded the 500 MB limit.')
                    f.write(chunk)
            return
    raise RuntimeError('Too many redirects while retrieving Instagram media.')

def media_duration(path):
    # imageio-ffmpeg provides ffmpeg, not necessarily ffprobe.
    result = subprocess.run([FFMPEG, '-i', str(path)], capture_output=True, text=True, timeout=30)
    match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', result.stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

def run_instagram_download(job_id, metadata, kind, quality, section=None, source_url=None):
    folder = DOWNLOADS / job_id
    folder.mkdir(exist_ok=True)
    output_files = []
    try:
        items = metadata['_items']
        if kind == 'audio' or section is not None: items = [item for item in items if item['type'] == 'video']
        if not items: raise RuntimeError('This post does not contain a video to trim or extract audio from.')
        for index, item in enumerate(items, 1):
            ext = 'mp4' if item['type'] == 'video' else 'jpg'
            path = folder / f"instagram_{metadata['id']}_{index}.{ext}"
            try:
                fetch_instagram_media(item['url'], path)
            except requests.RequestException as exc:
                # Only a single video may safely use the already-supported public yt-dlp fallback.
                # Never replace a carousel with one item or attempt to defeat access blocks.
                response = getattr(exc, 'response', None)
                status = getattr(response, 'status_code', None)
                if source_url and len(metadata['_items']) == 1 and item['type'] == 'video' and status not in (401, 403, 429):
                    try:
                        fallback = extract_instagram_video_fallback(source_url)
                    except Exception as fallback_error:
                        raise RuntimeError('Instagram could not retrieve this public video using either supported method. Check your connection and the post availability.') from fallback_error
                    duration = fallback.get('duration')
                    if section is not None and duration is not None and section['end'] > duration:
                        raise RuntimeError('The end time exceeds the video duration.')
                    with lock: jobs[job_id].update(progress=0, message='Trying the public video fallback…')
                    run_download(job_id, source_url, kind, quality, False, section)
                    return
                raise
            if section is not None:
                duration = media_duration(path)
                if duration is not None and section['end'] > duration + 0.01:
                    raise RuntimeError(f'The end time exceeds the duration of Instagram video {index} ({duration:.2f} seconds). Choose a range within every video.')
            if kind == 'audio' or section is not None:
                output = path.with_suffix('.mp3') if kind == 'audio' else path.with_name(path.stem + '_clip.mp4')
                cmd = [FFMPEG, '-y', '-i', str(path)]
                if section is not None: cmd += ['-ss', str(section['start']), '-t', str(section['end'] - section['start'])]
                if kind == 'audio': cmd += ['-vn', '-codec:a', 'libmp3lame', '-b:a', str(quality) + 'k']
                else: cmd += ['-map', '0:v:0', '-map', '0:a?', '-c:v', 'libx264', '-c:a', 'aac', '-movflags', '+faststart']
                result = subprocess.run(cmd + [str(output)], capture_output=True, timeout=600)
                if result.returncode != 0 or not output.exists() or output.stat().st_size == 0:
                    raise RuntimeError('Could not prepare this Instagram clip. The video may lack audio or use an unsupported format.')
                path.unlink()
                path = output
            output_files.append(path)
            with lock: jobs[job_id]['progress'] = min(99, round(index / len(items) * 100))
        if len(output_files) > 1:
            output = folder / f"instagram_{metadata['id']}.zip"
            with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
                for path in output_files: archive.write(path, path.name)
        else: output = output_files[0]
        with lock: jobs[job_id].update(state='ready', progress=100, file=str(output))
    except Exception as e:
        message = ('Instagram media could not be retrieved. It may be unavailable, restricted, or blocked; check your connection and try again.'
                   if isinstance(e, requests.RequestException) else str(e) or 'Instagram download failed.')
        with lock: jobs[job_id].update(state='error', error=message, message='')

@app.post('/api/download')
def download():
    body = request.get_json(silent=True) or {}
    url = canonical_url(body.get('url', ''))
    kind = body.get('kind')
    quality = body.get('quality')
    if not url or kind not in ('video', 'audio'): return jsonify(error='Invalid download request.'), 400
    try: quality = int(quality)
    except (TypeError, ValueError): return jsonify(error='Select a valid quality.'), 400
    is_instagram = urlparse(url).hostname == 'www.instagram.com'
    if (kind == 'video' and not (quality == 0 if is_instagram else 144 <= quality <= 4320)) or (kind == 'audio' and quality not in (128, 192, 256, 320)):
        return jsonify(error='Select a valid quality.'), 400
    section = body.get('section')
    if section is not None:
        if not isinstance(section, dict):
            return jsonify(error='Provide both a valid start and end time.'), 400
        start, end = section.get('start'), section.get('end')
        if type(start) is not int or type(end) is not int or not (0 <= start < end <= 10_000_000):
            return jsonify(error='Start and end times must be whole seconds, with the end after the start.'), 400
        section = {'start': start, 'end': end}
    # Fetch server-side metadata rather than trusting live status from the browser.
    try: metadata = extract_info(url)
    except Exception as e: return jsonify(error=str(e) or 'Could not inspect this video.'), 400
    if section is not None:
        if metadata.get('is_live'):
            return jsonify(error='Selecting a section is not available for live streams.'), 400
        if is_instagram and metadata.get('_download_engine') != 'yt-dlp' and not any(item.get('type') == 'video' for item in metadata.get('_items', [])):
            return jsonify(error='This Instagram post has no video to trim.'), 400
        duration = metadata.get('duration')
        if duration is not None and section['end'] > duration:
            return jsonify(error='The end time cannot be later than the video duration.'), 400
    job_id = uuid.uuid4().hex
    with lock: jobs[job_id] = {'state': 'working', 'progress': 0, 'created': time.time()}
    if is_instagram and metadata.get('_download_engine') != 'yt-dlp':
        threading.Thread(target=run_instagram_download, args=(job_id, metadata, kind, quality, section), kwargs={'source_url':url}, daemon=True).start()
    else:
        threading.Thread(target=run_download, args=(job_id, url, kind, quality, metadata.get('is_live'), section), daemon=True).start()
    return jsonify(id=job_id)

@app.get('/api/status/<job_id>')
def status(job_id):
    with lock: job = jobs.get(job_id, {}).copy()
    if not job: return jsonify(error='Download not found.'), 404
    return jsonify(state=job['state'], progress=job.get('progress', 0), error=job.get('error'), message=job.get('message', ''))

@app.get('/api/file/<job_id>')
def file(job_id):
    with lock: job = jobs.get(job_id, {}).copy()
    if job.get('state') != 'ready' or not Path(job.get('file', '')).is_file(): return jsonify(error='File not ready.'), 404
    return send_file(job['file'], as_attachment=True, download_name=Path(job['file']).name)

if __name__ == '__main__': app.run(host=os.environ.get('HOST', '127.0.0.1'), port=int(os.environ.get('PORT', 3000)), threaded=True)
