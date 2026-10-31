import json
import os
import struct
import tempfile
import uuid
from io import StringIO
from unittest.mock import call, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .ai.client import MissingAPIKeyError
from .ai.transcribe import TranscriptionError, transcribe_audio
from .models import AgentCompany, Document, StaffMember

VOICE_MEDIA_ROOT = tempfile.mkdtemp(prefix='voice-test-media')
FRIENDLY = 'We could not hear that. Please type it instead.'


def wav_bytes(channels=1, rate=24000, bits=16, fmt_tag=1, seconds=1):
    samples = rate * seconds
    data = b'\x00' * (samples * channels * bits // 8)
    fmt = struct.pack(
        '<HHIIHH', fmt_tag, channels, rate,
        rate * channels * bits // 8, channels * bits // 8, bits,
    )
    return (
        b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVE'
        + b'fmt ' + struct.pack('<I', 16) + fmt
        + b'data' + struct.pack('<I', len(data)) + data
    )


def ok_response(transcript='  duty paid  '):
    return 200, json.dumps(
        {
            'sessionId': 's', 'transcript': transcript,
            'audioDurationMs': 1000, 'turns': [],
        }
    ).encode('utf-8')


class TranscribeAudioTests(TestCase):
    def _send(self, replies):
        calls = []

        def fake_send(url, body, headers):
            calls.append((url, body, headers))
            return replies.pop(0)

        return calls, fake_send

    def test_sends_reference_request(self):
        wav = wav_bytes()
        calls, fake_send = self._send([ok_response()])
        with patch(
            'tracking.ai.transcribe._send', side_effect=fake_send
        ):
            with override_settings(
                META_API_KEY='k', META_API_BASE_URL='https://api.meta.ai/v1'
            ):
                text = transcribe_audio(wav)
        self.assertEqual(text, 'duty paid')
        self.assertEqual(len(calls), 1)
        url, body, headers = calls[0]
        self.assertTrue(
            url.startswith('https://api.meta.ai/v1/asr/transcribe?sessionId=')
        )
        uuid.UUID(url.split('sessionId=')[1])
        self.assertEqual(headers['Authorization'], 'Bearer k')
        self.assertEqual(headers['Accept'], 'application/json')
        start = body.index(b'{')
        end = body.rindex(b'}') + 1
        self.assertEqual(
            json.loads(body[start:end]),
            {
                'mode': 'PUSH_TO_TALK',
                'model': 'muse-voice-transcribe-1.0',
                'audioEncoding': 'WAV',
            },
        )
        self.assertIn(b'name="request"', body)
        self.assertIn(b'name="audio"', body)
        self.assertIn(wav, body)

    def test_mode_is_a_setting(self):
        calls, fake_send = self._send([ok_response()])
        with patch(
            'tracking.ai.transcribe._send', side_effect=fake_send
        ):
            with override_settings(
                META_API_KEY='k', META_ASR_MODE='ENDPOINTING'
            ):
                transcribe_audio(wav_bytes())
        body = calls[0][1]
        payload = json.loads(body[body.index(b'{'):body.rindex(b'}') + 1])
        self.assertEqual(payload['mode'], 'ENDPOINTING')

    def test_session_id_unique_per_request(self):
        calls, fake_send = self._send([ok_response(), ok_response()])
        with patch(
            'tracking.ai.transcribe._send', side_effect=fake_send
        ):
            with override_settings(META_API_KEY='k'):
                transcribe_audio(wav_bytes())
                transcribe_audio(wav_bytes())
        first = calls[0][0].split('sessionId=')[1]
        second = calls[1][0].split('sessionId=')[1]
        self.assertNotEqual(first, second)

    def test_missing_key_raises(self):
        with patch('tracking.ai.transcribe._send') as sender:
            with override_settings(META_API_KEY=''):
                with self.assertRaises(MissingAPIKeyError):
                    transcribe_audio(wav_bytes())
        sender.assert_not_called()

    def test_empty_transcript_raises(self):
        _, fake_send = self._send(
            [ok_response(transcript='   ')]
        )
        with patch(
            'tracking.ai.transcribe._send', side_effect=fake_send
        ):
            with override_settings(META_API_KEY='k'):
                with self.assertRaises(TranscriptionError):
                    transcribe_audio(wav_bytes())

    def test_failed_call_raises_with_status(self):
        _, fake_send = self._send([(400, b'bad wav')])
        with patch(
            'tracking.ai.transcribe._send', side_effect=fake_send
        ):
            with override_settings(META_API_KEY='k'):
                with self.assertRaises(TranscriptionError) as raised:
                    transcribe_audio(wav_bytes())
        self.assertEqual(raised.exception.status, 400)

    def test_500_retried_once(self):
        calls, fake_send = self._send(
            [(500, b''), ok_response()]
        )
        with patch(
            'tracking.ai.transcribe._send', side_effect=fake_send
        ):
            with override_settings(META_API_KEY='k'):
                self.assertEqual(
                    transcribe_audio(wav_bytes()), 'duty paid'
                )
        self.assertEqual(len(calls), 2)

    def test_500_twice_raises(self):
        calls, fake_send = self._send([(500, b''), (500, b'')])
        with patch(
            'tracking.ai.transcribe._send', side_effect=fake_send
        ):
            with override_settings(META_API_KEY='k'):
                with self.assertRaises(TranscriptionError):
                    transcribe_audio(wav_bytes())
        self.assertEqual(len(calls), 2)

    def test_429_retried_at_most_twice(self):
        calls, fake_send = self._send(
            [(429, b''), (429, b''), (429, b'')]
        )
        with patch(
            'tracking.ai.transcribe._send', side_effect=fake_send
        ):
            with patch('tracking.ai.transcribe.sleep') as sleeper:
                with override_settings(META_API_KEY='k'):
                    with self.assertRaises(TranscriptionError):
                        transcribe_audio(wav_bytes())
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeper.call_args_list, [call(2), call(4)])

    def test_429_then_success(self):
        _, fake_send = self._send([(429, b''), ok_response()])
        with patch(
            'tracking.ai.transcribe._send', side_effect=fake_send
        ):
            with patch('tracking.ai.transcribe.sleep'):
                with override_settings(META_API_KEY='k'):
                    self.assertEqual(
                        transcribe_audio(wav_bytes()), 'duty paid'
                    )

    def test_key_never_logged(self):
        _, fake_send = self._send([(400, b'bad')])
        with override_settings(META_API_KEY='sentinel-key-xyz'):
            with patch(
                'tracking.ai.transcribe._send', side_effect=fake_send
            ):
                with self.assertLogs(
                    'tracking.ai.transcribe', level='WARNING'
                ) as logs:
                    with self.assertRaises(TranscriptionError):
                        transcribe_audio(wav_bytes())
        self.assertNotIn('sentinel-key-xyz', '\n'.join(logs.output))


