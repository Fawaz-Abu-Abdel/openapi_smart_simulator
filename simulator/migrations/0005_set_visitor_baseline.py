from django.db import migrations


def set_visitor_baseline(apps, schema_editor):
    """Set the existing SiteStats row's total_visitors to 290 if it's below that."""
    SiteStats = apps.get_model('simulator', 'SiteStats')
    SiteStats.objects.filter(id=1, total_visitors__lt=290).update(total_visitors=290)


class Migration(migrations.Migration):

    dependencies = [
        ('simulator', '0004_visitor_default_290'),
    ]

    operations = [
        migrations.RunPython(set_visitor_baseline, migrations.RunPython.noop),
    ]
