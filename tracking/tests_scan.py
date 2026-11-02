import json
import os
import tempfile
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from PIL import Image

from .ai.client import MissingAPIKeyError, get_client
from .ai.extract_bl import ExtractionError, extract_shipment
from .ai.images import ImageError, prepare_image
from .ai.parsing import (
    ModelJSONError,
    normalise_extraction,
    normalise_size,
    parse_model_json,
)
from .models import (
    AgentCompany,
    Document,
    Importer,
    Shipment,
    StaffMember,
)

SCAN_MEDIA_ROOT = tempfile.mkdtemp(prefix='scan-test-media')


def make_image_bytes(width=300, height=200, orientation=None):
    image = Image.new('RGB', (width, height), 'red')
    buffer = BytesIO()
    if orientation is None:
        image.save(buffer, format='PNG')
    else:
        exif = Image.Exif()
        exif[0x0112] = orientation
        image.save(buffer, format='PNG', exif=exif)
    return buffer.getvalue()


class ClientTests(TestCase):
    def test_missing_key_raises_clear_error(self):
        with override_settings(META_API_KEY=''):
            with self.assertRaises(MissingAPIKeyError):
                get_client()

    def test_client_points_at_meta_base_url(self):
        with override_settings(
            META_API_BASE_URL='https://example.invalid/v1',
            META_API_KEY='test-key',
        ):
            client = get_client()
        self.assertIn('example.invalid', str(client.base_url))


class ParseModelJsonTests(TestCase):
    def test_plain_json(self):
        self.assertEqual(parse_model_json('{"a": 1}'), {'a': 1})

    def test_fenced_json(self):
        self.assertEqual(
            parse_model_json('```json\n{"a": 1}\n```'), {'a': 1}
        )

    def test_prose_wrapped_json(self):
        text = 'Here is the document:\n```\n{"a": 1}\n```\nLet me know.'
        self.assertEqual(parse_model_json(text), {'a': 1})

    def test_bare_prose_around_json(self):
        self.assertEqual(
            parse_model_json('Sure! {"a": 1} all done.'), {'a': 1}
        )

    def test_invalid_raises(self):
        with self.assertRaises(ModelJSONError):
            parse_model_json('no json here at all')

    def test_non_object_raises(self):
        with self.assertRaises(ModelJSONError):
            parse_model_json('[1, 2, 3]')


class NormaliseSizeTests(TestCase):
    def test_recognised_sizes(self):
        cases = {
            "40' HC": '40HC',
            '40HC': '40HC',
            '40 high cube': '40HC',
            "20' DC": '20ft',
            '20GP': '20ft',
            '20 dry': '20ft',
            '20ft': '20ft',
            '40ft': '40ft',
            '40DC': '40ft',
        }
        for raw, expected in cases.items():
            self.assertEqual(normalise_size(raw), expected, raw)

    def test_unrecognised_sizes_are_none(self):
        for raw in ('', 'reefer', '45HC', '20', 'LCL', None):
            self.assertIsNone(normalise_size(raw), raw)

    def test_tolerant_sizes(self):
        cases = {
            "1X40'HC": '40HC',
            "1 x 20' DV": '20ft',
            '40HQ': '40HC',
            "40' High Cube": '40HC',
            '20GP': '20ft',
            "20' STD": '20ft',
            '20DC': '20ft',
            '40 DRY': '40ft',
            "1X40'HC / SEAL 771201": '40HC',
            '2 X40HC': '40HC',
            '40DRYVAN': '40ft',
        }
        for raw, expected in cases.items():
            self.assertEqual(normalise_size(raw), expected, raw)

    def test_45HC_is_none(self):
        self.assertIsNone(normalise_size('45HC'))