@override_settings(MEDIA_ROOT=VOICE_MEDIA_ROOT)
class TranscribeEndpointTests(TestCase):
    def setUp(self):
        cache.clear()

    def _agent(self, company, username):
        user = User.objects.create_user(
            username=username, password='secret123'
        )
        StaffMember.objects.create(
            user=user, agent_company=company, role=StaffMember.Role.OWNER
        )
        return user

    def _upload(self, data, name='recording.wav'):
        return SimpleUploadedFile(name, data, content_type='audio/wav')

    def test_requires_login(self):
        response = self.client.post(
            '/agent/assistant/transcribe/',
            {'audio': self._upload(wav_bytes())},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response['Location'].startswith('/agent/login/')
        )

    def test_requires_csrf(self):
        company = AgentCompany.objects.create(name='Voice Csrf Co')
        user = self._agent(company, 'voice-csrf')
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(user)
        no_token = csrf_client.post(
            '/agent/assistant/transcribe/',
            {'audio': self._upload(wav_bytes())},
        )
        self.assertEqual(no_token.status_code, 403)
        csrf_client.get('/agent/login/')
        token = csrf_client.cookies['csrftoken'].value
        with patch(
            'tracking.agent_views.transcribe_audio', return_value='hi'
        ):
            with_token = csrf_client.post(
                '/agent/assistant/transcribe/',
                {'audio': self._upload(wav_bytes())},
                HTTP_X_CSRFTOKEN=token,
            )
        self.assertEqual(with_token.status_code, 200)

    def test_rejects_non_wav(self):
        company = AgentCompany.objects.create(name='Voice Type Co')
        self.client.force_login(self._agent(company, 'voice-typer'))
        with patch(
            'tracking.agent_views.transcribe_audio'
        ) as transcribe:
            response = self.client.post(
                '/agent/assistant/transcribe/',
                {'audio': self._upload(b'this is not audio')},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            json.loads(response.content), {'error': FRIENDLY}
        )
        transcribe.assert_not_called()

    def test_rejects_wrong_format(self):
        company = AgentCompany.objects.create(name='Voice Fmt Co')
        self.client.force_login(self._agent(company, 'voice-fmt'))
        for bad in (
            wav_bytes(rate=16000),
            wav_bytes(channels=2),
            wav_bytes(bits=8),
        ):
            with patch(
                'tracking.agent_views.transcribe_audio'
            ) as transcribe:
                response = self.client.post(
                    '/agent/assistant/transcribe/',
                    {'audio': self._upload(bad)},
                )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(
                json.loads(response.content), {'error': FRIENDLY}
            )
            transcribe.assert_not_called()

    def test_rejects_oversize_file(self):
        company = AgentCompany.objects.create(name='Voice Big Co')
        self.client.force_login(self._agent(company, 'voice-big'))
        with patch(
            'tracking.agent_views.transcribe_audio'
        ) as transcribe:
            response = self.client.post(
                '/agent/assistant/transcribe/',
                {
                    'audio': self._upload(
                        b'RIFF' + b'x' * (2 * 1024 * 1024 + 1)
                    )
                },
            )
        self.assertEqual(response.status_code, 400)
        transcribe.assert_not_called()

    def test_rejects_missing_field(self):
        company = AgentCompany.objects.create(name='Voice Empty Co')
        self.client.force_login(self._agent(company, 'voice-empty'))
        response = self.client.post('/agent/assistant/transcribe/', {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            json.loads(response.content), {'error': FRIENDLY}
        )

    def test_success_returns_text_and_stores_nothing(self):
        company = AgentCompany.objects.create(name='Voice Ok Co')
        self.client.force_login(self._agent(company, 'voice-ok'))
        wav = wav_bytes()
        with patch(
            'tracking.agent_views.transcribe_audio', return_value='duty paid'
        ) as transcribe:
            response = self.client.post(
                '/agent/assistant/transcribe/',
                {'audio': self._upload(wav)},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.content), {'text': 'duty paid'}
        )
        transcribe.assert_called_once_with(wav)
        self.assertEqual(Document.objects.count(), 0)
        self.assertEqual(os.listdir(VOICE_MEDIA_ROOT), [])

    def test_failure_returns_friendly_message(self):
        company = AgentCompany.objects.create(name='Voice Fail Co')
        self.client.force_login(self._agent(company, 'voice-fail'))
        with override_settings(META_API_KEY='sentinel-key-xyz'):
            with patch(
                'tracking.agent_views.transcribe_audio',
                side_effect=TranscriptionError('boom', status=500),
            ):
                response = self.client.post(
                    '/agent/assistant/transcribe/',
                    {'audio': self._upload(wav_bytes())},
                )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            json.loads(response.content), {'error': FRIENDLY}
        )
        self.assertNotIn('sentinel-key-xyz', response.content.decode())
        self.assertNotIn('boom', response.content.decode())

    def test_daily_limit_enforced(self):
        company = AgentCompany.objects.create(name='Voice Limit Co')
        user = self._agent(company, 'voice-limit')
        cache.set(
            'assistant-transcribe:%d:%s' % (
                user.pk, timezone.localdate().isoformat()
            ),
            100,
            24 * 60 * 60,
        )
        self.client.force_login(user)
        with patch(
            'tracking.agent_views.transcribe_audio'
        ) as transcribe:
            response = self.client.post(
                '/agent/assistant/transcribe/',
                {'audio': self._upload(wav_bytes())},
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(
            json.loads(response.content), {'error': FRIENDLY}
        )
        transcribe.assert_not_called()

    def test_page_has_mic_button_and_notice(self):
        company = AgentCompany.objects.create(name='Voice Page Co')
        self.client.force_login(self._agent(company, 'voice-page'))
        response = self.client.get('/agent/shipments/')
        self.assertContains(response, 'assistant-mic')
        self.assertContains(response, 'Record voice message')
        self.assertContains(response, 'assistant-voice-status')
        self.assertContains(
            response,
            'Voice is transcribed by an AI model. '
            'Check the text before sending.',
        )


class TranscribeCheckTests(TestCase):
    def _wav_path(self):
        handle = tempfile.NamedTemporaryFile(
            suffix='.wav', delete=False
        )
        handle.write(wav_bytes())
        handle.close()
        self.addCleanup(os.remove, handle.name)
        return handle.name

    def test_prints_transcript(self):
        out = StringIO()
        with patch(
            'tracking.management.commands.transcribe_check.transcribe_audio',
            return_value='duty paid',
        ):
            call_command('transcribe_check', self._wav_path(), stdout=out)
        self.assertEqual(out.getvalue().strip(), 'duty paid')

    def test_failure_prints_class_not_key(self):
        out, err = StringIO(), StringIO()
        with override_settings(META_API_KEY='sentinel-key-xyz'):
            with patch(
                'tracking.management.commands.transcribe_check.transcribe_audio',
                side_effect=TranscriptionError('unreadable', status=500),
            ):
                with self.assertRaises(SystemExit):
                    call_command(
                        'transcribe_check', self._wav_path(),
                        stdout=out, stderr=err,
                    )
        self.assertIn('TranscriptionError', err.getvalue())
        self.assertNotIn('sentinel-key-xyz', err.getvalue())
