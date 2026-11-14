"""Loopback-only UCC explorer. Run: .venv/bin/python app.py"""
import hashlib
import base64
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlparse
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException
from ucc import Client, Completion, Facet, Metadata
from ucc.corpus import IncompleteError
from PIL import Image

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
CACHE = ROOT / '.cache'
JOBS = CACHE / 'jobs'
MEDIA = CACHE / 'media'
for folder in (JOBS, MEDIA):
    folder.mkdir(parents=True, exist_ok=True)
TOKEN = secrets.token_urlsafe(32)
PORT = int(os.getenv('PORT', '8765'))
BASE = 'https://api.universalconstructor.co/v1'
POOL = ThreadPoolExecutor(max_workers=2)
job_lock = threading.RLock()
media_locks = {}
file_cache = {}
thumbnail_slots = threading.Semaphore(3)
transcode_slots = threading.Semaphore(2)
cancelled_jobs = set()
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 65 * 1024 * 1024


def plain(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(warnings=False)
    return value


def checked(value, kind='corpus'):
    pattern = r'[A-Za-z0-9_-]{1,122}' if kind == 'corpus' else r'file_[0-9a-f]{32}'
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f'Invalid {kind}.')
    return value


def safe_error(error):
    return str(error).replace(os.getenv('UCC_API_KEY', '') or '\x00', '[redacted]')[:1500]


@app.before_request
def local_only():
    if request.host not in {f'127.0.0.1:{PORT}', f'localhost:{PORT}'}:
        return jsonify(error='Use the local address shown by the server.'), 403
    if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
        if not secrets.compare_digest(request.headers.get('X-Local-Token', ''), TOKEN):
            return jsonify(error='Reload the page before trying again.'), 403
        origin = request.headers.get('Origin')
        if origin and urlparse(origin).netloc != request.host:
            return jsonify(error='Cross-origin requests are disabled.'), 403


@app.after_request
def headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-src 'self'; object-src 'none'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'"
    if request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.errorhandler(Exception)
def error_response(error):
    status = error.code if isinstance(error, HTTPException) else 400 if isinstance(error, ValueError) else 502
    return jsonify(error=safe_error(error)), status


@app.get('/')
def index():
    return render_template('index.html', token=TOKEN)


@app.get('/api/corpora')
def corpora():
    with Client() as client:
        return jsonify(client.with_options(corpus='default').corpora.list())


@app.post('/api/corpora')
def create_corpus():
    name = checked((request.get_json() or {}).get('name'))
    with Client() as client:
        return jsonify(plain(client.with_options(corpus=name).corpora.create(name)))


@app.get('/api/files')
def files():
    corpus = checked(request.args.get('corpus'))
    args = {'limit': 1000}
    if request.args.get('after'):
        args['after'] = checked(request.args['after'], 'file')
    with Client() as client:
        page = client.with_options(corpus=corpus).files.list(**args)
        return jsonify(plain(page))


@app.get('/api/file')
def file_info():
    corpus = checked(request.args.get('corpus'))
    file_id = checked(request.args.get('id'), 'file')
    return jsonify(cached_file(corpus, file_id))


def cached_file(corpus, file_id):
    key = (corpus, file_id)
    cached = file_cache.get(key)
    if cached and time.monotonic() - cached[0] < 120:
        return cached[1]
    with Client() as client:
        value = plain(client.with_options(corpus=corpus).files.retrieve(file_id))
    file_cache[key] = (time.monotonic(), value)
    return value


@app.get('/api/thumbnail')
def thumbnail():
    corpus = checked(request.args.get('corpus'))
    file_id = checked(request.args.get('id'), 'file')
    document_id = request.args.get('document', '')
    if document_id and not re.fullmatch(r'doc_[0-9a-f]{32}', document_id):
        raise ValueError('Invalid document ID.')
    digest = hashlib.sha256(f'thumb/{corpus}/{file_id}/{document_id}'.encode()).hexdigest()
    path = MEDIA / f'{digest}.jpg'
    if not path.exists():
        with thumbnail_slots:
            if not path.exists():
                file = cached_file(corpus, file_id)
                docs = file.get('ucc', {}).get('documents') or []
                selected = next((d for d in docs if d['id'] == document_id), None) if document_id else (docs[0] if docs else None)
                if not selected:
                    raise ValueError('No prepared image in this file.')
                with Client() as client:
                    doc = client.corpus(corpus).document(selected['id'])
                opening, closing = '<｜image｜>', '<｜/image｜>'
                start = doc.text.find(opening)
                end = doc.text.find(closing, start)
                if start < 0 or end < 0:
                    raise ValueError('No visual preview available.')
                pixels = base64.b64decode(doc.text[start + len(opening):end])
                with Image.open(io.BytesIO(pixels)) as img:
                    img = img.convert('RGB')
                    img.thumbnail((360, 225))
                    temporary = path.with_suffix(f'.{uuid4().hex}.tmp')
                    img.save(temporary, format='JPEG', quality=82)
                    temporary.replace(path)
    return send_file(path, mimetype='image/jpeg', conditional=True)