class NormaliseExtractionTests(TestCase):
    def test_normalise_extraction(self):
        data = {
            'bl_number': '  demobl001 ',
            'shipping_line': '   ',
            'port_of_discharge': 'Onne',
            'place_of_delivery': '',
            'importer_name': 'Foo Foods',
            'goods_description': ' '.join(
                ['word%d' % number for number in range(20)]
            ),
            'containers': [
                {'number': 'csqu 3054-383', 'size': "40' HC"},
                {'number': 'CSQU3054384', 'size': "20' DC"},
                {'number': 'DEMO4416733', 'size': '20GP'},
            ],
            'uncertain': ['port_of_discharge', 'bl_number'],
        }
        result = normalise_extraction(data)
        self.assertEqual(result['bl_number'], 'demobl001')
        self.assertIsNone(result['shipping_line'])
        self.assertEqual(result['port'], 'Onne')
        self.assertIsNone(result['destination_area'])
        self.assertEqual(result['importer_name'], 'Foo Foods')
        self.assertEqual(len(result['goods_description'].split()), 15)
        first, second, third = result['containers']
        self.assertEqual(first['number'], 'CSQU3054383')
        self.assertTrue(first['valid'])
        self.assertEqual(first['size'], '40HC')
        self.assertEqual(second['number'], 'CSQU3054384')
        self.assertFalse(second['valid'])
        self.assertEqual(second['size'], '20ft')
        self.assertTrue(third['valid'])
        self.assertEqual(third['size'], '20ft')
        self.assertIn('port', result['uncertain'])
        self.assertIn('bl_number', result['uncertain'])
        self.assertIn('containers[1].number', result['uncertain'])
        self.assertNotIn('containers[0].number', result['uncertain'])

    def test_place_names_are_tidied(self):
        cases = {
            'Onne, Nigeria': 'Onne',
            'PORT HARCOURT, NIGERIA': 'Port Harcourt',
            'Apapa, NG': 'Apapa',
            'Aba, Abia State': 'Aba, Abia State',
            'Apapa': 'Apapa',
            'Port Harcourt Nigeria': 'Port Harcourt',
            'ONNE NIGERIA': 'Onne',
            'Apapa NG': 'Apapa',
            'Nigeria': 'Nigeria',
            'NG': 'Ng',
        }
        for raw, expected in cases.items():
            for key in ('port_of_discharge', 'place_of_delivery'):
                result = normalise_extraction({key: raw})
                field = (
                    'port'
                    if key == 'port_of_discharge'
                    else 'destination_area'
                )
                self.assertEqual(result[field], expected, raw)

    def test_other_text_fields_untouched(self):
        result = normalise_extraction(
            {
                'shipping_line': 'MSC',
                'importer_name': 'DEMO LINE',
                'goods_description': 'FROZEN FISH',
            }
        )
        self.assertEqual(result['shipping_line'], 'MSC')
        self.assertEqual(result['importer_name'], 'DEMO LINE')
        self.assertEqual(result['goods_description'], 'FROZEN FISH')

    def test_bl_lookalikes_flagged_uncertain(self):
        for raw in ('DEM0BL002', 'AB12O34', 'MA1ERSK', 'X5I7Z'):
            result = normalise_extraction({'bl_number': raw})
            self.assertEqual(result['bl_number'], raw)
            self.assertIn('bl_number', result['uncertain'], raw)
        for raw in ('DEMOBL002', 'MEDU1234567'):
            result = normalise_extraction({'bl_number': raw})
            self.assertNotIn('bl_number', result['uncertain'], raw)

    def test_bl_lookalike_not_duplicated(self):
        result = normalise_extraction(
            {'bl_number': 'DEM0BL002', 'uncertain': ['bl_number']}
        )
        self.assertEqual(result['uncertain'], ['bl_number'])

    def test_notify_used_for_to_order_consignee(self):
        result = normalise_extraction(
            {
                'consignee': 'TO THE ORDER OF DEMO TRADE BANK PLC',
                'notify_party': 'DEMO IMPORTER D LTD',
            }
        )
        self.assertEqual(result['importer_name'], 'DEMO IMPORTER D LTD')
        self.assertEqual(result['importer_source'], 'notify')
        self.assertIn('importer_name', result['uncertain'])

    def test_normal_consignee_used_as_is(self):
        result = normalise_extraction(
            {
                'consignee': 'Demo Importer B Ltd.',
                'notify_party': 'Someone Else',
            }
        )
        self.assertEqual(result['importer_name'], 'Demo Importer B Ltd')
        self.assertEqual(result['importer_source'], 'consignee')
        self.assertNotIn('importer_name', result['uncertain'])

    def test_to_order_with_same_as_consignee_gives_none(self):
        result = normalise_extraction(
            {'consignee': 'To Order', 'notify_party': 'SAME AS CONSIGNEE'}
        )
        self.assertIsNone(result['importer_name'])
        self.assertIsNone(result['importer_source'])
        self.assertIn('importer_name', result['uncertain'])

    def test_to_order_variants_use_notify(self):
        for consignee in (
            'TO ORDER',
            'to the order of Demo Trade Bank',
            'ORDER OF SHIPPER',
            'To Order Of Demo Bank Plc, Lagos.',
        ):
            result = normalise_extraction(
                {'consignee': consignee, 'notify_party': 'Notify Foods'}
            )
            self.assertEqual(
                result['importer_name'], 'Notify Foods', consignee
            )

    def test_never_returns_to_order_name(self):
        for data in (
            {'consignee': 'TO THE ORDER OF DEMO BANK'},
            {
                'consignee': 'TO THE ORDER OF DEMO BANK',
                'notify_party': '',
            },
            {
                'consignee': 'TO THE ORDER OF DEMO BANK',
                'notify_party': 'SAME AS CONSIGNEE',
            },
        ):
            result = normalise_extraction(data)
            name = result['importer_name'] or ''
            self.assertFalse(
                name.upper().startswith('TO THE ORDER OF'), data
            )

    def test_company_name_trimmed_without_case_change(self):
        result = normalise_extraction(
            {'consignee': '  Demo Importer F Ltd., '}
        )
        self.assertEqual(result['importer_name'], 'Demo Importer F Ltd.')
        self.assertNotIn('importer_name', result['uncertain'])

    def test_replay_carrier_bl_with_to_order_consignee(self):
        result = normalise_extraction(
            {
                'bl_number': 'DEMOBL003',
                'shipping_line': 'Demo Line',
                'port_of_discharge': 'Onne, Nigeria',
                'place_of_delivery': 'Port Harcourt',
                'consignee': 'TO THE ORDER OF DEMO TRADE BANK PLC',
                'notify_party': 'DEMO IMPORTER D LTD',
                'goods_description': 'FROZEN FISH IN CARTONS',
                'containers': [
                    {'number': 'MSKU1000000', 'size': "40'HC"},
                    {'number': 'TCLU2000008', 'size': '40ft'},
                    {'number': 'MSKU3000000', 'size': "1X40'HC"},
                    {'number': 'TCLU4000007', 'size': "20' DC"},
                    {'number': 'OOLU5000000', 'size': '20GP'},
                    {'number': 'EITU6000001', 'size': '40 DRY'},
                ],
                'uncertain': [],
            }
        )
        self.assertEqual(
            result,
            {
                'bl_number': 'DEMOBL003',
                'shipping_line': 'Demo Line',
                'port': 'Onne',
                'destination_area': 'Port Harcourt',
                'importer_name': 'DEMO IMPORTER D LTD',
                'importer_source': 'notify',
                'goods_description': 'FROZEN FISH IN CARTONS',
                'containers': [
                    {
                        'number': 'MSKU1000000',
                        'size': '40HC',
                        'valid': True,
                    },
                    {
                        'number': 'TCLU2000008',
                        'size': '40ft',
                        'valid': True,
                    },
                    {
                        'number': 'MSKU3000000',
                        'size': '40HC',
                        'valid': True,
                    },
                    {
                        'number': 'TCLU4000007',
                        'size': '20ft',
                        'valid': True,
                    },
                    {
                        'number': 'OOLU5000000',
                        'size': '20ft',
                        'valid': True,
                    },
                    {
                        'number': 'EITU6000001',
                        'size': '40ft',
                        'valid': True,
                    },
                ],
                'uncertain': ['importer_name'],
            },
        )

    def test_replay_carrier_bl_with_raw_sizes(self):
        result = normalise_extraction(
            {
                'bl_number': 'DEMOBL004',
                'shipping_line': 'Demo Line',
                'port_of_discharge': 'Onne',
                'place_of_delivery': 'PORT HARCOURT, NIGERIA',
                'consignee': 'TO ORDER',
                'notify_party': 'DEMO IMPORTER D LTD',
                'goods_description': 'FROZEN FISH IN CARTONS',
                'containers': [
                    {'number': 'MSKU1000000', 'size': "1X40'HC"},
                    {'number': 'TCLU2000008', 'size': "1 x 20' DV"},
                    {'number': 'MSKU3000000', 'size': '40HQ'},
                    {'number': 'TCLU4000007', 'size': '20GP'},
                    {'number': 'OOLU5000000', 'size': '40 DRY'},
                    {
                        'number': 'EITU6000001',
                        'size': "1X40'HC / SEAL 771201",
                    },
                ],
                'uncertain': [],
            }
        )
        self.assertEqual(
            [item['size'] for item in result['containers']],
            ['40HC', '20ft', '40HC', '20ft', '40ft', '40HC'],
        )
        self.assertTrue(
            all(item['valid'] for item in result['containers'])
        )
        self.assertEqual(result['destination_area'], 'Port Harcourt')
        self.assertEqual(result['importer_name'], 'DEMO IMPORTER D LTD')
        self.assertEqual(result['importer_source'], 'notify')
        self.assertEqual(result['uncertain'], ['importer_name'])

    def test_replay_direct_consignee_bl(self):
        result = normalise_extraction(
            {
                'bl_number': 'DEMOBL005',
                'shipping_line': 'Demo Line',
                'port_of_discharge': 'Apapa',
                'place_of_delivery': '   ',
                'consignee': 'Demo Importer E Ltd',
                'notify_party': None,
                'goods_description': 'CERAMIC TILES',
                'containers': [
                    {'number': 'MSKU1000000', 'size': None},
                ],
                'uncertain': [],
            }
        )
        self.assertEqual(
            result,
            {
                'bl_number': 'DEMOBL005',
                'shipping_line': 'Demo Line',
                'port': 'Apapa',
                'destination_area': None,
                'importer_name': 'Demo Importer E Ltd',
                'importer_source': 'consignee',
                'goods_description': 'CERAMIC TILES',
                'containers': [
                    {
                        'number': 'MSKU1000000',
                        'size': None,
                        'valid': True,
                    },
                ],
                'uncertain': [],
            },
        )


