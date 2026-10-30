import hashlib
from datetime import date, datetime, timedelta

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from . import access as access_helpers
from . import clocks as clock_helpers
from . import timeline as timeline_helpers
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
    def _verify_clock_container(self):
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
        return self.client.get(verify['Location'])

    def test_free_time_labels_hidden_by_default(self):
        timeline = self._verify_clock_container()
        self.assertEqual(timeline.status_code, 200)
        self.assertNotContains(timeline, 'Terminal free time')
        self.assertNotContains(timeline, 'Shipping line free time')

    @override_settings(SHOW_FREE_TIME_CLOCKS=True)
    def test_free_time_labels_shown_when_flag_on(self):
        timeline = self._verify_clock_container()
        self.assertEqual(timeline.status_code, 200)
        self.assertContains(timeline, 'Terminal free time')
        self.assertContains(timeline, 'Shipping line free time')


class DemoTimelineViewTests(TestCase):
    def setUp(self):
        call_command('seed_demo')
        self.client.post(
            '/track/verify/', {'q': 'CSQU3054383', 'code': '482910'}
        )

    def test_demo_container_page(self):
        response = self.client.get('/s/CSQU3054383/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'CSQU3054383')
        self.assertNotContains(response, 'DEMOBL001')
        self.assertContains(response, 'Shipping line processed')
        self.assertContains(response, 'Current')
        self.assertNotContains(response, 'Terminal free time')
        self.assertNotContains(response, 'Shipping line free time')

    def test_demo_container2_waiting_line(self):
        response = self.client.get('/s/DEMO4416733/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Waiting on')
        self.assertContains(response, 'Customs')

    def test_bl_page_shows_two_cards(self):
        response = self.client.get('/s/DEMOBL001/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'CSQU3054383')
        self.assertContains(response, 'DEMO4416733')
        self.assertNotContains(response, 'View timeline')

    def test_no_demo_names_on_pages(self):
        for url in (
            '/',
            '/s/CSQU3054383/',
            '/s/DEMO4416733/',
            '/s/DEMOBL001/',
        ):
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, 'Demo Importer A')
            self.assertNotContains(page, 'Demo Clearing Ltd')


class ContainerTimelineTests(TestCase):
    def setUp(self):
        call_command('seed_demo')
        self.container1 = Container.objects.get(number='CSQU3054383')
        self.container2 = Container.objects.get(number='DEMO4416733')

    def _make_container(self, number):
        company = AgentCompany.objects.create(name='Timeline Co %s' % number)
        importer = Importer.objects.create(
            agent_company=company,
            name='Timeline Importer %s' % number,
            access_code='333333',
        )
        shipment = Shipment.objects.create(
            importer=importer,
            bl_number='TIMELINEBL%s' % number,
            shipping_line='Line',
            port='Onne',
            destination_area='Port Harcourt',
        )
        return Container.objects.create(
            shipment=shipment, number=number, size='40ft'
        )

    def _add_event(self, container, key, occurred_at=None, skipped=False):
        return StageEvent.objects.create(
            container=container,
            stage=Stage.objects.get(key=key),
            occurred_at=occurred_at,
            skipped=skipped,
        )

    def test_demo_container1_current_and_processing(self):
        steps = timeline_helpers.build_timeline(self.container1)
        by_key = {step['key']: step for step in steps}
        current = [step for step in steps if step['state'] == 'current']
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]['key'], 'shipping_line_processed')
        self.assertEqual(
            [step['key'] for step in steps[:5]],
            [
                'arrived_at_port',
                'manifest_available',
                'paar_assessed',
                'duty_paid',
                'shipping_line_processed',
            ],
        )
        processing = {
            step['key'] for step in steps if step['state'] == 'processing'
        }
        self.assertEqual(
            processing, {'terminal_processed', 'customs_processed'}
        )
        self.assertNotIn('en_route_to_bonded_terminal', by_key)
        self.assertNotIn('customs_examination', by_key)
        for key in (
            'gate_out',
            'in_transit',
            'delivered',
            'empty_container_returned',
        ):
            self.assertEqual(by_key[key]['state'], 'upcoming')

    def test_demo_container2_states_and_holding(self):
        steps = timeline_helpers.build_timeline(self.container2)
        by_key = {step['key']: step for step in steps}
        current = [step for step in steps if step['state'] == 'current']
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]['key'], 'manifest_available')
        self.assertEqual(
            by_key['shipping_line_processed']['state'], 'upcoming'
        )
        hold = timeline_helpers.holding(self.container2)
        self.assertEqual(hold['waiting_on'], 'customs')
        self.assertEqual(hold['pending_processing'], [])

    def test_optional_selected_shown_and_skipped_hidden(self):
        container = self._make_container('OPT0000001')
        self._add_event(container, 'customs_examination')
        self._add_event(container, 'en_route_to_bonded_terminal', skipped=True)
        by_key = {
            step['key']: step
            for step in timeline_helpers.build_timeline(container)
        }
        self.assertEqual(by_key['customs_examination']['state'], 'upcoming')
        self.assertNotIn('en_route_to_bonded_terminal', by_key)

    def test_stale_after_72_hours(self):
        old = timezone.now() - timedelta(hours=73)
        StageEvent.objects.filter(container=self.container1).update(
            updated_at=old
        )
        fresh = timeline_helpers.freshness(self.container1)
        self.assertTrue(fresh['stale'])
        self.assertIsNotNone(fresh['updated_at'])
        recent = timeline_helpers.freshness(self.container2)
        self.assertFalse(recent['stale'])

    def test_delivered_container_not_stale(self):
        container = self._make_container('STALE000001')
        self._add_event(
            container, 'delivered', occurred_at=timezone.now()
        )
        old = timezone.now() - timedelta(hours=100)
        StageEvent.objects.filter(container=container).update(updated_at=old)
        fresh = timeline_helpers.freshness(container)
        self.assertFalse(fresh['stale'])

    def test_holding_pending_processing_labels(self):
        hold = timeline_helpers.holding(self.container1)
        self.assertEqual(
            hold['pending_processing'],
            ['Terminal processed', 'Customs processed'],
        )


