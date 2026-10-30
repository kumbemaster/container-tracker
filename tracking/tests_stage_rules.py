from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from . import stage_rules
from .models import (
    AgentCompany,
    Container,
    Importer,
    Shipment,
    Stage,
    StageEvent,
)


class StageRuleTests(TestCase):
    def setUp(self):
        company = AgentCompany.objects.create(name='Rules Co')
        importer = Importer.objects.create(
            agent_company=company, name='Rules Importer', access_code='111111'
        )
        shipment = Shipment.objects.create(
            importer=importer,
            bl_number='RULESBL001',
            shipping_line='Line',
            port='Onne',
            destination_area='Port Harcourt',
        )
        self.container = Container.objects.create(
            shipment=shipment, number='RULES000001', size='40ft'
        )
        self.now = timezone.now()

    def _stage(self, key):
        return Stage.objects.get(key=key)

    def _complete(self, key, days_ago=1):
        return StageEvent.objects.create(
            container=self.container,
            stage=self._stage(key),
            occurred_at=self.now - timedelta(days=days_ago),
        )

    def test_arrived_has_no_prerequisites(self):
        problems = stage_rules.check_can_complete(
            self.container, self._stage('arrived_at_port'), self.now
        )
        self.assertEqual(problems, [])

    def test_duty_paid_blocked_before_paar(self):
        self._complete('arrived_at_port', days_ago=4)
        self._complete('manifest_available', days_ago=3)
        problems = stage_rules.check_can_complete(
            self.container, self._stage('duty_paid'), self.now
        )
        self.assertIn('PAAR assessed is not recorded yet.', problems)

    def test_future_time_refused(self):
        problems = stage_rules.check_can_complete(
            self.container,
            self._stage('arrived_at_port'),
            self.now + timedelta(hours=1),
        )
        self.assertIn('This time is in the future.', problems)

    def test_gate_out_needs_processing_tracks(self):
        for key, days_ago in [
            ('arrived_at_port', 5),
            ('manifest_available', 4),
            ('paar_assessed', 3),
            ('duty_paid', 2),
            ('shipping_line_processed', 1),
        ]:
            self._complete(key, days_ago=days_ago)
        problems = stage_rules.check_can_complete(
            self.container, self._stage('gate_out'), self.now
        )
        self.assertIn('Terminal processed is not recorded yet.', problems)
        self.assertIn('Customs processed is not recorded yet.', problems)

    def test_gate_out_waits_for_selected_customs_examination(self):
        for key, days_ago in [
            ('arrived_at_port', 7),
            ('manifest_available', 6),
            ('paar_assessed', 5),
            ('duty_paid', 4),
            ('shipping_line_processed', 3),
            ('terminal_processed', 2),
            ('customs_processed', 1),
        ]:
            self._complete(key, days_ago=days_ago)
        gate_out = self._stage('gate_out')
        self.assertEqual(
            stage_rules.check_can_complete(self.container, gate_out, self.now),
            [],
        )
        StageEvent.objects.create(
            container=self.container,
            stage=self._stage('customs_examination'),
            occurred_at=None,
        )
        problems = stage_rules.check_can_complete(
            self.container, gate_out, self.now
        )
        self.assertIn('Customs examination is not recorded yet.', problems)

    def test_gate_out_exact_missing_step_sentence(self):
        for key, days_ago in [
            ('arrived_at_port', 6),
            ('manifest_available', 5),
            ('paar_assessed', 4),
            ('duty_paid', 3),
            ('shipping_line_processed', 2),
            ('terminal_processed', 1),
        ]:
            self._complete(key, days_ago=days_ago)
        problems = stage_rules.check_can_complete(
            self.container, self._stage('gate_out'), self.now
        )
        self.assertEqual(
            problems, ['Customs processed is not recorded yet.']
        )

    def test_undo_duty_paid_blocked_by_shipping(self):
        self._complete('arrived_at_port', days_ago=4)
        self._complete('manifest_available', days_ago=3)
        self._complete('paar_assessed', days_ago=2)
        self._complete('duty_paid', days_ago=1)
        self._complete('shipping_line_processed', days_ago=0.5)
        problems = stage_rules.check_can_undo(
            self.container, self._stage('duty_paid')
        )
        self.assertTrue(
            any('Shipping line processed' in problem for problem in problems)
        )

    def test_undo_free_stage_allowed(self):
        self._complete('arrived_at_port', days_ago=1)
        problems = stage_rules.check_can_undo(
            self.container, self._stage('arrived_at_port')
        )
        self.assertEqual(problems, [])

    def test_completed_stage_cannot_complete_again(self):
        self._complete('arrived_at_port', days_ago=4)
        problems = stage_rules.check_can_complete(
            self.container, self._stage('arrived_at_port'), self.now
        )
        self.assertEqual(problems, ['Arrived at port is already recorded.'])
