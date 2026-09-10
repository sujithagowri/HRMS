from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('api', '0058_merge_20260825_2020')]

    operations = [
        migrations.CreateModel(
            name='SourcingSearch',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('job_title', models.CharField(blank=True, default='', max_length=255)),
                ('job_description', models.TextField()),
                ('requirements', models.JSONField(blank=True, default=dict)),
                ('sources', models.JSONField(blank=True, default=list)),
                ('status', models.CharField(default='pending', max_length=30)),
                ('requested_by_email', models.CharField(blank=True, db_index=True, default='', max_length=255)),
                ('result_count', models.PositiveIntegerField(default=0)),
                ('error', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('job', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='sourcing_searches', to='api.jobpost')),
            ],
            options={'db_table': 'sourcing_searches', 'ordering': ['-created_at']},
        ),
        migrations.CreateModel(
            name='SourcingResult',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source', models.CharField(default='internal', max_length=100)),
                ('external_id', models.CharField(blank=True, default='', max_length=255)),
                ('candidate_name', models.CharField(max_length=255)),
                ('candidate_role', models.CharField(blank=True, default='', max_length=255)),
                ('profile_url', models.TextField(blank=True, default='')),
                ('normalized_profile', models.JSONField(blank=True, default=dict)),
                ('match_score', models.PositiveSmallIntegerField(default=0)),
                ('explanation', models.JSONField(blank=True, default=dict)),
                ('status', models.CharField(default='new', max_length=30)),
                ('feedback', models.TextField(blank=True, default='')),
                ('reviewed_by_email', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('resume', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='sourcing_results', to='api.resumescore')),
                ('search', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='results', to='api.sourcingsearch')),
            ],
            options={'db_table': 'sourcing_results', 'ordering': ['-match_score', 'id']},
        ),
        migrations.AddConstraint(
            model_name='sourcingresult',
            constraint=models.UniqueConstraint(fields=('search', 'source', 'external_id'), name='unique_sourcing_result_per_source'),
        ),
    ]
