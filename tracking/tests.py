import hashlib
from datetime import date, datetime, timedelta

from django.test import TestCase
from django.utils import timezone

from . import access as access_helpers
from . import clocks as clock_helpers
from .views import LOCKED_MESSAGE, WRONG_DETAILS_MESSAGE
from .models import (
    AccessAttempt,
    AgentCompany,
    Container,
    DeviceGrant,
    DeviceSession,
    Importer,
    Shipment,
    Stage,
    StageEvent,
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


class ClockTests(TestCase):
    def _make_container(self, number, discharge_date, terminal_days, shipping_days):
        company = AgentCompany.objects.create(name='Clock Co %s' % number)
        importer = Importer.objects.create(
            agent_company=company,
            name='Clock Importer %s' % number,
            access_code='111111',
        )
        shipment = Shipment.objects.create(
            importer=importer,
            bl_number='CLOCKBL%s' % number,
            shipping_line='Line',
            port='Onne',
            destination_area='Port Harcourt',
            discharge_date=discharge_date,
            terminal_free_days=terminal_days,
            shipping_line_free_days=shipping_days,
        )
        return Container.objects.create(
            shipment=shipment, number=number, size='40ft'
        )

    def _add_event(self, container, key, day):
        stage = Stage.objects.get(key=key)
        occurred = timezone.make_aware(
            datetime(day.year, day.month, day.day, 12, 0)
        )
        return StageEvent.objects.create(
            container=container, stage=stage, occurred_at=occurred
        )

    def test_sunday_discharge_end_dates(self):
        sunday = date(2026, 10, 25)
        self.assertEqual(sunday.weekday(), 6)
        container = self._make_container('SUN0000001', sunday, 3, 5)
        wednesday = clock_helpers.port_clock(
            container, 'Terminal free time', 3, today=date(2026, 10, 26)
        )
        self.assertEqual(wednesday['end_date'], date(2026, 10, 28))
        friday = clock_helpers.port_clock(
            container, 'Shipping line free time', 5, today=date(2026, 10, 26)
        )
        self.assertEqual(friday['end_date'], date(2026, 10, 30))
        self.assertEqual(date(2026, 10, 30).weekday(), 4)

    def test_weekends_count(self):
        thursday = date(2026, 10, 22)
        self.assertEqual(thursday.weekday(), 3)
        container = self._make_container('THU0000001', thursday, 3, 3)
        clock = clock_helpers.port_clock(
            container, 'Terminal free time', 3, today=thursday
        )
        self.assertEqual(clock['end_date'], date(2026, 10, 25))
        self.assertEqual(clock['end_date'].weekday(), 6)

    def test_stopped_within_free_time(self):
        container = self._make_container(
            'STP0000001', date(2026, 10, 25), 3, 3
        )
        self._add_event(container, 'gate_out', date(2026, 10, 27))
        clock = clock_helpers.port_clock(
            container, 'Terminal free time', 3, today=date(2026, 10, 29)
        )
        self.assertEqual(clock['state'], 'stopped')
        self.assertEqual(clock['stopped_date'], date(2026, 10, 27))
        self.assertTrue(clock['within_free_time'])

    def test_stopped_after_free_time(self):
        container = self._make_container(
            'STP0000002', date(2026, 10, 25), 3, 3
        )
        self._add_event(container, 'gate_out', date(2026, 10, 30))
        clock = clock_helpers.port_clock(
            container, 'Terminal free time', 3, today=date(2026, 10, 31)
        )
        self.assertEqual(clock['state'], 'stopped')
        self.assertFalse(clock['within_free_time'])

    def test_last_free_day(self):
        container = self._make_container(
            'LST0000001', date(2026, 10, 25), 3, 3
        )
        clock = clock_helpers.port_clock(
            container, 'Terminal free time', 3, today=date(2026, 10, 28)
        )
        self.assertEqual(clock['days_left'], 0)
        self.assertTrue(clock['last_free_day'])
        self.assertEqual(clock['state'], 'amber')

    def test_ok_amber_and_red(self):
        container = self._make_container(
            'LEV0000001', date(2026, 10, 25), 3, 3
        )
        ok_clock = clock_helpers.port_clock(
            container, 'Terminal free time', 3, today=date(2026, 10, 25)
        )
        self.assertEqual(ok_clock['state'], 'ok')
        self.assertEqual(ok_clock['days_left'], 3)
        amber_clock = clock_helpers.port_clock(
            container, 'Terminal free time', 3, today=date(2026, 10, 26)
        )
        self.assertEqual(amber_clock['state'], 'amber')
        self.assertEqual(amber_clock['days_left'], 2)
        self.assertFalse(amber_clock['last_free_day'])
        red_clock = clock_helpers.port_clock(
            container, 'Terminal free time', 3, today=date(2026, 10, 29)
        )
        self.assertEqual(red_clock['state'], 'red')
        self.assertEqual(red_clock['days_left'], -1)

    def test_return_clock_needs_delivered(self):
        container = self._make_container(
            'RET0000001', date(2026, 10, 25), 3, 3
        )
        self.assertIsNone(clock_helpers.return_clock(container))

    def test_return_clock_amber_red_and_stopped(self):
        container = self._make_container(
            'RET0000002', date(2026, 10, 25), 3, 3
        )
        self._add_event(container, 'delivered', date(2026, 10, 27))
        amber = clock_helpers.return_clock(container, today=date(2026, 11, 8))
        self.assertEqual(amber['end_date'], date(2026, 11, 10))
        self.assertEqual(amber['state'], 'amber')
        self.assertEqual(amber['days_left'], 2)
        red = clock_helpers.return_clock(container, today=date(2026, 11, 11))
        self.assertEqual(red['state'], 'red')
        self.assertEqual(red['days_left'], -1)
        self._add_event(container, 'empty_container_returned', date(2026, 11, 9))
        stopped = clock_helpers.return_clock(container, today=date(2026, 11, 12))
        self.assertEqual(stopped['state'], 'stopped')
        self.assertEqual(stopped['stopped_date'], date(2026, 11, 9))
        self.assertTrue(stopped['within_window'])

    def test_return_clock_stopped_after_window(self):
        container = self._make_container(
            'RET0000003', date(2026, 10, 25), 3, 3
        )
        self._add_event(container, 'delivered', date(2026, 10, 27))
        self._add_event(
            container, 'empty_container_returned', date(2026, 11, 12)
        )
        stopped = clock_helpers.return_clock(container, today=date(2026, 11, 13))
        self.assertEqual(stopped['state'], 'stopped')
        self.assertFalse(stopped['within_window'])

    def test_missing_fields_give_none(self):
        container = self._make_container('MIS0000001', None, 3, 5)
        self.assertIsNone(
            clock_helpers.port_clock(container, 'Terminal free time', 3)
        )
        container.shipment.discharge_date = date(2026, 10, 25)
        container.shipment.save()
        self.assertIsNone(
            clock_helpers.port_clock(container, 'Terminal free time', None)
        )
        clocks = clock_helpers.build_clocks(container, today=date(2026, 10, 26))
        self.assertEqual(
            [clock['label'] if clock else None for clock in clocks],
            ['Terminal free time', 'Shipping line free time', None],
        )

    def test_build_clocks_order(self):
        container = self._make_container(
            'BLD0000001', date(2026, 10, 25), 3, 5
        )
        clocks = clock_helpers.build_clocks(container, today=date(2026, 10, 26))
        self.assertEqual(
            [clock['label'] for clock in clocks[:2]],
            ['Terminal free time', 'Shipping line free time'],
        )
        self.assertEqual(clocks[0]['end_date'], date(2026, 10, 28))
        self.assertEqual(clocks[1]['end_date'], date(2026, 10, 30))
        self.assertIsNone(clocks[2])


class TimelineClockViewTests(TestCase):
    def test_timeline_shows_both_free_time_labels(self):
        company = AgentCompany.objects.create(name='Timeline Clock Co')
        importer = Importer.objects.create(
            agent_company=company,
            name='Timeline Clock Importer',
            access_code='222222',
        )
        shipment = Shipment.objects.create(
            importer=importer,
            bl_number='TIMECLOCKBL001',
            shipping_line='Line',
            port='Onne',
            destination_area='Port Harcourt',
            discharge_date=date(2026, 10, 25),
            terminal_free_days=3,
            shipping_line_free_days=5,
        )
        Container.objects.create(
            shipment=shipment, number='TIMECLOCK01', size='40ft'
        )
        verify = self.client.post(
            '/track/verify/', {'q': 'TIMECLOCK01', 'code': '222222'}
        )
        self.assertEqual(verify.status_code, 302)
        timeline = self.client.get(verify['Location'])
        self.assertEqual(timeline.status_code, 200)
        self.assertContains(timeline, 'Terminal free time')
        self.assertContains(timeline, 'Shipping line free time')
