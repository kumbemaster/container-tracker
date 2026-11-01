import io
import json
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from .models import (
    AgentCompany,
    Container,
    Importer,
    Shipment,
    StaffMember,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _bootstrap(*args, **kwargs):
    out = io.StringIO()
    call_command('bootstrap_demo', *args, stdout=out, **kwargs)
    return out.getvalue()


def _agent(company, username):
    user = User.objects.create_user(username=username, password='pw')
    StaffMember.objects.create(
        user=user, agent_company=company, role=StaffMember.Role.OWNER
    )
    return user


class BootstrapDemoTests(TestCase):
    # The test runner sets DEBUG=False, so these use the DEBUG=True
    # override to exercise the normal path; refusal is covered below.
    @override_settings(DEBUG=True)
    def test_idempotent_and_creates_both_shipments(self):
        first = _bootstrap(force=True)
        counts = (
            AgentCompany.objects.count(),
            Importer.objects.count(),
            Shipment.objects.count(),
            Container.objects.count(),
            User.objects.filter(username='demo_agent').count(),
        )
        second = _bootstrap()
        self.assertEqual(
            (
                AgentCompany.objects.count(),
                Importer.objects.count(),
                Shipment.objects.count(),
                Container.objects.count(),
                User.objects.filter(username='demo_agent').count(),
            ),
            counts,
        )
        self.assertIn('Password: ', first)
        self.assertNotIn('Password: ', second)
        self.assertIn('already exists; password unchanged', second)
        shipment1 = Shipment.objects.get(bl_number='DEMOBL001')
        self.assertEqual(shipment1.containers.count(), 2)
        shipment2 = Shipment.objects.get(bl_number='DEMOBL002')
        self.assertEqual(
            shipment2.importer.name, 'Demo Importer C Ltd'
        )
        self.assertEqual(
            sorted(
                shipment2.containers.values_list('number', flat=True)
            ),
            ['TSTU1234568', 'TSTU6543214'],
        )
        self.assertEqual(
            shipment2.containers.first().stageevent_set.count(), 0
        )
        self.assertRegex(shipment2.importer.access_code, r'^\d{6}$')
        self.assertIn('DEMOBL002', second)
        self.assertIn('/agent/login/', second)

    @override_settings(DEBUG=True)
    def test_demo_agent_password_only_once(self):
        _bootstrap(force=True)
        first_hash = User.objects.get(username='demo_agent').password
        self.assertTrue(User.objects.get(username='demo_agent')
                        .has_usable_password())
        staff = StaffMember.objects.get(user__username='demo_agent')
        self.assertEqual(staff.role, StaffMember.Role.OWNER)
        self.assertEqual(staff.agent_company.name, 'Demo Clearing Ltd')
        _bootstrap()
        self.assertEqual(
            User.objects.get(username='demo_agent').password, first_hash
        )

    @override_settings(DEBUG=True)
    def test_reset_password(self):
        _bootstrap(force=True)
        first_hash = User.objects.get(username='demo_agent').password
        out = _bootstrap(force=True, reset_password=True)
        user = User.objects.get(username='demo_agent')
        self.assertNotEqual(user.password, first_hash)
        self.assertTrue(user.has_usable_password())
        self.assertIn('Password: ', out)

    @override_settings(DEBUG=False)
    def test_refuses_when_not_debug_without_force(self):
        with self.assertRaises(CommandError):
            _bootstrap()

    @override_settings(DEBUG=False)
    def test_force_runs_when_not_debug(self):
        out = _bootstrap(force=True)
        self.assertIn('demo_agent', out)


class NoKeyFriendlyTests(TestCase):
    def setUp(self):
        call_command('seed_demo', stdout=io.StringIO())
        company = AgentCompany.objects.get(name='Demo Clearing Ltd')
        self.user = _agent(company, 'nokey-agent')
        self.client.force_login(self.user)

    @override_settings(META_API_KEY='')
    def test_scan_without_key_shows_friendly_message(self):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new('RGB', (10, 10), 'white').save(buffer, format='JPEG')
        buffer.seek(0)
        buffer.name = 'scan.jpg'
        response = self.client.post(
            '/agent/shipments/scan/', {'scan': buffer}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'We could not read this document. '
            'You can enter the shipment manually.',
        )
        self.assertNotContains(response, 'MODEL_API_KEY')

    @override_settings(META_API_KEY='')
    def test_assistant_without_key_is_unavailable(self):
        response = self.client.post(
            '/agent/assistant/',
            data=json.dumps({'text': 'duty paid for DEMOBL001'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'The assistant is unavailable',
            response.content.decode(),
        )
        self.assertNotContains(response, 'MODEL_API_KEY')

    @override_settings(META_API_KEY='')
    def test_transcribe_without_key_shows_friendly_message(self):
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(b'\x00\x00' * 24000)
        buffer.seek(0)
        buffer.name = 'clip.wav'
        response = self.client.post(
            '/agent/assistant/transcribe/', {'audio': buffer}
        )
        self.assertEqual(response.status_code, 502)
        content = response.content.decode()
        self.assertIn(
            'We could not hear that. Please type it instead.', content
        )
        self.assertNotIn('MODEL_API_KEY', content)

    def test_stage_tiles_still_work(self):
        shipment = Shipment.objects.get(bl_number='DEMOBL001')
        response = self.client.get('/agent/shipments/%d/' % shipment.pk)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Duty paid')


class SettingsEnvTests(TestCase):
    def _run_manage(self, extra_env, *args):
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in (
                'DJANGO_SECRET_KEY', 'DJANGO_DEBUG',
                'DJANGO_ALLOWED_HOSTS', 'MODEL_API_KEY',
                'BASETEN_API_KEY', 'META_MODEL', 'META_API_BASE_URL',
                'META_ASR_MODE', 'META_ASR_HINTS_ENABLED',
                'ALLOW_AGENT_SIGNUP', 'STAGE_TEXT_PROVIDER',
                'BASETEN_MODEL',
            )
        }
        env.update(extra_env)
        # settings.py discovers the checkout's .env by caller file
        # location, so absence of a key is simulated with an empty
        # value, which follows the same code path as a missing one.
        env.setdefault('DJANGO_SECRET_KEY', '')
        workdir = tempfile.mkdtemp(prefix='freshclone')
        try:
            return subprocess.run(
                [sys.executable, str(REPO_ROOT / 'manage.py')] + list(args),
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
        finally:
            import shutil

            shutil.rmtree(workdir, ignore_errors=True)

    def _debug_flag(self, extra_env):
        result = self._run_manage(
            extra_env,
            'shell',
            '-c',
            'from django.conf import settings; '
            'print("DEBUG=%r" % settings.DEBUG); '
            'print("HOSTS=%r" % settings.ALLOWED_HOSTS)',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_debug_defaults_true(self):
        out = self._debug_flag({'DJANGO_SECRET_KEY': 'x'})
        self.assertIn('DEBUG=True', out)
        self.assertIn("'localhost'", out)

    def test_debug_falsy_values(self):
        for value in ('0', 'false', 'no'):
            out = self._debug_flag(
                {'DJANGO_SECRET_KEY': 'x', 'DJANGO_DEBUG': value}
            )
            self.assertIn('DEBUG=False', out)

    def test_allowed_hosts_extendable(self):
        out = self._debug_flag(
            {
                'DJANGO_SECRET_KEY': 'x',
                'DJANGO_ALLOWED_HOSTS': 'example.com, shop.example.com',
            }
        )
        self.assertIn('example.com', out)
        self.assertIn('shop.example.com', out)

    def test_missing_key_warns_in_debug(self):
        result = self._run_manage({}, 'check')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('WARNING', result.stderr)
        self.assertNotIn('MODEL_API_KEY', result.stderr)

    def test_missing_key_stops_when_not_debug(self):
        result = self._run_manage({'DJANGO_DEBUG': '0'}, 'check')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('DJANGO_SECRET_KEY must be set', result.stderr)