class ResultsPagesTests(TestCase):
    def setUp(self):
        call_command('seed_demo')
        self.client.post(
            '/track/verify/', {'q': 'CSQU3054383', 'code': '482910'}
        )

    def test_bl_page_has_two_details_and_no_timeline_links(self):
        response = self.client.get('/s/DEMOBL001/')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertContains(response, 'DEMOBL001')
        self.assertContains(response, 'CSQU3054383')
        self.assertContains(response, 'DEMO4416733')
        self.assertContains(response, 'Shipping line processed')
        self.assertContains(response, 'Manifest available')
        self.assertEqual(html.count('<details class="row"'), 2)
        self.assertNotContains(response, 'View timeline')

    def test_container_page_hides_bl_number(self):
        response = self.client.get('/s/CSQU3054383/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'DEMOBL001')

    def test_no_demo_names_on_result_pages(self):
        for url in ('/s/DEMOBL001/', '/s/CSQU3054383/', '/s/DEMO4416733/'):
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, 'Demo Importer A')
            self.assertNotContains(page, 'Demo Clearing Ltd')

    def test_top_bar_form_on_result_pages(self):
        for url in ('/s/DEMOBL001/', '/s/CSQU3054383/'):
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200)
            html = page.content.decode()
            self.assertContains(page, '<form class="topsearch"')
            self.assertContains(page, 'action="/track/"')
            self.assertContains(page, 'id-top-q')
            self.assertContains(page, 'placeholder="Container or BL number"')
            self.assertContains(page, 'csrfmiddlewaretoken')
            self.assertNotContains(page, 'Track another shipment')
            self.assertNotIn('value="DEMOBL001"', html)
            self.assertNotIn('value="CSQU3054383"', html)

    def test_container_box_open_by_default(self):
        response = self.client.get('/s/CSQU3054383/')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertEqual(html.count('<details class="row"'), 1)
        self.assertIn('<details class="row" open>', html)

    def test_bl_two_containers_both_closed(self):
        response = self.client.get('/s/DEMOBL001/')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertEqual(html.count('<details class="row"'), 2)
        self.assertNotIn('<details class="row" open>', html)

    def test_collapsed_rows_have_no_free_time_text(self):
        response = self.client.get('/s/DEMOBL001/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'free time')
        self.assertNotContains(response, 'Free time')

    def test_bl_page_header_and_rows(self):
        response = self.client.get('/s/DEMOBL001/')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertContains(response, 'DEMOBL001')
        self.assertContains(response, 'Onne')
        self.assertContains(response, 'Port Harcourt')
        self.assertEqual(html.count('<details class="row"'), 2)

    def test_collapsed_box_contents(self):
        response = self.client.get('/s/DEMOBL001/')
        self.assertEqual(response.status_code, 200)
        for number, size in (('CSQU3054383', '40ft'), ('DEMO4416733', '20ft')):
            self.assertContains(response, number)
            self.assertContains(response, size)
        self.assertContains(response, 'Arrived')
        self.assertContains(response, 'Current state')
        self.assertContains(response, 'Shipping line processed')
        self.assertContains(response, 'Manifest available')


class TimelineGroupTests(TestCase):
    def setUp(self):
        call_command('seed_demo')
        self.container1 = Container.objects.get(number='CSQU3054383')

    def test_demo_groups_in_phase_order(self):
        groups = timeline_helpers.build_timeline_groups(self.container1)
        self.assertEqual(
            [group['phase'] for group in groups],
            ['arrival', 'release', 'delivery', 'return'],
        )
        self.assertEqual(
            [group['heading'] for group in groups],
            ['ONNE', 'ONNE', 'PORT HARCOURT', 'EMPTY RETURN'],
        )
        arrival = groups[0]
        self.assertEqual(arrival['subtitle'], 'Arrival and documents')
        self.assertEqual(arrival['completed_count'], 4)
        self.assertFalse(arrival['is_current_group'])
        release = groups[1]
        self.assertTrue(release['is_current_group'])

    def test_demo_flat_timeline(self):
        self.client.post(
            '/track/verify/', {'q': 'CSQU3054383', 'code': '482910'}
        )
        response = self.client.get('/s/CSQU3054383/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Shipping line processed')
        self.assertContains(response, 'Completed')
        self.assertContains(response, 'In progress')
        self.assertNotContains(response, 'Upcoming')
        self.assertNotContains(response, 'tag-current')
        self.assertNotContains(response, 'Arrival and documents')
        self.assertNotContains(response, 'steps completed')
        self.assertRegex(
            response.content.decode(), r'\d{1,2} \w{3} \d{4}, \d{2}:\d{2}'
        )

    def test_small_timeline_is_flat(self):
        company = AgentCompany.objects.create(name='Fold Co')
        importer = Importer.objects.create(
            agent_company=company, name='Fold Importer', access_code='444444'
        )
        shipment = Shipment.objects.create(
            importer=importer,
            bl_number='FOLDBL001',
            shipping_line='Line',
            port='Onne',
            destination_area='Port Harcourt',
        )
        container = Container.objects.create(
            shipment=shipment, number='FOLD0000001', size='40ft'
        )
        now = timezone.now()
        for key, days_ago in [
            ('arrived_at_port', 3),
            ('manifest_available', 2),
            ('shipping_line_processed', 1),
        ]:
            StageEvent.objects.create(
                container=container,
                stage=Stage.objects.get(key=key),
                occurred_at=now - timedelta(days=days_ago),
            )
        self.client.post(
            '/track/verify/', {'q': 'FOLD0000001', 'code': '444444'}
        )
        response = self.client.get('/s/FOLD0000001/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Arrived at port')
        self.assertContains(response, 'Completed')
        self.assertNotContains(response, 'steps completed')
        self.assertNotContains(response, 'Arrival and documents')

    def test_bl_single_container_box_open(self):
        company = AgentCompany.objects.create(name='Solo Co')
        importer = Importer.objects.create(
            agent_company=company, name='Solo Importer', access_code='555555'
        )
        shipment = Shipment.objects.create(
            importer=importer,
            bl_number='SOLOBL001',
            shipping_line='Line',
            port='Onne',
            destination_area='Port Harcourt',
        )
        Container.objects.create(
            shipment=shipment, number='SOLO0000001', size='20ft'
        )
        self.client.post(
            '/track/verify/', {'q': 'SOLOBL001', 'code': '555555'}
        )
        response = self.client.get('/s/SOLOBL001/')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertEqual(html.count('<details class="row"'), 1)
        self.assertIn('<details class="row" open>', html)

    def test_bl_page_has_one_row_per_container(self):
        self.client.post(
            '/track/verify/', {'q': 'CSQU3054383', 'code': '482910'}
        )
        response = self.client.get('/s/DEMOBL001/')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertContains(response, 'DEMOBL001')
        self.assertEqual(html.count('<details class="row"'), 2)

    def test_clocks_stay_hidden(self):
        self.client.post(
            '/track/verify/', {'q': 'CSQU3054383', 'code': '482910'}
        )
        for url in ('/s/DEMOBL001/', '/s/CSQU3054383/'):
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, 'Terminal free time')
            self.assertNotContains(page, 'Shipping line free time')
            self.assertNotContains(page, 'Return the empty container')
            self.assertNotContains(page, 'return window')