def upstream(path, corpus):
    return httpx.Client(headers={
        'Authorization': f"Bearer {os.environ['UCC_API_KEY']}",
        'X-UCC-Corpus': corpus,
    }, timeout=120, follow_redirects=False)


def browser_video(path, mime):
    """Preserve original bytes; cache an H.264/AAC copy for unsupported codecs."""
    compatible = path.with_suffix('.playback.mp4')
    checked_path = path.with_suffix('.playback.json')
    if compatible.exists():
        return compatible, 'video/mp4'
    if checked_path.exists():
        return path, mime
    ffprobe, ffmpeg = shutil.which('ffprobe'), shutil.which('ffmpeg')
    if not ffprobe or not ffmpeg:
        raise RuntimeError('Video playback requires ffmpeg and ffprobe on the local server.')
    probe = subprocess.run([ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(path)],
                           capture_output=True, text=True, timeout=30)
    if probe.returncode:
        raise RuntimeError('The downloaded video could not be read. Its original file may be damaged.')
    streams = json.loads(probe.stdout).get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    audio = [s for s in streams if s.get('codec_type') == 'audio']
    if not video:
        raise ValueError('This file has no video stream.')
    if (mime == 'video/mp4' and video.get('codec_name') == 'h264'
            and video.get('pix_fmt') == 'yuv420p'
            and all(s.get('codec_name') in {'aac', 'mp3'} for s in audio)):
        checked_path.write_text(json.dumps({'compatible': True}))
        return path, mime
    temporary = path.with_suffix(f'.{uuid4().hex}.mp4')
    try:
        with transcode_slots:
            result = subprocess.run([
                ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(path),
                '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'libx264', '-preset', 'veryfast',
                '-crf', '20', '-pix_fmt', 'yuv420p', '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
                '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(temporary),
            ], capture_output=True, text=True, timeout=900)
        if result.returncode:
            raise RuntimeError('Could not prepare a browser-compatible copy of this video: ' + result.stderr[-400:])
        temporary.replace(compatible)
    finally:
        temporary.unlink(missing_ok=True)
    return compatible, 'video/mp4'


@app.get('/api/content')
def content():
    corpus = checked(request.args.get('corpus'))
    file_id = checked(request.args.get('id'), 'file')
    mode = request.args.get('mode', 'media')
    if mode == 'text':
        with upstream('', corpus) as http:
            with http.stream('GET', f'{BASE}/files/{file_id}/text') as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 200_000:
                        break
        truncated = len(data) > 200_000
        text = bytes(data[:200_000]).decode('utf-8', errors='replace')
        return jsonify(text=text, truncated=truncated)
    digest = hashlib.sha256(f'{corpus}/{file_id}'.encode()).hexdigest()
    path = MEDIA / digest
    meta = MEDIA / f'{digest}.json'
    with job_lock:
        lock = media_locks.setdefault(digest, threading.Lock())
    with lock:
        if not path.exists() or not meta.exists():
            with upstream('', corpus) as http:
                with http.stream('GET', f'{BASE}/files/{file_id}/content') as response:
                    response.raise_for_status()
                    mime = response.headers.get('content-type', 'application/octet-stream').split(';')[0]
                    temporary = path.with_suffix('.part')
                    try:
                        with temporary.open('wb') as handle:
                            for chunk in response.iter_bytes():
                                handle.write(chunk)
                        temporary.replace(path)
                        meta.write_text(json.dumps({'mime': mime}))
                    finally:
                        temporary.unlink(missing_ok=True)
        mime = json.loads(meta.read_text())['mime']
        if mime.startswith('video/'):
            path, mime = browser_video(path, mime)
    safe_inline = mime.startswith(('video/', 'image/')) or mime == 'application/pdf'
    return send_file(path, mimetype=mime if safe_inline else 'application/octet-stream', conditional=True,
                     as_attachment=not safe_inline, download_name=file_id)


