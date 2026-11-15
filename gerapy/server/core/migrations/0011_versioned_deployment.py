# Generated for versioned deployment support

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0010_auto_20191027_2040'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProjectVersion',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('project', models.CharField(max_length=255)),
                ('version', models.CharField(max_length=255, unique=True)),
                ('checksum', models.CharField(max_length=64)),
                ('egg_path', models.CharField(blank=True, max_length=1024, null=True)),
                ('size', models.IntegerField(blank=True, default=0)),
                ('manifest', models.TextField(blank=True, null=True)),
                ('description', models.CharField(blank=True, max_length=255, null=True)),
                ('build_status', models.CharField(
                    choices=[('building', 'building'), ('ready', 'ready'), ('failed', 'failed')],
                    default='building', max_length=32)),
                ('build_log', models.TextField(blank=True, null=True)),
                ('built_at', models.DateTimeField(blank=True, default=None, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True, blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name='DeploymentRecord',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('client_name', models.CharField(blank=True, max_length=255, null=True)),
                ('project', models.CharField(max_length=255)),
                ('version', models.CharField(blank=True, max_length=255, null=True)),
                ('previous_version', models.CharField(blank=True, max_length=255, null=True)),
                ('status', models.CharField(
                    choices=[('deploying', 'deploying'), ('success', 'success'), ('failed', 'failed'),
                             ('rolled_back', 'rolled_back'), ('rejected', 'rejected'), ('rollback', 'rollback')],
                    default='deploying', max_length=32)),
                ('reason', models.CharField(blank=True, max_length=64, null=True)),
                ('diff', models.TextField(blank=True, null=True)),
                ('health_checks', models.TextField(blank=True, null=True)),
                ('rollback_reason', models.TextField(blank=True, null=True)),
                ('running_task_policy', models.CharField(blank=True, max_length=32, null=True)),
                ('running_jobs', models.IntegerField(blank=True, default=0)),
                ('operator', models.CharField(blank=True, max_length=255, null=True)),
                ('log', models.TextField(blank=True, null=True)),
                ('log_path', models.CharField(blank=True, max_length=1024, null=True)),
                ('started_at', models.DateTimeField(blank=True, default=None, null=True)),
                ('finished_at', models.DateTimeField(blank=True, default=None, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True, blank=True, null=True)),
                ('client', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.DO_NOTHING,
                    to='core.client')),
            ],
        ),
    ]
