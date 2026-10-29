from django.db import migrations


STAGES = [
    (1, 'arrived_at_port', 'Arrived at port', False, False, False),
    (2, 'manifest_available', 'Manifest available', False, True, False),
    (3, 'paar_assessed', 'PAAR assessed', False, True, False),
    (4, 'duty_paid', 'Duty paid', False, True, False),
    (5, 'en_route_to_bonded_terminal', 'En route to bonded terminal', True, False, False),
    (6, 'shipping_line_processed', 'Shipping line processed', False, False, True),
    (7, 'terminal_processed', 'Terminal processed', False, False, True),
    (8, 'customs_examination', 'Customs examination', True, False, False),
    (9, 'customs_processed', 'Customs processed', False, False, True),
    (10, 'gate_out', 'Gate out', False, True, False),
    (11, 'in_transit', 'In transit', False, True, False),
    (12, 'delivered', 'Delivered', False, True, False),
    (13, 'empty_container_returned', 'Empty container returned', False, True, False),
]


def seed_stages(apps, schema_editor):
    Stage = apps.get_model('tracking', 'Stage')
    for order, key, label, is_optional, is_sequenced, is_processing_track in STAGES:
        Stage.objects.update_or_create(
            key=key,
            defaults={
                'order': order,
                'label': label,
                'is_optional': is_optional,
                'is_sequenced': is_sequenced,
                'is_processing_track': is_processing_track,
            },
        )


def unseed_stages(apps, schema_editor):
    Stage = apps.get_model('tracking', 'Stage')
    Stage.objects.filter(key__in=[key for _, key, _, _, _, _ in STAGES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tracking', '0002_stage_is_processing_track_stage_is_sequenced'),
    ]

    operations = [
        migrations.RunPython(seed_stages, unseed_stages),
    ]