@app.post('/api/upload')
def upload():
    corpus = checked(request.form.get('corpus'))
    incoming = request.files.get('file')
    if incoming is None or not incoming.filename:
        raise ValueError('Choose a file to upload.')
    name = Path(incoming.filename).name
    data = incoming.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024:
        raise ValueError('This interface accepts files up to 64 MiB each.')
    with Client() as client:
        file = client.with_options(corpus=corpus).files.create(file=(name, data), purpose='user_data')
        return jsonify(plain(file))


def save_job(job):
    with job_lock:
        path = JOBS / f"{job['id']}.json"
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(job, default=str))
        temp.replace(path)


def read_job(job_id):
    if not re.fullmatch(r'[0-9a-f]{32}', job_id):
        raise ValueError('Invalid query ID.')
    path = JOBS / f'{job_id}.json'
    if not path.exists():
        raise ValueError('Query not found.')
    with job_lock:
        return json.loads(path.read_text())


def run_c1(job):
    try:
        job['status'] = 'running'
        job['rows'] = []
        save_job(job)
        started = time.monotonic()
        with Client() as client:
            corpus = client.corpus(job['corpus'])
            view = corpus
            if job['file_ids']:
                view = view.where(Metadata('file_id', 'in', job['file_ids']))
            if job.get('videos_only'):
                view = view.where(Metadata('video_level', 'ge', 1))
            ids = view.ids()
            if not ids:
                raise ValueError('No prepared segments in this selection. Wait for preparation, then refresh.')
            job.update(document_count=len(ids), processed=0, phase='Searching video segments' if job['mode'] == 'match' else 'Reading segments')
            save_job(job)
            all_rows = []
            for offset in range(0, len(ids), 100):
                if job['id'] in cancelled_jobs:
                    job['status'] = 'cancelled'
                    break
                batch = ids[offset:offset+100]
                metadata = {r['document_id']: r for r in corpus.documents(batch).head(rows=len(batch)).to_dicts()}
                columns = {'answer': Completion(job['question'], max_tokens=200)}
                if job['mode'] == 'match':
                    columns = {'score': Facet('Does this segment show the visual behavior or content requested here? ' + job['question'])}
                try:
                    rows = corpus.documents(batch).select(**columns).to_dicts()
                except IncompleteError as error:
                    rows = error.table.to_dicts() if error.table is not None else []
                    job.update(status='incomplete', error=safe_error(error), request_id=error.request_id)
                all_rows.extend({**metadata.get(row['document_id'], {}), **row} for row in rows)
                job['processed'] = offset + len(batch)
                if job['mode'] == 'match':
                    all_rows.sort(key=lambda r: r.get('score', 0), reverse=True)
                job['rows'] = all_rows
                job['matched_videos'] = len({r.get('file_id') for r in all_rows if r.get('score', 0) >= 0.5})
                save_job(job)
                if job['status'] == 'incomplete':
                    break
            else:
                job['status'] = 'completed'
            if job['mode'] == 'match' and all_rows and job['status'] == 'completed' and job['id'] not in cancelled_jobs:
                job.update(status='running', phase='Checking evidence for the strongest matches')
                save_job(job)
                # Enrich only the best segment per video, rather than generating thousands of captions.
                top = []
                seen = set()
                for row in all_rows:
                    if row.get('score', 0) < 0.5:
                        continue
                    if row.get('file_id') not in seen:
                        top.append(row)
                        seen.add(row.get('file_id'))
                    if len(top) == 20:
                        break
                if top:
                    try:
                        evidence = corpus.documents([r['document_id'] for r in top]).select(
                            answer=Completion('Describe only the visible evidence relevant to this search, including uncertainty: ' + job['question'], max_tokens=180))
                        answers = {r['document_id']: r['answer'] for r in evidence.to_dicts()}
                        for row in all_rows:
                            if row['document_id'] in answers:
                                row['answer'] = answers[row['document_id']]
                    except Exception as error:
                        job['evidence_error'] = safe_error(error)
                job['status'] = 'cancelled' if job['id'] in cancelled_jobs else 'completed'
            job['elapsed'] = round(time.monotonic() - started, 2)
    except Exception as error:
        job.update(status='failed', error=safe_error(error))
    cancelled_jobs.discard(job['id'])
    save_job(job)


