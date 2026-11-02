"""Shared invented demo data used by seed_demo and bootstrap_demo."""

import secrets
from datetime import timedelta

from django.utils import timezone

from .models import (
    AgentCompany,
    Container,
    Importer,
    Shipment,
    Stage,
    StageEvent,
)

DEMO_COMPANY_NAME = 'Demo Clearing Ltd'
DEMO_IMPORTER_A_NAME = 'Demo Importer A'
DEMO_IMPORTER_A_CODE = '482910'
DEMO_IMPORTER_C_NAME = 'Demo Importer C Ltd'


def _random_code():
    return '%06d' % secrets.randbelow(1000000)


def seed_demo_data():
    """Create the demo company, importers and shipments (idempotent).

    Returns a dict with the company, importers and shipments so callers
    can report on what was created.
    """
    now = timezone.now()

    company, _ = AgentCompany.objects.get_or_create(
        name=DEMO_COMPANY_NAME
    )

    importer_a, _ = Importer.objects.get_or_create(
        agent_company=company,
        name=DEMO_IMPORTER_A_NAME,
        defaults={'access_code': DEMO_IMPORTER_A_CODE},
    )

    shipment1, _ = Shipment.objects.get_or_create(
        importer=importer_a,
        bl_number='DEMOBL001',
        defaults={
            'shipping_line': 'Demo Line',
            'port': 'Onne',
            'destination_area': 'Port Harcourt',
            'discharge_date': (now - timedelta(days=4)).date(),
            'shipping_line_free_days': 5,
            'terminal_free_days': 3,
        },
    )
    if shipment1.deleted_at is not None:
        shipment1.deleted_at = None
        shipment1.save(update_fields=['deleted_at'])
    shipment1.shipping_line_free_days = 5
    shipment1.terminal_free_days = 3
    shipment1.save(
        update_fields=['shipping_line_free_days', 'terminal_free_days']
    )

    container1, _ = Container.objects.get_or_create(
        shipment=shipment1,
        number='CSQU3054383',
        defaults={'size': '40ft'},
    )
    container2, _ = Container.objects.get_or_create(
        shipment=shipment1,
        number='DEMO4416733',
        defaults={'size': '20ft'},
    )

    for key, days_ago in [
        ('arrived_at_port', 4),
        ('manifest_available', 3),
        ('paar_assessed', 2),
        ('duty_paid', 1),
        ('shipping_line_processed', 0.5),
    ]:
        StageEvent.objects.get_or_create(
            container=container1,
            stage=Stage.objects.get(key=key),
            defaults={'occurred_at': now - timedelta(days=days_ago)},
        )

    for key, days_ago in [
        ('arrived_at_port', 4),
        ('manifest_available', 3),
    ]:
        StageEvent.objects.get_or_create(
            container=container2,
            stage=Stage.objects.get(key=key),
            defaults={'occurred_at': now - timedelta(days=days_ago)},
        )

    container2.waiting_on = Container.WaitingOn.CUSTOMS
    container2.waiting_on_since = now - timedelta(days=1)
    container2.waiting_on_note = 'Awaiting customs examination.'
    container2.save()

    importer_c, _ = Importer.objects.get_or_create(
        agent_company=company,
        name=DEMO_IMPORTER_C_NAME,
        defaults={'access_code': _random_code()},
    )

    shipment2, _ = Shipment.objects.get_or_create(
        importer=importer_c,
        bl_number='DEMOBL002',
        defaults={
            'shipping_line': 'Demo Line',
            'port': 'Onne',
            'destination_area': 'Port Harcourt',
        },
    )
    if shipment2.deleted_at is not None:
        shipment2.deleted_at = None
        shipment2.save(update_fields=['deleted_at'])
    Container.objects.get_or_create(
        shipment=shipment2,
        number='TSTU1234568',
        defaults={'size': '40HC'},
    )
    Container.objects.get_or_create(
        shipment=shipment2,
        number='TSTU6543214',
        defaults={'size': '20ft'},
    )

    return {
        'company': company,
        'importer_a': importer_a,
        'importer_c': importer_c,
        'shipment1': shipment1,
        'shipment2': shipment2,
    }
