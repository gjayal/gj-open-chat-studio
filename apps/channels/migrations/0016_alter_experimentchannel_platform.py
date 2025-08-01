# Generated by Django 4.2.7 on 2024-05-08 06:22

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bot_channels', '0015_alter_experimentchannel_name'),
    ]

    operations = [
        migrations.AlterField(
            model_name='experimentchannel',
            name='platform',
            field=models.CharField(choices=[('telegram', 'Telegram'), ('web', 'Web'), ('whatsapp', 'WhatsApp'), ('facebook', 'Facebook'), ('api', 'API')], default='telegram', max_length=32),
        ),
    ]
