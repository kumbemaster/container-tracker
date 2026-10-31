import json
import os
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from .management.commands.load_stage_explanations import CONTENT_DIR
from .models import (
    AgentCompany,
    Container,
    Importer,
    Shipment,
    Stage,
    StageExplanation,
)


def _draft_path():
    return CONTENT_DIR / 'stage_explanations_pcm.draft.json'


def _final_path():
    return CONTENT_DIR / 'stage_explanations_pcm.json'


def _model_reply(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


class LoadExplanationsTests(TestCase):
    def _backup_final(self):
        path = CONTENT_DIR / 'stage_explanations_pcm.json'
        old = path.read_text(encoding='utf-8') if path.exists() else None
        self.addCleanup(self._restore_final, old)
        return path

    def _restore_final(self, old):
        path = CONTENT_DIR / 'stage_explanations_pcm.json'
        if old is None:
            if path.exists():
                os.remove(path)
        else:
            path.write_text(old, encoding='utf-8')

    def test_loads_english(self):
        out = StringIO()
        call_command('load_stage_explanations', stdout=out)
        self.assertIn('en: 13 loaded', out.getvalue())
        self.assertEqual(
            StageExplanation.objects.filter(lang='en').count(), 13
        )
        arrived = StageExplanation.objects.get(
            stage__key='arrived_at_port', lang='en'
        )
        self.assertTrue(arrived.text.startswith('This is when the vessel'))

    def test_idempotent(self):
        call_command('load_stage_explanations', stdout=StringIO())
        before = StageExplanation.objects.count()
        call_command('load_stage_explanations', stdout=StringIO())
        self.assertEqual(StageExplanation.objects.count(), before)
        self.assertEqual(
            StageExplanation.objects.filter(lang='en').count(), 13
        )

    def test_refuses_mismatched_keys(self):
        path = self._backup_final()
        path.write_text(
            json.dumps({'not_a_stage': 'sometext'}), encoding='utf-8'
        )
        with self.assertRaises(Exception):
            call_command('load_stage_explanations', stdout=StringIO())
        self.assertEqual(
            StageExplanation.objects.filter(lang='pcm').count(), 0
        )

    def test_refuses_overlong_text(self):
        path = self._backup_final()
        keys = list(Stage.objects.values_list('key', flat=True))
        data = {key: 'x' for key in keys}
        data[keys[0]] = 'y' * 301
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(Exception):
            call_command('load_stage_explanations', stdout=StringIO())
        self.assertEqual(
            StageExplanation.objects.filter(lang='pcm').count(), 0
        )


class GeneratePidginTests(TestCase):
    def setUp(self):
        draft = _draft_path()
        self._had_draft = draft.exists()
        self._old_draft = draft.read_text(encoding='utf-8') if (
            self._had_draft
        ) else None
        self.addCleanup(self._restore_draft)
        final = _final_path()
        self._had_final = final.exists()
        self._old_final = final.read_text(encoding='utf-8') if (
            self._had_final
        ) else None
        self.addCleanup(self._restore_final)

    def _restore_draft(self):
        draft = _draft_path()
        if self._had_draft:
            draft.write_text(self._old_draft, encoding='utf-8')
        elif draft.exists():
            os.remove(draft)

    def _restore_final(self):
        final = _final_path()
        if self._had_final:
            final.write_text(self._old_final, encoding='utf-8')
        elif final.exists():
            os.remove(final)

    def _final_content(self):
        final = _final_path()
        return final.read_text(encoding='utf-8') if final.exists() else None

    def _fake_create(self, replies):
        create = MagicMock()
        create.side_effect = replies
        return create

    def test_baseten_when_key_set(self):
        create = self._fake_create([_model_reply('Na wen dem pay duty.')])
        client = MagicMock()
        client.chat.completions.create = create
        out = StringIO()
        with patch('openai.OpenAI', return_value=client) as factory:
            with override_settings(
                BASETEN_API_KEY='sentinel-baseten-xyz',
                STAGE_TEXT_PROVIDER='',
            ):
                call_command(
                    'generate_pidgin', '--only', 'duty_paid', stdout=out
                )
        _, kwargs = factory.call_args
        self.assertIn('baseten.co', kwargs['base_url'])
        self.assertEqual(kwargs['api_key'], 'sentinel-baseten-xyz')
        _, create_kwargs = create.call_args
        self.assertEqual(create_kwargs['temperature'], 0.3)
        self.assertEqual(create_kwargs['timeout'], 30)
        self.assertIn(
            'Pidgin', create_kwargs['messages'][0]['content']
        )
        draft = json.loads(_draft_path().read_text(encoding='utf-8'))
        self.assertEqual(draft, {'duty_paid': 'Na wen dem pay duty.'})
        self.assertEqual(self._final_content(), self._old_final)
        self.assertIn('duty_paid: ok', out.getvalue())
        self.assertNotIn('sentinel-baseten-xyz', out.getvalue())

    def test_meta_when_no_baseten_key(self):
        create = self._fake_create([_model_reply('Na manifest don ready.')])
        client = MagicMock()
        client.chat.completions.create = create
        out = StringIO()
        with patch(
            'tracking.management.commands.generate_pidgin.get_client',
            return_value=client,
        ):
            with override_settings(
                BASETEN_API_KEY='', STAGE_TEXT_PROVIDER=''
            ):
                call_command(
                    'generate_pidgin', '--only', 'manifest_available',
                    stdout=out,
                )
        _, create_kwargs = create.call_args
        self.assertEqual(create_kwargs['temperature'], 0.3)
        draft = json.loads(_draft_path().read_text(encoding='utf-8'))
        self.assertEqual(
            draft, {'manifest_available': 'Na manifest don ready.'}
        )
        self.assertEqual(self._final_content(), self._old_final)
        self.assertIn('manifest_available: ok', out.getvalue())

    def test_explicit_provider_flag_wins(self):
        create = self._fake_create([_model_reply('Na so.')])
        client = MagicMock()
        client.chat.completions.create = create
        with patch(
            'tracking.management.commands.generate_pidgin.get_client',
            return_value=client,
        ):
            with patch('openai.OpenAI') as factory:
                with override_settings(
                    BASETEN_API_KEY='sentinel-baseten-xyz',
                    STAGE_TEXT_PROVIDER='',
                ):
                    call_command(
                        'generate_pidgin', '--provider', 'meta',
                        '--only', 'delivered', stdout=StringIO(),
                    )
        factory.assert_not_called()
        self.assertTrue(create.called)

    def test_retries_once_on_failure(self):
        create = self._fake_create(
            [Exception('boom'), _model_reply('Na so.')]
        )
        client = MagicMock()
        client.chat.completions.create = create
        out = StringIO()
        with patch(
            'tracking.management.commands.generate_pidgin.get_client',
            return_value=client,
        ):
            with override_settings(
                BASETEN_API_KEY='', STAGE_TEXT_PROVIDER=''
            ):
                call_command(
                    'generate_pidgin', '--only', 'delivered', stdout=out
                )
        self.assertEqual(create.call_count, 2)
        self.assertIn('delivered: ok', out.getvalue())


class ImporterExplanationPageTests(TestCase):
    def setUp(self):
        call_command('seed_demo')
        call_command('load_stage_explanations', stdout=StringIO())
        for stage in Stage.objects.all():
            StageExplanation.objects.update_or_create(
                stage=stage,
                lang='pcm',
                defaults={'text': 'Pidgin for %s.' % stage.key},
            )
        self.client.post(
            '/track/verify/', {'q': 'CSQU3054383', 'code': '482910'}
        )

    def test_container_page_renders_both_languages(self):
        response = self.client.get('/s/CSQU3054383/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Delivery Order (DO)')
        self.assertContains(
            response, 'Pidgin for shipping_line_processed.'
        )
        self.assertContains(response, 'lang="pcm"')
        self.assertContains(response, 'data-set-lang="pcm"')
        self.assertContains(response, 'aria-pressed')
        self.assertContains(response, 'localStorage')
        self.assertContains(response, 'listen-btn')
        self.assertContains(response, 'speechSynthesis')
        self.assertContains(response, 'i-circle')
        self.assertContains(response, 'What does this mean?')
        self.assertNotContains(response, 'info-btn')

    def test_each_step_is_one_details_with_one_summary(self):
        response = self.client.get('/s/CSQU3054383/')
        html = response.content.decode()
        total = html.count('<details class="step"')
        self.assertGreater(total, 1)
        self.assertEqual(
            html.count('<summary class="step-summary"'), total
        )

    def test_current_step_open_others_closed(self):
        response = self.client.get('/s/CSQU3054383/')
        html = response.content.decode()
        self.assertEqual(html.count('<details class="step" open>'), 1)
        total = html.count('<details class="step"')
        self.assertGreater(total - 1, 1)
        open_at = html.index('<details class="step" open>')
        summary_end = html.index('</summary>', open_at)
        self.assertIn(
            'Shipping line processed', html[open_at:summary_end]
        )

    def test_explanation_and_listen_inside_details(self):
        response = self.client.get('/s/CSQU3054383/')
        html = response.content.decode()
        self.assertRegex(
            html,
            r'(?s)<details class="step"[^>]*>.*?'
            r'<div class="step-info-body">.*?listen-btn.*?</details>',
        )
        self.assertContains(
            response, 'class="i-circle" aria-hidden="true"'
        )

    def test_step_summary_has_three_block_lines(self):
        response = self.client.get('/s/CSQU3054383/')
        self.assertContains(response, 'step-line')
        self.assertContains(response, 'step-date')
        self.assertContains(response, 'st-word')
        css = (
            Path(__file__).parent / 'static' / 'tracking' / 'style.css'
        ).read_text()
        self.assertIn('.step-summary {\n  display: block;', css)
        self.assertIn('.step-line {\n  display: block;', css)
        self.assertIn('.step-date {\n  display: block;', css)

    def test_pidgin_falls_back_to_english(self):
        StageExplanation.objects.filter(
            stage__key='duty_paid', lang='pcm'
        ).delete()
        response = self.client.get('/s/CSQU3054383/')
        self.assertContains(
            response,
            '<p lang="pcm" class="lang-pcm">'
            'This is when the import duty is paid.',
        )

    def test_no_private_names_and_clocks_hidden(self):
        response = self.client.get('/s/CSQU3054383/')
        self.assertNotContains(response, 'DEMOBL001')
        self.assertNotContains(response, 'Demo Importer A')
        self.assertNotContains(response, 'Demo Clearing Ltd')
        self.assertNotContains(response, 'Terminal free time')
        self.assertNotContains(response, 'Shipping line free time')

    def test_no_model_call_when_rendering(self):
        with patch(
            'tracking.ai.client.get_client',
            side_effect=AssertionError('model called'),
        ):
            with patch(
                'openai.OpenAI',
                side_effect=AssertionError('model called'),
            ):
                response = self.client.get('/s/CSQU3054383/')
        self.assertEqual(response.status_code, 200)
