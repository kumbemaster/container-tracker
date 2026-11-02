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

    def test_undo_duty_paid_allowed_with_only_shipping_done(self):
        self._complete('arrived_at_port', days_ago=4)
        self._complete('manifest_available', days_ago=3)
        self._complete('paar_assessed', days_ago=2)
        self._complete('duty_paid', days_ago=1)
        self._complete('shipping_line_processed', days_ago=0.5)
        problems = stage_rules.check_can_undo(
            self.container, self._stage('duty_paid')
        )
        self.assertEqual(problems, [])

    def test_undo_duty_paid_blocked_by_customs(self):
        self._complete('duty_paid', days_ago=1)
        self._complete('customs_processed', days_ago=0.5)
        problems = stage_rules.check_can_undo(
            self.container, self._stage('duty_paid')
        )
        self.assertEqual(problems, ['Undo Customs processed first.'])

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

    def test_tracks_allowed_after_paar_without_duty(self):
        self._complete('arrived_at_port', days_ago=4)
        self._complete('manifest_available', days_ago=3)
        self._complete('paar_assessed', days_ago=2)
        for key in ('shipping_line_processed', 'terminal_processed'):
            problems = stage_rules.check_can_complete(
                self.container, self._stage(key), self.now
            )
            self.assertEqual(problems, [], key)

    def test_tracks_refused_before_paar(self):
        self._complete('arrived_at_port', days_ago=4)
        self._complete('manifest_available', days_ago=3)
        for key in ('shipping_line_processed', 'terminal_processed'):
            problems = stage_rules.check_can_complete(
                self.container, self._stage(key), self.now
            )
            self.assertEqual(
                problems, ['PAAR assessed is not recorded yet.'], key
            )

    def test_customs_processed_needs_duty_paid(self):
        self._complete('paar_assessed', days_ago=2)
        problems = stage_rules.check_can_complete(
            self.container, self._stage('customs_processed'), self.now
        )
        self.assertEqual(problems, ['Duty paid is not recorded yet.'])
        self._complete('duty_paid', days_ago=1)
        problems = stage_rules.check_can_complete(
            self.container, self._stage('customs_processed'), self.now
        )
        self.assertEqual(problems, [])

    def test_en_route_needs_duty_paid(self):
        self._complete('paar_assessed', days_ago=2)
        problems = stage_rules.check_can_complete(
            self.container,
            self._stage('en_route_to_bonded_terminal'),
            self.now,
        )
        self.assertEqual(problems, ['Duty paid is not recorded yet.'])
        self._complete('duty_paid', days_ago=1)
        problems = stage_rules.check_can_complete(
            self.container,
            self._stage('en_route_to_bonded_terminal'),
            self.now,
        )
        self.assertEqual(problems, [])

    def test_undo_arrived_always_allowed(self):
        for key, days_ago in [
            ('arrived_at_port', 4),
            ('manifest_available', 3),
            ('paar_assessed', 2),
            ('duty_paid', 1),
        ]:
            self._complete(key, days_ago=days_ago)
        problems = stage_rules.check_can_undo(
            self.container, self._stage('arrived_at_port')
        )
        self.assertEqual(problems, [])

    def test_undo_manifest_blocked_only_by_paar(self):
        self._complete('manifest_available', days_ago=2)
        problems = stage_rules.check_can_undo(
            self.container, self._stage('manifest_available')
        )
        self.assertEqual(problems, [])
        self._complete('paar_assessed', days_ago=1)
        problems = stage_rules.check_can_undo(
            self.container, self._stage('manifest_available')
        )
        self.assertEqual(problems, ['Undo PAAR assessed first.'])

    def test_undo_paar_blockers(self):
        self._complete('paar_assessed', days_ago=3)
        self._complete('duty_paid', days_ago=2)
        problems = stage_rules.check_can_undo(
            self.container, self._stage('paar_assessed')
        )
        self.assertEqual(problems, ['Undo Duty paid first.'])
        StageEvent.objects.filter(
            container=self.container, stage__key='duty_paid'
        ).delete()
        self._complete('shipping_line_processed', days_ago=1)
        problems = stage_rules.check_can_undo(
            self.container, self._stage('paar_assessed')
        )
        self.assertEqual(
            problems, ['Undo Shipping line processed first.']
        )
        StageEvent.objects.filter(
            container=self.container, stage__key='shipping_line_processed'
        ).delete()
        self._complete('terminal_processed', days_ago=1)
        problems = stage_rules.check_can_undo(
            self.container, self._stage('paar_assessed')
        )
        self.assertEqual(problems, ['Undo Terminal processed first.'])

    def test_undo_duty_blocked_by_en_route(self):
        self._complete('duty_paid', days_ago=1)
        self._complete('en_route_to_bonded_terminal', days_ago=0.5)
        problems = stage_rules.check_can_undo(
            self.container, self._stage('duty_paid')
        )
        self.assertEqual(
            problems, ['Undo En route to bonded terminal first.']
        )

    def test_undo_track_blocked_only_by_gate_out(self):
        self._complete('paar_assessed', days_ago=2)
        self._complete('shipping_line_processed', days_ago=1)
        problems = stage_rules.check_can_undo(
            self.container, self._stage('shipping_line_processed')
        )
        self.assertEqual(problems, [])
        for key, days_ago in [
            ('terminal_processed', 1),
            ('customs_processed', 0.75),
            ('duty_paid', 1.5),
            ('manifest_available', 3),
            ('arrived_at_port', 4),
            ('gate_out', 0.5),
        ]:
            self._complete(key, days_ago=days_ago)
        problems = stage_rules.check_can_undo(
            self.container, self._stage('shipping_line_processed')
        )
        self.assertEqual(problems, ['Undo Gate out first.'])

    def test_undo_en_route_always_allowed(self):
        for key, days_ago in [
            ('duty_paid', 3),
            ('en_route_to_bonded_terminal', 2),
            ('shipping_line_processed', 1),
            ('terminal_processed', 1),
            ('customs_processed', 0.75),
            ('gate_out', 0.5),
        ]:
            self._complete(key, days_ago=days_ago)
        problems = stage_rules.check_can_undo(
            self.container, self._stage('en_route_to_bonded_terminal')
        )
        self.assertEqual(problems, [])

    def test_undo_gate_out_blocked_by_in_transit(self):
        self._complete('gate_out', days_ago=1)
        problems = stage_rules.check_can_undo(
            self.container, self._stage('gate_out')
        )
        self.assertEqual(problems, [])
        self._complete('in_transit', days_ago=0.5)
        problems = stage_rules.check_can_undo(
            self.container, self._stage('gate_out')
        )
        self.assertEqual(problems, ['Undo In transit first.'])

    def test_prerequisites_respected_by_both(self):
        for stage in Stage.objects.order_by('order'):
            prereqs = stage_rules.prerequisites_of(stage)
            selected = []
            if stage.key == 'gate_out':
                selected = ['customs_examination']
                prereqs = stage_rules.prerequisites_of(
                    stage, selected=selected
                )
            for prereq in prereqs:
                container = self._fresh_container(
                    'CHK%d%s' % (stage.order, prereq.key)
                )
                needed = list(dict.fromkeys(
                    [
                        key
                        for key in self._chain_keys(stage)
                        if key != prereq.key
                    ]
                    + [
                        key
                        for key in [p.key for p in prereqs]
                        if key != prereq.key
                    ]
                    + (
                        [
                            key
                            for key in selected
                            if key != prereq.key
                        ]
                        if stage.key == 'gate_out'
                        else []
                    )
                ))
                for key in needed:
                    self._complete_on(
                        container, key, days_ago=stage.order
                    )
                if prereq.key == 'customs_examination':
                    StageEvent.objects.create(
                        container=container,
                        stage=self._stage('customs_examination'),
                        occurred_at=None,
                    )
                problems = stage_rules.check_can_complete(
                    container, stage, self.now
                )
                self.assertIn(
                    '%s is not recorded yet.' % prereq.label,
                    problems,
                    (stage.key, prereq.key),
                )
                if prereq.key != 'customs_examination':
                    self._complete_on(
                        container, prereq.key, days_ago=1
                    )
                if prereq.key == 'customs_examination':
                    StageEvent.objects.filter(
                        container=container,
                        stage__key='customs_examination',
                    ).update(
                        occurred_at=self.now - timedelta(hours=1)
                    )
                problems = stage_rules.check_can_complete(
                    container, stage, self.now
                )
                self.assertNotIn(
                    '%s is not recorded yet.' % prereq.label,
                    problems,
                    (stage.key, prereq.key),
                )

    def _fresh_container(self, suffix):
        number = ('CHK%s' % suffix).ljust(11, '0')[:11]
        shipment = Shipment.objects.create(
            importer=Importer.objects.create(
                agent_company=AgentCompany.objects.create(
                    name='Conf %s' % suffix
                ),
                name='Conf Importer %s' % suffix,
                access_code='123456',
            ),
            bl_number='CONF%s' % suffix,
            shipping_line='Line',
            port='Onne',
            destination_area='Port Harcourt',
        )
        return Container.objects.create(
            shipment=shipment, number=number, size='20ft'
        )

    def _chain_keys(self, stage):
        return [
            item.key
            for item in Stage.objects.filter(
                is_sequenced=True, order__lt=stage.order
            ).order_by('order')
        ]

    def _complete_on(self, container, key, days_ago=1):
        return StageEvent.objects.create(
            container=container,
            stage=self._stage(key),
            occurred_at=self.now - timedelta(days=days_ago),
        )