class StageExplanationTextsTests(TestCase):
    def test_english_texts_match_new_process(self):
        content = (
            Path(__file__).parent / 'content'
            / 'stage_explanations_en.json'
        ).read_text()
        texts = json.loads(content)
        self.assertEqual(
            texts['paar_assessed'],
            'Customs assesses your PAAR (Pre-Arrival Assessment Report) '
            'and works out the duty to pay for your shipment. After this, '
            'the shipping line and the terminal can start their '
            'processing, and the duty can be paid.',
        )
        self.assertEqual(
            texts['duty_paid'],
            'This is when the import duty is paid. After it, customs can '
            'start its own processing.',
        )
        self.assertEqual(
            texts['customs_processed'],
            'Customs finishes its processing and releases your cargo. '
            'This can only happen after the duty is paid, and it must be '
            'done before the container can leave the port.',
        )


class ExtractionInstructionTests(TestCase):
    def test_instruction_asks_for_consignee_notify_and_raw_sizes(self):
        from .ai.extract_bl import SYSTEM_INSTRUCTION

        for token in (
            'consignee',
            'notify_party',
            'without any address',
            'copied as printed',
            'exactly as printed',
            "1X40'HC",
        ):
            self.assertIn(token, SYSTEM_INSTRUCTION)


class PrepareImageTests(TestCase):
    def test_shrinks_large_image_to_jpeg(self):
        out = prepare_image(make_image_bytes(3000, 1000))
        self.assertTrue(out[:3] == b'\xff\xd8\xff')
        image = Image.open(BytesIO(out))
        self.assertLessEqual(max(image.size), 2000)

    def test_applies_exif_orientation(self):
        out = prepare_image(make_image_bytes(100, 200, orientation=6))
        image = Image.open(BytesIO(out))
        self.assertEqual(image.size, (200, 100))

    def test_unreadable_file_raises(self):
        with self.assertRaises(ImageError):
            prepare_image(b'this is not an image')


