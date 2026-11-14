"""Local request-boundary and media tests; no UCC calls or billable queries."""
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as explorer


class ExplorerTests(unittest.TestCase):
    def setUp(self):
        self.client = explorer.app.test_client()
        self.base = f'http://127.0.0.1:{explorer.PORT}'
        self.headers = {'X-Local-Token': explorer.TOKEN}

    def post(self, path, body, headers=None):
        return self.client.post(path, json=body, headers=self.headers if headers is None else headers, base_url=self.base)

    def test_rejects_foreign_host(self):
        response = self.client.get('/api/corpora', base_url='http://attacker.example')
        self.assertEqual(response.status_code, 403)

    def test_mutations_require_local_token_and_origin(self):
        self.assertEqual(self.post('/api/corpora', {'name': 'demo'}, {}).status_code, 403)
        foreign = {**self.headers, 'Origin': 'https://attacker.example'}
        self.assertEqual(self.post('/api/corpora', {'name': 'demo'}, foreign).status_code, 403)

    def test_query_limits_before_network_access(self):
        valid = {'corpus': 'probio', 'mode': 'ask', 'question': 'What is visible?', 'file_ids': ['file_' + 'a' * 32]}
        for payload in (
            {**valid, 'question': 'é' * 501},
            {**valid, 'file_ids': valid['file_ids'] * 10001},
            {**valid, 'file_ids': []},
            {**valid, 'corpus': '../private'},
            {**valid, 'mode': 'research', 'file_ids': [], 'scope': 'selected'},
        ):
            with self.subTest(payload=payload), patch.object(explorer, 'Client') as client:
                self.assertEqual(self.post('/api/query', payload).status_code, 400)
                client.assert_not_called()

    def test_api_key_not_in_page_or_errors(self):
        with patch.dict(explorer.os.environ, {'UCC_API_KEY': 'secret-test-key'}):
            self.assertEqual(explorer.safe_error(ValueError('oops secret-test-key')), 'oops [redacted]')
            self.assertNotIn(b'secret-test-key', self.client.get('/', base_url=self.base).data)

    def test_cached_media_supports_seeking(self):
        corpus, file_id = 'test', 'file_' + 'a' * 32
        digest = hashlib.sha256(f'{corpus}/{file_id}'.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as directory, patch.object(explorer, 'MEDIA', Path(directory)), \
                patch.object(explorer, 'browser_video', side_effect=lambda path, mime: (path, mime)):
            Path(directory, digest).write_bytes(b'0123456789')
            Path(directory, digest + '.json').write_text(json.dumps({'mime': 'video/mp4'}))
            response = self.client.get(f'/api/content?corpus={corpus}&id={file_id}',
                                       headers={'Range': 'bytes=2-5'}, base_url=self.base)
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.data, b'2345')
            self.assertEqual(response.headers['Content-Range'], 'bytes 2-5/10')
            response.close()

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg is required')
    def test_legacy_video_is_converted_and_seekable_without_changing_original(self):
        corpus, file_id = 'test', 'file_' + 'b' * 32
        digest = hashlib.sha256(f'{corpus}/{file_id}'.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as directory, patch.object(explorer, 'MEDIA', Path(directory)):
            source = Path(directory, digest)
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'color=c=red:s=64x64:r=10', '-t', '1', '-c:v', 'mpeg4',
                            '-f', 'mp4', str(source)], check=True, capture_output=True)
            original = source.read_bytes()
            source.with_suffix('.json').write_text(json.dumps({'mime': 'video/mp4'}))
            response = self.client.get(f'/api/content?corpus={corpus}&id={file_id}',
                                       headers={'Range': 'bytes=0-99'}, base_url=self.base)
            self.assertEqual(response.status_code, 206)
            converted = source.with_suffix('.playback.mp4')
            self.assertEqual(response.data, converted.read_bytes()[:100])
            response.close()
            self.assertEqual(source.read_bytes(), original)
            metadata = json.loads(subprocess.check_output([
                'ffprobe', '-v', 'error', '-show_entries',
                'stream=codec_name,pix_fmt:format=duration', '-of', 'json', str(converted)]))
            self.assertEqual(metadata['streams'][0]['codec_name'], 'h264')
            self.assertEqual(metadata['streams'][0]['pix_fmt'], 'yuv420p')
            self.assertAlmostEqual(float(metadata['format']['duration']), 1, places=1)
            with patch.object(explorer.subprocess, 'run') as transcode:
                self.assertEqual(explorer.browser_video(source, 'video/mp4')[0], converted)
                transcode.assert_not_called()

    def test_unknown_job_returns_clear_error(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(explorer, 'JOBS', Path(directory)):
            response = self.client.get('/api/jobs/' + 'a' * 32, base_url=self.base)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json['error'], 'Query not found.')

    def test_multi_video_search_processes_every_batch(self):
        class Table:
            def __init__(self, rows):
                self.rows = rows
            def to_dicts(self):
                return self.rows
        class Corpus:
            def __init__(self, ids=None):
                self.document_ids = ids or [f'doc_{i:032x}' for i in range(201)]
            def where(self, predicate):
                return self
            def ids(self):
                return self.document_ids
            def documents(self, ids):
                return Corpus(ids)
            def head(self, rows):
                return Table([{'document_id': ident, 'file_id': f'file_{int(ident[4:], 16) % 3:032x}'} for ident in self.document_ids[:rows]])
            def select(self, **columns):
                return Table([{'document_id': ident, **({'score': 0.9} if 'score' in columns else {'answer': 'Visible evidence.'})} for ident in self.document_ids])
        class Client:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def corpus(self, name):
                return Corpus()
        job = {'id': 'a' * 32, 'corpus': 'test', 'file_ids': [], 'mode': 'match', 'question': 'An object is visible.'}
        with tempfile.TemporaryDirectory() as directory, patch.object(explorer, 'JOBS', Path(directory)), patch.object(explorer, 'Client', Client):
            explorer.run_c1(job)
            result = explorer.read_job(job['id'])
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['processed'], 201)
            self.assertEqual(result['document_count'], 201)
            self.assertEqual(len(result['rows']), 201)
            self.assertEqual(result['matched_videos'], 3)


if __name__ == '__main__':
    unittest.main()
