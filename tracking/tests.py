import hashlib
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from . import access as access_helpers
from .views import LOCKED_MESSAGE, WRONG_DETAILS_MESSAGE
from .models import (
    AccessAttempt,
    AgentCompany,
    Container,
    DeviceGrant,
    DeviceSession,
    Importer,
    Shipment,
)
from .validators import is_valid_container_number


class ContainerNumberTests(TestCase):
    def test_known_valid_number(self):
        self.assertTrue(is_valid_container_number('CSQU3054383'))

    def test_wrong_check_digit_is_invalid(self):
        self.assertFalse(is_valid_container_number('CSQU3054384'))

    def test_too_short_string_is_invalid(self):
        self.assertFalse(is_valid_container_number('CSQU30'))


class AccessTests(TestCase):
    def setUp(self):
        self.company = AgentCompany.objects.create(name='Access Co')
        self.importer = Importer.objects.create(
            agent_company=self.company, name='Access Importer', access_code='482910'
        )
        self.shipment = Shipment.objects.create(
            importer=self.importer,
            bl_number='ACCESSBL001',
            shipping_line='Line',
            port='Onne',
            destination_area='Port Harcourt',
        )

    def test_wrong_code_rejected(self):
        self.assertTrue(access_helpers.code_is_valid(self.importer, '482910'))
        self.assertFalse(access_helpers.code_is_valid(self.importer, '000000'))

    def test_expired_code_rejected(self):
        self.importer.access_expires_at = timezone.now() - timedelta(days=1)
        self.importer.save()
        self.assertFalse(access_helpers.code_is_valid(self.importer, '482910'))

    def test_inactive_access_rejected(self):
        self.importer.access_active = False
        self.importer.save()
        self.assertFalse(access_helpers.code_is_valid(self.importer, '482910'))

    def test_lockout_after_five_failures(self):
        key = 'target:LOCKTEST001'
        self.assertFalse(access_helpers.is_locked(key))
        for _ in range(4):
            access_helpers.record_failure(key)
        self.assertFalse(access_helpers.is_locked(key))
        access_helpers.record_failure(key)
        self.assertTrue(access_helpers.is_locked(key))

    def test_old_failures_do_not_lock(self):
        key = 'target:OLDTEST001'
        old = timezone.now() - timedelta(minutes=16)
        for _ in range(5):
            AccessAttempt.objects.create(key=key, created_at=old)
        self.assertFalse(access_helpers.is_locked(key))

    def test_reused_container_number_prefers_open_shipment(self):
        open_shipment = Shipment.objects.create(
            importer=self.importer,
            bl_number='OPENBL001',
            shipping_line='Line',
            port='Onne',
            destination_area='Port Harcourt',
        )
        closed_shipment = Shipment.objects.create(
            importer=self.importer,
            bl_number='CLOSEDBL001',
            shipping_line='Line',
            port='Onne',
            destination_area='Port Harcourt',
            closed_at=timezone.now(),
        )
        open_container = Container.objects.create(
            shipment=open_shipment, number='REUSE1234567', size='40ft'
        )
        Container.objects.create(
            shipment=closed_shipment, number='REUSE1234567', size='20ft'
        )
        self.assertEqual(
            access_helpers.find_target('reuse1234567'), open_container
        )

    def test_rotate_code_revokes_grants(self):
        session, _ = access_helpers.create_device_session()
        access_helpers.grant_access(session, self.importer)
        self.assertTrue(access_helpers.has_access(session, self.importer))
        access_helpers.rotate_code(self.importer, '123456')
        self.importer.refresh_from_db()
        self.assertEqual(self.importer.access_code, '123456')
        self.assertTrue(
            DeviceGrant.objects.filter(
                session=session, importer=self.importer, revoked=True
            ).exists()
        )
        self.assertFalse(access_helpers.has_access(session, self.importer))

    def test_only_token_hash_is_stored(self):
        session, raw_token = access_helpers.create_device_session()
        expected_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        self.assertEqual(session.token_hash, expected_hash)
        self.assertNotEqual(session.token_hash, raw_token)
        stored = list(
            DeviceSession.objects.values_list('token_hash', flat=True)
        )
        self.assertIn(expected_hash, stored)
        self.assertNotIn(raw_token, stored)


class ImporterFlowTests(TestCase):
    def setUp(self):
        self.company_a = AgentCompany.objects.create(name='Harbour Light Ltd')
        self.importer_a = Importer.objects.create(
            agent_company=self.company_a,
            name='Harbour Foods',
            access_code='482910',
        )
        self.shipment_a = Shipment.objects.create(
            importer=self.importer_a,
            bl_number='FLOWBL001',
            shipping_line='Line',
            port='Onne',
            destination_area='Port Harcourt',
        )
        self.container_a = Container.objects.create(
            shipment=self.shipment_a, number='TCLU1000010', size='40ft'
        )
        self.company_b = AgentCompany.objects.create(name='Riverside Brands Ltd')
        self.importer_b = Importer.objects.create(
            agent_company=self.company_b,
            name='Riverside Goods',
            access_code='730119',
        )
        self.shipment_b = Shipment.objects.create(
            importer=self.importer_b,
            bl_number='FLOWBL002',
            shipping_line='Line',
            port='Onne',
            destination_area='Port Harcourt',
        )
        self.container_b = Container.objects.create(
            shipment=self.shipment_b, number='TCLU2000020', size='20ft'
        )

    def _private_names(self):
        return [
            'Harbour Light Ltd',
            'Harbour Foods',
            'Riverside Brands Ltd',
            'Riverside Goods',
        ]

    def test_same_message_for_all_failures(self):
        responses = [
            self.client.post(
                '/track/verify/', {'q': 'TCLU1000010', 'code': '000000'}
            ),
            self.client.post(
                '/track/verify/', {'q': 'NOPE9999999', 'code': '482910'}
            ),
            self.client.post(
                '/track/verify/', {'q': 'TCLU2000020', 'code': '482910'}
            ),
        ]
        for response in responses:
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, WRONG_DETAILS_MESSAGE)
            for name in self._private_names():
                self.assertNotContains(response, name)

    def test_lockout_after_five_failures(self):
        for _ in range(5):
            self.client.post(
                '/track/verify/', {'q': 'LOCKC000001', 'code': '000000'}
            )
        response = self.client.post(
            '/track/verify/', {'q': 'LOCKC000001', 'code': '000000'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, LOCKED_MESSAGE)

    def test_correct_code_sets_cookie_and_redirects(self):
        response = self.client.post(
            '/track/verify/', {'q': 'TCLU1000010', 'code': '482910'}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/s/TCLU1000010/')
        self.assertIn('device_token', response.cookies)

    def test_second_search_goes_straight_to_timeline(self):
        self.client.post('/track/verify/', {'q': 'TCLU1000010', 'code': '482910'})
        response = self.client.post('/track/', {'q': 'TCLU1000010'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/s/TCLU1000010/')

    def test_timeline_without_access_redirects(self):
        response = self.client.get('/s/TCLU1000010/')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/')

    def test_no_private_names_in_html(self):
        pages = [self.client.get('/')]
        pages.append(self.client.post('/track/', {'q': 'TCLU1000010'}))
        verify = self.client.post(
            '/track/verify/', {'q': 'FLOWBL001', 'code': '482910'}
        )
        self.assertEqual(verify.status_code, 302)
        timeline = self.client.get(verify['Location'])
        pages.append(timeline)
        self.assertContains(timeline, 'TCLU1000010')
        for page in pages:
            self.assertEqual(page.status_code, 200)
            for name in self._private_names():
                self.assertNotContains(page, name)