def _fake_response(content):
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=content))]
    return response


class ExtractShipmentTests(TestCase):
    def _client(self, replies):
        client = MagicMock()
        client.chat.completions.create.side_effect = replies
        return client

    def test_sends_image_and_system_instruction(self):
        payload = json.dumps({'bl_number': 'X', 'containers': []})
        client = self._client([_fake_response(payload)])
        with patch(
            'tracking.ai.extract_bl.get_client', return_value=client
        ):
            result = extract_shipment(make_image_bytes())
        self.assertEqual(result['bl_number'], 'X')
        (call,) = client.chat.completions.create.call_args_list
        sent = call.kwargs['messages']
        self.assertIn(
            'never guess a container number', sent[0]['content']
        )
        parts = sent[1]['content']
        image_part = next(
            part for part in parts if part.get('type') == 'image_url'
        )
        self.assertTrue(
            image_part['image_url']['url'].startswith(
                'data:image/jpeg;base64,'
            )
        )

    def test_retries_once_on_invalid_json(self):
        payload = json.dumps({'bl_number': 'Y', 'containers': []})
        client = self._client(
            [_fake_response('not json'), _fake_response(payload)]
        )
        with patch(
            'tracking.ai.extract_bl.get_client', return_value=client
        ):
            result = extract_shipment(make_image_bytes())
        self.assertEqual(result['bl_number'], 'Y')
        self.assertEqual(client.chat.completions.create.call_count, 2)
        retry_messages = (
            client.chat.completions.create.call_args_list[1].kwargs[
                'messages'
            ]
        )
        self.assertIn('not valid JSON', retry_messages[-1]['content'])

    def test_raises_after_second_invalid_reply(self):
        client = self._client(
            [_fake_response('nope'), _fake_response('still nope')]
        )
        with patch(
            'tracking.ai.extract_bl.get_client', return_value=client
        ):
            with self.assertRaises(ExtractionError):
                extract_shipment(make_image_bytes())
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_api_error_carries_status(self):
        failure = Exception('boom')
        failure.status_code = 500
        client = self._client([failure])
        with patch(
            'tracking.ai.extract_bl.get_client', return_value=client
        ):
            with self.assertRaises(ExtractionError) as raised:
                extract_shipment(make_image_bytes())
        self.assertEqual(raised.exception.status, 500)

    def test_missing_key_propagates(self):
        with patch(
            'tracking.ai.extract_bl.get_client',
            side_effect=MissingAPIKeyError('no key'),
        ):
            with self.assertRaises(MissingAPIKeyError):
                extract_shipment(make_image_bytes())


