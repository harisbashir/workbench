"""Moves attachments from Workbench 1.0 (projects.TaskAttachment) into the file library."""
from django.db import migrations


def forwards(apps, schema_editor):
    TaskAttachment = apps.get_model("projects", "TaskAttachment")
    Folder = apps.get_model("files", "Folder")
    Document = apps.get_model("files", "Document")
    Version = apps.get_model("files", "DocumentVersion")
    for att in TaskAttachment.objects.select_related("task__project"):
        task = att.task
        project = task.project
        root, _ = Folder.objects.get_or_create(project=project, parent=None, name="Task files")
        folder, _ = Folder.objects.get_or_create(project=project, parent=root, name=f"{project.key}-{task.number}")
        doc = Document.objects.create(project=project, folder=folder, task=task, name=att.name[:200], size=att.size,
                                      version_count=1, created_by=att.uploaded_by, updated_by=att.uploaded_by)
        # The stored file keeps its old path; it's still under the files folder.
        Version.objects.create(document=doc, number=1, file=att.file.name, original_name=att.name[:200],
                               size=att.size, uploaded_by=att.uploaded_by)


class Migration(migrations.Migration):
    dependencies = [
        ("files", "0001_initial"),
        ("projects", "0001_initial"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
