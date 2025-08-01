# Generated by Django 5.1.5 on 2025-07-02 10:02

import apps.utils.models
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0021_statictrigger_is_active_timeouttrigger_is_active'),
        ('teams', '0007_create_commcare_connect_flag'),
    ]

    operations = [
        migrations.CreateModel(
            name='ScheduledMessageAttempt',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('trigger_number', models.IntegerField()),
                ('attempt_number', models.IntegerField()),
                ('attempt_result', models.CharField(choices=[('success', 'Success'), ('failure', 'Failure')], max_length=10)),
                ('log_message', models.TextField(blank=True)),
                ('trace_info', models.JSONField(blank=True, null=True)),
                ('scheduled_message', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='attempts', to='events.scheduledmessage')),
                ('team', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='teams.team', verbose_name='Team')),
            ],
            options={
                'unique_together': {('scheduled_message', 'trigger_number', 'attempt_number')},
            },
            bases=(models.Model, apps.utils.models.VersioningMixin),
        ),
    ]
