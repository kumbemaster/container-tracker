from django.db import migrations


PHASES = {
    'arrival': [
        'arrived_at_port',
        'manifest_available',
        'paar_assessed',
        'duty_paid',
    ],
    'release': [
        'en_route_to_bonded_terminal',
        'shipping_line_processed',
        'terminal_processed',
        'customs_examination',
        'customs_processed',
        'gate_out',
    ],
    'delivery': [
        'in_transit',
        'delivered',
    ],
    'return': [
        'empty_container_returned',
    ],
}


def set_phases(apps, schema_editor):
    Stage = apps.get_model('tracking', 'Stage')
    for phase, keys in PHASES.items():
        Stage.objects.filter(key__in=keys).update(phase=phase)


def unset_phases(apps, schema_editor):
    Stage = apps.get_model('tracking', 'Stage')
    Stage.objects.update(phase='arrival')


class Migration(migrations.Migration):

    dependencies = [
        ('tracking', '0007_stage_phase'),
    ]

    operations = [
        migrations.RunPython(set_phases, unset_phases),
    ]
