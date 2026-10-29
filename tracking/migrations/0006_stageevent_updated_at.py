import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracking', '0005_free_time_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='stageevent',
            name='updated_at',
            field=models.DateTimeField(
                auto_now=True, default=django.utils.timezone.now
            ),
            preserve_default=False,
        ),
    ]