class ScanCheckTests(TestCase):
    def _image_path(self):
        handle = tempfile.NamedTemporaryFile(
            suffix='.png', delete=False
        )
        handle.write(make_image_bytes())
        handle.close()
        self.addCleanup(os.remove, handle.name)
        return handle.name

    def test_prints_normalised_json(self):
        result = {'bl_number': 'Z', 'containers': []}
        out = StringIO()
        with patch(
            'tracking.management.commands.scan_check.extract_shipment',
            return_value=result,
        ):
            call_command('scan_check', self._image_path(), stdout=out)
        self.assertEqual(json.loads(out.getvalue()), result)

    def test_failure_prints_class_and_status(self):
        out, err = StringIO(), StringIO()
        with patch(
            'tracking.management.commands.scan_check.extract_shipment',
            side_effect=ExtractionError('unreadable', status=500),
        ):
            with self.assertRaises(SystemExit):
                call_command(
                    'scan_check', self._image_path(),
                    stdout=out, stderr=err,
                )
        self.assertIn('ExtractionError', err.getvalue())
        self.assertIn('500', err.getvalue())


@override_settings(
    MEDIA_ROOT=SCAN_MEDIA_ROOT, META_API_KEY='sentinel-key-xyz'
)
class ScanFlowTests(TestCase):
    def _company(self, name):
        return AgentCompany.objects.create(name=name)

    def _agent(self, company, username):
        user = User.objects.create_user(
            username=username, password='secret123'
        )
        StaffMember.objects.create(
            user=user, agent_company=company, role=StaffMember.Role.OWNER
        )
        return user

    def _importer(self, company, name='Scan Importer'):
        return Importer.objects.create(
            agent_company=company, name=name, access_code='123456'
        )

    def _upload(self, data, name='bl.png', content_type='image/png'):
        return SimpleUploadedFile(name, data, content_type=content_type)

    def _result(self):
        return {
            'bl_number': 'SCANBL001',
            'shipping_line': 'Scan Line',
            'port': 'Onne',
            'destination_area': 'Lekki',
            'importer_name': 'Matched Foods',
            'goods_description': None,
            'containers': [
                {'number': 'CSQU3054383', 'size': '40ft', 'valid': True},
                {'number': 'CSQU3054384', 'size': None, 'valid': False},
            ],
            'uncertain': ['port', 'importer_name'],
        }

    def test_scan_requires_login(self):
        response = self.client.get('/agent/shipments/scan/')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response['Location'].startswith('/agent/login/')
        )

    def test_rejects_wrong_content_type(self):
        company = self._company('Type Co')
        self.client.force_login(self._agent(company, 'typer'))
        with patch(
            'tracking.agent_views.extract_shipment'
        ) as extract:
            response = self.client.post(
                '/agent/shipments/scan/',
                {'scan': self._upload(b'hello', 'x.txt', 'text/plain')},
            )
        self.assertContains(response, 'Only JPEG or PNG files')
        self.assertFalse(Document.objects.exists())
        extract.assert_not_called()

    def test_rejects_fake_extension(self):
        company = self._company('Fake Co')
        self.client.force_login(self._agent(company, 'faker'))
        with patch(
            'tracking.agent_views.extract_shipment'
        ) as extract:
            response = self.client.post(
                '/agent/shipments/scan/',
                {'scan': self._upload(b'not an image at all')},
            )
        self.assertContains(response, 'Only JPEG or PNG files')
        self.assertFalse(Document.objects.exists())
        extract.assert_not_called()

    def test_rejects_oversize_file(self):
        company = self._company('Big Co')
        self.client.force_login(self._agent(company, 'bigger'))
        data = b'\x89PNG\r\n\x1a\n' + b'x' * (8 * 1024 * 1024 + 1)
        with patch(
            'tracking.agent_views.extract_shipment'
        ) as extract:
            response = self.client.post(
                '/agent/shipments/scan/', {'scan': self._upload(data)}
            )
        self.assertContains(response, '8 MB or smaller')
        self.assertFalse(Document.objects.exists())
        extract.assert_not_called()

    def test_enforces_daily_limit(self):
        company = self._company('Limit Co')
        user = self._agent(company, 'limiter')
        for _ in range(50):
            Document.objects.create(
                company=company, uploaded_by=user, kind=Document.Kind.BL
            )
        self.client.force_login(user)
        with patch(
            'tracking.agent_views.extract_shipment'
        ) as extract:
            response = self.client.post(
                '/agent/shipments/scan/',
                {'scan': self._upload(make_image_bytes())},
            )
        self.assertContains(response, 'daily scan limit')
        self.assertEqual(Document.objects.count(), 50)
        extract.assert_not_called()

    def test_failure_shows_friendly_message(self):
        company = self._company('Fail Co')
        self.client.force_login(self._agent(company, 'failer'))
        with patch(
            'tracking.agent_views.extract_shipment',
            side_effect=ExtractionError('unreadable', status=500),
        ):
            response = self.client.post(
                '/agent/shipments/scan/',
                {'scan': self._upload(make_image_bytes())},
            )
        self.assertContains(
            response,
            'We could not read this document. '
            'You can enter the shipment manually.',
        )
        self.assertContains(response, '/agent/shipments/new/')
        self.assertNotContains(response, 'sentinel-key-xyz')
        self.assertEqual(Document.objects.count(), 1)
        self.assertEqual(Shipment.objects.count(), 0)

    def test_success_prefills_new_form(self):
        company = self._company('Prefill Co')
        user = self._agent(company, 'prefiller')
        importer = self._importer(company, name='matched foods')
        self.client.force_login(user)
        with patch(
            'tracking.agent_views.extract_shipment',
            return_value=self._result(),
        ):
            response = self.client.post(
                '/agent/shipments/scan/',
                {'scan': self._upload(make_image_bytes())},
            )
        document = Document.objects.get()
        self.assertEqual(document.company, company)
        self.assertTrue(
            document.file.name.startswith('private/documents/')
        )
        self.assertRedirects(
            response, '/agent/shipments/new/?scan=%d' % document.pk
        )
        self.assertEqual(Shipment.objects.count(), 0)
        response = self.client.get(
            '/agent/shipments/new/?scan=%d' % document.pk
        )
        self.assertContains(response, 'Prefilled from your scan')
        self.assertContains(response, 'Nothing is saved until you submit')
        self.assertContains(response, 'value="SCANBL001"')
        self.assertContains(response, 'CSQU3054383')
        self.assertContains(response, 'CSQU3054384')
        self.assertContains(
            response, '<option value="40ft" selected>'
        )
        self.assertContains(response, 'value="Matched Foods"')
        self.assertContains(response, '<option value="matched foods">')
        self.assertContains(response, 'Check this')
        self.assertContains(response, 'check-row')
        self.assertContains(response, 'name="scan_document"')
        self.assertNotContains(response, 'sentinel-key-xyz')

    def test_prefill_unknown_importer_name(self):
        company = self._company('New Name Co')
        self.client.force_login(self._agent(company, 'namer'))
        document = Document.objects.create(
            company=company, kind=Document.Kind.BL
        )
        session = self.client.session
        result = self._result()
        result['importer_name'] = 'Brand New Foods'
        session['bl_scan_%d' % document.pk] = result
        session.save()
        response = self.client.get(
            '/agent/shipments/new/?scan=%d' % document.pk
        )
        self.assertContains(response, 'value="Brand New Foods"')

    def test_prefill_banner_uses_flex_markup(self):
        company = self._company('Flex Co')
        self.client.force_login(self._agent(company, 'flexer'))
        document = Document.objects.create(
            company=company, kind=Document.Kind.BL
        )
        session = self.client.session
        session['bl_scan_%d' % document.pk] = self._result()
        session.save()
        response = self.client.get(
            '/agent/shipments/new/?scan=%d' % document.pk
        )
        html = response.content.decode()
        self.assertContains(response, 'class="scan-banner banner"')
        self.assertContains(response, 'class="banner-text"')
        self.assertLess(html.index('banner-text'), html.index('data-dismiss'))

    def test_prefill_banner_markup(self):
        company = self._company('Banner Co')
        self.client.force_login(self._agent(company, 'banner'))
        document = Document.objects.create(
            company=company, kind=Document.Kind.BL
        )
        session = self.client.session
        session['bl_scan_%d' % document.pk] = self._result()
        session.save()
        response = self.client.get(
            '/agent/shipments/new/?scan=%d' % document.pk
        )
        self.assertContains(response, 'role="status"')
        self.assertContains(response, 'data-transient')
        self.assertContains(response, 'data-dismiss')
        self.assertContains(response, 'aria-label="Dismiss"')
        self.assertContains(response, 'prefers-reduced-motion')

    def test_prefill_notify_importer_shows_note_and_check(self):
        company = self._company('Notify Co')
        self.client.force_login(self._agent(company, 'notifier'))
        document = Document.objects.create(
            company=company, kind=Document.Kind.BL
        )
        session = self.client.session
        result = self._result()
        result['importer_name'] = 'Notify Foods'
        result['importer_source'] = 'notify'
        result['uncertain'] = ['importer_name']
        session['bl_scan_%d' % document.pk] = result
        session.save()
        response = self.client.get(
            '/agent/shipments/new/?scan=%d' % document.pk
        )
        self.assertContains(response, 'value="Notify Foods"')
        self.assertContains(response, 'Chosen from the notify party.')
        self.assertContains(response, 'Check this')

    def test_prefill_consignee_importer_has_no_note(self):
        company = self._company('Consignee Co')
        self.client.force_login(self._agent(company, 'consignee'))
        document = Document.objects.create(
            company=company, kind=Document.Kind.BL
        )
        session = self.client.session
        result = self._result()
        result['importer_name'] = 'Consignee Foods'
        result['importer_source'] = 'consignee'
        result['uncertain'] = []
        session['bl_scan_%d' % document.pk] = result
        session.save()
        response = self.client.get(
            '/agent/shipments/new/?scan=%d' % document.pk
        )
        self.assertContains(response, 'value="Consignee Foods"')
        self.assertNotContains(response, 'Chosen from the notify party.')

    def test_prefill_missing_importer_shows_note(self):
        company = self._company('Missing Co')
        self.client.force_login(self._agent(company, 'misser'))
        document = Document.objects.create(
            company=company, kind=Document.Kind.BL
        )
        session = self.client.session
        result = self._result()
        result['importer_name'] = None
        result['importer_source'] = None
        result['uncertain'] = ['importer_name']
        session['bl_scan_%d' % document.pk] = result
        session.save()
        response = self.client.get(
            '/agent/shipments/new/?scan=%d' % document.pk
        )
        self.assertContains(response, 'Not found on the document.')

    def test_prefill_unrecognised_size_is_highlighted(self):
        company = self._company('Size Co')
        self.client.force_login(self._agent(company, 'sizer'))
        document = Document.objects.create(
            company=company, kind=Document.Kind.BL
        )
        session = self.client.session
        result = self._result()
        result['containers'] = [
            {'number': 'MSKU1000000', 'size': None, 'valid': True},
        ]
        session['bl_scan_%d' % document.pk] = result
        session.save()
        response = self.client.get(
            '/agent/shipments/new/?scan=%d' % document.pk
        )
        self.assertContains(response, 'MSKU1000000')
        self.assertContains(response, 'check-row')

    def test_saving_links_document(self):
        company = self._company('Link Co')
        user = self._agent(company, 'linker')
        importer = self._importer(company, name='Matched Foods')
        self.client.force_login(user)
        with patch(
            'tracking.agent_views.extract_shipment',
            return_value=self._result(),
        ):
            self.client.post(
                '/agent/shipments/scan/',
                {'scan': self._upload(make_image_bytes())},
            )
        document = Document.objects.get()
        response = self.client.post(
            '/agent/shipments/new/',
            {
                'importer_name': importer.name,
                'bl_number': 'SCANBL001',
                'shipping_line': 'Scan Line',
                'port': 'Onne',
                'destination_area': 'Lekki',
                'c_number': ['CSQU3054383'],
                'c_size': ['40ft'],
                'scan_document': str(document.pk),
            },
        )
        self.assertEqual(response.status_code, 302)
        shipment = Shipment.objects.get(bl_number='SCANBL001')
        document.refresh_from_db()
        self.assertEqual(document.shipment, shipment)
        self.assertNotIn(
            'bl_scan_%d' % document.pk, self.client.session
        )

    def test_foreign_scan_ignored(self):
        company_a = self._company('Scan A')
        company_b = self._company('Scan B')
        self.client.force_login(self._agent(company_a, 'scan-a'))
        foreign = Document.objects.create(
            company=company_b, kind=Document.Kind.BL
        )
        response = self.client.get(
            '/agent/shipments/new/?scan=%d' % foreign.pk
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Prefilled from your scan')

    def test_document_download_scoped(self):
        company_a = self._company('Doc A')
        company_b = self._company('Doc B')
        self.client.force_login(self._agent(company_a, 'doc-a'))
        data = make_image_bytes()
        own = Document.objects.create(
            company=company_a, kind=Document.Kind.BL
        )
        from django.core.files.base import ContentFile

        own.file.save('scan-own.png', ContentFile(data))
        foreign = Document.objects.create(
            company=company_b, kind=Document.Kind.BL
        )
        response = self.client.get('/agent/documents/%d/' % own.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), data)
        response = self.client.get('/agent/documents/%d/' % foreign.pk)
        self.assertEqual(response.status_code, 404)
