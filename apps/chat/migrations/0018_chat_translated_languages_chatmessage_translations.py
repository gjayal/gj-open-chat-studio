# Generated by Django 5.1.5 on 2025-06-13 18:12

import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0017_change_chatmessage_metadata'),
    ]

    operations = [
        migrations.AddField(
            model_name='chat',
            name='translated_languages',
            field=django.contrib.postgres.fields.ArrayField(base_field=models.CharField(max_length=3), blank=True, default=list, help_text='List of language codes for which translated text is available', null=True, size=None),
        ),
        migrations.AddField(
            model_name='chatmessage',
            name='translations',
            field=models.JSONField(default=dict, help_text='Dictionary of translated text keyed by the language code'),
        ),
    ]
