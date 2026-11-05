from django.db import migrations, models


def classify_existing(apps, schema_editor):
    recordings = apps.get_model('api', 'InterviewRecording').objects.using(schema_editor.connection.alias)
    recordings.update(video_status='failed', video_error='No video stored for this interview.')
    recordings.filter(
        (models.Q(video_buffer__isnull=False) & ~models.Q(video_buffer=b'')) |
        (models.Q(recording_data__isnull=False) & ~models.Q(recording_data=''))
    ).update(video_status='ready', video_error='')


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0066_merge_20260910_1919'),
        ('api', '0059_sourcing_search_and_results'),
    ]

    operations = [
        migrations.AddField(
            model_name='interviewrecording', name='client_upload_id',
            field=models.UUIDField(null=True, blank=True, unique=True),
        ),
        migrations.AddField(
            model_name='interviewrecording', name='video_status',
            field=models.CharField(max_length=16, default='pending', choices=[
                ('pending', 'Pending upload'), ('ready', 'Ready'), ('failed', 'Upload failed'),
            ]),
        ),
        migrations.AddField(
            model_name='interviewrecording', name='video_error',
            field=models.CharField(max_length=255, default='', blank=True),
        ),
        migrations.RunPython(classify_existing, migrations.RunPython.noop),
    ]