@app.post('/api/query')
def query():
    data = request.get_json() or {}
    corpus = checked(data.get('corpus'))
    mode = data.get('mode')
    if mode not in {'ask', 'match', 'research'}:
        raise ValueError('Choose a query mode.')
    question = str(data.get('question', '')).strip()
    limit = 1000 if mode != 'research' else 12000
    if not question or len(question.encode()) > limit:
        raise ValueError(f'Enter a question under {limit} UTF-8 bytes.')
    file_ids = data.get('file_ids', [])
    if not isinstance(file_ids, list) or len(file_ids) > 10000:
        raise ValueError('Select at most 10,000 files.')
    file_ids = [checked(fid, 'file') for fid in file_ids]
    if not file_ids and data.get('scope') != 'dataset':
        raise ValueError('Select at least one ready file.')
    job = dict(id=uuid4().hex, corpus=corpus, mode=mode, question=question,
               file_ids=file_ids, scope=data.get('scope', 'selected'), videos_only=bool(data.get('videos_only')), created_at=time.time(), status='queued')
    if mode == 'research':
        previous_response_id = None
        if data.get('parent_job'):
            parent = read_job(data['parent_job'])
            if (parent['corpus'] == corpus and parent['mode'] == 'research'
                    and parent['status'] == 'completed'
                    and set(parent['file_ids']) == set(file_ids)
                    and parent['scope'] == job['scope']):
                previous_response_id = parent.get('response_id')
                job['parent_job'] = parent['id']
        with Client() as client:
            content = [{'type': 'input_text', 'text': question}]
            content += [{'type': 'input_file', 'file_id': fid} for fid in file_ids]
            response = client.with_options(corpus=corpus).responses.create(
                model='napa', input=[{'role': 'user', 'content': content}],
                previous_response_id=previous_response_id,
                extra_body={'tools': [{'type': 'citations'}, {'type': 'findings'}]}, background=True,
                instructions='Answer concisely. Use a Markdown table when comparing videos or grouping observations. Link supporting source documents using findings; only cite evidence you actually inspected. Distinguish inference from visible facts.',
                extra_headers={'Idempotency-Key': job['id']})
            job.update(response_id=response.id, status=response.status)
        save_job(job)
    else:
        save_job(job)
        POOL.submit(run_c1, job)
    return jsonify(job)


@app.get('/api/jobs')
def list_jobs():
    corpus = checked(request.args.get('corpus'))
    jobs = []
    with job_lock:
        for path in JOBS.glob('*.json'):
            job = json.loads(path.read_text())
            if job['corpus'] == corpus:
                jobs.append({k: job.get(k) for k in ('id', 'question', 'mode', 'status', 'created_at')})
    return jsonify(sorted(jobs, key=lambda j: j['created_at'], reverse=True)[:50])


@app.get('/api/jobs/<job_id>')
def get_job(job_id):
    job = read_job(job_id)
    if job['mode'] == 'research' and job['status'] in {'queued', 'in_progress', 'running'}:
        with Client() as client:
            response = client.with_options(corpus=job['corpus']).responses.retrieve(job['response_id'])
            raw = plain(response)
            job.update(status=response.status, answer=response.output_text,
                       citations=raw.get('ucc', {}).get('citations') or [], findings=raw.get('ucc', {}).get('findings'), phase=raw.get('ucc', {}).get('phase'), error=raw.get('error'))
            save_job(job)
    return jsonify(job)


@app.post('/api/jobs/<job_id>/cancel')
def cancel_job(job_id):
    job = read_job(job_id)
    if job['mode'] != 'research':
        cancelled_jobs.add(job_id)
        return jsonify(status='cancelling', id=job_id)
    with Client() as client:
        response = client.with_options(corpus=job['corpus']).responses.cancel(job['response_id'])
        job['status'] = response.status
        save_job(job)
    return jsonify(job)


if __name__ == '__main__':
    if not os.getenv('UCC_API_KEY'):
        raise SystemExit('Add UCC_API_KEY to .env before starting.')
    # C1 work cannot survive a server restart; never silently submit it again.
    for path in JOBS.glob('*.json'):
        job = json.loads(path.read_text())
        if job['mode'] != 'research' and job['status'] in {'queued', 'running'}:
            job.update(status='interrupted', error='The server restarted. Submit a new query to try again.')
            save_job(job)
    app.run(host='127.0.0.1', port=PORT, threaded=True, debug=False, use_reloader=False)
