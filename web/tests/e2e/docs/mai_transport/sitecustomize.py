"""Replay one recorded MAI transport response in the isolated docs harness.

The fixture is the recorded request/response JSON, including clip_sha256.
No keys or authorization headers belong in this file.
"""
import base64
import hashlib
import json
import os
from pathlib import Path

import httpx

_original_post = httpx.AsyncClient.post


async def _post(self, url, *args, **kwargs):
    if str(url) != 'https://openrouter.ai/api/v1/audio/transcriptions':
        return await _original_post(self, url, *args, **kwargs)
    fixture_path = Path(os.environ.get('FRISKET_DOCS_MAI_FIXTURE', Path(__file__).resolve().parents[1] / 'fixtures' / 'mai-30s-recording.json'))
    fixture = json.loads(fixture_path.read_text())
    body = kwargs.get('json')
    if not isinstance(body, dict) or not isinstance(body.get('input_audio'), dict):
        raise RuntimeError('MAI replay expected JSON input_audio')
    actual = json.loads(json.dumps(body))
    audio_data = actual['input_audio'].pop('data', None)
    try:
        digest = hashlib.sha256(base64.b64decode(audio_data, validate=True)).hexdigest()
    except (TypeError, ValueError) as exc:
        raise RuntimeError('MAI replay received invalid audio') from exc
    expected = json.loads(json.dumps(fixture['request']))
    expected_audio = expected['input_audio'].pop('data', None)
    try:
        expected_digest = hashlib.sha256(base64.b64decode(expected_audio, validate=True)).hexdigest()
    except (TypeError, ValueError) as exc:
        raise RuntimeError('MAI fixture contains invalid audio') from exc
    if digest != fixture['clip_sha256'] or expected_digest != digest or actual != expected:
        raise RuntimeError('MAI replay request does not match the recorded audio and options')
    return httpx.Response(200, json=fixture['response'], request=httpx.Request('POST', str(url)))


httpx.AsyncClient.post = _post
